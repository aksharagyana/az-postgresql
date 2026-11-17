import importlib.util
import io
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import tarfile
import unittest
from unittest.mock import patch


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

    def logical_fixture(self, root):
        root.mkdir()
        (root / "database-0000.dump").write_bytes(b"synthetic database contents\n")
        (root / "roles.sql").write_text("-- synthetic roles\n")
        manifest = {
            "format": 1, "source_host": "source", "source_port": 5432,
            "source_major": 16, "admin": "migration_admin",
            "archives": {"app": "database-0000.dump"},
            "baseline": {"databases": {"app": {}}},
            "files": {
                name: self.tool.file_hash(root / name)
                for name in ("database-0000.dump", "roles.sql")
            },
        }
        self.tool.write_json(root / "manifest.json", manifest)
        (root / "manifest.sha256").write_text(self.tool.file_hash(root / "manifest.json"))
        return manifest

    def test_compressed_archive_round_trip_preserves_data_and_excludes_unlisted_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, restored, archive = root / "source", root / "restored", root / "backup.tar.gz"
            manifest = self.logical_fixture(source)
            (source / "private-credentials.txt").write_text("synthetic:must-not-upload")
            self.tool.create_backup_archive(source, archive)
            self.tool.extract_backup_archive(archive, restored)
            self.assertEqual(self.tool.load_backup(restored), manifest)
            self.assertEqual(
                (restored / "database-0000.dump").read_bytes(),
                (source / "database-0000.dump").read_bytes(),
            )
            self.assertFalse((restored / "private-credentials.txt").exists())
            self.assertEqual(restored.stat().st_mode & 0o777, 0o700)
            for path in restored.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(self.tool.BackupError, "already exists"):
                self.tool.extract_backup_archive(archive, restored)
            with patch.object(self.tool.shutil, "disk_usage") as usage:
                usage.return_value.free = 1
                with self.assertRaisesRegex(self.tool.BackupError, "free space"):
                    self.tool.extract_backup_archive(archive, root / "no-space")
            self.assertFalse((root / "no-space").exists())

    def test_logical_upload_is_timestamped_compressed_and_entra_authenticated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "backup"
            self.logical_fixture(root)
            uploaded = {}

            def az(*arguments):
                if arguments[:2] == ("cloud", "show"):
                    return {"suffixes": {"storageEndpoint": "core.windows.net"}}
                archive = Path(arguments[arguments.index("--file") + 1])
                uploaded["contents"] = archive.read_bytes()
                uploaded["command"] = arguments
                self.assertEqual(
                    arguments[arguments.index("--metadata") + 1],
                    "sha256=" + self.tool.file_hash(archive),
                )
                return {}

            with patch.object(self.tool.blob, "az_json", side_effect=az):
                url = self.tool.upload_backup(root, "migrationstore", "backups")
            command = uploaded["command"]
            self.assertEqual(command[:3], ("storage", "blob", "upload"))
            self.assertEqual(command[command.index("--auth-mode") + 1], "login")
            self.assertEqual(command[command.index("--overwrite") + 1], "false")
            self.assertRegex(url, r"^https://migrationstore\.blob\.core\.windows\.net/backups/data-\d{14}/backup\.tar\.gz$")
            with tarfile.open(fileobj=io.BytesIO(uploaded["contents"]), mode="r:gz") as archive:
                self.assertEqual(
                    archive.extractfile("database-0000.dump").read(),
                    b"synthetic database contents\n",
                )
            self.assertEqual(sorted(path.name for path in root.parent.iterdir()), ["backup"])
            with patch.object(self.tool.blob, "upload_file", side_effect=self.tool.BackupError("denied")), patch.object(
                self.tool.blob, "blob_url", return_value=url,
            ):
                with self.assertRaisesRegex(self.tool.BackupError, "local backup remains"):
                    self.tool.upload_backup(root, "migrationstore", "backups")
            self.tool.load_backup(root)
            self.assertEqual(sorted(path.name for path in root.parent.iterdir()), ["backup"])

    def test_download_verifies_archive_and_file_checksums_before_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, archive = root / "source", root / "backup.tar.gz"
            self.logical_fixture(source)
            url = "https://migrationstore.blob.core.windows.net/backups/data-20261117130811/backup.tar.gz"
            server = self.tool.Server("target", "migration_admin")
            for failure in ("none", "archive-hash", "file-hash", "missing-hash"):
                with self.subTest(failure=failure):
                    (source / "database-0000.dump").write_bytes(b"synthetic database contents\n")
                    self.tool.create_backup_archive(source, archive)
                    if failure == "file-hash":
                        with tarfile.open(archive, "w:gz") as output:
                            for path in source.iterdir():
                                if path.name == "database-0000.dump":
                                    data = b"corrupted data"
                                    member = tarfile.TarInfo(path.name)
                                    member.size = len(data)
                                    output.addfile(member, io.BytesIO(data))
                                else:
                                    output.add(path, arcname=path.name)
                    contents = archive.read_bytes()
                    digest = self.tool.file_hash(archive)
                    metadata = {} if failure == "missing-hash" else {
                        "sha256": "0" * 64 if failure == "archive-hash" else digest,
                    }

                    def az(*arguments):
                        if arguments[:3] == ("storage", "blob", "show"):
                            return {
                                "properties": {"contentLength": len(contents), "etag": '"archive-etag"'},
                                "metadata": metadata,
                            }
                        self.assertEqual(arguments[arguments.index("--auth-mode") + 1], "login")
                        self.assertEqual(arguments[arguments.index("--if-match") + 1], '"archive-etag"')
                        Path(arguments[arguments.index("--file") + 1]).write_bytes(contents)
                        return {}

                    destination = root / ("download-" + failure)
                    with patch.object(self.tool.blob, "az_json", side_effect=az), patch.object(
                        self.tool.Server, "check_clients",
                    ) as clients:
                        if failure == "none":
                            self.tool.download_backup(url, destination)
                            self.assertEqual(
                                (destination / "database-0000.dump").read_bytes(),
                                b"synthetic database contents\n",
                            )
                        else:
                            with self.assertRaisesRegex(self.tool.BackupError, "[Cc]hecksum|SHA-256"):
                                self.tool.restore_server(
                                    server, destination, root / "report.json", server.host,
                                    backup_url=url,
                                )
                        clients.assert_not_called()
                    self.assertFalse((root / "report.json").exists())
                    archive.unlink()

    def test_archive_rejects_paths_links_devices_and_duplicate_entries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (name, kind) in enumerate((
                ("../outside", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
                ("link", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE),
                ("device", tarfile.CHRTYPE), ("directory", tarfile.DIRTYPE),
                ("duplicate", tarfile.REGTYPE),
            )):
                with self.subTest(name=name):
                    archive = root / f"malicious-{index}.tar.gz"
                    with tarfile.open(archive, "w:gz") as output:
                        member = tarfile.TarInfo(name)
                        member.type = kind
                        member.linkname = "../outside" if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ""
                        output.addfile(member)
                        if name == "duplicate":
                            output.addfile(member)
                    destination = root / f"restored-{index}"
                    with self.assertRaisesRegex(self.tool.BackupError, "[Uu]nsafe|[Dd]uplicate"):
                        self.tool.extract_backup_archive(archive, destination)
                    self.assertFalse(destination.exists())
            self.assertFalse((root / "outside").exists())

    def test_blob_options_fail_before_database_or_existing_directory_changes(self):
        server = self.tool.Server("target", "migration_admin")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(self.tool.Server, "check_clients") as clients:
                with self.assertRaisesRegex(self.tool.BackupError, "together"):
                    self.tool.backup_server(server, root / "new", storage_account="migrationstore")
                clients.assert_not_called()
                self.assertFalse((root / "new").exists())
            with patch.object(self.tool.blob, "az_json") as azure:
                with self.assertRaisesRegex(self.tool.BackupError, "already exists"):
                    self.tool.download_backup(
                        "https://migrationstore.blob.core.windows.net/backups/archive.tar.gz", root,
                    )
                azure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
