import importlib.util
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1] / "server_backup.py"


class BackupSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not MODULE.exists():
            return
        spec = importlib.util.spec_from_file_location("server_backup", MODULE)
        cls.tool = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.tool
        spec.loader.exec_module(cls.tool)

    def setUp(self):
        self.assertTrue(MODULE.exists(), "The server backup implementation is missing")

    def test_hash_covers_values_duplicates_and_ignores_row_order(self):
        digest = self.tool.hash_rows
        original = digest(io.BytesIO(b'a\nb\na\n'))
        self.assertEqual(original, digest(io.BytesIO(b'b\na\na\n')))
        self.assertNotEqual(original, digest(io.BytesIO(b'a\nb\n')))
        self.assertNotEqual(original, digest(io.BytesIO(b'a\nc\na\n')))
        self.assertEqual(original["rows"], 3)

    def test_schema_comparison_normalizes_only_dump_headers(self):
        prefix = (
            b"--\n-- PostgreSQL database dump\n--\n\n"
            b"\\restrict RANDOM_KEY\n\n"
            b"-- Dumped from database version 16.9\n"
            b"-- Dumped by pg_dump version 18.1\n\n"
        )
        suffix = b"--\n-- PostgreSQL database dump complete\n--\n\n\\unrestrict RANDOM_KEY\n\n"
        reconnect = b"\\unrestrict RANDOM_KEY\n\\connect app\n\\restrict RANDOM_KEY\n"
        schema = prefix + reconnect + b"CREATE FUNCTION f() RETURNS text AS $$\n-- keep body\n$$ LANGUAGE sql;\n" + suffix
        different_key = schema.replace(b"RANDOM_KEY", b"OTHER_KEY")
        different_version = schema.replace(b"16.9", b"18.1")
        self.assertEqual(self.tool.normalize_schema(schema), self.tool.normalize_schema(different_key))
        self.assertEqual(self.tool.normalize_schema(schema), self.tool.normalize_schema(different_version))
        self.assertNotEqual(
            self.tool.normalize_schema(schema),
            self.tool.normalize_schema(schema.replace(b"keep body", b"changed body")),
        )

    def test_archive_filter_protects_defaults_without_matching_user_comments(self):
        toc = (
            '; archive\n'
            '1; 0 0 SCHEMA - public admin\n'
            '2; 0 0 ACL - SCHEMA public admin\n'
            '3; 0 0 COMMENT - EXTENSION plpgsql admin\n'
            '4; 0 0 TABLE public azure_pg_admin_notes admin\n'
            '5; 0 0 COMMENT public TABLE azure_pg_admin_notes admin\n'
            '6; 0 0 DATABASE - postgres admin\n'
            '7; 0 0 ACL public SCHEMA public azure_pg_admin\n'
            '8; 0 0 ACL - SCHEMA "public" azure_pg_admin\n'
            '9; 0 0 COMMENT - EXTENSION "plpgsql" azuresu\n'
            '10; 0 0 SCHEMA - "public" azure_pg_admin\n'
        )
        filtered = self.tool.safe_toc(toc, "postgres")
        for number in (1, 2, 3, 6, 7, 8, 9, 10):
            self.assertIn(f";{number};", filtered)
        for number in (4, 5):
            self.assertIn(f"\n{number};", filtered)

    def test_role_script_quotes_names_and_never_alters_managed_roles(self):
        role = {
            "name": 'app"; DROP ROLE x; --',
            "login": True, "inherit": True, "createdb": False,
            "createrole": False, "connection_limit": -1,
            "valid_until": None, "config": ["search_path=public"],
            "comment": "O'Reilly",
        }
        sql = self.tool.role_sql(role)
        self.assertIn('CREATE ROLE "app""; DROP ROLE x; --"', sql)
        self.assertIn("'O''Reilly'", sql)
        self.assertNotIn("PASSWORD", sql)
        for name in ("azure_pg_admin", "azuresu", "pg_monitor", "replication"):
            with self.subTest(name=name), self.assertRaises(self.tool.BackupError):
                self.tool.role_sql(dict(role, name=name))

    def test_corrupt_backup_rejected_before_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "db.dump").write_bytes(b"corrupted")
            manifest = {
                "format": 1,
                "files": {"db.dump": "0" * 64},
            }
            data = json.dumps(manifest).encode()
            (root / "manifest.json").write_bytes(data)
            (root / "manifest.sha256").write_text(
                self.tool.hashlib.sha256(data).hexdigest()
            )
            with self.assertRaisesRegex(self.tool.BackupError, "checksum"):
                self.tool.load_backup(root)

    def test_manifest_cannot_escape_backup_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {"format": 1, "files": {"../outside": "0" * 64}}
            data = json.dumps(manifest).encode()
            (root / "manifest.json").write_bytes(data)
            (root / "manifest.sha256").write_text(
                self.tool.hashlib.sha256(data).hexdigest()
            )
            with self.assertRaisesRegex(self.tool.BackupError, "path"):
                self.tool.load_backup(root)

    def test_cli_requires_explicit_outage_and_target_acknowledgement(self):
        command = [
            sys.executable, "-B", str(MODULE), "restore",
            "--host", "target.postgres.database.azure.com",
            "--user", "migration_admin", "--backup-dir", "/nonexistent-backup",
            "--report", "/nonexistent-report-directory/report.json",
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--quiesced is mandatory", result.stderr)
        result = subprocess.run(command + ["--quiesced"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--confirm-target is required", result.stderr)


if __name__ == "__main__":
    unittest.main()
