"""Opt-in real 16 -> 16/18 rehearsals; isolated Docker network, no published ports."""

import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


MODULE = Path(__file__).resolve().parents[1] / "server_backup.py"


@unittest.skipUnless(os.environ.get("PG_BACKUP_INTEGRATION") == "1", "opt-in Docker rehearsal")
class RecoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("integration_backup", MODULE)
        cls.tool = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.tool
        spec.loader.exec_module(cls.tool)
        cls.work = tempfile.TemporaryDirectory(prefix="pg-backup-test-")
        cls.root = Path(cls.work.name).resolve()
        cls.old_tempdir = tempfile.tempdir
        tempfile.tempdir = str(cls.root)
        cls.old_sslmode = os.environ.get("PGSSLMODE")
        os.environ["PGSSLMODE"] = "disable"
        cls.prefix = "pg-backup-test-" + uuid.uuid4().hex[:10]
        cls.containers = []
        cls.network_created = False
        cls.addClassCleanup(cls.cleanup)
        cls.docker("network", "create", "--internal", cls.prefix)
        cls.network_created = True
        for suffix, version in (("source", "16"), ("rollback", "16"), ("upgrade", "18")):
            name = cls.prefix + "-" + suffix
            cls.docker(
                "run", "--detach", "--name", name, "--network", cls.prefix,
                "--mount", f"type=bind,source={cls.root},target={cls.root}",
                "--env", "POSTGRES_HOST_AUTH_METHOD=trust",
                "--env", "POSTGRES_USER=azuresu", "--env", "POSTGRES_DB=postgres",
                "postgres:" + version,
            )
            cls.containers.append(name)
            for attempt in range(60):
                result = subprocess.run(
                    ["docker", "exec", name, "pg_isready", "-U", "azuresu"],
                    capture_output=True,
                )
                if result.returncode == 0:
                    # The entrypoint briefly runs a socket-only initialization server.
                    result = subprocess.run(
                        ["docker", "exec", name, "psql", "-h", name, "-U", "azuresu",
                         "-d", "postgres",
                         "-Atc", "SELECT 1"],
                        capture_output=True,
                    )
                    if result.returncode == 0:
                        break
                time.sleep(1)
            else:
                raise RuntimeError(f"Test database did not become ready: {name}")
        cls.bin16 = cls.clients("16", cls.containers[0])
        cls.bin18 = cls.clients("18", cls.containers[2])
        for name in cls.containers:
            bootstrap = cls.tool.Server(name, "azuresu", bin_dir=cls.bin18)
            bootstrap.sql("postgres", "CREATE ROLE azure_pg_admin NOLOGIN")
            bootstrap.sql("postgres", "CREATE ROLE migration_admin LOGIN CREATEDB CREATEROLE")
            bootstrap.sql("postgres", "GRANT pg_read_all_data, azure_pg_admin TO migration_admin")
            bootstrap.sql("postgres", "CREATE DATABASE azure_sys")
            bootstrap.sql("postgres", "CREATE DATABASE azure_maintenance")
            bootstrap.sql("postgres", "REVOKE CONNECT ON DATABASE azure_maintenance FROM PUBLIC")
            bootstrap.sql("azure_sys", "CREATE TABLE public.service_state(value text); INSERT INTO public.service_state VALUES ('managed')")
            bootstrap.sql("postgres", "ALTER DATABASE postgres OWNER TO migration_admin")
            bootstrap.sql("postgres", "ALTER SCHEMA public OWNER TO azure_pg_admin")
            bootstrap.sql("template1", "ALTER SCHEMA public OWNER TO azure_pg_admin")
            bootstrap.sql("postgres", "ALTER DATABASE template0 ALLOW_CONNECTIONS true")
            bootstrap.sql("template0", "ALTER SCHEMA public OWNER TO azure_pg_admin")
            bootstrap.sql("postgres", "ALTER DATABASE template0 ALLOW_CONNECTIONS false")
        cls.source = cls.tool.Server(
            cls.containers[0], "migration_admin", bin_dir=cls.bin16, schema_bin_dir=cls.bin18,
        )
        cls.rollback = cls.tool.Server(
            cls.containers[1], "migration_admin", bin_dir=cls.bin16, schema_bin_dir=cls.bin18,
        )
        cls.upgrade = cls.tool.Server(
            cls.containers[2], "migration_admin", bin_dir=cls.bin18, schema_bin_dir=cls.bin18,
        )
        cls.source.sql("postgres", "CREATE ROLE app_owner LOGIN")
        cls.source.sql("postgres", "CREATE ROLE app_reader NOLOGIN")
        cls.source.sql("postgres", "GRANT app_owner TO migration_admin WITH INHERIT TRUE, SET TRUE")
        cls.source.sql("postgres", "GRANT app_reader TO app_owner WITH ADMIN TRUE")
        cls.source.sql("postgres", "COMMENT ON ROLE app_owner IS 'Application owner'")
        cls.source.sql("postgres", "ALTER ROLE app_owner SET search_path TO public")
        cls.source.sql("postgres", 'CREATE DATABASE "app = weird" OWNER app_owner')
        cls.source.sql("postgres", "CREATE DATABASE empty_app")
        cls.source.sql("app = weird", "GRANT USAGE, CREATE ON SCHEMA public TO app_owner WITH GRANT OPTION")
        cls.source.sql("postgres", 'ALTER DATABASE "app = weird" SET default_statistics_target TO 200')
        cls.source.sql("postgres", 'ALTER ROLE app_reader IN DATABASE "app = weird" SET statement_timeout TO \'2s\'')
        cls.source.sql("postgres", """
            CREATE TABLE public.admin_notes(note text);
            INSERT INTO public.admin_notes VALUES ('user data in postgres is included');
        """)
        cls.source.sql("app = weird", r"""
            SET ROLE app_owner;
            CREATE DOMAIN public.positive AS integer CHECK (VALUE > 0);
            CREATE TABLE public.records(
                id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                value text, raw bytea, document jsonb, nullable text,
                measured numeric, occurred timestamptz, valid public.positive
            );
            INSERT INTO public.records(value, raw, document, measured, occurred, valid)
                VALUES (E'quoted " and new\nline', decode('000aff', 'hex'),
                        '{"a":1,"b":null}', 1234.560, '2026-01-02T03:04:05Z', 1);
            CREATE TABLE public.duplicates(value text);
            INSERT INTO public.duplicates VALUES ('same'), ('same'), (NULL), ('');
            CREATE TABLE public.partitioned(id integer) PARTITION BY RANGE(id);
            CREATE TABLE public.partition_one PARTITION OF public.partitioned
                FOR VALUES FROM (0) TO (10);
            INSERT INTO public.partitioned VALUES (1), (2);
            CREATE TABLE public.parent(id integer);
            CREATE TABLE public.child() INHERITS (public.parent);
            INSERT INTO public.parent VALUES (1);
            INSERT INTO public.child VALUES (2);
            CREATE SEQUENCE public.unused START 42;
            CREATE VIEW public.visible AS SELECT id, value FROM public.records;
            CREATE MATERIALIZED VIEW public.cached AS SELECT * FROM public.duplicates;
            CREATE MATERIALIZED VIEW public.unpopulated AS SELECT 1 WITH NO DATA;
            CREATE FUNCTION public.answer() RETURNS integer LANGUAGE sql
                AS 'SELECT 42';
            CREATE PROCEDURE public.do_nothing() LANGUAGE plpgsql
                AS 'BEGIN NULL; END';
            CREATE FUNCTION public.audit() RETURNS trigger LANGUAGE plpgsql
                AS 'BEGIN RETURN NEW; END';
            CREATE TRIGGER record_insert BEFORE INSERT ON public.records
                FOR EACH ROW EXECUTE FUNCTION public.audit();
            GRANT USAGE ON SCHEMA public TO app_reader;
            GRANT SELECT ON ALL TABLES IN SCHEMA public TO app_reader;
            ALTER DEFAULT PRIVILEGES GRANT SELECT ON TABLES TO app_reader;
            COMMENT ON TABLE public.records IS 'Keep this comment';
            SELECT lo_from_bytea(123456, decode(repeat('00aaff', 30000), 'hex'));
            GRANT SELECT ON LARGE OBJECT 123456 TO migration_admin;
        """)
        cls.backup = cls.root / "backup"

    @classmethod
    def docker(cls, *args):
        result = subprocess.run(["docker", *args], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return result.stdout.strip()

    @classmethod
    def clients(cls, version, container):
        directory = cls.root / ("clients" + version)
        directory.mkdir()
        for tool in ("psql", "pg_dump", "pg_restore"):
            path = directory / tool
            path.write_text(
                "#!/bin/sh\nexec docker exec -i "
                "-e PGOPTIONS -e PGSSLMODE -e PGCLIENTENCODING "
                "-e PGAPPNAME -e PGCONNECT_TIMEOUT "
                + shlex.quote(container) + " " + tool + ' "$@"\n'
            )
            path.chmod(0o700)
        return directory

    @classmethod
    def cleanup(cls):
        errors = []
        for name in reversed(cls.containers):
            result = subprocess.run(["docker", "rm", "-f", "-v", name], capture_output=True)
            if result.returncode:
                errors.append(result.stderr.decode())
        if cls.network_created:
            result = subprocess.run(["docker", "network", "rm", cls.prefix], capture_output=True)
            if result.returncode:
                errors.append(result.stderr.decode())
        tempfile.tempdir = cls.old_tempdir
        if cls.old_sslmode is None:
            os.environ.pop("PGSSLMODE", None)
        else:
            os.environ["PGSSLMODE"] = cls.old_sslmode
        cls.work.cleanup()
        if errors:
            raise RuntimeError("Test resource cleanup failed:\n" + "\n".join(errors))

    def test_complete_recovery_and_mismatch_detection(self):
        if os.environ.get("PG_BACKUP_TEST_IMAGE"):
            self.check_packaged_image()
            return
        with self.tool.private_files():
            self.tool.backup_server(self.source, self.backup)
            for target in (self.rollback, self.upgrade):
                with self.subTest(target=target.host):
                    report = self.root / (target.host + ".json")
                    self.tool.restore_server(
                        target, self.backup, report, confirm_target=target.host,
                    )
                    self.assertTrue(json.loads(report.read_text())["passed"])
                    self.assertEqual(target.sql("app = weird", "SELECT public.answer()"), "42")
                    self.assertEqual(target.sql("app = weird", "SELECT count(*) FROM public.partitioned"), "2")
                    self.assertEqual(target.sql("postgres", "SELECT count(*) FROM public.admin_notes"), "1")
                    self.assertEqual(target.sql("app = weird", "SELECT octet_length(lo_get(123456))"), "90000")
                    self.assertEqual(target.sql("azure_sys", "SELECT value FROM public.service_state"), "managed")
                    self.assertNotIn("azure_sys", self.tool.load_backup(self.backup)["archives"])
                    with self.assertRaisesRegex(self.tool.BackupError, "fresh target"):
                        self.tool.restore_server(
                            target, self.backup, self.root / "refused.json", target.host,
                        )
            self.upgrade.sql("app = weird", "UPDATE public.duplicates SET value='changed' WHERE value='same'")
            with self.assertRaisesRegex(self.tool.BackupError, "Integrity validation FAILED"):
                self.tool.validate_server(
                    self.upgrade, self.backup, self.root / "changed.json",
                )
            changed = json.loads((self.root / "changed.json").read_text())
            self.assertTrue(any("duplicates" in item for item in changed["differences"]))
            self.upgrade.sql("app = weird", "UPDATE public.duplicates SET value='same' WHERE value='changed'")
            self.upgrade.sql("app = weird", "SELECT nextval('public.unused')")
            self.upgrade.sql("app = weird", "CREATE OR REPLACE FUNCTION public.answer() RETURNS integer LANGUAGE sql AS 'SELECT 43'")
            with self.assertRaisesRegex(self.tool.BackupError, "Integrity validation FAILED"):
                self.tool.validate_server(
                    self.upgrade, self.backup, self.root / "routine-sequence.json",
                )
            changed = json.loads((self.root / "routine-sequence.json").read_text())
            self.assertTrue(any("schema_sha256" in item for item in changed["differences"]))
            self.assertTrue(any("unused" in item for item in changed["differences"]))
            bootstrap = self.tool.Server(self.source.host, "azuresu", bin_dir=self.bin16)
            bootstrap.sql("app = weird", "CREATE EXTENSION postgres_fdw")
            bootstrap.sql("app = weird", "CREATE SERVER external FOREIGN DATA WRAPPER postgres_fdw")
            bootstrap.sql("app = weird", "CREATE FOREIGN TABLE public.external(id integer) SERVER external")
            with self.assertRaisesRegex(self.tool.BackupError, "foreign table data"):
                self.tool.preflight(self.source)
            bootstrap.sql("azure_sys", """
                CREATE SCHEMA cron;
                CREATE TABLE cron.job(id integer);
                INSERT INTO cron.job VALUES (1);
            """)
            with self.assertRaisesRegex(self.tool.BackupError, "pg_cron jobs in azure_sys"):
                self.tool.preflight(self.source)

    def check_packaged_image(self):
        name = self.prefix + "-toolbox"
        self.docker(
            "run", "--detach", "--name", name, "--network", self.prefix,
            "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--mount", "type=volume,target=/data",
            "--env", "PGSSLMODE=disable", os.environ["PG_BACKUP_TEST_IMAGE"],
        )
        self.containers.append(name)
        self.assertEqual(self.docker("inspect", "--format", "{{.State.Running}}", name), "true")
        self.assertEqual(self.docker("exec", name, "id", "-u"), "10001")
        self.docker("exec", name, "make", "test")
        base = "--user migration_admin --backup-dir /data/preupgrade --quiesced"
        self.docker(
            "exec", name, "make", "backup", "PG_MAJOR=16",
            f"ARGS=--host {self.source.host} {base}",
        )
        # A restarted toolbox retains the backup, but has a fresh /tmp.
        self.docker("restart", name)
        self.docker("exec", name, "test", "-f", "/data/preupgrade/manifest.json")
        for target, major in ((self.rollback, 16), (self.upgrade, 18)):
            report = f"/data/recovery-{major}.json"
            self.docker(
                "exec", name, "make", "restore", f"PG_MAJOR={major}",
                f"ARGS=--host {target.host} --confirm-target {target.host} "
                f"{base} --report {report}",
            )
            result = json.loads(self.docker("exec", name, "cat", report))
            self.assertTrue(result["passed"])
            self.assertEqual(target.sql("app = weird", "SELECT public.answer()"), "42")
            self.assertEqual(target.sql("app = weird", "SELECT octet_length(lo_get(123456))"), "90000")
            self.assertEqual(target.sql("azure_sys", "SELECT value FROM public.service_state"), "managed")
        self.upgrade.sql("app = weird", "UPDATE public.duplicates SET value='changed' WHERE value='same'")
        result = subprocess.run(
            ["docker", "exec", name, "make", "validate", "PG_MAJOR=18",
             f"ARGS=--host {self.upgrade.host} {base} --report /data/mismatch.json"],
            capture_output=True, text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Integrity validation FAILED", result.stderr)
        report = json.loads(self.docker("exec", name, "cat", "/data/mismatch.json"))
        self.assertTrue(any("duplicates" in item for item in report["differences"]))


if __name__ == "__main__":
    unittest.main()
