import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
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

    def take_backup(self):
        with patch.object(self.tool, "az_json", side_effect=[
            {"id": SUBSCRIPTION, "tenantId": TENANT},
            self.source, [], self.backup, self.backup, self.source,
        ]) as azure:
            result = self.tool.take_pitr_backup(
                SUBSCRIPTION, "source-rg", "source16", self.checkpoint,
                backup_name="preupgrade", quiesced=True,
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


if __name__ == "__main__":
    unittest.main()
