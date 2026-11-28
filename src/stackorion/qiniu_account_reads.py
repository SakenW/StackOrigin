"""Bounded, GET-only reads for the local Qiniu account panel."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import hmac
import json
from pathlib import Path
import re
from threading import Lock
from time import monotonic
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .catalog import CatalogError
from .qiniu_cli import credentials


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class QiniuAccountReads:
    def __init__(self, credential_file, las_regions):
        if (not isinstance(credential_file, str) or not Path(credential_file).is_absolute()
                or not isinstance(las_regions, list) or not 1 <= len(las_regions) <= 24
                or any(not isinstance(r, str) or not re.fullmatch(r'[a-z]{2}-[a-z]+-\d+', r) for r in las_regions)
                or len(set(las_regions)) != len(las_regions)):
            raise ValueError("Invalid Qiniu account read configuration")
        self.credential_file = credential_file
        self.regions = las_regions
        self.lock = Lock()
        self.cache = None
        self.expires = 0

    @classmethod
    def from_file(cls, path):
        with path.open() as stream:
            settings = json.loads(stream.read(4097))
        if not isinstance(settings, dict) or settings.keys() != {"credential_file", "las_regions"}:
            raise ValueError("Invalid Qiniu account read configuration")
        return cls(**settings)

    def _get(self, host, path, *, legacy=False):
        keys = credentials(self.credential_file)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if legacy:
            message, scheme, headers = path + "\n", "QBox", {}
        else:
            message = f"GET {path}\nHost: {host}\nX-Qiniu-Date: {stamp}\n\n"
            scheme, headers = "Qiniu", {"X-Qiniu-Date": stamp}
        digest = hmac.new(keys["QINIU_SK"].encode(), message.encode(), hashlib.sha1).digest()
        signature = base64.urlsafe_b64encode(digest).decode()
        headers["Authorization"] = f'{scheme} {keys["QINIU_AK"]}:{signature}'
        request = Request(f"https://{host}{path}", headers=headers, method="GET")
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            body = response.read(4_194_305)
        if len(body) > 4_194_304:
            raise ValueError("Qiniu response exceeds size limit")
        return json.loads(body)

    def _pages(self, host, path, rows_key, marker_key):
        marker = ""
        seen = set()
        rows = []
        for _ in range(20):
            query = {"limit": 100}
            if marker:
                query["marker"] = marker
            page = self._get(host, f"{path}?{urlencode(query)}")
            batch, marker = page[rows_key], page[marker_key]
            if (page.get("code", 200) not in (0, 200) or not isinstance(batch, list) or len(batch) > 100
                    or not isinstance(marker, str) or marker in seen):
                raise ValueError("Invalid Qiniu resource page")
            rows.extend(batch)
            if not marker:
                return rows
            seen.add(marker)
        raise ValueError("Qiniu pagination limit reached")

    def _las(self, region):
        rows = self._pages(f"{region}-ecs.qiniuapi.com", "/v1/instances", "items", "nextMarker")
        if any(row["region"]["id"] != region for row in rows):
            raise ValueError("Unexpected LAS region")
        return [{"id": row["instanceID"], "name": row["name"] or row["instanceID"], "region": region,
                 "state": row["state"], "expires_at": row["expiredAt"]} for row in rows]

    def _buckets(self):
        rows = self._get("rs.qiniu.com", "/buckets", legacy=True)
        if not isinstance(rows, list) or any(not isinstance(row, str) or not row for row in rows):
            raise ValueError("Invalid Kodo bucket list")
        return sorted(set(rows))

    def _cdn(self):
        rows = self._pages("api.qiniu.com", "/domain", "domains", "marker")
        return [{"name": row["name"], "state": row["operatingState"], "protocol": row["protocol"]}
                for row in rows]

    def _finance(self, path):
        result = self._get("api.qiniu.com", f"/billing-api/v1/{path}")
        if result["code"] != 0:
            raise ValueError("Qiniu finance query failed")
        return result["data"]

    def _balance(self):
        row = self._finance("account/balance-overview")
        return {"available": money(row["available_balance"]), "currency": row["currency"]}

    def _bill(self, now):
        end = now.replace(day=1)
        if now.day < 5:
            end = (end - timedelta(days=1)).replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
        query = urlencode({"start": start.strftime("%Y-%m-01T00:00:00"), "end": end.strftime("%Y-%m-01T00:00:00")})
        rows = self._finance("bill/overview?" + query)
        if not isinstance(rows, list):
            raise ValueError("Invalid Qiniu bill list")
        return {"period": start.strftime("%Y-%m"), "rows": [
            {"product": row["product"], "item": row["itemDesc"], "type": row["type"],
             "payment": row["payStatus"], "fee": money(row["fee"]), "currency": row["currency"]} for row in rows
        ]}

    def snapshot(self):
        with self.lock:
            if monotonic() >= self.expires:
                now = datetime.now(timezone(timedelta(hours=8)))
                with ThreadPoolExecutor(max_workers=8) as pool:
                    regions = {r: pool.submit(read, self._las, r) for r in self.regions}
                    results = {key: pool.submit(read, fn, *args) for key, fn, args in (
                        ("kodo", self._buckets, ()), ("cdn", self._cdn, ()),
                        ("bill", self._bill, (now,)), ("balance", self._balance, ()),
                    )}
                    self.cache = {"observed_at": now.isoformat(),
                                  "las": {r: future.result() for r, future in regions.items()},
                                  **{key: future.result() for key, future in results.items()}}
                self.expires = monotonic() + 300
            return self.cache


def money(value):
    if type(value) is not int:
        raise ValueError("Invalid Qiniu money value")
    return format(Decimal(value).scaleb(-8), "f")


def read(query, *args):
    try:
        return {"data": query(*args), "status": "observed"}
    except HTTPError as exc:
        if exc.code in (401, 403):
            return {"status": "permission_denied"}
        return {"status": "unavailable"}
    except (CatalogError, KeyError, OSError, TypeError, ValueError):
        return {"status": "unavailable"}
