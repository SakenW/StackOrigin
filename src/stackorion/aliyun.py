"""Region-bound, read-only Alibaba Cloud ECS inventory via the official CLI."""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import CatalogError

REGION = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
INSTANCE_FIELDS = ("InstanceId", "InstanceName", "Status", "RegionId", "ZoneId", "ExpiredTime", "PublicIpAddress", "VpcAttributes")


def _credentials(path: Path) -> tuple[str, str]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CatalogError("Alibaba credential file is unavailable")
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise CatalogError("Alibaba credential file must be private (0600)")
    parent = path.parent
    if parent.is_symlink() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
        raise CatalogError("Alibaba credential directory must be private (0700)")
    if path.stat().st_size > 8192:
        raise CatalogError("Alibaba credential file is too large")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CatalogError("Alibaba credential file cannot be read") from exc
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise CatalogError("Alibaba credential format is invalid")
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET"} or key in values:
            raise CatalogError("Alibaba credential keys are invalid")
        value = value.strip()
        if value.startswith(('"', "'")) and value.endswith(value[0]):
            value = value[1:-1]
        if not value or len(value) > 512 or not value.isascii() or any(ord(char) < 33 or ord(char) > 126 for char in value):
            raise CatalogError("Alibaba credential value is invalid")
        values[key] = value
    if set(values) != {"ALIYUN_ACCESS_KEY_ID", "ALIYUN_ACCESS_KEY_SECRET"}:
        raise CatalogError("Alibaba credential keys are missing")
    return values["ALIYUN_ACCESS_KEY_ID"], values["ALIYUN_ACCESS_KEY_SECRET"]


class AliyunECSAdapter:
    def __init__(self, settings: dict[str, Any]):
        if set(settings) != {"cli_path", "credential_file", "regions"}:
            raise CatalogError("invalid Alibaba ECS adapter settings")
        if not isinstance(settings["cli_path"], str) or not isinstance(settings["credential_file"], str):
            raise CatalogError("invalid Alibaba ECS paths")
        self.cli = Path(settings["cli_path"])
        self.credential = Path(settings["credential_file"])
        if not self.cli.is_absolute() or not self.credential.is_absolute():
            raise CatalogError("Alibaba ECS paths must be absolute")
        regions = settings["regions"]
        if not isinstance(regions, list) or any(not isinstance(region, str) or not REGION.fullmatch(region) for region in regions):
            raise CatalogError("invalid Alibaba ECS regions")
        if len(regions) != len(set(regions)) or len(regions) > 100:
            raise CatalogError("duplicate or excessive Alibaba ECS regions")
        self.regions = set(regions)

    def list_targets(self) -> list[str]:
        return sorted(self.regions)

    def inspect(self, region: str) -> dict[str, Any]:
        if region not in self.regions:
            raise CatalogError("unknown Alibaba ECS region")
        if not self.cli.is_file() or not os.access(self.cli, os.X_OK):
            raise CatalogError("Alibaba CLI is unavailable")
        access_id, access_secret = _credentials(self.credential)
        env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "SSL_CERT_FILE") if key in os.environ}
        env.update({
            "ALIBABA_CLOUD_ACCESS_KEY_ID": access_id,
            "ALIBABA_CLOUD_ACCESS_KEY_SECRET": access_secret,
            "ALIBABA_CLOUD_IGNORE_PROFILE": "TRUE",
        })
        all_instances: list[dict[str, Any]] = []
        seen_tokens: set[str] = set()
        next_token: str | None = None
        for _page in range(10):
            argv = [str(self.cli), "ecs", "DescribeInstances", "--region", region,
                    "--RegionId", region, "--MaxResults", "100"]
            if next_token:
                argv.extend(["--NextToken", next_token])
            try:
                result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=25, check=False)
            except subprocess.TimeoutExpired as exc:
                raise CatalogError("Alibaba ECS query timed out") from exc
            if result.returncode != 0:
                if re.search(r"\bErrorCode:\s*Forbidden\.RAM\b", result.stderr):
                    raise CatalogError("Alibaba ECS permission denied (ecs:DescribeInstances)")
                raise CatalogError(f"Alibaba ECS query failed (exit {result.returncode})")
            if len(result.stdout) > 1_000_000:
                raise CatalogError("Alibaba ECS response exceeded limit")
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise CatalogError("Alibaba ECS returned invalid JSON") from exc
            if not isinstance(payload, dict) or not isinstance(payload.get("Instances"), dict):
                raise CatalogError("Alibaba ECS response has unexpected shape")
            instances = payload["Instances"].get("Instance")
            if not isinstance(instances, list) or any(not isinstance(item, dict) for item in instances):
                raise CatalogError("Alibaba ECS instance list is invalid")
            for item in instances:
                if item.get("RegionId") != region or not isinstance(item.get("InstanceId"), str):
                    raise CatalogError("Alibaba ECS instance target mismatch")
                all_instances.append({field: item.get(field) for field in INSTANCE_FIELDS})
            value = payload.get("NextToken")
            if not value:
                next_token = None
                break
            if not isinstance(value, str) or value in seen_tokens:
                raise CatalogError("Alibaba ECS pagination token is invalid")
            seen_tokens.add(value)
            next_token = value
        return {
            "provider": "aliyun", "target": region,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": "partial" if next_token else "observed",
            "coverage": "up to 10 pages of 100 ECS instances in one configured region",
            "instance_count": len(all_instances),
            "data": all_instances,
        }
