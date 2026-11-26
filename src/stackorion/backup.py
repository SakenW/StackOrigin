"""Export and restore a private catalog snapshot without connector credentials."""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from .catalog import Catalog, CatalogError, configured_path
from .dashboard_storage import BackupError, DashboardStorage
from .work_items import WorkItems


def _portable_catalog(source: Path) -> dict:
    before = source.stat()
    Catalog.load(source)
    data = json.loads(source.read_text(encoding="utf-8"))
    after = source.stat()
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise CatalogError("catalog changed during export; retry")
    for service in data["services"]:
        service["enabled"] = False
        service["settings"] = {}
    for account in data.get("accounts", []):
        account["verification"] = "declared"
    for resource in data.get("resources", []):
        resource["verification"] = "declared"
    return data


def _write_new_catalog(target: Path, data: dict) -> None:
    if not target.is_absolute() or target.parent.is_symlink() or not target.parent.is_dir():
        raise CatalogError("output must be an absolute path in an existing directory")
    if target.parent.stat().st_uid != os.getuid() or target.parent.stat().st_mode & 0o002:
        raise CatalogError("output directory must be owned by this user and not world-writable")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags, 0o600)
    except FileExistsError as exc:
        raise CatalogError("output already exists; no changes made") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Catalog.load(target)
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def export_catalog(source: Path, target: Path) -> None:
    """Create a portable disabled catalog; never replace the source or target."""
    _write_new_catalog(target, _portable_catalog(source))


def restore_catalog(snapshot: Path, target: Path) -> None:
    """Restore inventory to a new file with all connections and verification disabled."""
    if (snapshot.is_symlink() or not snapshot.is_file() or snapshot.stat().st_uid != os.getuid()
            or stat.S_IMODE(snapshot.stat().st_mode) != 0o600):
        raise CatalogError("snapshot must be a private regular file (0600)")
    _write_new_catalog(target, _portable_catalog(snapshot))


def main() -> None:
    parser = argparse.ArgumentParser(prog="stackorion-backup")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("export", help="create a private catalog snapshot").add_argument("output", type=Path)
    restore = commands.add_parser("restore", help="restore into a new disabled catalog file")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("output", type=Path)
    drill = commands.add_parser("verify-dashboard", help="rehearse recovery of a recorded age dashboard backup in memory")
    drill.add_argument("snapshot", type=Path)
    drill.add_argument("--identity", type=Path, required=True)
    drill.add_argument("--state-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "export":
            export_catalog(configured_path(), args.output)
        elif args.command == "restore":
            restore_catalog(args.snapshot, args.output)
        else:
            live = args.state_dir / "live"
            if not (live / "work-items.db").is_file():
                raise BackupError("dashboard_state_not_found")
            storage = DashboardStorage(WorkItems(live), None, None, None)
            print(json.dumps(storage.verify_restore(args.snapshot, args.identity)))
            return
    except (CatalogError, BackupError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"Created private disabled catalog: {args.output}")


if __name__ == "__main__":
    main()
