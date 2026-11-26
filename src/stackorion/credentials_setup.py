"""Save a Spaceship read-only API credential from an interactive terminal."""

from __future__ import annotations

import getpass
import os
import stat
from pathlib import Path


def _private_directory(path: Path) -> None:
    if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
        raise ValueError("credential directory is missing or not owned by this user")
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise ValueError("credential directory must have mode 0700")


def _validate_value(value: str) -> None:
    if not value or len(value) > 512 or not value.isascii():
        raise ValueError("credential value is invalid")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("credential value contains whitespace or control characters")
    if value.startswith(('"', "'")) or value.endswith(('"', "'")):
        raise ValueError("credential value must not include surrounding quotes")


def save_spaceship_credential(directory: Path, key: str, secret: str) -> Path:
    """Create exactly one private file; never overwrite an existing credential."""
    _private_directory(directory)
    _private_directory(directory.parent)
    _validate_value(key)
    _validate_value(secret)
    path = directory / "spaceship.env"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise ValueError("Spaceship credential file already exists; no changes made") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(f"SPACESHIP_API_KEY={key}\nSPACESHIP_API_SECRET={secret}\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def main() -> None:
    directory = Path.home() / ".config" / "cloud-ops" / "env"
    _private_directory(directory)
    _private_directory(directory.parent)
    if (directory / "spaceship.env").exists():
        raise SystemExit("Spaceship credential file already exists; no changes made")
    print("Paste the Spaceship API key and secret here. Input is hidden; do not paste them into chat.")
    key = getpass.getpass("Spaceship API key: ")
    secret = getpass.getpass("Spaceship API secret: ")
    try:
        path = save_spaceship_credential(directory, key, secret)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Saved private credential file at {path} (0600).")


if __name__ == "__main__":
    main()
