"""Entra-only Azure CLI and Blob transfers shared by both backup workflows."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import quote, urlsplit

from backup_common import BackupError


STORAGE_SUFFIXES = ("core.windows.net", "core.usgovcloudapi.net", "core.chinacloudapi.cn")


class AzureError(BackupError):
    pass


def az_json(*arguments: str) -> dict | list:
    if not shutil.which("az"):
        raise AzureError("Azure CLI is missing. Use the rebuilt toolbox image or install Azure CLI")
    result = subprocess.run(
        ["az", *arguments, "--output", "json", "--only-show-errors"],
        capture_output=True, text=True, check=False,
        env={
            **{key: value for key, value in os.environ.items() if not key.startswith("AZURE_STORAGE_")},
            "AZURE_CORE_COLLECT_TELEMETRY": "no",
        },
    )
    if result.returncode:
        raise AzureError(f"Azure CLI failed ({result.returncode}):\n{result.stderr.strip()}")
    if result.stderr.strip():
        print(result.stderr, file=sys.stderr, end="")
    data = json.loads(result.stdout)
    if not isinstance(data, (dict, list)):
        raise AzureError("Azure CLI returned an unexpected JSON response")
    return data


def check_destination(account: str, container: str) -> None:
    if not re.fullmatch(r"[a-z0-9]{3,24}", account):
        raise AzureError("Storage account must contain 3-24 lowercase letters or digits")
    if not re.fullmatch(r"(?=.{3,63}$)[a-z0-9]+(?:-[a-z0-9]+)*", container):
        raise AzureError("Invalid Blob container name")


def check_url(url: str) -> None:
    parsed = urlsplit(url)
    suffixes = "|".join(re.escape(suffix) for suffix in STORAGE_SUFFIXES)
    host = re.fullmatch(rf"([a-z0-9]{{3,24}})\.blob\.({suffixes})", parsed.netloc)
    parts = parsed.path.lstrip("/").split("/", 1)
    if (
        parsed.scheme != "https" or parsed.query or parsed.fragment or not host
        or len(parts) != 2 or not parts[1] or parts[1].endswith("/")
        or any(character.isspace() or ord(character) < 32 for character in url)
    ):
        raise AzureError("Use an HTTPS Azure Blob file URL without SAS, query, credentials or fragment")
    check_destination(host.group(1), parts[0])


def blob_url(account: str, container: str, name: str) -> str:
    check_destination(account, container)
    cloud = az_json("cloud", "show")
    if not isinstance(cloud, dict) or not isinstance(cloud.get("suffixes"), dict):
        raise AzureError("Azure did not return the cloud's storage endpoint")
    suffix = cloud["suffixes"]["storageEndpoint"]
    if suffix not in STORAGE_SUFFIXES:
        raise AzureError("This Azure cloud's Blob endpoint is not supported")
    return f"https://{account}.blob.{suffix}/{container}/{quote(name, safe='/')}"


def upload_file(
    path: Path, account: str, container: str, name: str, *,
    content_type: str, subscription: str | None = None, metadata: dict[str, str] | None = None,
) -> None:
    check_destination(account, container)
    arguments = [
        "storage", "blob", "upload", "--account-name", account, "--container-name", container,
        "--name", name, "--file", str(path), "--auth-mode", "login",
        "--overwrite", "false", "--content-type", content_type, "--validate-content", "--no-progress",
    ]
    if subscription:
        arguments.extend(("--subscription", subscription))
    if metadata:
        arguments.extend(("--metadata", *(f"{key}={value}" for key, value in metadata.items())))
    az_json(*arguments)


def download_file(url: str, path: Path, *, max_bytes: int | None = None) -> dict:
    if path.exists() or path.is_symlink():
        raise AzureError("Download destination already exists; choose a new path")
    check_url(url)
    scope = ("--blob-url", url, "--auth-mode", "login")
    blob = az_json("storage", "blob", "show", *scope)
    if not isinstance(blob, dict):
        raise AzureError("Azure did not return Blob metadata")
    properties = blob.get("properties")
    if not isinstance(properties, dict):
        raise AzureError("Azure did not return Blob properties")
    size = properties.get("contentLength")
    etag = properties.get("etag")
    if type(size) is not int or size <= 0 or (max_bytes is not None and size > max_bytes):
        raise AzureError(f"Invalid Blob size; expected 1-{max_bytes} bytes" if max_bytes else "Invalid Blob size")
    if size > shutil.disk_usage(path.parent).free:
        raise AzureError("Insufficient free space to download the Blob")
    if not isinstance(etag, str) or not etag:
        raise AzureError("Azure did not return a Blob ETag")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    complete = False
    try:
        az_json(
            "storage", "blob", "download", *scope, "--file", str(path),
            "--if-match", etag, "--overwrite", "true", "--validate-content", "--no-progress",
        )
        if path.stat().st_size != size:
            raise AzureError("Downloaded file size differs from the Azure Blob metadata")
        complete = True
        return blob
    finally:
        if not complete:
            path.unlink(missing_ok=True)
