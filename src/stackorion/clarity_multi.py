"""One read-only Clarity entry backed by separate project Data Export tokens."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from typing import Any

from .catalog import CatalogError, ID
from .clarity_official import _read_token, credential_path

PROJECT_ID = re.compile(r"^[a-z0-9]{8,32}$")
API_URL = "https://www.clarity.ms/export-data/api/v1/project-live-insights?numOfDays=1"


def _get_export(token: str) -> list[dict[str, Any]]:
    headers = f"Authorization: Bearer {token}\nAccept: application/json\n"
    try:
        response = subprocess.run(
            ["curl", "--header", "@-", "--silent", "--show-error", "--proto", "=https",
             "--connect-timeout", "5", "--max-time", "12", "--max-filesize", "524288",
             "--write-out", "\nSTACKORION_HTTP_STATUS:%{http_code}", API_URL],
            input=headers.encode("ascii"), capture_output=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CatalogError("Clarity Data Export API unavailable") from exc
    raw, marker, status = response.stdout.rpartition(b"\nSTACKORION_HTTP_STATUS:")
    if response.returncode == 63 or len(raw) > 524_288:
        raise CatalogError("Clarity Data Export response exceeded limit")
    if response.returncode != 0 or not marker or not status.isdigit():
        raise CatalogError("Clarity Data Export API unavailable")
    if status != b"200":
        raise CatalogError(f"Clarity Data Export API HTTP {status.decode('ascii')}")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise CatalogError("Clarity Data Export returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise CatalogError("Clarity Data Export returned unexpected data")
    return payload


def _counts(payload: list[dict[str, Any]]) -> dict[str, int]:
    traffic = [row for row in payload if isinstance(row, dict) and row.get("metricName") == "Traffic"]
    if len(traffic) != 1 or not isinstance(traffic[0].get("information"), list):
        raise CatalogError("Clarity Traffic metric is unavailable")
    information = traffic[0]["information"]
    if len(information) != 1 or not isinstance(information[0], dict):
        raise CatalogError("Clarity Traffic coverage is incomplete")
    row = information[0]

    def count(names: tuple[str, ...]) -> int:
        value = next((row[name] for name in names if name in row), None)
        if type(value) is int and value >= 0:
            return value
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        raise CatalogError("Clarity Traffic count is invalid")

    return {"sessions": count(("totalSessionCount",)),
            "users": count(("distantUserCount", "distinctUserCount"))}


class ClarityMultiAdapter:
    def __init__(self, settings: dict[str, Any]):
        if set(settings) != {"profiles"} or not isinstance(settings["profiles"], list):
            raise CatalogError("invalid Clarity profile settings")
        profiles = settings["profiles"]
        if not profiles or len(profiles) > 50:
            raise CatalogError("Clarity profiles must contain 1 to 50 entries")
        self.profiles: dict[str, str] = {}
        for row in profiles:
            if not isinstance(row, dict) or set(row) != {"alias", "project_id"}:
                raise CatalogError("invalid Clarity project entry")
            alias, project_id = row["alias"], row["project_id"]
            if (not isinstance(alias, str) or not ID.fullmatch(alias)
                    or not isinstance(project_id, str) or not PROJECT_ID.fullmatch(project_id)
                    or alias in self.profiles or project_id in self.profiles.values()):
                raise CatalogError("invalid or duplicate Clarity project identity")
            self.profiles[alias] = project_id

    def list_targets(self) -> list[str]:
        return sorted(self.profiles)

    def discover(self) -> dict[str, Any]:
        rows = []
        for alias, project_id in sorted(self.profiles.items()):
            path = credential_path(alias)
            configured = (path.is_file() and not path.is_symlink()
                          and path.stat().st_uid == os.getuid()
                          and stat.S_IMODE(path.stat().st_mode) == 0o600)
            rows.append({"alias": alias, "project_id": project_id,
                         "credential_status": "file-present" if configured else "needs-token"})
        return {"provider": "clarity", "status": "declared", "observed_at": datetime.now(timezone.utc).isoformat(),
                "coverage": "configured projects and local credential file presence; no provider query",
                "data": rows}

    def inspect(self, alias: str) -> dict[str, Any]:
        project_id = self.profiles.get(alias)
        if project_id is None:
            raise CatalogError("unknown Clarity project target")
        try:
            token = _read_token(credential_path(alias))
        except CatalogError as exc:
            raise CatalogError("Clarity project credential unavailable") from exc
        counts = _counts(_get_export(token))
        return {"provider": "clarity", "target": alias, "project_id": project_id,
                "status": "observed", "observed_at": datetime.now(timezone.utc).isoformat(),
                "data_window": "previous 24 hours; Clarity reports UTC",
                "coverage": "one project, aggregate sessions and users only; no recordings or URLs",
                "data": counts}
