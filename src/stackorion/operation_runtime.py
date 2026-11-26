"""Private local state and locks shared by operation plugins."""

from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat

from .catalog import CatalogError

OPERATION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")


def state_root(*, create=True):
    if create and not os.environ.get("STACKORION_OPERATION_ROOT"):
        raise CatalogError("STACKORION_OPERATION_ROOT must be configured before executing operations")
    root = Path(os.environ.get("STACKORION_OPERATION_ROOT", str(Path.home() / ".config/stackorion/operations")))
    if not root.is_absolute() or any(p.is_symlink() for p in [root, *root.parents]):
        raise CatalogError("operation state root must be absolute without symlinks")
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not root.is_dir() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise CatalogError("operation state root must be available and private (0700)")
    return root


def load_journal(source):
    source = Path(source)
    if source.is_symlink() or not source.is_file() or source.stat().st_uid != os.getuid():
        raise CatalogError("Foundation journal is unavailable")
    spec = importlib.util.spec_from_file_location("stackorion_foundation_journal", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def resource_lock(root, resource):
    directory = root / "resource-locks"
    directory.mkdir(exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise CatalogError("unsafe operation lock directory")
    path = directory / (hashlib.sha256(resource.encode()).hexdigest() + ".lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CatalogError("unsafe operation lock file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CatalogError("another operation is running for this resource") from exc
        yield
    finally:
        os.close(fd)


def read_receipt(operation_id, allowed_operations):
    if not isinstance(operation_id, str) or not OPERATION_ID.fullmatch(operation_id):
        raise CatalogError("invalid operation id")
    root = state_root(create=False)
    before = root.lstat()
    descriptors = []
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        directory_flags = flags | os.O_DIRECTORY
        root_fd = os.open(root, directory_flags)
        descriptors.append(root_fd)
        opened = os.fstat(root_fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise CatalogError("operation state root changed")
        job_fd = os.open(operation_id, directory_flags, dir_fd=root_fd)
        descriptors.append(job_fd)
        info = os.fstat(job_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CatalogError("operation receipt directory is not private")
        fd = os.open("operation.json", flags, dir_fd=job_fd)
        descriptors.append(fd)
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                or info.st_nlink != 1 or info.st_size > 2097152):
            raise CatalogError("operation receipt is unavailable or not private")
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as handle:
            data = json.loads(handle.read(2097153))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CatalogError("operation receipt could not be read safely") from exc
    finally:
        for fd in reversed(descriptors):
            os.close(fd)
    if not isinstance(data, dict) or not isinstance(data.get("identity"), dict):
        raise CatalogError("operation receipt has invalid structure")
    if data["identity"].get("operation") not in allowed_operations:
        raise CatalogError("operation receipt belongs to another plugin")
    return {"operation_id": operation_id, "status": data.get("status"), "identity": data.get("identity"),
            "stages": data.get("stages"), "updated_at": data.get("updated_at")}
