import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "toolbox-entrypoint.sh"


class ContainerWorkflowTests(unittest.TestCase):
    def test_make_selects_both_client_versions_without_host_paths(self):
        for operation, major in (("backup", "16"), ("restore", "18"), ("validate", "18")):
            with self.subTest(operation=operation):
                result = subprocess.run(
                    ["make", "--no-print-directory", "-n", operation,
                     f"PG_MAJOR={major}", "SCHEMA_BIN_DIR=/usr/lib/postgresql/18/bin"],
                    cwd=ROOT, capture_output=True, text=True, check=True,
                )
                self.assertIn(f"--bin-dir /usr/lib/postgresql/{major}/bin", result.stdout)
                self.assertIn("--schema-bin-dir /usr/lib/postgresql/18/bin", result.stdout)

    def test_entrypoint_copies_projected_credentials_with_private_permissions(self):
        self.assertTrue(ENTRYPOINT.exists(), "Container entrypoint is missing")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "projected-secret"
            source.write_text("test-only-synthetic-credential\n")
            source.chmod(0o440)
            destination = root / "private-pgpass"
            scratch = root / "data" / "tmp"
            env = {
                **os.environ, "PGPASS_SOURCE": str(source),
                "PGPASSFILE": str(destination), "TMPDIR": str(scratch),
            }
            subprocess.run(
                ["bash", str(ENTRYPOINT), "sh", "-c",
                 'test -d "$TMPDIR" && test -r "$PGPASSFILE"'],
                env=env, check=True, capture_output=True,
            )
            self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(scratch.stat().st_mode), 0o700)

    def test_missing_requested_credentials_fail_instead_of_starting_idle(self):
        self.assertTrue(ENTRYPOINT.exists(), "Container entrypoint is missing")
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                ["bash", str(ENTRYPOINT), "true"],
                env={**os.environ, "PGPASS_SOURCE": directory + "/missing",
                     "PGPASSFILE": directory + "/pgpass", "TMPDIR": directory + "/tmp"},
                capture_output=True, text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("credential", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
