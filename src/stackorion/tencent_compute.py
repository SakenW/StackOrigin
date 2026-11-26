"""Read-only Tencent CVM and Lighthouse inventory by explicit region."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from tencentcloud.common.common_client import CommonClient
from tencentcloud.common.credential import Credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile

from .catalog import CatalogError

REGION = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
PRODUCTS = {"cvm": "2017-03-12", "lighthouse": "2020-03-24"}
PUBLIC_FIELDS = (
    "InstanceId", "InstanceName", "InstanceState", "Zone", "ExpiredTime",
    "PublicIpAddresses", "PrivateIpAddresses", "PublicAddresses", "PrivateAddresses",
)


def _credentials(path: Path) -> tuple[str, str]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise CatalogError("Tencent credential file is unavailable")
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise CatalogError("Tencent credential file must be private (0600)")
    parent = path.parent
    if parent.is_symlink() or parent.stat().st_uid != os.getuid() or parent.stat().st_mode & 0o077:
        raise CatalogError("Tencent credential directory must be private (0700)")
    if path.stat().st_size > 8192:
        raise CatalogError("Tencent credential file is too large")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise CatalogError("Tencent credential file cannot be read") from exc
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"}:
            continue
        if key in values:
            raise CatalogError("Tencent credential key is duplicated")
        value = value.strip()
        if value.startswith(('"', "'")) and value.endswith(value[0]):
            value = value[1:-1]
        if not value or len(value) > 512 or not value.isascii() or any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
            raise CatalogError("Tencent credential value is invalid")
        values[key] = value
    if set(values) != {"TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY"}:
        raise CatalogError("Tencent credential keys are missing")
    return values["TENCENTCLOUD_SECRET_ID"], values["TENCENTCLOUD_SECRET_KEY"]


class TencentComputeAdapter:
    def __init__(self, settings: dict[str, Any]):
        if set(settings) != {"credential_file", "product", "regions"}:
            raise CatalogError("invalid Tencent compute settings")
        if (not isinstance(settings["credential_file"], str)
                or not isinstance(settings["product"], str) or settings["product"] not in PRODUCTS):
            raise CatalogError("invalid Tencent compute profile")
        self.credential = Path(settings["credential_file"])
        self.product = settings["product"]
        regions = settings["regions"]
        if not isinstance(regions, list) or any(not isinstance(region, str) or not REGION.fullmatch(region) for region in regions):
            raise CatalogError("invalid Tencent compute regions")
        if len(regions) != len(set(regions)) or len(regions) > 100:
            raise CatalogError("duplicate or excessive Tencent compute regions")
        self.regions = set(regions)

    def list_targets(self) -> list[str]:
        return sorted(self.regions)

    def inspect(self, region: str) -> dict[str, Any]:
        if region not in self.regions:
            raise CatalogError("unknown Tencent compute region")
        secret_id, secret_key = _credentials(self.credential)
        profile = ClientProfile()
        profile.httpProfile = HttpProfile()
        profile.httpProfile.reqTimeout = 15
        client = CommonClient(self.product, PRODUCTS[self.product], Credential(secret_id, secret_key), region, profile)
        instances: list[dict[str, Any]] = []
        total: int | None = None
        for page in range(10):
            try:
                envelope = client.call_json("DescribeInstances", {"Offset": page * 100, "Limit": 100})
            except TencentCloudSDKException as exc:
                code = exc.get_code()
                if code.startswith(("AuthFailure", "UnauthorizedOperation")):
                    raise CatalogError(f"Tencent {self.product} permission denied ({code})") from exc
                raise CatalogError(f"Tencent {self.product} API failed ({code})") from exc
            except Exception as exc:
                raise CatalogError(f"Tencent {self.product} API unavailable") from exc
            response = envelope.get("Response") if isinstance(envelope, dict) else None
            if not isinstance(response, dict) or not isinstance(response.get("TotalCount"), int):
                raise CatalogError("Tencent compute response has unexpected shape")
            page_items = response.get("InstanceSet")
            if not isinstance(page_items, list) or any(not isinstance(item, dict) or not isinstance(item.get("InstanceId"), str) for item in page_items):
                raise CatalogError("Tencent compute instance list is invalid")
            total = response["TotalCount"]
            instances.extend({field: item.get(field) for field in PUBLIC_FIELDS} for item in page_items)
            if len(instances) >= total or not page_items:
                break
        return {
            "provider": "tencent", "product": self.product, "target": region,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "status": "observed" if total is not None and len(instances) >= total else "partial",
            "coverage": "one configured region, up to 10 pages of 100 instances",
            "total_count": total,
            "returned_count": len(instances),
            "data": instances,
        }
