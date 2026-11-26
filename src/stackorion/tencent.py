"""Read-only EdgeOne Zone adapter using the project-owned Tencent helper."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import CatalogError

ZONE_ID = re.compile(r"^zone-[a-z0-9]+$", re.I)


class TencentEdgeOneAdapter:
    def __init__(self, settings: dict[str, Any]):
        if set(settings) != {"script_path", "credential_file", "targets"}:
            raise CatalogError("invalid Tencent adapter settings")
        if not all(isinstance(settings[key], str) for key in ("script_path", "credential_file")):
            raise CatalogError("invalid Tencent adapter paths")
        self.script = Path(settings["script_path"])
        self.credential = Path(settings["credential_file"])
        if not self.script.is_absolute() or not self.credential.is_absolute():
            raise CatalogError("Tencent paths must be absolute")
        targets = settings["targets"]
        if not isinstance(targets, list):
            raise CatalogError("Tencent targets must be a list")
        self.targets: dict[str, str] = {}
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"alias", "zone_id"}:
                raise CatalogError("invalid Tencent target")
            alias, zone_id = target["alias"], target["zone_id"]
            if not isinstance(alias, str) or not alias or not isinstance(zone_id, str) or not ZONE_ID.fullmatch(zone_id):
                raise CatalogError("invalid Tencent zone identifier")
            if alias in self.targets or zone_id in self.targets.values():
                raise CatalogError("duplicate Tencent target")
            self.targets[alias] = zone_id

    def list_targets(self) -> list[str]:
        return sorted(self.targets)

    def inspect(self, alias: str) -> dict[str, Any]:
        if alias not in self.targets:
            raise CatalogError("unknown Tencent target")
        if self.script.is_symlink() or not self.script.is_file() or self.script.stat().st_uid != os.getuid():
            raise CatalogError("Tencent helper is unavailable")
        if (self.credential.is_symlink() or not self.credential.is_file()
                or self.credential.stat().st_uid != os.getuid() or self.credential.stat().st_mode & 0o077):
            raise CatalogError("Tencent credential file must be private (0600)")
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "SSL_CERT_FILE") if key in os.environ}
        env["TENCENTCLOUD_ENV_FILE"] = str(self.credential)
        body = json.dumps({"Filters": [{"Name": "zone-id", "Values": [self.targets[alias]]}], "Limit": 100, "Offset": 0})
        try:
            result = subprocess.run(
                [sys.executable, str(self.script), "DescribeZones", body],
                env=env, capture_output=True, text=True, timeout=25, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise CatalogError("Tencent Zone query timed out") from exc
        if result.returncode != 0:
            raise CatalogError(f"Tencent Zone query failed (exit {result.returncode})")
        if len(result.stdout) > 262_144:
            raise CatalogError("Tencent Zone response exceeded limit")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CatalogError("Tencent Zone response is invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("Response"), dict):
            raise CatalogError("Tencent Zone response has unexpected shape")
        response = payload["Response"]
        if isinstance(response.get("Error"), dict):
            raise CatalogError("Tencent Zone API error: " + str(response["Error"].get("Code", "unknown")))
        zones = response.get("Zones")
        if not isinstance(zones, list):
            raise CatalogError("Tencent Zone list is unavailable")
        exact = [zone for zone in zones if isinstance(zone, dict) and zone.get("ZoneId") == self.targets[alias]]
        if len(exact) != 1:
            raise CatalogError("Tencent Zone target mismatch or absent")
        zone = exact[0]
        fields = ("ZoneId", "ZoneName", "Status", "Type", "Area")
        return {
            "provider": "tencent", "target": alias,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": "observed", "coverage": "one configured EdgeOne Zone; CVM and other products not queried",
            "data": {field: zone.get(field) for field in fields},
        }
