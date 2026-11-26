"""Locations for optional provider tools and their local state."""

import os
from pathlib import Path

from .catalog import CatalogError
from .credentials_setup import _private_directory


def runtime_root() -> Path:
    location = os.environ.get("STACKORION_RUNTIME_ROOT")
    root = Path(location).expanduser() if location else Path.home() / ".local/share/stackorion"
    if not root.is_absolute() or ".." in root.parts:
        raise CatalogError("STACKORION_RUNTIME_ROOT must be an absolute path")
    return root


def private_runtime_directory(path: Path) -> Path:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise CatalogError("Provider runtime directory must not use symlinks")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        _private_directory(path)
    except (OSError, ValueError):
        raise CatalogError("Provider runtime directory must be owned by this user with mode 0700") from None
    return path
