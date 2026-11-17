#!/usr/bin/env python3
"""Azure-managed on-demand backup and new-server point-in-time restore."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote, urlsplit
from uuid import UUID, uuid4

from server_backup import BackupError, private_files, write_json


STORAGE_SUFFIXES = ("core.windows.net", "core.usgovcloudapi.net", "core.chinacloudapi.cn")
MAX_CHECKPOINT_BYTES = 1024 * 1024


class PitrError(BackupError):
    pass


def az_json(*arguments: str) -> dict | list:
    if not shutil.which("az"):
        raise PitrError("Azure CLI is missing. Use the rebuilt toolbox image or install Azure CLI")
    result = subprocess.run(
        ["az", *arguments, "--output", "json", "--only-show-errors"],
        capture_output=True, text=True, check=False,
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("AZURE_STORAGE_")},
            "AZURE_CORE_COLLECT_TELEMETRY": "no",
        },
    )
    if result.returncode:
        raise PitrError(f"Azure CLI failed ({result.returncode}):\n{result.stderr.strip()}")
    if result.stderr.strip():
        print(result.stderr, file=sys.stderr, end="")
    data = json.loads(result.stdout)
    if not isinstance(data, (dict, list)):
        raise PitrError("Azure CLI returned an unexpected JSON response")
    return data


def utc_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise PitrError("Recovery timestamps must include an explicit UTC offset")
    return result.astimezone(timezone.utc)


def server_id(subscription: str, resource_group: str, name: str) -> str:
    UUID(subscription)
    if not resource_group or "/" in resource_group or resource_group.startswith("-"):
        raise PitrError("Invalid resource group")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", name):
        raise PitrError("Server name must be 3-63 lowercase letters, digits or hyphens")
    return (
        f"/subscriptions/{subscription}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.DBforPostgreSQL/flexibleServers/{name}"
    )


def same_id(left: str, right: str) -> bool:
    return left.casefold() == right.casefold()


def check_source(source: dict, expected_id: str) -> None:
    if not same_id(source["id"], expected_id):
        raise PitrError("Azure returned a different source server resource ID")
    if (source.get("dataEncryption") or {}).get("type") == "AzureKeyVault":
        raise PitrError(
            "Customer-managed encryption keys require a reviewed identity/key restore plan; "
            "use the Azure portal rather than this script"
        )
    if source.get("cluster") and source["cluster"].get("clusterSize", 1) != 1:
        raise PitrError("Elastic clusters require a separate recovery workflow")


def check_window(source: dict, restore_time: str) -> None:
    point = utc_time(restore_time)
    earliest = source["backup"].get("earliestRestoreDate")
    if not earliest:
        raise PitrError("Azure did not report an earliest restore point; recovery is not verified")
    if point < utc_time(earliest):
        raise PitrError("Saved recovery point is outside the current backup retention window")
    if point > datetime.now(timezone.utc):
        raise PitrError("Backup completed time is in the future; check local clock and Azure response")


def completed_backup(backup: dict, expected_id: str) -> str:
    if not same_id(backup["id"], expected_id):
        raise PitrError("Azure returned a different backup resource ID")
    completed = backup.get("completedTime")
    if not completed:
        raise PitrError(
            "Azure has not reported a completed backup. Do NOT upgrade; "
            "inspect the on-demand backup in the portal"
        )
    utc_time(completed)
    return completed


def replace_record(path: Path, record: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".pitr-", suffix=".json", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def check_blob_destination(account: str, container: str) -> None:
    if not re.fullmatch(r"[a-z0-9]{3,24}", account):
        raise PitrError("Storage account must contain 3-24 lowercase letters or digits")
    if not re.fullmatch(r"(?=.{3,63}$)[a-z0-9]+(?:-[a-z0-9]+)*", container):
        raise PitrError("Invalid Blob container name")


def download_checkpoint(url: str, checkpoint: Path) -> None:
    if checkpoint.exists() or checkpoint.is_symlink():
        raise PitrError("Checkpoint download destination already exists; choose a new path")
    parsed = urlsplit(url)
    suffixes = "|".join(re.escape(suffix) for suffix in STORAGE_SUFFIXES)
    host = re.fullmatch(rf"([a-z0-9]{{3,24}})\.blob\.({suffixes})", parsed.netloc)
    parts = parsed.path.lstrip("/").split("/", 1)
    if (
        parsed.scheme != "https" or parsed.query or parsed.fragment or not host
        or len(parts) != 2 or not parts[1] or parts[1].endswith("/")
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise PitrError("Use an HTTPS Azure Blob file URL without SAS, query, credentials or fragment")
    check_blob_destination(host.group(1), parts[0])
    scope = ("--blob-url", url, "--auth-mode", "login")
    blob = az_json("storage", "blob", "show", *scope)
    size = blob["properties"]["contentLength"]
    etag = blob["properties"]["etag"]
    if not isinstance(size, int) or not 0 < size <= MAX_CHECKPOINT_BYTES:
        raise PitrError("Reference Blob size must be between 1 byte and 1 MiB")
    if not isinstance(etag, str) or not etag:
        raise PitrError("Azure did not return a reference Blob ETag")
    descriptor, temporary = tempfile.mkstemp(prefix=".pitr-download-", suffix=".json", dir=checkpoint.parent)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        az_json(
            "storage", "blob", "download", *scope, "--file", str(temporary_path),
            "--if-match", etag, "--overwrite", "true", "--validate-content", "--no-progress",
        )
        if temporary_path.stat().st_size != size:
            raise PitrError("Downloaded reference size differs from the Azure Blob metadata")
        record = json.loads(temporary_path.read_text())
        if not isinstance(record, dict) or record.get("format") != 1 or record.get("status") != "backup_ready":
            raise PitrError("Downloaded reference is incomplete or unsupported")
        write_json(checkpoint, record)
    finally:
        temporary_path.unlink(missing_ok=True)
    print(f"Downloaded PITR recovery reference to {checkpoint}", file=sys.stderr)


def take_pitr_backup(
    subscription: str, resource_group: str, source_server: str, checkpoint: Path,
    *, backup_name: str | None = None, quiesced: bool = False,
    storage_account: str | None = None, blob_container: str | None = None,
) -> dict:
    """Create an Azure backup and save a verified recovery reference, not backup data."""
    if not quiesced:
        raise PitrError("quiesced=True/--quiesced is required: stop all writers, jobs and DDL first")
    if storage_account is not None or blob_container is not None:
        if not storage_account or not blob_container:
            raise PitrError("--storage-account and --blob-container must be supplied together")
        check_blob_destination(storage_account, blob_container)
    source_id = server_id(subscription, resource_group, source_server)
    if checkpoint.exists() or checkpoint.is_symlink():
        raise PitrError("Checkpoint file already exists; choose a new path")
    backup_name = backup_name or (
        datetime.now(timezone.utc).strftime("preupgrade-%Y%m%dT%H%M%SZ-") + uuid4().hex[:8]
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", backup_name):
        raise PitrError("Invalid backup name")
    account = az_json("account", "show", "--subscription", subscription)
    if not same_id(account["id"], subscription):
        raise PitrError("Azure subscription does not match the explicit subscription ID")
    source = az_json("postgres", "flexible-server", "show", "--ids", source_id)
    check_source(source, source_id)
    if source["state"] != "Ready":
        raise PitrError("Source must be Ready before taking the pre-upgrade backup")
    if source["version"] not in ("16", "18"):
        raise PitrError("This migration workflow supports PostgreSQL 16 and 18")
    scope = (
        "--subscription", subscription, "--resource-group", resource_group,
        "--server-name", source_server,
    )
    backups = az_json("postgres", "flexible-server", "backup", "list", *scope)
    if any(backup["name"].casefold() == backup_name.casefold() for backup in backups):
        raise PitrError("Backup name already exists; choose a new name")
    record = {
        "format": 1, "status": "backup_requested",
        "tenant_id": account["tenantId"], "subscription_id": subscription,
        "source_server_id": source_id, "source_server": source_server,
        "source_resource_group": resource_group, "source_version": source["version"],
        "source_location": source["location"], "source_network": source["network"],
        "source_created_at": (source.get("systemData") or {}).get("createdAt"),
        "backup_name": backup_name, "backup_id": source_id + "/backups/" + backup_name,
        "requested_utc": datetime.now(timezone.utc).isoformat(),
        "retention_days_at_backup": source["backup"]["backupRetentionDays"],
    }
    write_json(checkpoint, record)
    print(f"Creating Azure-managed backup {backup_name!r}; keep all writers stopped", file=sys.stderr)
    az_json("postgres", "flexible-server", "backup", "create", *scope, "--name", backup_name)
    backup = az_json("postgres", "flexible-server", "backup", "show", *scope, "--name", backup_name)
    point = completed_backup(backup, record["backup_id"])
    after = az_json("postgres", "flexible-server", "show", "--ids", source_id)
    check_source(after, source_id)
    if after["version"] != source["version"] or after["state"] != "Ready":
        raise PitrError("Source changed version/state during backup. Do NOT proceed with the upgrade")
    if after["network"] != source["network"]:
        raise PitrError("Source networking changed during backup; review before upgrading")
    check_window(after, point)
    record.update(status="backup_ready", restore_time=point)
    replace_record(checkpoint, record)
    if storage_account and blob_container:
        try:
            cloud = az_json("cloud", "show")
            suffix = cloud["suffixes"]["storageEndpoint"]
            if suffix not in STORAGE_SUFFIXES:
                raise PitrError("This Azure cloud's Blob endpoint is not supported")
            name = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "/" + checkpoint.name
            url = f"https://{storage_account}.blob.{suffix}/{blob_container}/{quote(name, safe='/')}"
            record["checkpoint_blob_url"] = url
            replace_record(checkpoint, record)
            az_json(
                "storage", "blob", "upload", "--subscription", subscription,
                "--account-name", storage_account, "--container-name", blob_container,
                "--name", name, "--file", str(checkpoint), "--auth-mode", "login",
                "--overwrite", "false", "--content-type", "application/json",
                "--validate-content", "--no-progress",
            )
        except PitrError as error:
            raise PitrError(
                f"Reference upload failed; Azure backup and local reference remain at {checkpoint}. "
                f"Verify an independent copy before upgrading. {error}"
            ) from error
        print(f"PITR reference JSON uploaded (not database backup data): {url}", file=sys.stderr)
    print(
        f"Azure backup completed. Saved recovery reference: {checkpoint}\n"
        f"Restore timestamp: {point}; PostgreSQL {source['version']}.\n"
        "The JSON is NOT a portable backup and does not extend Azure retention.",
        file=sys.stderr,
    )
    return record


def restore_pitr_backup(
    checkpoint: Path, target_resource_group: str, target_server: str, report: Path,
    *, confirm_target: str, checkpoint_url: str | None = None,
) -> dict:
    """Ask Azure to create a new server at the saved pre-upgrade recovery point."""
    if confirm_target != target_server:
        raise PitrError("--confirm-target must exactly match the new target server name")
    if report.exists() or report.is_symlink():
        raise PitrError("Report already exists; choose a new path")
    if checkpoint_url is not None:
        download_checkpoint(checkpoint_url, checkpoint)
    record = json.loads(checkpoint.read_text())
    if record["format"] != 1 or record["status"] != "backup_ready":
        raise PitrError("Checkpoint is incomplete or unsupported; do not restore from it")
    subscription = record["subscription_id"]
    source_id = server_id(subscription, record["source_resource_group"], record["source_server"])
    if not same_id(source_id, record["source_server_id"]):
        raise PitrError("Checkpoint source resource ID does not match its subscription/server fields")
    target_id = server_id(subscription, target_resource_group, target_server)
    if target_server == record["source_server"]:
        raise PitrError("Target must have a different name from the source server")
    expected_backup_id = source_id + "/backups/" + record["backup_name"]
    if not same_id(expected_backup_id, record["backup_id"]):
        raise PitrError("Checkpoint backup resource ID does not match its source server")
    account = az_json("account", "show", "--subscription", subscription)
    if not same_id(account["id"], subscription) or not same_id(account["tenantId"], record["tenant_id"]):
        raise PitrError("Azure subscription/tenant differs from the saved checkpoint")
    source = az_json("postgres", "flexible-server", "show", "--ids", source_id)
    check_source(source, source_id)
    check_window(source, record["restore_time"])
    created = (source.get("systemData") or {}).get("createdAt")
    if created != record["source_created_at"]:
        raise PitrError("Source creation metadata changed; it may have been deleted and recreated")
    if source["network"] != record["source_network"] or source["location"] != record["source_location"]:
        raise PitrError("Source network/location changed; restore needs a reviewed portal workflow")
    backup = az_json(
        "postgres", "flexible-server", "backup", "show",
        "--subscription", subscription, "--resource-group", record["source_resource_group"],
        "--server-name", record["source_server"], "--name", record["backup_name"],
    )
    if utc_time(completed_backup(backup, expected_backup_id)) != utc_time(record["restore_time"]):
        raise PitrError("Saved backup has changed; refusing to restore a different recovery point")
    targets = az_json(
        "postgres", "flexible-server", "list", "--subscription", subscription,
        "--resource-group", target_resource_group,
    )
    if any(target["name"].casefold() == target_server.casefold() for target in targets):
        raise PitrError("Target server already exists; this script never overwrites a server")
    result = {
        "format": 1, "status": "restore_requested",
        "source_server_id": source_id, "target_server_id": target_id,
        "restore_time": record["restore_time"], "expected_version": record["source_version"],
        "requested_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(report, result)
    print(f"Azure is creating and restoring NEW server {target_server!r}", file=sys.stderr)
    az_json(
        "postgres", "flexible-server", "restore",
        "--subscription", subscription, "--resource-group", target_resource_group,
        "--name", target_server, "--source-server", source_id,
        "--restore-time", record["restore_time"], "--yes",
    )
    target = az_json("postgres", "flexible-server", "show", "--ids", target_id)
    if not same_id(target["id"], target_id) or target["state"] != "Ready":
        raise PitrError("Restored server is not Ready at the expected resource ID; inspect Azure")
    if target["version"] != record["source_version"]:
        raise PitrError(
            f"Restored version {target['version']} != backed-up version {record['source_version']}; "
            "do NOT cut over"
        )
    result.update(
        status="server_ready", restored_version=target["version"],
        target_fqdn=target["fullyQualifiedDomainName"],
        completed_utc=datetime.now(timezone.utc).isoformat(),
        data_integrity_validated=False,
    )
    replace_record(report, result)
    print(
        f"Azure restore completed: {result['target_fqdn']}\n"
        "Keep traffic stopped. Verify data, networking, authentication, server configuration "
        "and application smoke tests before changing connection strings.",
        file=sys.stderr,
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest="operation", required=True)
    backup = operations.add_parser("backup", help="Create and verify an Azure on-demand backup")
    backup.add_argument("--subscription", required=True, help="Explicit subscription UUID")
    backup.add_argument("--resource-group", required=True)
    backup.add_argument("--server", required=True)
    backup.add_argument("--checkpoint", type=Path, required=True, help="NEW recovery-reference JSON path")
    backup.add_argument("--backup-name", help="Unique Azure backup name; generated if omitted")
    backup.add_argument("--storage-account", help="Optional account for uploading the reference JSON only")
    backup.add_argument("--blob-container", help="Existing container; requires --storage-account")
    backup.add_argument("--quiesced", action="store_true", help="Confirm all writers, jobs and DDL are stopped")
    restore = operations.add_parser("restore", help="Create a NEW server from a saved recovery reference")
    restore.add_argument("--checkpoint", type=Path, required=True, help="Local reference, or NEW download path with --checkpoint-url")
    restore.add_argument("--checkpoint-url", "--blob-url", help="Optional HTTPS Blob reference URL; Entra login only, no SAS")
    restore.add_argument("--target-resource-group", required=True, help="Existing RG in the source subscription")
    restore.add_argument("--target-server", required=True)
    restore.add_argument("--confirm-target", required=True)
    restore.add_argument("--report", type=Path, required=True, help="NEW restore-report JSON path")
    args = parser.parse_args()
    try:
        with private_files():
            if args.operation == "backup":
                take_pitr_backup(
                    args.subscription, args.resource_group, args.server, args.checkpoint,
                    backup_name=args.backup_name, quiesced=args.quiesced,
                    storage_account=args.storage_account, blob_container=args.blob_container,
                )
            else:
                restore_pitr_backup(
                    args.checkpoint, args.target_resource_group, args.target_server, args.report,
                    confirm_target=args.confirm_target, checkpoint_url=args.checkpoint_url,
                )
    except (BackupError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        print(
            "Operation is NOT confirmed successful. Inspect Azure before retrying; an Azure "
            "backup/restore may still be running. Do not delete the source or resume traffic.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
