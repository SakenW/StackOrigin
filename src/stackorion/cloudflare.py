"""Exact-zone, read-only Cloudflare inventory adapter."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import CatalogError

HEX_ID = re.compile(r"^[0-9a-f]{32}$", re.I)
API_BASE = "https://api.cloudflare.com/client/v4/zones/"
PUBLIC_FIELDS = ("id", "name", "status", "paused", "type", "name_servers", "activated_on")


def _token(path: Path) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CatalogError("Cloudflare credential file is unavailable")
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise CatalogError("Cloudflare credential file must be private (0600)")
    parent = path.parent
    if parent.is_symlink() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
        raise CatalogError("Cloudflare credential directory must be private (0700)")
    if path.stat().st_size > 8192:
        raise CatalogError("Cloudflare credential file is too large")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CatalogError("Cloudflare credential file cannot be read") from exc
    values = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CatalogError("Cloudflare credential format is invalid")
        key, value = line.split("=", 1)
        if key.strip() == "CLOUDFLARE_API_TOKEN":
            value = value.strip()
            if value.startswith(('"', "'")) and value.endswith(value[0]):
                value = value[1:-1]
            if not value or len(value) > 512 or not value.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in value):
                raise CatalogError("Cloudflare API token format is invalid")
            values.append(value)
    if len(values) != 1:
        raise CatalogError("Cloudflare API token is missing or duplicated")
    return values[0]


class CloudflareZoneAdapter:
    def __init__(self, settings: dict[str, Any]):
        if set(settings) != {"credential_file", "targets"}:
            raise CatalogError("invalid Cloudflare adapter settings")
        if not isinstance(settings["credential_file"], str):
            raise CatalogError("invalid Cloudflare credential path")
        self.credential = Path(settings["credential_file"])
        targets = settings["targets"]
        if not isinstance(targets, list):
            raise CatalogError("Cloudflare targets must be a list")
        self.targets: dict[str, dict[str, str]] = {}
        seen_ids: set[str] = set()
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"alias", "zone_id", "account_id", "zone_name"}:
                raise CatalogError("invalid Cloudflare zone target")
            alias = target["alias"]
            zone_id = target["zone_id"]
            account_id = target["account_id"]
            zone_name = target["zone_name"]
            if not isinstance(alias, str) or not alias or not isinstance(zone_id, str) or not HEX_ID.fullmatch(zone_id):
                raise CatalogError("invalid Cloudflare zone target")
            if not isinstance(account_id, str) or not HEX_ID.fullmatch(account_id):
                raise CatalogError("invalid Cloudflare account")
            if not isinstance(zone_name, str) or not zone_name:
                raise CatalogError("invalid Cloudflare zone name")
            if alias in self.targets or zone_id.lower() in seen_ids:
                raise CatalogError("duplicate Cloudflare zone target")
            self.targets[alias] = {"zone_id": zone_id.lower(), "account_id": account_id.lower(), "zone_name": zone_name.lower()}
            seen_ids.add(zone_id.lower())

    def list_targets(self) -> list[str]:
        return sorted(self.targets)

    def inspect(self, alias: str) -> dict[str, Any]:
        if alias not in self.targets:
            raise CatalogError("unknown Cloudflare zone target")
        target = self.targets[alias]
        token = _token(self.credential)
        request = urllib.request.Request(
            API_BASE + target["zone_id"],
            headers={"Accept": "application/json"},
            method="GET",
        )
        request.add_unredirected_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read(65_537)
        except urllib.error.HTTPError as exc:
            exc.close()
            raise CatalogError(f"Cloudflare API HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise CatalogError("Cloudflare API unavailable") from exc
        if len(raw) > 65_536:
            raise CatalogError("Cloudflare API response exceeded limit")
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise CatalogError("Cloudflare API returned invalid JSON") from exc
        if not isinstance(envelope, dict) or envelope.get("success") is not True or not isinstance(envelope.get("result"), dict):
            raise CatalogError("Cloudflare API returned unsuccessful response")
        zone = envelope["result"]
        account = zone.get("account")
        if (str(zone.get("id", "")).lower() != target["zone_id"]
                or str(zone.get("name", "")).lower() != target["zone_name"]
                or not isinstance(account, dict)
                or str(account.get("id", "")).lower() != target["account_id"]):
            raise CatalogError("Cloudflare API target mismatch")
        return {
            "provider": "cloudflare", "target": alias,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": "observed", "coverage": "one configured zone; DNS records and account inventory not queried",
            "data": {field: zone.get(field) for field in PUBLIC_FIELDS},
        }
