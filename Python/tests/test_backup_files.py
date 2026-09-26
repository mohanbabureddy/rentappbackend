"""Uploaded-file backup and old-backup pruning, against a tiny fake of the Supabase
Storage API running on this machine -- nothing real is touched."""
import importlib.util
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import unquote

_SPEC = importlib.util.spec_from_file_location(
    "backup_prod_to_local_files", Path(__file__).resolve().parents[1] / "scripts" / "backup_prod_to_local.py")
backup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backup)

KEY = "test-service-key"


class _FakeStorage:
    """bucket 'aadhaar': Room1/{a.pdf,b.jpg} and Room2/f000..f149.txt (forces a second page)."""

    def __init__(self):
        self.files = {"Room1/a.pdf": b"AAAAA", "Room1/b.jpg": b"BBB"}
        for i in range(150):
            self.files[f"Room2/f{i:03d}.txt"] = f"file {i}".encode()
        self.truncate = set()      # objects whose download comes back shorter than advertised
        self.hits = []

    def entries(self, prefix):
        seen, out = set(), []
        for path in sorted(self.files):
            if prefix and not path.startswith(prefix + "/"):
                continue
            rest = path[len(prefix) + 1:] if prefix else path
            head = rest.split("/")[0]
            if "/" in rest:
                if head not in seen:
                    seen.add(head)
                    out.append({"name": head, "id": None})
            else:
                out.append({"name": head, "id": "id-" + path, "metadata": {"size": len(self.files[path])}})
        return out


def _serve(storage):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _authed(self):
            if self.headers.get("Authorization") != f"Bearer {KEY}":
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            return True

        def do_POST(self):
            if not self._authed():
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            storage.hits.append(("list", body["prefix"], body["offset"]))
            page = storage.entries(body["prefix"])[body["offset"]: body["offset"] + body["limit"]]
            payload = json.dumps(page).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if not self._authed():
                return
            path = unquote(self.path.split("/storage/v1/object/aadhaar/", 1)[1])
            storage.hits.append(("get", path))
            data = storage.files.get(path)
            if data is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path in storage.truncate:
                data = data[:-1]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class FileBackupTest(unittest.TestCase):
    def setUp(self):
        self.storage = _FakeStorage()
        self.server = _serve(self.storage)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.dest = Path(tempfile.mkdtemp(prefix="rentapp_backup_test_"))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)      # runs first (cleanups are last-in, first-out)

    def test_listing_walks_folders_and_pages(self):
        files = backup.list_storage_files(self.url, KEY)
        self.assertEqual(len(files), 152)                       # the 150 needed a second page
        self.assertIn({"name": "Room1/a.pdf", "size": 5}, files)

    def test_downloads_everything_with_the_right_content(self):
        stats = backup.backup_files(self.url, KEY, self.dest)
        self.assertEqual((stats["total"], stats["downloaded"], stats["failed"]), (152, 152, 0))
        self.assertEqual((self.dest / "aadhaar" / "Room1" / "a.pdf").read_bytes(), b"AAAAA")
        self.assertEqual((self.dest / "aadhaar" / "Room2" / "f149.txt").read_bytes(), b"file 149")

    def test_a_second_run_downloads_nothing_new(self):
        backup.backup_files(self.url, KEY, self.dest)
        self.storage.hits.clear()
        stats = backup.backup_files(self.url, KEY, self.dest)
        self.assertEqual((stats["downloaded"], stats["skipped"]), (0, 152))
        self.assertFalse([h for h in self.storage.hits if h[0] == "get"])

    def test_a_changed_file_is_fetched_again(self):
        backup.backup_files(self.url, KEY, self.dest)
        self.storage.files["Room1/a.pdf"] = b"AAAAAAAA"          # replaced with a different size
        stats = backup.backup_files(self.url, KEY, self.dest)
        self.assertEqual(stats["downloaded"], 1)
        self.assertEqual((self.dest / "aadhaar" / "Room1" / "a.pdf").read_bytes(), b"AAAAAAAA")

    def test_files_deleted_from_production_are_kept_in_the_backup(self):
        backup.backup_files(self.url, KEY, self.dest)
        del self.storage.files["Room1/b.jpg"]
        backup.backup_files(self.url, KEY, self.dest)
        self.assertTrue((self.dest / "aadhaar" / "Room1" / "b.jpg").is_file())

    def test_a_short_download_is_rejected_leaves_no_file_and_does_not_stop_the_rest(self):
        self.storage.truncate.add("Room1/a.pdf")
        stats = backup.backup_files(self.url, KEY, self.dest)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["downloaded"], 151)
        self.assertFalse((self.dest / "aadhaar" / "Room1" / "a.pdf").exists())
        self.assertFalse(list(self.dest.rglob("*.part")))

    def test_the_wrong_service_key_fails_loudly(self):
        import requests
        with self.assertRaises(requests.HTTPError):
            backup.backup_files(self.url, "wrong-key", self.dest)

    def test_names_that_could_escape_the_backup_folder_are_refused(self):
        root = self.dest / "aadhaar"
        root.mkdir()
        for bad in ("../evil.txt", "Room1/../../evil.txt", "/abs.txt", "\\abs.txt", ""):
            self.assertIsNone(backup.safe_local_path(root, bad), repr(bad))
        self.assertIsNotNone(backup.safe_local_path(root, "Room1/ok.pdf"))

    def test_unsafe_object_names_from_the_server_are_skipped_not_written(self):
        self.storage.files["../../escape.txt"] = b"x"
        stats = backup.backup_files(self.url, KEY, self.dest)
        self.assertEqual(stats["unsafe"], 1)
        self.assertFalse((self.dest.parent / "escape.txt").exists())

    def test_id_documents_can_never_be_stored_inside_onedrive(self):
        with self.assertRaises(ValueError):
            backup.validate_files_dir(Path("C:/Users/someone/OneDrive/Documents/backups"))
        with self.assertRaises(ValueError):
            backup.validate_files_dir(Path("C:/Users/someone/OneDrive - Company/x"))
        self.assertEqual(backup.validate_files_dir(self.dest), self.dest.resolve())

    def test_check_mode_lists_files_but_downloads_none(self):
        env = {"SUPABASE_URL": self.url, "SUPABASE_SERVICE_KEY": KEY, "PROD_DATABASE_URL": "sqlite://"}
        with mock.patch.dict(os.environ, env), mock.patch.object(backup, "check", return_value=0):
            self.assertEqual(backup.main(["--check", "--files-dir", str(self.dest)]), 0)
        self.assertFalse(list(self.dest.rglob("*.pdf")))
        self.assertFalse([h for h in self.storage.hits if h[0] == "get"])


class PruneOldBackupsTest(unittest.TestCase):
    NAMES = ["rent_app", "sakila", "rent_app_prod_backup_my_manual_copy",
             "rent_app_prod_backup_20260101_0900", "rent_app_prod_backup_20260102_0900",
             "rent_app_prod_backup_20260103_0900", "rent_app_prod_backup_20260104_0900"]

    def _run(self, keep):
        dropped = []
        names = self.NAMES

        class Conn:
            def execute(self, stmt, *args, **kwargs):
                sql = str(stmt)
                if "information_schema" in sql:
                    return [(n,) for n in names]
                dropped.append(sql)

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        class Engine:
            def connect(self):
                return Conn()

            def dispose(self):
                pass

        with mock.patch.object(backup, "create_engine", return_value=Engine()):
            result = backup.prune_old_backups("mysql+pymysql://root:pw@localhost", keep)
        return result, dropped

    def test_drops_only_the_oldest_auto_named_backups(self):
        result, dropped = self._run(keep=2)
        self.assertEqual(result, ["rent_app_prod_backup_20260101_0900", "rent_app_prod_backup_20260102_0900"])
        self.assertEqual(len(dropped), 2)

    def test_never_touches_the_dev_database_other_schemas_or_hand_named_backups(self):
        _, dropped = self._run(keep=1)
        joined = " ".join(dropped)
        for safe in ("`rent_app`", "sakila", "my_manual_copy"):
            self.assertNotIn(safe, joined)

    def test_keep_zero_means_keep_everything(self):
        self.assertEqual(self._run(keep=0), ([], []))

    def test_fewer_backups_than_the_limit_drops_nothing(self):
        result, _ = self._run(keep=10)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
