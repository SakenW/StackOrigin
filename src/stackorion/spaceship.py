"""Read-only, exact-domain Spaceship API adapter."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .catalog import CatalogError

DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$", re.I)
API_BASE = "https://spaceship.dev/api/v1/domains/"
PUBLIC_FIELDS = (
    "name", "unicodeName", "autoRenew", "registrationDate", "expirationDate",
    "lifecycleStatus", "verificationStatus", "nameservers",
)


def _read_credentials(path: Path) -> tuple[str, str]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CatalogError("Spaceship credential file is unavailable")
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise CatalogError("Spaceship credential file must be private (0600)")
    parent = path.parent
    if parent.is_symlink() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
        raise CatalogError("Spaceship credential directory must be private (0700)")
    if path.stat().st_size > 8192:
        raise CatalogError("Spaceship credential file is too large")
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CatalogError("Spaceship credential file cannot be read") from exc
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CatalogError("Spaceship credential format is invalid")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in {"SPACESHIP_API_KEY", "SPACESHIP_API_SECRET"} or key in values:
            raise CatalogError("Spaceship credential keys are invalid")
        if value.startswith(('"', "'")) and value.endswith(value[0]):
            value = value[1:-1]
        if not value or len(value) > 512 or not value.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise CatalogError("Spaceship credential value is invalid")
        values[key] = value
    if set(values) != {"SPACESHIP_API_KEY", "SPACESHIP_API_SECRET"}:
        raise CatalogError("Spaceship credential keys are missing")
    return values["SPACESHIP_API_KEY"], values["SPACESHIP_API_SECRET"]


class SpaceshipDomainsAdapter:
    def __init__(self, settings: dict[str, Any]):
        if set(settings) != {"credential_file", "targets"}:
            raise CatalogError("invalid Spaceship adapter settings")
        if not isinstance(settings["credential_file"], str):
            raise CatalogError("invalid Spaceship credential path")
        self.credential = Path(settings["credential_file"])
        targets = settings["targets"]
        if not isinstance(targets, list):
            raise CatalogError("Spaceship targets must be a list")
        self.targets: dict[str, str] = {}
        for target in targets:
            if not isinstance(target, dict) or set(target) != {"alias", "domain"}:
                raise CatalogError("invalid Spaceship domain target")
            alias, domain = target["alias"], target["domain"]
            if not isinstance(alias, str) or not alias or not isinstance(domain, str) or not DOMAIN.fullmatch(domain):
                raise CatalogError("invalid Spaceship domain target")
            if alias in self.targets or domain.lower() in self.targets.values():
                raise CatalogError("duplicate Spaceship domain target")
            self.targets[alias] = domain.lower()

    def list_targets(self) -> list[str]:
        return sorted(self.targets)

    @staticmethod
    def _get_json(url: str, api_key: str, api_secret: str) -> dict[str, Any]:
        headers = f"X-API-Key: {api_key}\nX-API-Secret: {api_secret}\nAccept: application/json\n"
        try:
            response = subprocess.run(
                ["curl", "-q", "--request", "GET", "--header", "@-", "--silent", "--show-error", "--proto", "=https",
                 "--connect-timeout", "5", "--max-time", "12", "--max-filesize", "524288",
                 "--write-out", "\nSTACKORION_HTTP_STATUS:%{http_code}", url],
                input=headers.encode("ascii"), capture_output=True, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CatalogError("Spaceship API unavailable") from exc
        raw, marker, status = response.stdout.rpartition(b"\nSTACKORION_HTTP_STATUS:")
        if response.returncode == 63 or len(raw) > 524_288:
            raise CatalogError("Spaceship API response exceeded limit")
        if response.returncode != 0 or not marker or not status.isdigit():
            raise CatalogError("Spaceship API unavailable")
        if status != b"200":
            raise CatalogError(f"Spaceship API HTTP {status.decode('ascii')}")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise CatalogError("Spaceship API returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise CatalogError("Spaceship API returned unexpected data")
        return payload

    def discover(self) -> dict[str, Any]:
        """Read up to 1000 domains owned by this credential; never return contacts."""
        api_key, api_secret = _read_credentials(self.credential)
        domains: list[dict[str, Any]] = []
        seen: set[str] = set()
        total: int | None = None
        for page in range(10):
            payload = self._get_json(
                API_BASE.rstrip("/") + f"?take=100&skip={page * 100}", api_key, api_secret
            )
            items = payload.get("items")
            total = payload.get("total")
            if not isinstance(total, int) or isinstance(total, bool) or total < 0 or not isinstance(items, list):
                raise CatalogError("Spaceship domain list has unexpected shape")
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not DOMAIN.fullmatch(item["name"]):
                    raise CatalogError("Spaceship domain entry is invalid")
                name = item["name"].lower()
                if name in seen:
                    raise CatalogError("Spaceship domain list contains duplicates")
                seen.add(name)
                domains.append({field: item.get(field) for field in PUBLIC_FIELDS})
            if len(domains) >= total or not items:
                break
        return {
            "provider": "spaceship",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": "observed" if total is not None and len(domains) >= total else "partial",
            "coverage": "up to 10 pages of 100 domains for this credential; contacts excluded",
            "total_count": total,
            "returned_count": len(domains),
            "data": domains,
        }

    def dns_records_summary(self, domain: str) -> dict[str, Any]:
        """Read the total and up to 500 records without returning record values."""
        if not isinstance(domain, str) or len(domain) > 253 or not DOMAIN.fullmatch(domain):
            raise CatalogError("invalid Spaceship domain target")
        credentials = _read_credentials(self.credential)
        url = "https://spaceship.dev/api/v1/dns/records/" + quote(domain.lower(), safe="")
        records = []
        count = None
        for offset in range(0, 500, 100):
            page = self._get_json(url + f"?take=100&skip={offset}", *credentials)
            total, items = page.get("total"), page.get("items")
            if (type(total) is not int or total < offset or (count is not None and total != count)
                    or not isinstance(items, list) or len(items) != min(100, total - offset)):
                raise CatalogError("Spaceship DNS page has unexpected shape")
            count = total
            for item in items:
                if not isinstance(item, dict):
                    raise CatalogError("Spaceship DNS record has unexpected shape")
                name, kind, ttl = item.get("name"), item.get("type"), item.get("ttl")
                if (not isinstance(name, str) or not isinstance(kind, str) or not kind
                        or (ttl is not None and (type(ttl) is not int or ttl < 0))):
                    raise CatalogError("Spaceship DNS record has unexpected shape")
                records.append({"name": name, "type": kind, "ttl": ttl})
            if len(records) == count:
                break
        return {"count": count, "records": records}

    def inspect(self, alias: str) -> dict[str, Any]:
        if alias not in self.targets:
            raise CatalogError("unknown Spaceship domain target")
        api_key, api_secret = _read_credentials(self.credential)
        domain = self.targets[alias]
        payload = self._get_json(API_BASE + quote(domain, safe=""), api_key, api_secret)
        if not isinstance(payload, dict) or str(payload.get("name", "")).lower() != domain:
            raise CatalogError("Spaceship API target mismatch")
        return {
            "provider": "spaceship", "target": alias,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": "observed", "coverage": "one configured domain; registrar details only",
            "data": {field: payload.get(field) for field in PUBLIC_FIELDS},
        }
