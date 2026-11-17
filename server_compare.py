#!/usr/bin/env python3
"""Full-server source/destination and integrity-only before/after checks."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

from server_backup import (
    BackupError, Server, file_hash, private_files, snapshot,
    write_comparison_report, write_json,
)


def require_quiesced(quiesced: bool) -> None:
    if not quiesced:
        raise BackupError("--quiesced is mandatory; stop ALL writers, background jobs and DDL")


def require_new_path(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise BackupError(f"Output path already exists; choose a new path: {path}")
    if not path.parent.is_dir():
        raise BackupError(f"Output parent directory must already exist: {path.parent}")


def server_details(server: Server) -> dict:
    return {"host": server.host, "port": server.port, "user": server.user, "major": server.major()}


def compare_servers(
    source: Server, destination: Server, report: Path, *, quiesced: bool = False,
) -> None:
    require_quiesced(quiesced)
    require_new_path(report)
    if (source.host, source.port) == (destination.host, destination.port):
        raise BackupError("Source and destination endpoints must differ; do not compare a server to itself")
    if source.user != destination.user:
        raise BackupError("Use the same administrator role name on both servers; ownership is not remapped")
    source.check_clients("validate")
    destination.check_clients("validate")
    reference = {
        "kind": "live-source", "source": server_details(source),
        "destination": server_details(destination),
    }
    expected = snapshot(source)
    actual = snapshot(destination)
    write_comparison_report(
        expected, actual, report, target=destination.host,
        reference={**reference, "expected": expected},
    )


def capture_upgrade_baseline(server: Server, baseline: Path, *, quiesced: bool = False) -> None:
    require_quiesced(quiesced)
    checksum = Path(str(baseline) + ".sha256")
    require_new_path(baseline)
    require_new_path(checksum)
    server.check_clients("validate")
    record = {
        "format": 1, "kind": "postgresql-integrity-baseline",
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "source": server_details(server), "schema_client_major": 18,
        "baseline": snapshot(server),
    }
    with private_files():
        write_json(baseline, record)
        with checksum.open("x") as stream:
            stream.write(file_hash(baseline) + "\n")
    print(f"Integrity-only baseline saved: {baseline}; keep its .sha256 file", file=sys.stderr)
    print("This is NOT a recoverable data backup. Keep writers stopped through upgrade and verification.", file=sys.stderr)


def verify_upgrade(
    server: Server, baseline: Path, report: Path, *, quiesced: bool = False,
) -> None:
    require_quiesced(quiesced)
    require_new_path(report)
    checksum = Path(str(baseline) + ".sha256")
    if baseline.is_symlink() or checksum.is_symlink():
        raise BackupError("Integrity baseline/checksum must be regular files, not symbolic links")
    if file_hash(baseline) != checksum.read_text().strip():
        raise BackupError("Integrity baseline checksum mismatch; do not use this baseline")
    record = json.loads(baseline.read_text())
    if (
        not isinstance(record, dict) or record.get("format") != 1
        or record.get("kind") != "postgresql-integrity-baseline"
        or record.get("schema_client_major") != 18
        or not isinstance(record.get("captured_utc"), str) or not record["captured_utc"]
        or not isinstance(record.get("source"), dict)
        or not isinstance(record.get("baseline"), dict)
        or set(record["baseline"]) != {"globals", "databases"}
        or not isinstance(record["baseline"]["globals"], dict)
        or not isinstance(record["baseline"]["databases"], dict)
        or record["source"].get("major") not in (16, 18)
    ):
        raise BackupError("Unsupported or incomplete integrity baseline")
    source = record["source"]
    if (server.host, server.port, server.user) != (source.get("host"), source.get("port"), source.get("user")):
        raise BackupError("After-upgrade host, port and administrator must match the saved before-upgrade endpoint")
    server.check_clients("validate")
    reference = {
        "kind": "pre-upgrade", "baseline_file": str(baseline),
        "source": source, "destination": server_details(server),
        "captured_utc": record["captured_utc"], "expected": record["baseline"],
    }
    write_comparison_report(
        record["baseline"], snapshot(server), report, target=server.host, reference=reference,
    )


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiesced", action="store_true", help="Confirm ALL writers/jobs/DDL are stopped")
    common.add_argument("--bin-dir", type=Path, help="PostgreSQL 16+ psql directory")
    common.add_argument("--schema-bin-dir", type=Path, help="PostgreSQL 18 pg_dump directory on every run")
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    compare = operations.add_parser("compare-servers", parents=[common], help="Compare destination against the live source")
    compare.add_argument("--source-host", required=True)
    compare.add_argument("--source-port", type=int, default=5432)
    compare.add_argument("--source-user", required=True)
    compare.add_argument("--destination-host", required=True)
    compare.add_argument("--destination-port", type=int, default=5432)
    compare.add_argument("--destination-user", help="Defaults to --source-user; strict checks require the same role name")
    compare.add_argument("--report", type=Path, required=True, help="New comparison report path")
    upgrade = operations.add_parser("upgrade-check", parents=[common], help="Save before-upgrade fingerprints or verify afterward")
    upgrade.add_argument("--phase", choices=("before", "after"), required=True)
    upgrade.add_argument("--host", required=True)
    upgrade.add_argument("--port", type=int, default=5432)
    upgrade.add_argument("--user", required=True)
    upgrade.add_argument("--baseline", type=Path, required=True, help="New before-upgrade JSON, or existing JSON for after")
    upgrade.add_argument("--report", type=Path, help="New report path; required only for --phase after")
    args = parser.parse_args()
    if not args.quiesced:
        parser.error("--quiesced is mandatory; stop ALL writers, background jobs and DDL")
    if args.operation == "upgrade-check":
        if args.phase == "after" and args.report is None:
            parser.error("--report is required for --phase after")
        if args.phase == "before" and args.report is not None:
            parser.error("--report is only supported for --phase after")
    try:
        with private_files():
            if args.operation == "compare-servers":
                assert args.report is not None
                source = Server(args.source_host, args.source_user, args.source_port, args.bin_dir, args.schema_bin_dir)
                destination = Server(
                    args.destination_host,
                    args.destination_user if args.destination_user is not None else args.source_user,
                    args.destination_port, args.bin_dir, args.schema_bin_dir,
                )
                compare_servers(source, destination, args.report, quiesced=args.quiesced)
            else:
                server = Server(args.host, args.user, args.port, args.bin_dir, args.schema_bin_dir)
                if args.phase == "before":
                    capture_upgrade_baseline(server, args.baseline, quiesced=args.quiesced)
                else:
                    assert args.report is not None
                    verify_upgrade(server, args.baseline, args.report, quiesced=args.quiesced)
    except (BackupError, OSError, ValueError, KeyError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        print("Integrity is NOT verified. Keep traffic stopped and investigate before cutover.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
