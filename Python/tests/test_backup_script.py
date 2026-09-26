import importlib.util
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from sqlalchemy import Column, MetaData, Table
from sqlalchemy.dialects import postgresql

_SPEC = importlib.util.spec_from_file_location(
    "backup_prod_to_local", Path(__file__).resolve().parents[1] / "scripts" / "backup_prod_to_local.py")
backup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(backup)


class TargetSchemaSafetyTest(unittest.TestCase):
    def test_default_name_contains_backup_and_a_timestamp(self):
        name = backup.default_schema_name(datetime(2026, 9, 25, 21, 5))
        self.assertEqual(name, "rent_app_prod_backup_20260925_2105")
        self.assertEqual(backup.validate_target_schema(name), name)

    def test_the_dev_database_and_system_schemas_can_never_be_a_target(self):
        for bad in ("rent_app", "RENT_APP", "mysql", "sys", "information_schema", "performance_schema", "sakila", "world"):
            with self.assertRaises(ValueError, msg=bad):
                backup.validate_target_schema(bad)

    def test_a_name_without_backup_is_refused(self):
        with self.assertRaises(ValueError):
            backup.validate_target_schema("rent_app_copy")

    def test_names_that_could_inject_sql_are_refused(self):
        for bad in ("x`; DROP DATABASE rent_app; --backup", "a b backup", "backup-1", "", "b" * 70 + "backup"):
            with self.assertRaises(ValueError, msg=bad):
                backup.validate_target_schema(bad)


class LocalServerUrlTest(unittest.TestCase):
    def _env(self, db_url):
        d = Path(tempfile.mkdtemp())
        (d / ".env").write_text(f"DB_URL={db_url}\n", encoding="utf-8")
        return d / ".env"

    def test_takes_db_url_from_env_and_drops_the_database_name(self):
        url = backup.mysql_server_url(self._env("mysql+pymysql://root:pw@localhost:3306/rent_app"))
        self.assertEqual(url, "mysql+pymysql://root:pw@localhost:3306")

    def test_refuses_a_remote_mysql_server(self):
        with self.assertRaises(ValueError):
            backup.mysql_server_url(self._env("mysql+pymysql://root:pw@db.example.com:3306/rent_app"))

    def test_refuses_a_non_mysql_target(self):
        with self.assertRaises(ValueError):
            backup.mysql_server_url(self._env("postgresql://u:p@localhost/x"))

    def test_an_explicit_override_is_still_held_to_the_local_rule(self):
        with self.assertRaises(ValueError):
            backup.mysql_server_url(Path("nope.env"), override="mysql+pymysql://root:pw@203.0.113.9/x")

    def test_missing_config_is_a_clear_error(self):
        d = Path(tempfile.mkdtemp())
        (d / ".env").write_text("OTHER=1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            backup.mysql_server_url(d / ".env")


class PostgresUrlTest(unittest.TestCase):
    def test_plain_postgres_urls_are_routed_to_psycopg_v3(self):
        self.assertEqual(backup.normalize_postgres_url("postgres://u:p@h/db"), "postgresql+psycopg://u:p@h/db")
        self.assertEqual(backup.normalize_postgres_url("postgresql://u:p@h/db"), "postgresql+psycopg://u:p@h/db")
        self.assertEqual(backup.normalize_postgres_url("postgresql+psycopg://u:p@h/db"), "postgresql+psycopg://u:p@h/db")


class ColumnTypeConversionTest(unittest.TestCase):
    """Reflected Postgres types must become something MySQL can create."""

    def _convert(self, coltype, **kw):
        t = Table("t", MetaData(), Column("c", coltype, **kw))
        return backup.generic_column(t.c.c)

    def test_postgres_specific_types_become_generic(self):
        from sqlalchemy.dialects import mysql
        from sqlalchemy.schema import CreateTable
        meta = MetaData()
        src = Table("users", meta,
                    Column("id", postgresql.INTEGER, primary_key=True),
                    Column("amount", postgresql.DOUBLE_PRECISION),
                    Column("created", postgresql.TIMESTAMP(timezone=False)),
                    Column("name", postgresql.VARCHAR(255)),
                    Column("notes", postgresql.TEXT))
        dst = backup.build_target_table(src, MetaData())
        ddl = str(CreateTable(dst).compile(dialect=mysql.dialect()))   # would raise on an untranslatable type
        for col in ("id", "amount", "created", "name", "notes"):
            self.assertIn(col, ddl)

    def test_every_float_and_numeric_shape_creates_on_mysql(self):
        import sqlalchemy as sa
        from sqlalchemy.dialects import mysql
        from sqlalchemy.schema import CreateTable
        shapes = [sa.Double(precision=53), sa.Float(precision=53), sa.Float(precision=24), postgresql.DOUBLE_PRECISION(),
                  postgresql.REAL(), postgresql.NUMERIC(), postgresql.NUMERIC(10), postgresql.NUMERIC(10, 2), postgresql.NUMERIC(scale=2)]
        for shape in shapes:
            src = Table("t", MetaData(), Column("c", shape))
            ddl = str(CreateTable(backup.build_target_table(src, MetaData())).compile(dialect=mysql.dialect()))
            self.assertIn("c ", ddl, repr(shape))

    def test_unlimited_numeric_keeps_its_decimals(self):
        self.assertEqual(self._convert(postgresql.NUMERIC()).type.scale, 10)

    def test_varchar_without_a_length_becomes_text(self):
        from sqlalchemy import Text
        self.assertIsInstance(self._convert(postgresql.VARCHAR()).type, Text)

    def test_primary_key_and_nullability_are_kept(self):
        col = self._convert(postgresql.INTEGER, primary_key=True)
        self.assertTrue(col.primary_key)
        self.assertFalse(self._convert(postgresql.TEXT, nullable=False).nullable)
        self.assertTrue(self._convert(postgresql.TEXT, nullable=True).nullable)


class CliGuardTest(unittest.TestCase):
    def test_without_a_production_url_it_stops_and_writes_nothing(self):
        import os
        old = os.environ.pop("PROD_DATABASE_URL", None)
        try:
            self.assertEqual(backup.main([]), 2)
        finally:
            if old is not None:
                os.environ["PROD_DATABASE_URL"] = old

    def test_a_bad_schema_name_is_refused_before_any_connection(self):
        # Would try to connect to a server that doesn't exist if the guard didn't run first.
        # --skip-files so the refusal we're testing is the schema one, not "no Supabase keys".
        self.assertEqual(backup.main(["--source-url", "postgresql://u:p@127.0.0.1:1/x", "--schema", "rent_app", "--skip-files"]), 2)

    def test_files_backup_without_supabase_keys_stops_before_touching_anything(self):
        import os
        saved = {k: os.environ.pop(k, None) for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY")}
        try:
            self.assertEqual(backup.main(["--source-url", "postgresql://u:p@127.0.0.1:1/x"]), 2)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
