"""Launch the pinned Microsoft Clarity MCP without putting its token in argv."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
from pathlib import Path

from .catalog import CatalogError

PACKAGE_VERSION = "2.0.1"
PROFILE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")


def credential_path(profile: str) -> Path:
    if not PROFILE.fullmatch(profile):
        raise CatalogError("invalid Clarity profile")
    filename = "clarity.env" if profile == "transhub" else f"clarity-{profile}.env"
    return Path.home() / ".config" / "cloud-ops" / "env" / filename


def _read_token(path: Path) -> str:
    parent = path.parent
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) != 0o600
            or parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid()
            or stat.S_IMODE(parent.stat().st_mode) != 0o700 or path.stat().st_size > 8192):
        raise CatalogError("Clarity credential file must be private (0600) in a private directory (0700)")
    try:
        lines = [line.strip() for line in path.read_text(encoding="ascii").splitlines() if line.strip()]
    except (OSError, UnicodeError) as exc:
        raise CatalogError("Clarity credential file cannot be read") from exc
    if len(lines) != 1 or not lines[0].startswith("CLARITY_API_TOKEN="):
        raise CatalogError("Clarity credential file has an invalid format")
    token = lines[0].split("=", 1)[1]
    if not token or len(token) > 4096 or any(ord(char) < 33 or ord(char) > 126 for char in token):
        raise CatalogError("Clarity token has an invalid format")
    return token


def launch(credential: Path, install_root: Path) -> None:
    token = _read_token(credential)
    package = install_root / "node_modules" / "@microsoft" / "clarity-mcp-server"
    manifest = package / "package.json"
    entry = package / "dist" / "cli.js"
    try:
        version = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogError("Clarity MCP package is unavailable") from exc
    if version != PACKAGE_VERSION or not entry.is_file() or entry.is_symlink():
        raise CatalogError("Clarity MCP package version or entrypoint is invalid")
    node = shutil.which("node")
    if node is None:
        raise CatalogError("Node.js is unavailable")
    env = {name: os.environ[name] for name in ("HOME", "PATH", "TMPDIR", "LANG", "LC_ALL") if name in os.environ}
    env["CLARITY_API_TOKEN"] = token
    os.execve(node, [node, str(entry)], env)


def main() -> None:
    parser = argparse.ArgumentParser(prog="stackorion-clarity-mcp")
    parser.add_argument("--profile", default="transhub")
    args = parser.parse_args()
    credential = credential_path(args.profile)
    default_root = Path.home() / ".local" / "share" / "stackorion" / "clarity-mcp"
    install_root = Path(os.environ.get("STACKORION_CLARITY_ROOT", str(default_root)))
    try:
        launch(credential, install_root)
    except (CatalogError, OSError) as exc:
        raise SystemExit(f"Clarity MCP unavailable: {exc}") from exc


if __name__ == "__main__":
    main()
