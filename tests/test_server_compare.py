import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "server_compare.py"


class ServerComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if MODULE.exists():
            spec = importlib.util.spec_from_file_location("server_compare", MODULE)
            cls.tool = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(cls.tool)

    def setUp(self):
        self.assertTrue(MODULE.exists(), "The two server comparison operations are missing")
        self.source = self.tool.Server("source", "migration_admin")
        self.destination = self.tool.Server("destination", "migration_admin")
        self.state = {
            "globals": {"roles": [], "memberships": []},
            "databases": {"postgres": {
                "schema_sha256": "schema",
                "relations": {'"public"."records"': {"rows": 2, "sha256": "original"}},
            }},
        }

    def capture(self, baseline):
        with patch.object(self.tool.Server, "check_clients"), patch.object(
            self.tool.Server, "major", return_value=16,
        ), patch.object(self.tool, "snapshot", return_value=self.state):
            self.tool.capture_upgrade_baseline(self.source, baseline, quiesced=True)

    def test_source_is_truth_and_same_count_data_changes_fail_with_report(self):
        with tempfile.TemporaryDirectory() as directory:
            for change in ("none", "data", "schema", "database"):
                with self.subTest(change=change):
                    actual = copy.deepcopy(self.state)
                    if change == "data":
                        actual["databases"]["postgres"]["relations"]['"public"."records"']["sha256"] = "changed"
                    elif change == "schema":
                        actual["databases"]["postgres"]["schema_sha256"] = "changed"
                    elif change == "database":
                        actual["databases"]["unexpected"] = {}
                    report = Path(directory) / (change + ".json")
                    with patch.object(self.tool.Server, "check_clients"), patch.object(
                        self.tool.Server, "major", return_value=18,
                    ), patch.object(self.tool, "snapshot", side_effect=[self.state, actual]):
                        if change == "none":
                            self.tool.compare_servers(self.source, self.destination, report, quiesced=True)
                        else:
                            with self.assertRaisesRegex(self.tool.BackupError, "Integrity validation FAILED"):
                                self.tool.compare_servers(self.source, self.destination, report, quiesced=True)
                    result = json.loads(report.read_text())
                    self.assertEqual(result["passed"], change == "none")
                    self.assertEqual(result["actual"], actual)
                    self.assertEqual(result["reference"]["expected"], self.state)
                    if change == "database":
                        self.assertIn("Unexpected: /databases/unexpected", result["differences"])

    def test_before_after_uses_immutable_integrity_only_baseline_across_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "before.json"
            self.capture(baseline)
            saved = baseline.read_bytes()
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["before.json", "before.json.sha256"])
            self.assertEqual(baseline.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / "before.json.sha256").stat().st_mode & 0o777, 0o600)
            for changed in (False, True):
                actual = copy.deepcopy(self.state)
                if changed:
                    actual["databases"]["postgres"]["relations"]['"public"."records"']["sha256"] = "changed"
                report = root / f"after-{changed}.json"
                with patch.object(self.tool.Server, "check_clients"), patch.object(
                    self.tool.Server, "major", return_value=18,
                ), patch.object(self.tool, "snapshot", return_value=actual):
                    if changed:
                        with self.assertRaisesRegex(self.tool.BackupError, "Integrity validation FAILED"):
                            self.tool.verify_upgrade(self.source, baseline, report, quiesced=True)
                    else:
                        self.tool.verify_upgrade(self.source, baseline, report, quiesced=True)
                self.assertEqual(json.loads(report.read_text())["passed"], not changed)
                self.assertEqual(baseline.read_bytes(), saved)

    def test_invalid_baseline_and_wrong_endpoint_fail_before_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "before.json"
            self.capture(baseline)
            saved = baseline.read_text()
            cases = (
                ("corrupt", self.source),
                ("endpoint", self.destination),
                ("admin", self.tool.Server("source", "other_admin")),
                ("unsupported", self.source),
                ("missing-capture", self.source),
            )
            for case, server in cases:
                baseline.write_text(saved)
                if case == "corrupt":
                    baseline.write_text(saved + " ")
                elif case in ("unsupported", "missing-capture"):
                    record = json.loads(saved)
                    if case == "unsupported":
                        record["kind"] = "not-an-integrity-baseline"
                    else:
                        del record["captured_utc"]
                    baseline.write_text(json.dumps(record))
                    (root / "before.json.sha256").write_text(self.tool.file_hash(baseline))
                with self.subTest(case=case), patch.object(self.tool.Server, "check_clients") as clients, patch.object(
                    self.tool, "snapshot",
                ) as scan:
                    with self.assertRaises(self.tool.BackupError):
                        self.tool.verify_upgrade(server, baseline, root / (case + ".json"), quiesced=True)
                    clients.assert_not_called()
                    scan.assert_not_called()
                    self.assertFalse((root / (case + ".json")).exists())

    def test_outage_and_new_output_paths_are_required_before_scanning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline, report = root / "before.json", root / "report.json"
            for operation in (
                lambda: self.tool.compare_servers(self.source, self.destination, report),
                lambda: self.tool.capture_upgrade_baseline(self.source, baseline),
                lambda: self.tool.verify_upgrade(self.source, baseline, report),
            ):
                with patch.object(self.tool.Server, "check_clients") as clients:
                    with self.assertRaisesRegex(self.tool.BackupError, "quiesced"):
                        operation()
                    clients.assert_not_called()
            self.capture(baseline)
            report.write_text("existing report")
            for operation in (
                lambda: self.tool.compare_servers(self.source, self.destination, report, quiesced=True),
                lambda: self.tool.capture_upgrade_baseline(self.source, baseline, quiesced=True),
                lambda: self.tool.verify_upgrade(self.source, baseline, report, quiesced=True),
            ):
                with patch.object(self.tool.Server, "check_clients") as clients:
                    with self.assertRaisesRegex(self.tool.BackupError, "already exists"):
                        operation()
                    clients.assert_not_called()
            self.assertEqual(report.read_text(), "existing report")
            baseline.unlink()
            with self.assertRaisesRegex(self.tool.BackupError, "already exists"):
                self.tool.capture_upgrade_baseline(self.source, baseline, quiesced=True)

    def test_comparison_rejects_self_and_different_admin_names(self):
        with tempfile.TemporaryDirectory() as directory:
            for destination in (self.source, self.tool.Server("destination", "other_admin")):
                with patch.object(self.tool.Server, "check_clients") as clients:
                    with self.assertRaises(self.tool.BackupError):
                        self.tool.compare_servers(
                            self.source, destination, Path(directory) / "report.json", quiesced=True,
                        )
                    clients.assert_not_called()

    def test_cli_rejects_missing_outage_and_missing_after_report(self):
        command = [
            sys.executable, "-B", str(MODULE), "upgrade-check",
            "--phase", "after", "--host", "source", "--user", "migration_admin",
            "--baseline", "/nonexistent/before.json",
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--quiesced", result.stderr)
        result = subprocess.run(command + ["--quiesced"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("--report", result.stderr)


if __name__ == "__main__":
    unittest.main()
