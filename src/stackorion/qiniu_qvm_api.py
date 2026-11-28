"""GET-only client for the public QVM API and the adapter's existing result contract."""

import base64
import hmac
import json
import re
from http.client import HTTPException
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener

from .catalog import CatalogError
from .kodo_objects import _NoRedirect
from .qiniu_cli import credentials

HOST = "api-qvm.qiniu.com"
INSTANCE_PATH = "/v1/vm/instance"
EIP_PATH = "/v1/vm/eip"
VPC_PATH = "/v1/vm/vpc"
DISK_PATH = "/v1/vm/disk"
BANDWIDTH_PATH = "/v1/vpc/common_bandwidth"
RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
PAGE_SIZE = 100
MAX_PAGES = 10
RESPONSE_LIMIT = 2_097_152


class QVMReadAPI:
    def __init__(self, credential):
        try:
            keys = credentials(credential)
        except (OSError, UnicodeError):
            raise CatalogError("QVM credentials could not be read") from None
        self._access_key, self._secret_key = keys["QINIU_AK"], keys["QINIU_SK"].encode()
        self._deadline = monotonic() + 90
        self._opener = build_opener(_NoRedirect())

    def _get(self, path, **params):
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise CatalogError("query_deadline")
        target = path + ("?" + urlencode(params) if params else "")
        # Qiniu V2 signs the same encoded request target sent over HTTPS.
        message = f"GET {target}\nHost: {HOST}\n\n".encode()
        signature = base64.urlsafe_b64encode(hmac.digest(self._secret_key, message, "sha1")).decode()
        request = Request(f"https://{HOST}{target}", method="GET", headers={
            "Host": HOST,
            "Authorization": f"Qiniu {self._access_key}:{signature}",
        })
        try:
            with self._opener.open(request, timeout=min(20, remaining)) as response:
                raw = response.read(RESPONSE_LIMIT + 1)
        except HTTPError as exc:
            exc.close()
            raise CatalogError(f"http_{exc.code}") from None
        except (OSError, HTTPException):
            raise CatalogError("transport_error") from None
        if len(raw) > RESPONSE_LIMIT:
            raise CatalogError("response_too_large")
        try:
            payload = json.loads(raw)
        except (ValueError, RecursionError):
            raise CatalogError("invalid_response") from None
        if not isinstance(payload, dict) or "error_code" in payload or "error_message" in payload:
            raise CatalogError("invalid_response")
        return payload

    def _collection(self, path, identity, **filters):
        rows, seen = [], set()
        total, pages, size = None, 0, None
        status, reason = "observed", None
        for page in range(1, MAX_PAGES + 1):
            try:
                payload = self._get(path, page=page, size=PAGE_SIZE, **filters)
                batch, count = payload.get("data"), payload.get("total")
                page_size = payload.get("size", PAGE_SIZE)
                if pages and (count != total or page_size != size):
                    raise CatalogError("pagination_changed")
                if (type(count) is not int or count < 0 or payload.get("page", page) != page
                        or type(page_size) is not int or not 1 <= page_size <= PAGE_SIZE
                        or not isinstance(batch, list) or len(batch) != min(page_size, count - len(rows))):
                    raise CatalogError("invalid_page")
                ids = [row.get(identity) if isinstance(row, dict) else None for row in batch]
                if any(not isinstance(key, str) or not key for key in ids):
                    raise CatalogError("invalid_resource")
                if len(set(ids)) != len(ids) or seen.intersection(ids):
                    raise CatalogError("duplicate_resource")
            except CatalogError as exc:
                status, reason = ("partial" if pages else "unavailable"), str(exc)
                break
            total, pages, size = count, page, page_size
            rows.extend(batch)
            seen.update(ids)
            if len(rows) == total:
                break
        else:
            status, reason = "partial", "page_limit"
        result = {"status": status, "total": total, "returned_count": len(rows), "pages": pages, "data": rows}
        if reason is not None:
            result["reason"] = reason
        return result

    def inventory(self):
        instances = self._collection(INSTANCE_PATH, "instance_id")
        instances["data"] = [_instance_fields(row) for row in instances["data"]]
        return {"collections": {
            "instances": instances,
            "eips": self._collection(EIP_PATH, "allocation_id"),
            "vpcs": self._collection(VPC_PATH, "vpc_id"),
        }}

    def instance(self, instance_id):
        if not RESOURCE_ID.fullmatch(instance_id):
            raise CatalogError("invalid QVM instance ID")
        located = self._collection(INSTANCE_PATH, "instance_id", instance_id=instance_id)
        if located["status"] != "observed":
            raise CatalogError(f"QVM instance lookup unavailable ({located['reason']})")
        matches = located["data"]
        if len(matches) != 1 or matches[0]["instance_id"] != instance_id:
            raise CatalogError("QVM instance lookup did not return one exact target")
        region = matches[0].get("region_id")
        if not isinstance(region, str) or not RESOURCE_ID.fullmatch(region):
            raise CatalogError("QVM instance lookup returned an invalid region")
        try:
            instance = self._get(f"{INSTANCE_PATH}/{instance_id}", region_id=region).get("data")
        except CatalogError as exc:
            raise CatalogError(f"QVM instance detail unavailable ({exc})") from None
        if (not isinstance(instance, dict) or instance.get("instance_id") != instance_id
                or instance.get("region_id") != region):
            raise CatalogError("QVM audit target mismatch")

        clean_instance = _instance_fields(instance)
        groups = clean_instance.get("security_groups")
        related = self._related_resources(instance_id, region, clean_instance.get("vpc_id"))
        coverage = {"instance": {"status": "observed"}, **related}
        rules = {"security_group_ingress": {}, "security_group_egress": {}}
        if not groups:
            coverage["security_groups"] = ({"status": "unavailable", "reason": "invalid_security_groups"}
                                           if groups is None else {"status": "observed"})
        for group in groups or ():
            for direction in ("ingress", "egress"):
                result = self._collection(f"/v1/vm/sgr/{group}/{direction}", "security_group_rule_id",
                                          region_id=region)
                rules["security_group_" + direction][group] = [
                    {**row, "id": row["security_group_rule_id"], "protocol": row.get("ip_protocol")}
                    for row in result["data"]
                ]
                coverage[f"{direction}:{group}"] = result
        return {"instance": clean_instance, **rules, **related, "coverage": coverage}

    def _related_resources(self, instance_id, region, vpc_id):
        def instance_binding(row):
            if row.get("region_id") == region and row.get("instance_id") == instance_id:
                return True
            return None

        eips = _associated(self._collection(EIP_PATH, "allocation_id", region_id=region, instance_id=instance_id),
                           instance_binding)
        disks = _associated(self._collection(DISK_PATH, "disk_id", region_id=region, instance_id=instance_id),
                            instance_binding)
        if vpc_id == "":
            vpcs = _empty_collection()
        elif isinstance(vpc_id, str) and RESOURCE_ID.fullmatch(vpc_id):
            vpcs = _associated(self._collection(VPC_PATH, "vpc_id", region_id=region, vpc_id=vpc_id),
                               lambda row: True if row["vpc_id"] == vpc_id and row.get("region_id") == region else None)
            if vpcs["status"] == "observed" and not vpcs["data"]:
                vpcs.update(status="unavailable", total=None, reason="association_unverified")
        else:
            vpcs = _empty_collection("vpc_identity_unavailable")
        return {"eip_by_instance": eips, "disks": disks, "vpcs": vpcs,
                "common_bandwidth": self._bandwidth(region, eips)}

    def _bandwidth(self, region, eips):
        complete = eips["status"] == "observed"
        ids = {row["allocation_id"] for row in eips["data"]}
        if not ids:
            return _empty_collection(None if complete else "eip_coverage_incomplete")

        def membership(row):
            members = row.get("public_address")
            if (row.get("region_id") != region or not isinstance(members, list)
                    or any(not isinstance(member, dict) or not isinstance(member.get("eip_id"), str)
                           or not member["eip_id"] for member in members)):
                return None
            return any(member["eip_id"] in ids for member in members)

        result = _associated(self._collection(BANDWIDTH_PATH, "cb_id", region_id=region), membership)
        if not complete and result["status"] == "observed":
            result.update(status="partial" if result["data"] else "unavailable",
                          total=None, reason="eip_coverage_incomplete")
        for row in result["data"]:
            row["bandwidth_package_id"] = row["cb_id"]
            row.setdefault("expired_time", row.get("expire_time"))
        return result


def _empty_collection(reason=None):
    return ({"status": "observed", "total": 0, "returned_count": 0, "pages": 0, "data": []} if reason is None else
            {"status": "unavailable", "reason": reason, "total": None, "returned_count": 0, "pages": 0, "data": []})


def _associated(collection, matches):
    verified = []
    for row in collection["data"]:
        match = matches(row)
        if match is True:
            verified.append(row)
        elif match is None:
            collection.setdefault("reason", "association_unverified")
    if "reason" in collection:
        collection["status"] = "partial" if verified else "unavailable"
    collection.update(data=verified, returned_count=len(verified),
                      total=len(verified) if collection["status"] == "observed" else None)
    return collection


def _security_groups(instance):
    groups = instance.get("security_group_ids", instance.get("security_group_id"))
    if isinstance(groups, dict):
        groups = groups.get("security_group_id")
    if isinstance(groups, str):
        groups = [groups]
    if not isinstance(groups, list) or any(not isinstance(group, str) or not RESOURCE_ID.fullmatch(group)
                                           for group in groups):
        return None
    # Include the single-group field if the provider returns both forms.
    primary = instance.get("security_group_id")
    if isinstance(primary, str) and RESOURCE_ID.fullmatch(primary):
        groups = [*groups, primary]
    return list(dict.fromkeys(groups))


def _instance_fields(instance):
    # The adapter allowlists top-level fields. Keep only public subfields in
    # the structured fields exposed by that contract.
    row = dict(instance)
    groups = _security_groups(row)
    if isinstance(row.get("security_group_ids"), dict):
        row["security_group_ids"] = {"security_group_id": groups} if groups is not None else None
    elif "security_group_ids" in row:
        row["security_group_ids"] = groups
    row.pop("security_groups", None)
    if groups is not None:
        row["security_groups"] = groups
    eip = row.get("eip_address")
    if isinstance(eip, dict):
        row["eip_address"] = {key: eip[key] for key in ("allocation_id", "ip_address", "bandwidth",
                              "internet_charge_type", "is_support_dissociate") if key in eip}
    vpc = row.get("vpc_attributes")
    if isinstance(vpc, dict):
        private = vpc.get("private_ip_address")
        if isinstance(private, dict):
            row.setdefault("private_ip_address", private.get("ip_address"))
        nested = private.get("vpc_id") if isinstance(private, dict) else None
        row.setdefault("vpc_id", vpc.get("vpc_id", nested))
    return row
