"""Exact-host capacity observation; no arbitrary SSH command surface."""

import json
import os
from pathlib import Path
import subprocess
import sys

from .catalog import CatalogError
from .qiniu import _regular_owned


class QiniuHostCapacityAdapter:
    def __init__(self, settings):
        if set(settings) != {"script_path", "connection_file", "targets"}:
            raise CatalogError("invalid host capacity settings")
        if not all(isinstance(settings[k], str) for k in ("script_path", "connection_file")):
            raise CatalogError("invalid host capacity paths")
        self.script, self.connection = Path(settings["script_path"]), Path(settings["connection_file"])
        if not self.script.is_absolute() or not self.connection.is_absolute():
            raise CatalogError("host capacity paths must be absolute")
        if not isinstance(settings["targets"], list):
            raise CatalogError("invalid host targets")
        self.targets = {}
        for row in settings["targets"]:
            if not isinstance(row, dict) or set(row) != {"alias", "instance_id"}:
                raise CatalogError("invalid host target")
            if any(not isinstance(v, str) or not v for v in row.values()) or row["alias"] in self.targets:
                raise CatalogError("invalid or duplicate host target")
            self.targets[row["alias"]] = row["instance_id"]

    def list_targets(self):
        return sorted(self.targets)

    def inspect(self, alias):
        if alias not in self.targets:
            raise CatalogError("unknown host capacity target")
        _regular_owned(self.script, secret=False)
        if (self.connection.is_symlink() or not self.connection.is_file()
                or self.connection.stat().st_uid != os.getuid() or self.connection.stat().st_mode & 0o077):
            raise CatalogError("host connection file must be private")
        env = {key: os.environ[key] for key in ("HOME", "PATH", "LANG", "SSH_AUTH_SOCK") if key in os.environ}
        try:
            result = subprocess.run([sys.executable, str(self.script), "--connection-file", str(self.connection),
                                     "--instance-id", self.targets[alias]],
                                    capture_output=True, text=True, env=env, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CatalogError("host capacity unavailable") from exc
        if result.returncode or len(result.stdout) > 262144:
            raise CatalogError("host capacity or identity validation failed")
        try:
            data = json.loads(result.stdout)
        except ValueError as exc:
            raise CatalogError("invalid host capacity response") from exc
        if (not isinstance(data, dict) or data.get("instance_id") != self.targets[alias]
                or data.get("schema") != "foundation.host-capacity-audit.v1"
                or data.get("mutation_invoked") is not False):
            raise CatalogError("host capacity identity or contract mismatch")
        filesystem = data.get("filesystem")
        if not isinstance(filesystem, dict):
            raise CatalogError("host capacity filesystem unavailable")
        system = data.get("system", {})
        if not isinstance(system, dict):
            raise CatalogError("host system snapshot unavailable")
        return {"provider": "qiniu", "target": alias, "status": "observed",
                "observed_at": data.get("generated_at_utc"),
                "coverage": "exact cloud-init identity, root filesystem and OS load/memory snapshot; Docker and backups excluded",
                "data": {"instance_id": data["instance_id"], "capacity_status": data.get("status"),
                         "release_blocked": data.get("release_blocked"), "thresholds": data.get("thresholds"),
                         "system": {key: system.get(key) for key in ("cpu_count", "load_average", "memory_total_bytes",
                                                                     "memory_available_bytes", "uptime_seconds")},
                         "filesystem": {key: filesystem.get(key) for key in
                                        ("total_bytes", "available_bytes", "used_bytes", "used_percent", "used_inode_percent")}}}
