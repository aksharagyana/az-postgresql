"""Shared errors and private file handling for both recovery workflows."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
from typing import Generator


class BackupError(RuntimeError):
    pass


def write_json(path: Path, value: object) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(value, indent=2, ensure_ascii=True) + "\n")


@contextmanager
def private_files() -> Generator[None, None, None]:
    mask = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(mask)
