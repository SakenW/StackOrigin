"""Account-scoped Cloudflare queries and private, fixed-endpoint transport.

No constructor I/O. Mutation entry points live in cloudflare_domain_operations.
"""
from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat

from .catalog import CatalogError
from .provider_paths import runtime_root
from .cloudflare import _token
from . import cloudflare_cli

API = "https://api.cloudflare.com/client/v4"
ID = re.compile(r"[0-9a-f]{32}")
DIGEST = re.compile(r"[0-9a-f]{64}")
TYPES = frozenset({"A", "AAAA", "CNAME", "TXT", "MX", "NS", "SRV", "CAA"})
SETTINGS = {"ssl": {"off", "flexible", "full", "strict"},
            "always_use_https": {"on", "off"},
            "min_tls_version": {"1.0", "1.1", "1.2", "1.3"}}
MAX_PAGES = 20
PAGE_SIZE = 50
MAX_RESPONSE = 2_097_152


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise CatalogError("canonical lowercase 32-hex identifier required")
    return value


def domain_name(value, *, record=False):
    # Explicit ASCII/Punycode avoids surprising normalization at confirmation.
    if not isinstance(value, str) or value != value.lower() or len(value) > 253:
        raise CatalogError("canonical ASCII domain required")
    labels = value.split(".")
    if record and labels[0]=='*':
        labels=labels[1:]
    pattern = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?" if record else r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if len(labels) < 2 or any(not re.fullmatch(pattern, part) for part in labels):
        raise CatalogError("canonical ASCII domain required; wildcard is allowed only as a literal DNS owner label")
    return value


def within(name, zone):
    return name == zone or name.endswith("." + zone)


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise CatalogError("integer parameter outside supported range")
    return value


def text_value(value, maximum=2048):
    try:
        valid = (isinstance(value, str) and bool(value) and len(value.encode()) <= maximum
                 and not any(ord(c) < 32 or ord(c) == 127 for c in value))
    except UnicodeError:
        valid = False
    if not valid:
        raise CatalogError("invalid bounded text parameter")
    return value


def dns_body(domain, record_type, name, content, ttl, proxied, priority,
             srv_port, srv_weight, srv_target, caa_flags, caa_tag, caa_value):
    domain_name(name, record=True)
    if not within(name, domain) or record_type not in TYPES:
        raise CatalogError("record must have a supported type and belong to exact zone")
    if type(ttl) is not int or (ttl != 1 and not 60 <= ttl <= 86400):
        raise CatalogError("TTL must be 1 (automatic) or 60..86400")
    if type(proxied) is not bool or (proxied and (record_type not in {"A", "AAAA", "CNAME"} or ttl != 1)):
        raise CatalogError("proxy requires A/AAAA/CNAME with automatic TTL")
    if record_type != "MX" and record_type != "SRV" and priority is not None:
        raise CatalogError("priority is only supported for MX/SRV")
    if record_type != "SRV" and any(v is not None for v in (srv_port, srv_weight, srv_target)):
        raise CatalogError("SRV fields require SRV type")
    if record_type != "CAA" and any(v is not None for v in (caa_flags, caa_tag, caa_value)):
        raise CatalogError("CAA fields require CAA type")
    body = {"type": record_type, "name": name, "ttl": ttl, "proxied": proxied}
    if record_type in {"SRV", "CAA"}:
        if content is not None:
            raise CatalogError("structured records require typed fields, not content")
        if record_type == "SRV":
            parts = name.split(".")
            if len(parts) < 4 or not parts[0].startswith("_") or parts[1] not in {"_tcp", "_udp", "_tls"}:
                raise CatalogError("SRV name requires service and protocol labels")
            body["data"] = {"port": integer(srv_port, 0, 65535),
                            "priority": integer(priority, 0, 65535),
                            "weight": integer(srv_weight, 0, 65535),
                            "target": domain_name(srv_target)}
        else:
            if not isinstance(caa_tag, str) or caa_tag not in {"issue", "issuewild"}:
                raise CatalogError("CAA supports issue/issuewild only; contact-bearing iodef is excluded")
            body["data"] = {"flags": integer(caa_flags, 0, 255),
                            "tag": caa_tag, "value": text_value(caa_value, 255)}
    else:
        text_value(content)
        if record_type in {"A", "AAAA"}:
            try:
                ip = ipaddress.ip_address(content)
                if ip.version != (4 if record_type == "A" else 6) or "%" in content:
                    raise ValueError()
                content = str(ip)
            except ValueError:
                raise CatalogError("record address does not match type") from None
        elif record_type in {"CNAME", "MX", "NS"}:
            domain_name(content)
            if record_type == "CNAME" and content == name:
                raise CatalogError("CNAME cannot point to itself")
        body["content"] = content
        if record_type == "MX":
            body["priority"] = integer(priority, 0, 65535)
    return body


def record_view(row):
    # DNS data can carry verification secrets, key material or contacts. Keep all
    # provider-controlled free text out of query results and journal artifacts.
    return {"id": row["id"], "name": row["name"], "type": row["type"],
            "ttl": row.get("ttl"), "proxied": row.get("proxied"),
            "snapshot_digest": digest(row), "content_redacted": True}


def query_record_view(row,show_values):
    if type(show_values) is not bool:raise CatalogError('show_values must be boolean')
    result=record_view(row)
    kind=row['type']
    if show_values and kind in {'A','AAAA','CNAME','MX','NS','SRV'}:
        if kind in {'A','AAAA'}:
            try:address=ipaddress.ip_address(row['content'])
            except ValueError:raise CatalogError('invalid DNS address content') from None
            if address.version!=(4 if kind=='A' else 6):raise CatalogError('DNS address type mismatch')
            result['content']=str(address)
        elif kind=='SRV':
            result['data']={k:row['data'][k] for k in ('port','priority','weight','target')}
        else:
            result['content']=domain_name(row['content'].lower().rstrip('.'))
            if kind=='MX':result['priority']=row['priority']
        result['content_redacted']=False
    return result


class CloudflareDomainsAdapter:
    provider = "cloudflare"
    query_methods = {"domains": "discover", "zone": "zone", "records": "records",
                     "record": "record", "setting": "setting", "dnssec": "dnssec",
                     "token": "verify_token", "reconcile": "reconcile"}

    def __init__(self, settings):
        if not isinstance(settings, dict) or set(settings) not in ({"credential_file", "account_id", "foundation_root"}, {"credential_file", "account_id", "foundation_root", "cli_root"}):
            raise CatalogError("invalid Cloudflare domains settings")
        self.account_id = identifier(settings["account_id"])
        for key in ("credential_file", "foundation_root"):
            value = settings[key]
            if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
                raise CatalogError("absolute credential and Foundation paths required")
        self.credential = Path(settings["credential_file"])
        self.foundation_root = Path(settings["foundation_root"])
        cli_root = settings["cli_root"] if "cli_root" in settings else str(runtime_root() / "providers/cloudflare-cli")
        if not isinstance(cli_root, str):
            raise CatalogError("absolute Cloudflare CLI root required")
        self.cli_root = Path(cli_root)
        if not self.cli_root.is_absolute() or ".." in self.cli_root.parts:
            raise CatalogError("absolute Cloudflare CLI root required")
        self._auth_token = None

    def _credential(self):
        try:
            if any(p.is_symlink() for p in (self.credential, *self.credential.parents)):
                raise ValueError()
            return _token(self.credential)
        except Exception:
            raise CatalogError("Cloudflare credential unavailable or unsafe") from None

    def _locator(self):
        return digest([str(self.credential), str(self.foundation_root)])

    def _bound(self):
        """Pin a validated token for this invocation, never a shared mutable context."""
        source = self.foundation_root / "scripts/foundation_operation.py"
        try:
            if any(p.is_symlink() for p in (source, *source.parents)):
                raise ValueError()
            info = source.stat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o022 or info.st_nlink != 1 or info.st_size > MAX_RESPONSE):
                raise ValueError()
            helper_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            token = self._credential()
            cli_hash = cloudflare_cli.installation(self.cli_root)[3]
        except Exception:
            raise CatalogError("Cloudflare credential or Foundation binding unavailable") from None
        bound = copy.copy(self)
        bound._auth_token = token
        return bound, {"locator": self._locator(), "credential_sha256": digest(token),
                       "journal_sha256": helper_hash, "cli_sha256": cli_hash}

    def _request(self, method, path, *, params=None, body=None):
        # Only internal callers; no request/path method appears in query_methods.
        zone = r"/zones/[0-9a-f]{32}"
        reads = (r"/zones", r"/user/tokens/verify", r"/accounts/[0-9a-f]{32}/tokens/verify", zone,
                 zone + r"/dns_records(?:/[0-9a-f]{32})?", zone + r"/dnssec",
                 zone + r"/settings/(?:ssl|always_use_https|min_tls_version)")
        writes = {"POST": (zone + r"/dns_records", zone + r"/purge_cache"),
                  "PATCH": (zone + r"/dns_records/[0-9a-f]{32}",
                            zone + r"/settings/(?:ssl|always_use_https|min_tls_version)"),
                  "DELETE": (zone + r"/dns_records/[0-9a-f]{32}",)}
        routes = reads if method == "GET" else writes.get(method, ())
        if not any(re.fullmatch(route, path) for route in routes):
            raise CatalogError("Cloudflare endpoint is not allowlisted")
        token = self._auth_token if self._auth_token is not None else self._credential()
        return cloudflare_cli.run(self.cli_root, token, self.account_id, method, path,
                                  params=params, body=body)

    def _zone_value(self, value):
        if not isinstance(value, dict) or not isinstance(value.get("account"), dict):
            raise CatalogError("incomplete zone identity")
        if value["account"].get("id") != self.account_id:
            raise CatalogError("Cloudflare zone account mismatch")
        result = {"id": identifier(value.get("id")), "name": domain_name(value.get("name")),
                  "account_id": self.account_id}
        if not isinstance(value.get("status"), str) or value["status"] not in {"initializing", "pending", "active", "moved", "deleted", "deactivated"}:
            raise CatalogError("incomplete zone status")
        if (not isinstance(value.get("type"), str) or value["type"] not in {"full", "partial", "secondary", "internal"}
                or type(value.get("paused")) is not bool):
            raise CatalogError("incomplete zone state")
        result.update(status=value["status"], type=value["type"], paused=value["paused"])
        if 'name_servers' in value:
            servers=value['name_servers']
            if not isinstance(servers,list) or len(servers)>12:
                raise CatalogError('invalid Cloudflare nameserver metadata')
            result['name_servers']=sorted({domain_name(n) for n in servers})
        return result

    def _owned(self, zone_id, domain):
        identifier(zone_id)
        domain_name(domain)
        row = self._zone_value(self._request("GET", f"/zones/{zone_id}").get("result"))
        if row["id"] != zone_id or row["name"] != domain:
            raise CatalogError("Cloudflare zone/domain confirmation mismatch")
        return row

    @staticmethod
    def _record_value(value, domain):
        if not isinstance(value, dict):
            raise CatalogError("incomplete DNS record")
        identifier(value.get("id"))
        name = value.get("name")
        # Existing wildcard records still count toward complete inventory/conflicts.
        # Wildcard is a literal DNS owner, never a deletion/search selector.
        domain_name(name[2:] if isinstance(name, str) and name.startswith("*.") else name, record=True)
        kind = value.get("type")
        if (not within(name, domain) or not isinstance(kind, str) or not re.fullmatch(r"[A-Z0-9]{1,16}", kind)
                or type(value.get("ttl")) is not int or value["ttl"] < 1
                or type(value.get("proxied")) is not bool):
            raise CatalogError("incomplete or mismatched DNS record")
        if kind in TYPES:
            if kind in {"SRV", "CAA"}:
                required = {"port", "priority", "weight", "target"} if kind == "SRV" else {"flags", "tag", "value"}
                if not isinstance(value.get("data"), dict) or not required <= value["data"].keys():
                    raise CatalogError("incomplete structured DNS record")
                data = value["data"]
                if kind == "SRV":
                    for key in ("port", "priority", "weight"):
                        integer(data[key], 0, 65535)
                    if data["target"] != ".":
                        domain_name(data["target"])
                else:
                    integer(data["flags"], 0, 255)
                    text_value(data["tag"], 255)
                    text_value(data["value"], 2048)
            elif not isinstance(value.get("content"), str):
                raise CatalogError("incomplete DNS record content")
            if kind == "MX":
                integer(value.get("priority"), 0, 65535)
        return copy.deepcopy(value)

    def _pages(self, path, params, validate):
        rows, seen, total = [], set(), None
        pages = 0
        status = "unavailable"
        try:
            for page in range(1, MAX_PAGES + 1):
                value = self._request("GET", path, params={**params, "page": page, "per_page": PAGE_SIZE})
                info, batch = value.get("result_info"), value.get("result")
                if not isinstance(info, dict) or not isinstance(batch, list):
                    raise CatalogError("pagination unavailable")
                for field in ("page", "per_page", "count", "total_count"):
                    if type(info.get(field)) is not int:
                        raise CatalogError("pagination incomplete")
                count = info["total_count"]
                expected_pages = max(1, (count + PAGE_SIZE - 1) // PAGE_SIZE)
                if (info["page"] != page or info["per_page"] != PAGE_SIZE or count < 0
                        or info["count"] != len(batch) or len(batch) > PAGE_SIZE
                        or ("total_pages" in info and (type(info["total_pages"]) is not int
                            or info["total_pages"] not in ({0, 1} if count == 0 else {expected_pages})))
                        or (total is not None and total != count)):
                    raise CatalogError("pagination changed or incomplete")
                valid = [validate(row) for row in batch]
                ids = [row["id"] for row in valid]
                if len(set(ids)) != len(ids) or seen.intersection(ids):
                    raise CatalogError("duplicate page identities")
                total = count
                rows.extend(valid)
                seen.update(ids)
                pages = page
                status = "partial"
                if len(rows) == total and page == expected_pages:
                    status = "complete"
                    break
                if len(rows) >= total or len(batch) != PAGE_SIZE or page >= expected_pages:
                    break
        except CatalogError:
            pass
        return {"status": status, "items": rows, "pages": pages, "total_count": total,
                "coverage": "bounded token-visible API pages; not an atomic provider snapshot"}

    def discover(self):
        return self._pages("/zones", {"account.id": self.account_id, "match": "all", "order": "name", "direction": "asc"}, self._zone_value)

    def zone(self, zone_id: str, domain: str):
        return {"status": "complete", "zone": self._owned(zone_id, domain)}

    def _records(self, zone_id, domain):
        self._owned(zone_id, domain)
        result = self._pages(f"/zones/{zone_id}/dns_records", {"order": "name", "direction": "asc"},
                             lambda row: self._record_value(row, domain))
        return result

    def records(self, zone_id: str, domain: str, show_values: bool = False):
        if type(show_values) is not bool:
            raise CatalogError("show_values must be a boolean")
        result = self._records(zone_id, domain)
        return {**result, "items": [query_record_view(row,show_values) for row in result["items"]]}

    def _record(self, zone_id, domain, record_id):
        identifier(record_id)
        row = self._record_value(self._request("GET", f"/zones/{zone_id}/dns_records/{record_id}").get("result"), domain)
        if row["id"] != record_id:
            raise CatalogError("DNS record identity mismatch")
        return row

    def record(self, zone_id: str, domain: str, record_id: str, show_values: bool = False):
        if type(show_values) is not bool:
            raise CatalogError("show_values must be a boolean")
        self._owned(zone_id, domain)
        return {"status": "complete", "record": query_record_view(self._record(zone_id, domain, record_id),show_values)}

    def _setting(self, zone_id, setting_id):
        if not isinstance(setting_id, str) or setting_id not in SETTINGS:
            raise CatalogError("unsupported zone setting")
        row = self._request("GET", f"/zones/{zone_id}/settings/{setting_id}").get("result")
        if (not isinstance(row, dict) or row.get("id") != setting_id
                or not isinstance(row.get("value"), str) or row["value"] not in SETTINGS[setting_id]
                or type(row.get("editable")) is not bool):
            raise CatalogError("zone setting identity/value/editability incomplete")
        return {"id": setting_id, "value": row["value"], "editable": row["editable"],
                "snapshot_digest": digest(row)}

    def setting(self, zone_id: str, domain: str, setting_id: str):
        self._owned(zone_id, domain)
        return {"status": "complete", "setting": self._setting(zone_id, setting_id)}

    def dnssec(self, zone_id: str, domain: str):
        self._owned(zone_id, domain)
        row = self._request("GET", f"/zones/{zone_id}/dnssec").get("result")
        if (not isinstance(row, dict) or not isinstance(row.get("status"), str)
                or row["status"] not in {"active", "disabled", "pending", "pending-disabled", "error"}):
            raise CatalogError("DNSSEC status unavailable")
        return {"status": "complete", "dnssec_status": row["status"], "key_data_redacted": True,
                "activation_supported": False, "delegation_verified": False}

    def verify_token(self):
        kind='account'
        try:row = self._request("GET", f"/accounts/{self.account_id}/tokens/verify").get("result")
        except CatalogError:
            kind='user'
            row = self._request("GET", "/user/tokens/verify").get("result")
        if (not isinstance(row, dict) or not isinstance(row.get("status"), str)
                or row["status"] not in {"active", "disabled", "expired"}):
            raise CatalogError("token verification unavailable")
        identifier(row.get("id"))
        return {"status": "complete", "token_status": row["status"], "token_kind":kind,"permissions_verified": False}

    def reconcile(self, operation_id: str):
        from .cloudflare_domain_operations import observe_operation
        return observe_operation(self, operation_id)
