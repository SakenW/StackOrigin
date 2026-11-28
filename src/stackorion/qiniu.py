"""Read-only QVM discovery and exact-instance audit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .catalog import CatalogError

INSTANCE_FIELDS = ("instance_id", "instance_name", "instance_type", "region_id", "zone_id", "status",
                   "private_ip_address", "eip_address", "security_groups", "security_group_ids",
                   "cost_charge_type", "instance_charge_type", "expired_time", "create_time", "vpc_id")
RULE_FIELDS = ("id", "protocol", "port_range", "source_cidr_ip", "dest_cidr_ip",
               "ipv6_source_cidr_ip", "ipv6_dest_cidr_ip", "source_group_id", "policy",
               "priority", "description", "direction")
RESOURCE_FIELDS = {
    "instances": INSTANCE_FIELDS,
    "eips": ("allocation_id", "allocation_name", "ip_address", "status", "instance_id", "region_id",
             "bandwidth", "instance_charge_type", "expired_time"),
    "vpcs": ("vpc_id", "vpc_name", "region_id", "cidr_block", "status", "create_time"),
    "disks": ("disk_id", "disk_name", "region_id", "zone_id", "instance_id", "status", "size",
              "category", "type", "device", "delete_with_instance", "expired_time"),
    "common_bandwidth": ("id", "bandwidth_package_id", "name", "region_id", "bandwidth", "status",
                         "instance_charge_type", "expired_time"),
}


def _regular_owned(path: Path, *, secret: bool) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise CatalogError("configured file is missing or has unsafe ownership")
    if secret and path.stat().st_mode & 0o077:
        raise CatalogError("credential file must be private (0600)")
    if secret and (path.parent.is_symlink() or path.parent.stat().st_uid != os.getuid()
                   or path.parent.stat().st_mode & 0o077):
        raise CatalogError("credential directory must be private (0700)")


def _rows(value: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise CatalogError("QVM returned invalid resource rows")
    return [{field: row.get(field) for field in fields if field in row} for row in value]


def _coverage(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("status") not in {"observed", "partial", "unavailable"}:
        raise CatalogError("QVM returned invalid coverage")
    return {field: value[field] for field in ("status", "total", "returned_count", "pages", "reason")
            if field in value}


def run_qiniu_helper(script: Path, credential: Path, args: list[str]) -> dict[str, Any]:
    _regular_owned(script, secret=False)
    _regular_owned(credential, secret=True)
    env = {key: os.environ[key] for key in ("PATH", "HOME", "LANG", "SSL_CERT_FILE") if key in os.environ}
    env["QINIU_ENV_FILE"] = str(credential)
    try:
        result = subprocess.run([sys.executable, str(script), *args],
                                env=env, capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CatalogError("QVM query unavailable or timed out") from exc
    if result.returncode:
        raise CatalogError(f"QVM query failed (exit {result.returncode})")
    if len(result.stdout) > 2_097_152:
        raise CatalogError("QVM query output exceeded limit")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise CatalogError("QVM query returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CatalogError("QVM query returned unexpected data")
    return payload


class QiniuQVMAdapter:
    def __init__(self, settings: dict[str, Any]):
        required = {"credential_file", "targets"}
        if set(settings) - {"script_path"} != required:
            raise CatalogError("invalid QVM adapter settings")
        if (not isinstance(settings["credential_file"], str)
                or ("script_path" in settings and not isinstance(settings["script_path"], str))):
            raise CatalogError("invalid QVM paths")
        self.script = Path(settings["script_path"]) if "script_path" in settings else None
        self.credential = Path(settings["credential_file"])
        if not self.credential.is_absolute() or (self.script is not None and not self.script.is_absolute()):
            raise CatalogError("QVM paths must be absolute")
        if not isinstance(settings["targets"], list):
            raise CatalogError("QVM targets must be a list")
        self.targets: dict[str, str] = {}
        for target in settings["targets"]:
            if not isinstance(target, dict) or set(target) != {"alias", "instance_id"}:
                raise CatalogError("invalid QVM target")
            alias, instance_id = target["alias"], target["instance_id"]
            if not isinstance(alias, str) or not isinstance(instance_id, str) or not alias or not instance_id:
                raise CatalogError("invalid QVM target identifier")
            if alias in self.targets or instance_id in self.targets.values():
                raise CatalogError("duplicate QVM target")
            self.targets[alias] = instance_id

    def list_targets(self) -> list[str]:
        return sorted(self.targets)

    @property
    def query_upstreams(self):
        return {"inventory": "official-api", "instance": "official-api"} if self.script is None else {}

    def _run(self, args: list[str]) -> dict[str, Any]:
        if self.script is None:
            raise CatalogError("QVM helper requires an explicit script_path")
        return run_qiniu_helper(self.script, self.credential, args)

    def discover(self) -> dict[str, Any]:
        if self.script is None:
            from .qiniu_qvm_api import QVMReadAPI
            payload = QVMReadAPI(self.credential).inventory()
        else:
            payload = self._run(["inventory"])
        collections = payload.get("collections")
        if not isinstance(collections, dict) or set(collections) != {"instances", "eips", "vpcs"}:
            raise CatalogError("QVM inventory returned unexpected collections")
        clean = {name: {**_coverage(value), "data": _rows(value.get("data"), RESOURCE_FIELDS[name])}
                 for name, value in collections.items()}
        return {"provider": "qiniu", "observed_at": datetime.now(timezone.utc).isoformat(),
                "status": "observed" if all(x["status"] == "observed" for x in clean.values()) else "partial",
                "coverage": "this credential's instance/EIP/VPC lists without region filter; at most 1000 rows each",
                "data": clean}

    def inspect(self, alias: str) -> dict[str, Any]:
        if not isinstance(alias, str) or alias not in self.targets:
            raise CatalogError("unknown QVM target")
        if self.script is None:
            from .qiniu_qvm_api import QVMReadAPI
            payload = QVMReadAPI(self.credential).instance(self.targets[alias])
        else:
            payload = self._run(["--instance-id", self.targets[alias], "audit"])
        instance = payload.get("instance")
        if not isinstance(instance, dict) or instance.get("instance_id") != self.targets[alias]:
            raise CatalogError("QVM audit target mismatch")
        data: dict[str, Any] = {"instance": {field: instance[field] for field in INSTANCE_FIELDS if field in instance}}
        for direction in ("ingress", "egress"):
            groups = payload.get("security_group_" + direction, {})
            if not isinstance(groups, dict) or any(not isinstance(group, str) for group in groups):
                raise CatalogError("QVM returned invalid security group data")
            data["security_group_" + direction] = {group: _rows(rules, RULE_FIELDS) for group, rules in groups.items()}
        for name, kind in (("eip_by_instance", "eips"), ("vpcs", "vpcs"), ("disks", "disks"),
                           ("common_bandwidth", "common_bandwidth")):
            if name in payload:
                value = payload[name]
                if not isinstance(value, dict):
                    raise CatalogError("QVM returned invalid related resource data")
                data[name] = _rows(value.get("data"), RESOURCE_FIELDS[kind])
        coverage = payload.get("coverage")
        clean_coverage = ({name: _coverage(value) for name, value in coverage.items()}
                          if isinstance(coverage, dict) else {})
        complete = bool(clean_coverage) and all(value["status"] == "observed" for value in clean_coverage.values())
        return {"provider": "qiniu", "target": alias, "observed_at": datetime.now(timezone.utc).isoformat(),
                "status": "observed" if complete else "partial",
                "coverage": clean_coverage or {"reason": "legacy_helper_coverage_unknown"}, "data": data}
