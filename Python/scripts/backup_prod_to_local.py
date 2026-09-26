"""Back up the PRODUCTION database (Supabase Postgres) into a SEPARATE schema on
your LOCAL MySQL. Run from the Python/ folder -- easiest via the PowerShell
wrapper, which asks for the production password without echoing it:

    powershell -File scripts\\backup-prod.ps1            # make a backup
    powershell -File scripts\\backup-prod.ps1 -Check     # look only: list tables + row counts, write nothing

or directly:

    $env:PROD_DATABASE_URL = "postgresql://postgres.xxxx:PASSWORD@...supabase.com:5432/postgres"
    .venv\\Scripts\\python.exe scripts\\backup_prod_to_local.py [--check] [--schema NAME] [--replace]

Safety, by design:
  * Production is only ever READ: the whole run is one read-only, repeatable-read
    transaction (a consistent snapshot, even if tenants are using the app).
  * It only ever WRITES to a new local MySQL schema, named rent_app_prod_backup_<date>_<time>
    by default. The name must contain "backup", and can never be your dev database
    (rent_app) or a system schema. It refuses to touch a schema that already has
    tables unless you pass --replace.
  * The MySQL server must be on this machine (localhost / 127.0.0.1).
  * It does not import the app, so it can't run migrations or touch anything else.
  * Every table in production is copied (found by reflection), so tables added
    later are covered automatically. Row counts are compared at the end.

What this does NOT cover: uploaded Aadhaar files (they live in Supabase Storage,
not the database) and the exact indexes/constraints -- this is a readable copy of
the data (primary keys kept), not a byte-for-byte restore image.
"""
import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from dotenv import dotenv_values
from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.sql import sqltypes

# Schemas that must never be a backup target, whatever the name says.
PROTECTED_SCHEMAS = {"rent_app", "mysql", "sys", "information_schema", "performance_schema", "sakila", "world"}
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
CHUNK = 1000


def normalize_postgres_url(url: str) -> str:
    """Plain postgres(ql):// URLs would make SQLAlchemy look for psycopg2, which
    isn't installed -- route them to psycopg (v3), as app/database.py does."""
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def default_schema_name(now: Optional[datetime] = None) -> str:
    return f"rent_app_prod_backup_{(now or datetime.now()):%Y%m%d_%H%M}"


def validate_target_schema(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", name or ""):
        raise ValueError("Schema name may only contain letters, digits and underscores.")
    if name.lower() in PROTECTED_SCHEMAS:
        raise ValueError(f"'{name}' is a protected schema and can't be a backup target.")
    if "backup" not in name.lower():
        raise ValueError("The target schema name must contain 'backup' so it can never be a live database.")
    return name


def mysql_server_url(env_path: Path, override: Optional[str] = None) -> str:
    """URL of the local MySQL *server* (no database in it), taken from DB_URL in
    .env unless overridden. Refuses anything that isn't this machine."""
    url = override or os.getenv("LOCAL_MYSQL_SERVER_URL") or dotenv_values(env_path).get("DB_URL")
    if not url:
        raise ValueError("No local MySQL URL: set LOCAL_MYSQL_SERVER_URL or DB_URL in Python/.env.")
    parsed = make_url(url)
    if not parsed.drivername.startswith("mysql"):
        raise ValueError("The backup target must be a MySQL server.")
    if (parsed.host or "").lower() not in LOCAL_HOSTS:
        raise ValueError(f"Refusing to write a backup to '{parsed.host}': the target MySQL must be on this machine.")
    # URL.set(database=None) silently keeps the old value, so replace it explicitly.
    return parsed._replace(database=None).render_as_string(hide_password=False)


def generic_column(col: Column) -> Column:
    """Same column, but with a dialect-neutral type so Postgres types (DOUBLE
    PRECISION, TIMESTAMP...) create cleanly on MySQL."""
    try:
        coltype = col.type.as_generic()
    except NotImplementedError:
        coltype = col.type
    # A VARCHAR with no length is legal in Postgres but not in MySQL.
    if isinstance(coltype, sqltypes.String) and not isinstance(coltype, sqltypes.Text) and coltype.length is None:
        coltype = Text()
    # Postgres reports DOUBLE PRECISION as precision 53; MySQL's DOUBLE refuses a precision without a scale.
    if isinstance(coltype, sqltypes.Double):
        coltype = sqltypes.Double()
    elif isinstance(coltype, sqltypes.Float):
        coltype = sqltypes.Double() if (coltype.precision or 53) > 24 else sqltypes.Float()
    elif isinstance(coltype, sqltypes.Numeric):
        # Postgres' bare NUMERIC has no limit; MySQL's would silently round to whole numbers.
        if coltype.precision is None:
            coltype = sqltypes.Numeric(38, 10)
        elif coltype.scale is None:
            coltype = sqltypes.Numeric(coltype.precision, 0)
    return Column(col.name, coltype, primary_key=col.primary_key, nullable=col.nullable, autoincrement=False)


def build_target_table(src_table: Table, target_meta: MetaData) -> Table:
    return Table(src_table.name, target_meta, *[generic_column(c) for c in src_table.columns])


def _masked(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


def _open_snapshot(prod_url: str):
    engine = create_engine(normalize_postgres_url(prod_url), pool_pre_ping=True)
    conn = engine.connect()
    if engine.dialect.name == "postgresql":
        # Must be the first statement of the transaction. Works through Supabase's
        # pooler too (unlike a startup "options" parameter, which pgbouncer rejects).
        conn.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    return engine, conn


def _reflect(conn) -> MetaData:
    meta = MetaData()
    meta.reflect(bind=conn)
    return meta


def check(prod_url: str) -> int:
    engine, src = _open_snapshot(prod_url)
    try:
        meta = _reflect(src)
        print(f"Connected (read-only) to {_masked(prod_url)}")
        print(f"{len(meta.tables)} tables:")
        for t in sorted(meta.tables.values(), key=lambda t: t.name):
            n = src.execute(select(func.count()).select_from(t)).scalar()
            print(f"  {t.name:<22} {n:>8} rows")
        print("Nothing was written anywhere.")
        return 0
    finally:
        src.close()
        engine.dispose()


def backup(prod_url: str, schema: str, server_url: str, replace: bool = False) -> int:
    validate_target_schema(schema)
    server = create_engine(server_url)
    with server.connect() as c:
        exists = c.execute(text("SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = :s"), {"s": schema}).scalar()
        tables = c.execute(text("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = :s"), {"s": schema}).scalar() if exists else 0
        if tables and not replace:
            print(f"Schema '{schema}' already has {tables} tables. Pick another name, or pass --replace to overwrite it.")
            return 2
        c.execute(text(f"CREATE DATABASE IF NOT EXISTS `{schema}` CHARACTER SET utf8mb4"))
        c.commit()

    target_url = make_url(server_url).set(database=schema, query={"charset": "utf8mb4"})
    target = create_engine(target_url)
    engine, src = _open_snapshot(prod_url)
    try:
        src_meta = _reflect(src)
        print(f"Reading (read-only) from {_masked(prod_url)}  ->  writing to local MySQL schema `{schema}`")

        dst_meta = MetaData()
        for t in src_meta.sorted_tables:
            build_target_table(t, dst_meta)
        if replace:
            dst_meta.drop_all(bind=target)
        dst_meta.create_all(bind=target)

        mismatches = 0
        print(f"{'table':<22} {'production':>10} {'backup':>8}")
        for t in sorted(src_meta.tables.values(), key=lambda t: t.name):
            dst = dst_meta.tables[t.name]
            copied = 0
            result = src.execute(select(t))
            with target.begin() as dst_conn:
                while True:
                    rows = result.fetchmany(CHUNK)
                    if not rows:
                        break
                    dst_conn.execute(dst.insert(), [dict(r._mapping) for r in rows])
                    copied += len(rows)
            in_prod = src.execute(select(func.count()).select_from(t)).scalar()
            with target.connect() as dst_conn:
                in_backup = dst_conn.execute(select(func.count()).select_from(dst)).scalar()
            flag = "" if in_prod == in_backup else "   <-- MISMATCH"
            mismatches += 0 if in_prod == in_backup else 1
            print(f"{t.name:<22} {in_prod:>10} {in_backup:>8}{flag}")
        if mismatches:
            print(f"\nBACKUP INCOMPLETE: {mismatches} table(s) don't match.")
            return 1
        print(f"\nBackup complete and verified: schema `{schema}` on your local MySQL.")
        return 0
    finally:
        src.close()
        engine.dispose()
        target.dispose()
        server.dispose()


# ---------------------------------------------------------------------------
# Uploaded files (Supabase Storage bucket "aadhaar", stored as <tenant>/<file>)
# ---------------------------------------------------------------------------
STORAGE_BUCKET = "aadhaar"
AUTO_SCHEMA_RE = re.compile(r"^rent_app_prod_backup_\d{8}_\d{4}$")


def default_files_dir() -> Path:
    return Path.home() / "RentAppBackups"


def validate_files_dir(path: Path) -> Path:
    """Aadhaar scans are government ID documents: they must never land in a folder
    that syncs to the cloud, so anything inside OneDrive is refused."""
    resolved = Path(path).expanduser().resolve()
    if any("onedrive" in part.lower() for part in resolved.parts):
        raise ValueError(f"Refusing to store ID documents in '{resolved}': it's inside OneDrive, which syncs to the cloud. "
                         f"Use a folder like {default_files_dir()}.")
    return resolved


def safe_local_path(root: Path, object_name: str) -> Optional[Path]:
    """Where an object goes on disk, or None if its name would escape `root`."""
    if not object_name or object_name.startswith(("/", "\\")) or ".." in Path(object_name.replace("\\", "/")).parts:
        return None
    target = (root / object_name).resolve()
    return target if root.resolve() in target.parents else None


def _storage_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}", "apikey": key}


def list_storage_files(base_url: str, key: str, bucket: str = STORAGE_BUCKET, prefix: str = "", session=None) -> list:
    """Every file in the bucket as [{'name': 'Room1/a.pdf', 'size': 123}, ...], walking
    into folders (which the API returns as entries with no id) and through pages."""
    import requests
    http = session or requests
    found, offset, page = [], 0, 100
    while True:
        resp = http.post(f"{base_url.rstrip('/')}/storage/v1/object/list/{bucket}", headers=_storage_headers(key), timeout=60,
                         json={"prefix": prefix, "limit": page, "offset": offset, "sortBy": {"column": "name", "order": "asc"}})
        resp.raise_for_status()
        entries = resp.json()
        for e in entries:
            name = f"{prefix}/{e['name']}" if prefix else e["name"]
            if e.get("id") is None:                       # a folder
                found.extend(list_storage_files(base_url, key, bucket, name, session))
            else:
                found.append({"name": name, "size": (e.get("metadata") or {}).get("size")})
        if len(entries) < page:
            return found
        offset += page


def backup_files(base_url: str, key: str, dest: Path, bucket: str = STORAGE_BUCKET, session=None) -> dict:
    """Mirror the bucket into dest/<bucket>/. Files already there with the same size
    are skipped, and files deleted from production are NOT deleted here -- a backup
    should remember them. Each download is size-checked before it's kept."""
    import requests
    http = session or requests
    root = validate_files_dir(dest) / bucket
    root.mkdir(parents=True, exist_ok=True)
    stats = {"total": 0, "downloaded": 0, "skipped": 0, "failed": 0, "unsafe": 0}
    for f in list_storage_files(base_url, key, bucket, session=session):
        stats["total"] += 1
        target = safe_local_path(root, f["name"])
        if target is None:
            stats["unsafe"] += 1
            print(f"  skipped (unsafe name): {f['name']}")
            continue
        if target.is_file() and f["size"] is not None and target.stat().st_size == f["size"]:
            stats["skipped"] += 1
            continue
        try:
            url = f"{base_url.rstrip('/')}/storage/v1/object/{bucket}/{quote(f['name'], safe='/')}"
            resp = http.get(url, headers=_storage_headers(key), timeout=120)
            resp.raise_for_status()
            data = resp.content
            if f["size"] is not None and len(data) != f["size"]:
                raise IOError(f"got {len(data)} bytes, expected {f['size']}")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".part")
            tmp.write_bytes(data)
            tmp.replace(target)
            stats["downloaded"] += 1
        except Exception as exc:                          # one bad file must not stop the rest
            stats["failed"] += 1
            print(f"  FAILED {f['name']}: {exc}")
    return stats


def prune_old_backups(server_url: str, keep: int) -> list:
    """Drop the oldest auto-named backup schemas, keeping the newest `keep`. Only
    names matching rent_app_prod_backup_<date>_<time> are ever candidates -- a
    schema you named yourself, or your dev database, is never touched."""
    if keep <= 0:
        return []
    server = create_engine(server_url)
    dropped = []
    try:
        with server.connect() as c:
            names = sorted(r[0] for r in c.execute(text("SELECT schema_name FROM information_schema.schemata")) if AUTO_SCHEMA_RE.match(r[0]))
            for name in names[:-keep] if len(names) > keep else []:
                validate_target_schema(name)
                c.execute(text(f"DROP DATABASE `{name}`"))
                dropped.append(name)
            c.commit()
    finally:
        server.dispose()
    return dropped


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Back up production data (database + uploaded files) to this machine.")
    p.add_argument("--check", action="store_true", help="only connect read-only and list tables, row counts and files; write nothing")
    p.add_argument("--schema", help="target schema (default: rent_app_prod_backup_<date>_<time>); must contain 'backup'")
    p.add_argument("--replace", action="store_true", help="overwrite the tables if that schema already has some")
    p.add_argument("--source-url", help="override PROD_DATABASE_URL (normally leave unset; mainly for testing)")
    p.add_argument("--skip-files", action="store_true", help="database only; don't back up the uploaded files")
    p.add_argument("--files-dir", help=f"where the files go (default: {default_files_dir()}); never inside OneDrive")
    p.add_argument("--keep", type=int, default=14, help="keep this many newest auto-named backup schemas, drop older ones (0 = keep all; default 14)")
    args = p.parse_args(argv)

    prod_url = args.source_url or os.getenv("PROD_DATABASE_URL")
    if not prod_url:
        print("Set PROD_DATABASE_URL (Supabase > Project Settings > Database > Connection string), or use scripts/backup-prod.ps1.")
        return 2
    supabase_url, supabase_key = os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_SERVICE_KEY")
    want_files = not args.skip_files
    if want_files and not (supabase_url and supabase_key):
        print("The uploaded files need SUPABASE_URL and SUPABASE_SERVICE_KEY (the same two Render has). "
              "Set them, or pass --skip-files for a database-only backup.")
        return 2

    try:
        files_dir = validate_files_dir(Path(args.files_dir) if args.files_dir else default_files_dir()) if want_files else None

        if args.check:
            code = check(prod_url)
            if want_files:
                files = list_storage_files(supabase_url, supabase_key)
                total_kb = sum((f["size"] or 0) for f in files) / 1024
                print(f"Uploaded files (bucket '{STORAGE_BUCKET}'): {len(files)} files, {total_kb:,.0f} KB. Nothing was downloaded.")
            return code

        env_path = Path(__file__).resolve().parents[1] / ".env"
        server_url = mysql_server_url(env_path)
        code = backup(prod_url, args.schema or default_schema_name(), server_url, args.replace)
        if code == 0 and not args.schema:
            for name in prune_old_backups(server_url, args.keep):
                print(f"Removed old backup schema `{name}` (keeping the newest {args.keep}).")

        if want_files:
            print(f"\nBacking up uploaded files to {files_dir / STORAGE_BUCKET} ...")
            st = backup_files(supabase_url, supabase_key, files_dir)
            print(f"Files: {st['total']} in production | {st['downloaded']} downloaded | {st['skipped']} already had | "
                  f"{st['failed']} failed | {st['unsafe']} skipped as unsafe names")
            if st["failed"] or st["unsafe"]:
                code = code or 1
        return code
    except ValueError as exc:
        print(f"Refused: {exc}")
        return 2
    except Exception as exc:
        print(f"BACKUP FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
