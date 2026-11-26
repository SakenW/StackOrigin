"""Local MCP handoff to a running dashboard's exact preview and record routes."""

import json
from http.client import HTTPException
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .catalog import CatalogError


RECORD_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
WORK_ITEM_ID = re.compile(r"[0-9a-f]{64}")
MAX_REPLY = 2 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def request_dashboard(path, data=None, *, address=None, timeout=90):
    if address is None:
        address = os.environ.get("STACKORION_DASHBOARD_URL", "")
    match = re.fullmatch(r"http://127\.0\.0\.1:([0-9]{1,5})", address)
    if not match or not 1 <= int(match[1]) <= 65535:
        raise CatalogError("Set STACKORION_DASHBOARD_URL to the running dashboard's http://127.0.0.1:PORT address")
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        headers.update({"Origin": address, "Content-Type": "application/json"})
        body = json.dumps(data).encode()
    request = Request(address + path, body, headers)
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(MAX_REPLY + 1)
    except HTTPError as exc:
        exc.close()
        raise CatalogError({400: "Dashboard rejected the target, work item or parameters",
                            404: "Dashboard target or record was not found"}.get(exc.code, "Dashboard request failed")) from None
    except (URLError, OSError, HTTPException):
        raise CatalogError("Dashboard is unavailable") from None
    if len(raw) > MAX_REPLY:
        raise CatalogError("Dashboard response exceeds its size limit")
    try:
        reply = json.loads(raw)
    except (UnicodeError, ValueError):
        raise CatalogError("Invalid dashboard response") from None
    if not isinstance(reply, dict):
        raise CatalogError("Invalid dashboard response")
    return address, reply


def get_work_item(work_item_id, *, include_demo=False, address=None):
    if not WORK_ITEM_ID.fullmatch(work_item_id):
        raise CatalogError("Invalid dashboard work item ID")
    mock = "1" if include_demo else "0"
    return request_dashboard(f"/api/work-items/{work_item_id}?mock={mock}", address=address)[1]


def list_work_items(include_demo=False, *, address=None):
    mock = "1" if include_demo else "0"
    return request_dashboard(f"/api/work-items?view=index&mock={mock}", address=address)[1]


def get_inspection(include_demo=False, *, address=None):
    return request_dashboard("/api/inspection/summary?mock=" + ("1" if include_demo else "0"), address=address)[1]


def run_inspection(include_demo=False, *, address=None):
    return request_dashboard("/api/inspection/summary", {"show_mock": include_demo}, address=address)[1]

def list_operation_targets():
    return request_dashboard("/api/resource-operations/targets")[1]


def prepare_operation(binding_id, parameters, work_item_id=None):
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,63}", binding_id) or parameters.keys() - {"name", "content", "ttl"}:
        raise CatalogError("Invalid dashboard binding or preview parameters")
    data = {"binding": binding_id, **parameters}
    if work_item_id is not None:
        if not WORK_ITEM_ID.fullmatch(work_item_id):
            raise CatalogError("Invalid dashboard work item ID")
        data["work_item_id"] = work_item_id
    address, preview = request_dashboard("/api/resource-operations/handoff", data)
    identifier = preview.get("record_id")
    if not isinstance(identifier, str) or not RECORD_ID.fullmatch(identifier):
        raise CatalogError("Dashboard returned an invalid operation record ID")
    preview.pop("plan_id", None)
    return {**preview, "record_url": f"{address}/#operation-records?record={identifier}"}


def get_operation(record_id):
    if not RECORD_ID.fullmatch(record_id):
        raise CatalogError("Invalid dashboard operation record ID")
    address, record = request_dashboard(f"/api/resource-operations/record/{record_id}")
    return {**record, "record_url": f"{address}/#operation-records?record={record_id}"}
