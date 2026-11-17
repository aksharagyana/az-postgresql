import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "azure_pitr.py"
SUBSCRIPTION = "11111111-1111-1111-1111-111111111111"
TENANT = "22222222-2222-2222-2222-222222222222"
SOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/source-rg"
    "/providers/Microsoft.DBforPostgreSQL/flexibleServers/source16"
)


class ManagedPitrTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if MODULE.exists():
            spec = importlib.util.spec_from_file_location("azure_pitr", MODULE)
            cls.tool = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = cls.tool
            spec.loader.exec_module(cls.tool)

    def setUp(self):
        self.assertTrue(MODULE.exists(), "Managed PITR implementation is missing")
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.checkpoint = self.root / "checkpoint.json"
        self.report = self.root / "restore.json"
        now = datetime.now(timezone.utc)
        self.completed = (now - timedelta(minutes=10)).isoformat()
        self.source = {
            "id": SOURCE_ID, "name": "source16", "state": "Ready",
            "version": "16", "location": "westeurope",
            "backup": {
                "earliestRestoreDate": (now - timedelta(days=2)).isoformat(),
                "backupRetentionDays": 7,
            },
            "network": {
                "delegatedSubnetResourceId": "/subscriptions/test/subnets/postgres",
                "privateDnsZoneArmResourceId": "/subscriptions/test/privateDnsZones/postgres",
                "publicNetworkAccess": "Disabled",
            },
            "dataEncryption": {"type": "SystemManaged"},
        }
        self.backup = {
            "id": SOURCE_ID + "/backups/preupgrade",
            "name": "preupgrade", "completedTime": self.completed,
            "backupType": "Customer On-Demand", "source": "OnDemand",
        }
        self.target_id = SOURCE_ID.replace("source-rg", "recovery-rg").replace("source16", "recovery16")
        self.target = {
            **copy.deepcopy(self.source), "id": self.target_id, "name": "recovery16",
            "fullyQualifiedDomainName": "recovery16.postgres.database.azure.com",
        }

    def take_backup(self, upload_error=None, **options):
        responses = [
            {"id": SUBSCRIPTION, "tenantId": TENANT},
            self.source, [], self.backup, self.backup, self.source,
        ]
        if options.get("storage_account"):
            responses.extend([
                {"suffixes": {"storageEndpoint": "core.windows.net"}},
                upload_error or {},
            ])
        with patch.object(self.tool, "az_json", side_effect=responses) as azure:
            result = self.tool.take_pitr_backup(
                SUBSCRIPTION, "source-rg", "source16", self.checkpoint,
                backup_name="preupgrade", quiesced=True, **options,
            )
        return result, azure

    def restore(self, source=None, targets=None, restored=None):
        with patch.object(self.tool, "az_json", side_effect=[
            {"id": SUBSCRIPTION, "tenantId": TENANT},
            source if source is not None else self.source,
            self.backup, targets if targets is not None else [],
            self.target, restored if restored is not None else self.target,
        ]) as azure:
            result = self.tool.restore_pitr_backup(
                self.checkpoint, "recovery-rg", "recovery16", self.report,
                confirm_target="recovery16",
            )
        return result, azure

    def test_backup_waits_for_real_completion_and_records_azure_timestamp(self):
        record, azure = self.take_backup()
        self.assertEqual(record["status"], "backup_ready")
        self.assertEqual(record["restore_time"], self.completed)
        self.assertEqual(record["source_version"], "16")
        self.assertEqual(json.loads(self.checkpoint.read_text()), record)
        self.assertEqual(self.checkpoint.stat().st_mode & 0o777, 0o600)
        create = next(call.args for call in azure.call_args_list if call.args[:4] == (
            "postgres", "flexible-server", "backup", "create"
        ))
        self.assertIn("--server-name", create)
        self.assertNotIn("--no-wait", create)

    def test_restore_after_upgrade_uses_saved_time_not_current_version_or_latest(self):
        self.take_backup()
        upgraded = dict(self.source, version="18", state="Disabled")
        result, azure = self.restore(source=upgraded)
        command = next(call.args for call in azure.call_args_list if call.args[:3] == (
            "postgres", "flexible-server", "restore"
        ))
        self.assertEqual(command[command.index("--restore-time") + 1], self.completed)
        self.assertEqual(command[command.index("--source-server") + 1], SOURCE_ID)
        self.assertNotIn("--no-wait", command)
        self.assertEqual(result["status"], "server_ready")
        self.assertEqual(result["restored_version"], "16")

    def test_backup_requires_stopped_writers_before_any_azure_call(self):
        with patch.object(self.tool, "az_json") as azure:
            with self.assertRaisesRegex(self.tool.PitrError, "quiesced"):
                self.tool.take_pitr_backup(
                    SUBSCRIPTION, "source-rg", "source16", self.checkpoint,
                )
            azure.assert_not_called()

    def test_incomplete_cloud_backup_is_not_reported_as_usable(self):
        incomplete = dict(self.backup, completedTime=None)
        with patch.object(self.tool, "az_json", side_effect=[
            {"id": SUBSCRIPTION, "tenantId": TENANT},
            self.source, [], incomplete, incomplete,
        ]):
            with self.assertRaisesRegex(self.tool.PitrError, "completed"):
                self.tool.take_pitr_backup(
                    SUBSCRIPTION, "source-rg", "source16", self.checkpoint,
                    backup_name="preupgrade", quiesced=True,
                )
        self.assertNotEqual(json.loads(self.checkpoint.read_text())["status"], "backup_ready")

    def test_expired_recovery_point_does_not_create_target(self):
        self.take_backup()
        expired = copy.deepcopy(self.source)
        expired["backup"]["earliestRestoreDate"] = datetime.now(timezone.utc).isoformat()
        with self.assertRaisesRegex(self.tool.PitrError, "retention"):
            self.restore(source=expired)

    def test_existing_target_and_wrong_confirmation_are_refused(self):
        self.take_backup()
        with self.assertRaisesRegex(self.tool.PitrError, "already exists"):
            self.restore(targets=[self.target])
        with patch.object(self.tool, "az_json") as azure:
            with self.assertRaisesRegex(self.tool.PitrError, "confirm-target"):
                self.tool.restore_pitr_backup(
                    self.checkpoint, "recovery-rg", "recovery16",
                    self.root / "another.json", confirm_target="wrong",
                )
            azure.assert_not_called()

    def test_unexpected_restored_version_is_not_success(self):
        self.take_backup()
        with self.assertRaisesRegex(self.tool.PitrError, "version"):
            self.restore(restored=dict(self.target, version="18"))
        self.assertNotEqual(json.loads(self.report.read_text())["status"], "server_ready")

    def test_wrong_tenant_and_changed_backup_are_refused(self):
        self.take_backup()
        with patch.object(self.tool, "az_json", return_value={
            "id": SUBSCRIPTION, "tenantId": "wrong-tenant",
        }):
            with self.assertRaisesRegex(self.tool.PitrError, "tenant"):
                self.tool.restore_pitr_backup(
                    self.checkpoint, "recovery-rg", "recovery16", self.report,
                    confirm_target="recovery16",
                )
        self.backup["completedTime"] = datetime.now(timezone.utc).isoformat()
        with self.assertRaisesRegex(self.tool.PitrError, "backup.*changed"):
            self.restore()

    def test_existing_checkpoint_and_nonready_source_do_not_start_backup(self):
        self.checkpoint.write_text("do not overwrite")
        with patch.object(self.tool, "az_json") as azure:
            with self.assertRaisesRegex(self.tool.PitrError, "already exists"):
                self.tool.take_pitr_backup(
                    SUBSCRIPTION, "source-rg", "source16", self.checkpoint, quiesced=True,
                )
            azure.assert_not_called()
        self.assertEqual(self.checkpoint.read_text(), "do not overwrite")
        with patch.object(self.tool, "az_json", side_effect=[
            {"id": SUBSCRIPTION, "tenantId": TENANT},
            dict(self.source, state="Updating"),
        ]) as azure:
            with self.assertRaisesRegex(self.tool.PitrError, "Ready"):
                self.tool.take_pitr_backup(
                    SUBSCRIPTION, "source-rg", "source16",
                    self.root / "other.json", quiesced=True,
                )
            self.assertEqual(azure.call_count, 2)

    def test_azure_restore_failure_leaves_pending_report_and_propagates_error(self):
        self.take_backup()
        with patch.object(self.tool, "az_json", side_effect=[
            {"id": SUBSCRIPTION, "tenantId": TENANT},
            self.source, self.backup, [],
            self.tool.PitrError("Azure restore failed: simulated capacity error"),
        ]):
            with self.assertRaisesRegex(self.tool.PitrError, "capacity"):
                self.tool.restore_pitr_backup(
                    self.checkpoint, "recovery-rg", "recovery16", self.report,
                    confirm_target="recovery16",
                )
        self.assertEqual(json.loads(self.report.read_text())["status"], "restore_requested")

    def test_optional_blob_upload_uses_timestamp_prefix_and_entra_without_overwrite(self):
        record, azure = self.take_backup(storage_account="migrationstore", blob_container="pitr")
        command = azure.call_args_list[-1].args
        self.assertEqual(command[:3], ("storage", "blob", "upload"))
        self.assertEqual(command[command.index("--auth-mode") + 1], "login")
        self.assertEqual(command[command.index("--overwrite") + 1], "false")
        self.assertEqual(command[command.index("--file") + 1], str(self.checkpoint))
        name = command[command.index("--name") + 1]
        self.assertRegex(name, r"^\d{14}/checkpoint\.json$")
        timestamp = datetime.strptime(name.split("/")[0], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        self.assertLess(abs((datetime.now(timezone.utc) - timestamp).total_seconds()), 30)
        self.assertEqual(
            record["checkpoint_blob_url"],
            "https://migrationstore.blob.core.windows.net/pitr/" + name,
        )
        self.assertEqual(json.loads(self.checkpoint.read_text()), record)

    def test_invalid_or_incomplete_blob_destination_does_not_start_backup(self):
        for options in (
            {"storage_account": "migrationstore"},
            {"blob_container": "pitr"},
            {"storage_account": "INVALID", "blob_container": "pitr"},
            {"storage_account": "migrationstore", "blob_container": "bad--container"},
        ):
            with self.subTest(options=options), patch.object(self.tool, "az_json") as azure:
                with self.assertRaises(self.tool.PitrError):
                    self.tool.take_pitr_backup(
                        SUBSCRIPTION, "source-rg", "source16", self.checkpoint,
                        quiesced=True, **options,
                    )
                azure.assert_not_called()

    def test_blob_upload_failure_preserves_completed_local_reference_and_fails(self):
        with self.assertRaisesRegex(self.tool.PitrError, "upload"):
            self.take_backup(
                storage_account="migrationstore", blob_container="pitr",
                upload_error=self.tool.PitrError("Storage authorization denied"),
            )
        self.assertEqual(json.loads(self.checkpoint.read_text())["status"], "backup_ready")

    def test_blob_restore_downloads_private_reference_before_any_server_operation(self):
        record, _ = self.take_backup()
        contents = json.dumps(record)
        self.checkpoint.unlink()
        responses = iter([
            {"id": SUBSCRIPTION, "tenantId": TENANT},
            self.source, self.backup, [], self.target, self.target,
        ])

        def az(*arguments):
            if arguments[:3] == ("storage", "blob", "show"):
                return {"properties": {"contentLength": len(contents), "etag": '"test-etag"'}}
            if arguments[:3] == ("storage", "blob", "download"):
                Path(arguments[arguments.index("--file") + 1]).write_text(contents)
                return {}
            return next(responses)

        url = "https://migrationstore.blob.core.windows.net/pitr/20261117124525/checkpoint.json"
        with patch.object(self.tool, "az_json", side_effect=az) as azure:
            result = self.tool.restore_pitr_backup(
                self.checkpoint, "recovery-rg", "recovery16", self.report,
                confirm_target="recovery16", checkpoint_url=url,
            )
        self.assertEqual(result["status"], "server_ready")
        self.assertEqual(self.checkpoint.stat().st_mode & 0o777, 0o600)
        download = azure.call_args_list[1].args
        self.assertEqual(download[:3], ("storage", "blob", "download"))
        self.assertEqual(download[download.index("--auth-mode") + 1], "login")
        self.assertEqual(download[download.index("--blob-url") + 1], url)
        self.assertEqual(download[download.index("--if-match") + 1], '"test-etag"')
        self.assertEqual(azure.call_args_list[2].args[:2], ("account", "show"))

    def test_blob_restore_rejects_unsafe_urls_and_existing_destination(self):
        for url in (
            "http://migrationstore.blob.core.windows.net/pitr/reference.json",
            "https://migrationstore.blob.core.windows.net/pitr/reference.json?sig=not-allowed",
            "https://example.com/pitr/reference.json",
            "https://migrationstore.blob.core.windows.net/pitr/",
            "https://user@migrationstore.blob.core.windows.net/pitr/reference.json",
        ):
            with self.subTest(url=url), patch.object(self.tool, "az_json") as azure:
                with self.assertRaises(self.tool.PitrError):
                    self.tool.restore_pitr_backup(
                        self.checkpoint, "recovery-rg", "recovery16", self.report,
                        confirm_target="recovery16", checkpoint_url=url,
                    )
                azure.assert_not_called()
        self.take_backup()
        with patch.object(self.tool, "az_json") as azure:
            with self.assertRaisesRegex(self.tool.PitrError, "already exists"):
                self.tool.restore_pitr_backup(
                    self.checkpoint, "recovery-rg", "recovery16", self.report,
                    confirm_target="recovery16",
                    checkpoint_url="https://migrationstore.blob.core.windows.net/pitr/reference.json",
                )
            azure.assert_not_called()

    def test_failed_oversized_or_invalid_blob_never_creates_server_or_checkpoint(self):
        url = "https://migrationstore.blob.core.windows.net/pitr/reference.json"
        for size in (0, 1024 * 1024 + 1):
            with self.subTest(size=size), patch.object(self.tool, "az_json", return_value={
                "properties": {"contentLength": size, "etag": '"test-etag"'},
            }) as azure:
                with self.assertRaisesRegex(self.tool.PitrError, "size"):
                    self.tool.restore_pitr_backup(
                        self.checkpoint, "recovery-rg", "recovery16", self.report,
                        confirm_target="recovery16", checkpoint_url=url,
                    )
                self.assertEqual(azure.call_count, 1)

        def az(*arguments):
            if arguments[:3] == ("storage", "blob", "show"):
                return {"properties": {"contentLength": 8, "etag": '"test-etag"'}}
            Path(arguments[arguments.index("--file") + 1]).write_text("not json")
            return {}

        with patch.object(self.tool, "az_json", side_effect=az) as azure:
            with self.assertRaises(ValueError):
                self.tool.restore_pitr_backup(
                    self.checkpoint, "recovery-rg", "recovery16", self.report,
                    confirm_target="recovery16", checkpoint_url=url,
                )
            self.assertEqual(azure.call_count, 2)
        with patch.object(self.tool, "az_json", side_effect=[
            {"properties": {"contentLength": 8, "etag": '"test-etag"'}},
            self.tool.PitrError("Download denied"),
        ]):
            with self.assertRaisesRegex(self.tool.PitrError, "denied"):
                self.tool.restore_pitr_backup(
                    self.checkpoint, "recovery-rg", "recovery16", self.report,
                    confirm_target="recovery16", checkpoint_url=url,
                )
        self.assertFalse(self.checkpoint.exists())
        self.assertFalse(self.report.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_storage_key_environment_is_not_inherited_by_azure_cli(self):
        with patch.dict(os.environ, {
            "AZURE_STORAGE_KEY": "synthetic-test-key",
            "AZURE_STORAGE_SAS_TOKEN": "synthetic-test-sas",
            "AZURE_STORAGE_CONNECTION_STRING": "synthetic-test-connection",
        }), patch.object(self.tool.shutil, "which", return_value="/usr/bin/az"), patch.object(
            self.tool.subprocess, "run",
        ) as run:
            run.return_value.returncode = 0
            run.return_value.stdout = "{}"
            run.return_value.stderr = ""
            self.tool.az_json("storage", "blob", "show", "--auth-mode", "login")
        environment = run.call_args.kwargs["env"]
        for name in ("AZURE_STORAGE_KEY", "AZURE_STORAGE_SAS_TOKEN", "AZURE_STORAGE_CONNECTION_STRING"):
            self.assertNotIn(name, environment)


if __name__ == "__main__":
    unittest.main()
