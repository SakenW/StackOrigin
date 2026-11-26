"""Bounded Spaceship queries and private, finite domain mutation transport."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from typing import Any
from uuid import UUID

from .catalog import CatalogError
from .spaceship import _read_credentials
from . import spaceship_mcp

BASE = "https://spaceship.dev/api/v1"
MAX_OUTPUT = 1_048_576
ASYNC_ID = re.compile(r"[a-zA-Z0-9]{1,36}")
DIGEST = re.compile(r"[0-9a-f]{64}")
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
RECORD_FIELDS = {"A": ("address",), "AAAA": ("address",),
                 "CNAME": ("cname",), "TXT": ("value",),
                 "MX": ("exchange", "preference"), "CAA": ("flag", "tag", "value"),
                 "SRV": ("service", "protocol", "priority", "weight", "port", "target"),
                 "ALIAS": ("aliasName",), "NS": ("nameserver",), "PTR": ("pointer",),
                 "HTTPS": ("svcPriority", "targetName", "svcParams"),
                 "SVCB": ("svcPriority", "targetName", "svcParams"),
                 "TLSA": ("port", "protocol", "usage", "selector", "matching", "associationData")}
WRITE_TYPES = frozenset(RECORD_FIELDS) - {"HTTPS", "SVCB", "TLSA"}
ACTIONS = ("nameservers", "dns-add", "dns-delete", "dns-update-ttl",
           "auto-renew", "transfer-lock", "privacy", "dns-replace", "receipt-resolve")
OPERATIONS = frozenset(f"spaceship.{action}.v1" for action in ACTIONS)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=True).encode()).hexdigest()


def canonical_uuid(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        raise CatalogError("canonical operation UUID required") from None
    return value


def domain_name(value: str) -> str:
    # Require the reviewed ASCII A-label, rather than silently changing a target.
    if (not isinstance(value, str) or not 4 <= len(value) <= 253 or
            value != value.lower() or "." not in value or
            any(not LABEL.fullmatch(part) for part in value.split("."))):
        raise CatalogError("canonical lower-case ASCII domain required")
    try:
        for label in value.split("."):
            if label.startswith("xn--") and label.encode().decode("idna").encode("idna").decode() != label:
                raise ValueError
    except (UnicodeError, ValueError):
        raise CatalogError("invalid domain A-label") from None
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise CatalogError("domain cannot be an IP address")


def record_name(value: str) -> str:
    # Preserve the exact reviewed spelling. Never lowercase or convert IDNA on read.
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise CatalogError("invalid record hostname")
    if value in {"@", "*"}:
        return value
    parts = value.split(".")
    if parts[0] in {"*", "_"}:
        parts = parts[1:]
    if not parts:
        raise CatalogError("invalid record hostname")
    for part in parts:
        if (not 1 <= len(part) <= 63 or not re.fullmatch(r"[\w-]+", part) or
                part.startswith("-") or part.endswith("-") or
                not re.search(r"[^\W_]", part)):
            raise CatalogError("invalid record hostname")
        try:
            if part.lower().startswith("xn--"):
                part.lower().encode("ascii").decode("idna")
            elif not part.isascii():
                part.encode("idna")
        except UnicodeError:
            raise CatalogError("invalid record IDNA label") from None
    return value


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise CatalogError("record integer outside allowed range")


def _text(value, minimum, maximum):
    if (not isinstance(value, str) or not minimum <= len(value) <= maximum or
            any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise CatalogError("invalid bounded record text")


def _service(value):
    if not isinstance(value, str) or not re.fullmatch(r"_[a-zA-Z0-9-]{1,62}", value):
        raise CatalogError("invalid service or protocol")


def record(value: dict, *, ttl: bool = True, reading: bool = False) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise CatalogError("one typed DNS record required")
    kind = value["type"]
    fields = RECORD_FIELDS.get(kind)
    if fields is None or (not reading and kind not in WRITE_TYPES):
        raise CatalogError("unsupported writable record type")
    required = {"type", "name", *fields}
    if ttl and not reading:
        required.add("ttl")
    optional = ({"ttl"} if reading else set()) | ({"port", "scheme"} if kind in {"HTTPS", "SVCB"} else set())
    if not required <= set(value) or set(value) - required - optional:
        raise CatalogError("unsupported record fields")
    result = dict(value)
    record_name(result["name"])
    if "ttl" in result:
        _integer(result["ttl"], 60, 3600)
    if kind in {"A", "AAAA"}:
        try:
            if not isinstance(result["address"], str) or "%" in result["address"]:
                raise ValueError
            address = ipaddress.ip_address(result["address"])
            if address.version != (4 if kind == "A" else 6):
                raise ValueError
        except ValueError:
            raise CatalogError("invalid record IP address") from None
    for field in {"CNAME": ("cname",), "MX": ("exchange",), "ALIAS": ("aliasName",),
                  "NS": ("nameserver",), "PTR": ("pointer",), "SRV": ("target",)}.get(kind, ()):
        record_name(result[field])
    if kind == "CNAME" and result["name"] == "@" and not reading:
        raise CatalogError("apex CNAME is not supported")
    if kind == "TXT":
        text = result["value"]
        _text(text, 1, 65535 if reading else 4096)
        if not reading and (not text.strip() or len(text.encode("utf-8")) > 4096 or
                            any(ord(c) < 32 or ord(c) == 127 for c in text)):
            raise CatalogError("TXT requires 1..4096 bytes without control characters")
    elif kind == "MX":
        _integer(result["preference"], 0, 65535)
    elif kind == "CAA":
        _integer(result["flag"], 0, 128)
        if result["flag"] not in (0, 128) or result["tag"] not in ("issue", "issuewild", "iodef"):
            raise CatalogError("invalid CAA flag or tag")
        _text(result["value"], 1, 256)
        if not re.fullmatch(r"[ -~]+", result["value"]):
            raise CatalogError("invalid CAA value")
    elif kind == "SRV":
        for field in ("service", "protocol"):
            _service(result[field])
        for field in ("priority", "weight", "port"):
            _integer(result[field], 1 if field == "port" else 0, 65535)
    elif kind in {"HTTPS", "SVCB"}:
        _integer(result["svcPriority"], 0, 65535)
        if result["targetName"] != ".":
            record_name(result["targetName"].removesuffix(".") if isinstance(result["targetName"], str) else None)
        _text(result["svcParams"], 0, 65535)
        if "scheme" in result:
            _service(result["scheme"])
            if kind == "HTTPS" and result["scheme"] != "_https":
                raise CatalogError("invalid HTTPS scheme")
        if "port" in result and "scheme" not in result:
            raise CatalogError("record port requires scheme")
    elif kind == "TLSA":
        _service(result["protocol"])
        for field in ("usage", "selector", "matching"):
            _integer(result[field], 0, 255)
        _text(result["associationData"], 64, 65535)
        if not re.fullmatch(r"[0-9a-f]{2}(?:\s?[0-9a-f]{2})*", result["associationData"]):
            raise CatalogError("invalid TLSA association data")
    if kind in {"HTTPS", "SVCB", "TLSA"} and "port" in result:
        port = result["port"]
        if port != "*" and (not isinstance(port, str) or not re.fullmatch(r"_[0-9]{1,5}", port)
                            or not 1 <= int(port[1:]) <= 65535):
            raise CatalogError("invalid underscored port")
    return result


def record_key(value: dict) -> str:
    return digest({k: v for k, v in value.items() if k not in {"ttl", "group"}})


def dns_owner(value: dict) -> str:
    name = value["name"]
    if value["type"] == "SRV":
        name = ".".join([value["service"], value["protocol"], *([] if name == "@" else [name])])
    elif value["type"] in {"TLSA", "HTTPS", "SVCB"}:
        name = ".".join([value[k] for k in ("port", "protocol", "scheme") if k in value] +
                        ([] if name == "@" else [name])) or "@"
    return ".".join(label.encode("idna").decode("ascii").lower() for label in name.split("."))


def record_identity(value: dict) -> str:
    # Only used to REJECT ambiguous aliases/overwrites. Wire selectors and stored
    # snapshots retain exact spelling, including TXT/CAA content and wildcards.
    result = {k: v for k, v in value.items() if k not in {"ttl", "group"}}
    result["name"] = dns_owner(value)
    for field in ("cname", "exchange", "aliasName", "nameserver", "pointer", "target", "targetName"):
        if field in result:
            result[field] = ".".join(p.encode("idna").decode("ascii").lower() for p in result[field].split("."))
    for field in ("service", "protocol", "scheme"):
        if field in result:
            result[field] = result[field].lower()
    if "address" in result:
        result["address"] = str(ipaddress.ip_address(result["address"]))
    if result["type"] == "CAA":
        # Vendor tuple matching is case-insensitive for non-TXT content.
        result["value"] = result["value"].lower()
    return digest(result)


def parameters(operation: str, values: dict) -> tuple[str, dict]:
    if not isinstance(operation, str) or operation not in OPERATIONS or not isinstance(values, dict):
        raise CatalogError("unsupported Spaceship operation")
    action = operation.removeprefix("spaceship.").removesuffix(".v1")
    if action == "receipt-resolve":
        if set(values) != {"original_operation_id", "mode"} or values["mode"] not in ("certify-success", "adopt-observed"):
            raise CatalogError("explicit original UUID and resolution mode required")
        canonical_uuid(values["original_operation_id"])
        return action, dict(values)
    if action == "dns-replace":
        if set(values) != {"record", "replacement"}:
            raise CatalogError("exact old record and replacement required")
        old, new = record(values["record"]), record(values["replacement"])
        if (old["name"], old["type"]) != (new["name"], new["type"]) or record_identity(old) == record_identity(new):
            raise CatalogError("replace requires same exact owner/type and different content; use TTL operation for TTL")
        if old["type"] == "SRV" and any(old[k] != new[k] for k in ("service", "protocol")):
            raise CatalogError("replace requires same SRV owner service/protocol")
        return action, {"record": old, "replacement": new}
    if action.startswith("dns-"):
        expected = {"record", "ttl"} if action == "dns-update-ttl" else {"record"}
        if set(values) != expected:
            raise CatalogError("exact DNS parameters required")
        result = {"record": record(values["record"], ttl=action != "dns-delete")}
        if action == "dns-update-ttl":
            new_ttl = values["ttl"]
            if type(new_ttl) is not int or not 60 <= new_ttl <= 3600:
                raise CatalogError("TTL must be an integer between 60 and 3600")
            result["ttl"] = new_ttl
        return action, result
    if action == "nameservers":
        provider = values.get("provider")
        if provider == "basic" and set(values) == {"provider"}:
            return action, {"provider": "basic"}
        if provider == "custom" and set(values) == {"provider", "hosts"}:
            hosts = values["hosts"]
            if isinstance(hosts, list) and 2 <= len(hosts) <= 12:
                hosts = [domain_name(host) for host in hosts]
                if len(set(hosts)) == len(hosts):
                    return action, {"provider": provider, "hosts": sorted(hosts)}
        raise CatalogError("basic omits hosts; custom requires 2..12 unique canonical hosts")
    field = {"auto-renew": "isEnabled", "transfer-lock": "isLocked"}.get(action)
    if field and set(values) == {field} and type(values[field]) is bool:
        return action, dict(values)
    if (action == "privacy" and set(values) == {"privacyLevel", "userConsent"} and
            values["privacyLevel"] in ("public", "high") and values["userConsent"] is True):
        return action, dict(values)
    raise CatalogError("invalid typed domain parameters")


@dataclass(frozen=True)
class Response:
    status: int
    data: Any = None
    async_id: str | None = None


def _curl(config: bytes) -> bytes:
    """Hard cap combined headers/body in memory; no stderr or inherited secrets."""
    try:
        with subprocess.Popen(["/usr/bin/curl", "-q", "--config", "-"], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              env={"PATH": "/usr/bin:/bin", "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"}) as process:
            try:
                output = bytearray()
                pending = memoryview(config)
                deadline = time.monotonic() + 20
                with selectors.DefaultSelector() as selector:
                    os.set_blocking(process.stdin.fileno(), False)
                    os.set_blocking(process.stdout.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                    selector.register(process.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise CatalogError("Spaceship request timed out")
                        for key, _ in selector.select(min(remaining, 0.5)):
                            if key.fileobj is process.stdin:
                                size = os.write(key.fd, pending[:4096])
                                pending = pending[size:]
                                if not pending:
                                    selector.unregister(process.stdin)
                                    process.stdin.close()
                            else:
                                chunk = os.read(key.fd, 65536)
                                if not chunk:
                                    selector.unregister(process.stdout)
                                output.extend(chunk)
                                if len(output) > MAX_OUTPUT:
                                    raise CatalogError("Spaceship response exceeded limit")
                if process.wait(timeout=max(0.01, deadline - time.monotonic())) != 0:
                    raise CatalogError("Spaceship transport unavailable")
                return bytes(output)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    except (OSError, subprocess.SubprocessError):
        raise CatalogError("Spaceship transport unavailable") from None


class SpaceshipManagementAdapter:
    provider = "spaceship"
    query_methods = {"domains": "discover", "domain": "inspect", "nameservers": "nameservers",
                     "dns-records": "dns_records", "operation-status": "operation_status"}

    def __init__(self, settings: dict[str, Any]):
        if not isinstance(settings, dict) or set(settings) not in ({"credential_file", "foundation_root"},
                {"credential_file", "foundation_root", "mcp_store"}):
            raise CatalogError("invalid Spaceship management settings")
        for value in settings.values():
            if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
                raise CatalogError("absolute management paths required")
        self.credential = Path(settings["credential_file"])
        self.foundation_root = Path(settings["foundation_root"])
        self.mcp_store = Path(settings['mcp_store']) if 'mcp_store' in settings else None

    @property
    def dns_upstream(self):
        return "official-mcp" if self.mcp_store else "official-api"

    @property
    def query_upstreams(self):
        return {"dns-records": self.dns_upstream}

    def _credentials(self):
        try:
            return _read_credentials(self.credential)
        except Exception:
            raise CatalogError("Spaceship credential unavailable") from None

    def _paths(self):
        return {"credential_path": digest(str(self.credential)),
                "foundation_path": digest(str(self.foundation_root))}

    def _binding(self, credentials):
        source = self.foundation_root / "scripts/foundation_operation.py"
        try:
            if (any(p.is_symlink() for p in [source, *source.parents]) or
                    not source.is_file() or source.stat().st_uid != os.getuid() or
                    source.stat().st_mode & 0o022 or source.stat().st_size > 262144):
                raise ValueError
            helper_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        except (OSError, ValueError):
            raise CatalogError("Foundation journal could not be verified") from None
        return {**self._paths(), "credential_sha256": digest(list(credentials)),
                "api_key_sha256": digest(credentials[0]), "foundation_sha256": helper_hash,
                "official_mcp_store": digest(str(self.mcp_store)) if self.mcp_store else None,
                "official_mcp_binding": spaceship_mcp.binding(self.mcp_store) if self.mcp_store else None}

    def _request(self, method, path, credentials, body=None):
        # No public endpoint/body surface. Even private callers must use fixed routes.
        query = re.fullmatch(r"/(?:domains|dns/records/[a-z0-9.-]+)\?take=(?:100|500)&skip=[0-9]+&orderBy=name", path)
        detail = re.fullmatch(r"/domains/([a-z0-9.-]+)", path)
        mutation = re.fullmatch(r"/domains/([a-z0-9.-]+)/(nameservers|autorenew|transfer/lock|privacy/preference)", path)
        dns = re.fullmatch(r"/dns/records/([a-z0-9.-]+)", path)
        asynchronous = re.fullmatch(r"/async-operations/([a-zA-Z0-9]{1,36})", path)
        if not ((method == "GET" and body is None and (query or detail or asynchronous)) or
                (method == "PUT" and mutation) or (method in {"PUT", "DELETE"} and dns)):
            raise CatalogError("Spaceship endpoint is not allowlisted")
        # Routing is explicit. MCP failure never triggers a REST write fallback.
        if self.dns_upstream == 'official-mcp' and (not mutation or mutation[2] == 'nameservers'):
            return self._mcp_request(method, path, body)
        def quote(value):
            return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
        key, secret = credentials
        if any(not isinstance(v, str) or not v or len(v) > 512 or
               any(ord(c) < 33 or ord(c) > 126 for c in v) for v in (key, secret)):
            raise CatalogError("invalid Spaceship credentials")
        lines = ['silent', 'proto = "=https"', 'proto-redir = "=https"', 'max-redirs = 0',
                 'noproxy = "*"', 'connect-timeout = 5', 'max-time = 15', 'max-filesize = 1048576',
                 'include', 'request = ' + quote(method), 'url = ' + quote(BASE + path),
                 'header = ' + quote("X-API-Key: " + key), 'header = ' + quote("X-API-Secret: " + secret),
                 'header = "Accept: application/json"']
        if body is not None:
            lines += ['header = "Content-Type: application/json"',
                      'data = ' + quote(json.dumps(body, ensure_ascii=True, separators=(",", ":")))]
        raw = _curl(("\n".join(lines) + "\n").encode("ascii"))
        if len(raw) > MAX_OUTPUT:
            raise CatalogError("Spaceship response exceeded limit")
        # Consume only bounded HTTP informational blocks, never a redirect.
        for _ in range(5):
            headers, separator, payload = raw.partition(b"\r\n\r\n")
            if not separator or len(headers) > 32768:
                raise CatalogError("invalid Spaceship response headers")
            rows = headers.split(b"\r\n")
            match = re.fullmatch(rb"HTTP/(?:1\.[01]|2|3) ([0-9]{3})(?: [^\r\n]*)?", rows[0])
            if not match:
                raise CatalogError("invalid Spaceship HTTP status")
            status = int(match[1])
            if 100 <= status < 200:
                raw = payload
                continue
            break
        else:
            raise CatalogError("too many informational responses")
        if status not in ({200} if method == "GET" else {200, 202, 204}):
            raise CatalogError(f"Spaceship API HTTP {status}; details suppressed")
        ids = [row.split(b":", 1)[1].strip() for row in rows[1:]
               if row.lower().startswith(b"spaceship-async-operationid:")]
        async_id = None
        if ids:
            if len(ids) != 1 or not re.fullmatch(rb"[a-zA-Z0-9]{1,36}", ids[0]):
                raise CatalogError("invalid Spaceship async identifier")
            async_id = ids[0].decode("ascii")
        if status == 202 and async_id is None:
            raise CatalogError("accepted request lacks async identifier; reconcile")
        data = None
        if method == "GET":
            try:
                data = json.loads(payload)
            except (ValueError, UnicodeError):
                raise CatalogError("invalid Spaceship response JSON") from None
            if not isinstance(data, dict):
                raise CatalogError("unexpected Spaceship response shape")
        return Response(status, data, async_id)

    def _mcp_request(self, method, path, body):
        from urllib.parse import parse_qs, urlsplit
        url = urlsplit(path)
        query = {k: values[0] for k, values in parse_qs(url.query).items()}
        if method == 'GET' and url.path == '/domains':
            name = 'domains_list'; args = {k: int(v) if k in {'take', 'skip'} else [v] for k, v in query.items()}
        elif method == 'GET' and url.path.startswith('/domains/'):
            name, args = 'domains_list', {'domain': url.path.rsplit('/', 1)[1]}
        elif method == 'GET' and url.path.startswith('/dns/records/'):
            name = 'dns_records_get'; args = {'domainName': url.path.rsplit('/', 1)[1],
                **{k: int(v) if k in {'take', 'skip'} else [v] for k, v in query.items()}}
        elif method == 'GET' and url.path.startswith('/async-operations/'):
            name, args = 'async_operation_get', {'operationId': url.path.rsplit('/', 1)[1]}
        elif method == 'PUT' and url.path.endswith('/nameservers'):
            name = 'domain_set_nameservers'; args = {'domainName': url.path.split('/')[2], **body}
            if args.get('provider') == 'basic': args.pop('hosts', None)
        elif method in {'PUT', 'DELETE'} and url.path.startswith('/dns/records/'):
            name = 'dns_records_save' if method == 'PUT' else 'dns_records_delete'
            args = {'domainName': url.path.rsplit('/', 1)[1], 'records': body['items'], 'force': body['force']} if method == 'PUT' else {
                'domainName': url.path.rsplit('/', 1)[1], 'records': body}
        else:
            raise CatalogError('Spaceship official MCP route is not allowlisted')
        value = spaceship_mcp.call(self.mcp_store, name, args)
        if name == 'domains_list' and 'domain' in args:
            items = value.get('items')
            if (not isinstance(items, list) or len(items) != 1 or value.get('total') != 1 or
                    items[0].get('name') != args['domain']):
                raise CatalogError('Spaceship MCP domain identity is incomplete')
            value = items[0]
        if method != 'GET':
            if ('operationId' in value or value.get('status') in
                    {'pending','failed','confirmation_required'}):
                raise CatalogError('Spaceship MCP returned an unexpected pending result; reconcile without replay')
            # The documented NS/DNS tools complete synchronously. Readback remains
            # the journal's independent check; never invent an async operation id.
            if name == 'domain_set_nameservers' and value != {k: v for k, v in args.items() if k != 'domainName'}:
                if args.get('provider') != 'basic' or value.get('provider') != 'basic':
                    raise CatalogError('Spaceship MCP nameserver result differs from reviewed target')
            return Response(200, None, None)
        return Response(200, value, None)

    @staticmethod
    def _domain(value, expected=None):
        if not isinstance(value, dict):
            raise CatalogError("invalid domain details")
        name = domain_name(value.get("name"))
        if expected is not None and name != expected:
            raise CatalogError("Spaceship target mismatch")
        result = {"name": name}
        if "autoRenew" in value:
            if type(value["autoRenew"]) is not bool:
                raise CatalogError("invalid autorenew state")
            result["autoRenew"] = value["autoRenew"]
        if "eppStatuses" in value:
            statuses = value["eppStatuses"]
            if (not isinstance(statuses, list) or len(statuses) > 40 or
                    any(not isinstance(s, str) or not re.fullmatch(r"[a-zA-Z]{1,64}", s) for s in statuses)):
                raise CatalogError("invalid EPP status")
            result["transferLocked"] = "clientTransferProhibited" in statuses
        if "privacyProtection" in value:
            privacy = value["privacyProtection"]
            if not isinstance(privacy, dict) or privacy.get("level") not in ("public", "high"):
                raise CatalogError("invalid privacy state")
            result["privacyLevel"] = privacy["level"]
        if "nameservers" in value:
            ns = value["nameservers"]
            if not isinstance(ns, dict) or ns.get("provider") not in ("basic", "custom"):
                raise CatalogError("invalid nameserver state")
            hosts = ns.get("hosts")
            if not isinstance(hosts, list) or not 2 <= len(hosts) <= 12:
                raise CatalogError("incomplete nameserver state")
            hosts = sorted(domain_name(h) for h in hosts)
            if len(set(hosts)) != len(hosts):
                raise CatalogError("duplicate nameservers")
            result["nameservers"] = {"provider": ns["provider"], "hosts": hosts}
        return result

    @staticmethod
    def _record(value):
        if not isinstance(value, dict) or not isinstance(value.get("type"), str):
            raise CatalogError("invalid DNS record")
        group = value.get("group")
        if not isinstance(group, dict) or set(group) != {"type"} or group["type"] not in ("custom", "product", "personalNs"):
            raise CatalogError("DNS record lacks a known ownership group")
        result = record({k: v for k, v in value.items() if k != "group"}, reading=True)
        result["group"] = group["type"]
        return result

    def _pages(self, path, credentials, max_pages, *, dns=False):
        if type(max_pages) is not int or not 1 <= max_pages <= 10:
            raise CatalogError("max_pages must be 1..10")
        rows, seen, total, status = [], set(), None, "unavailable"
        take = 500 if dns else 100
        for _ in range(max_pages):
            try:
                data = self._request("GET", f"{path}?take={take}&skip={len(rows)}&orderBy=name", credentials).data
                items, count = data.get("items"), data.get("total")
                if (not isinstance(items, list) or len(items) > take or type(count) is not int or
                        count < 0 or (total is not None and total != count)):
                    raise CatalogError("unstable page metadata")
                batch = [self._record(v) if dns else self._domain(v) for v in items]
                if not dns:
                    # Registration dates belong to inventory, not the operation state digest.
                    for row, original in zip(batch, items):
                        expires = original.get("expirationDate")
                        if isinstance(expires, str) and len(expires) <= 40:
                            row["expirationDate"] = expires
                keys = [digest(v) if dns else v["name"] for v in batch]
                if len(keys) != len(set(keys)) or seen.intersection(keys) or len(rows) + len(batch) > count:
                    raise CatalogError("duplicate or inconsistent pages")
                total = count
                seen.update(keys)
                rows.extend(batch)
                status = "complete" if len(rows) == total else "partial"
                if status == "complete" or not batch:
                    break
            except (CatalogError, TypeError, AttributeError):
                status = "partial" if rows else "unavailable"
                break
        return {"provider": self.provider, "status": status, "data": rows,
                "returned_count": len(rows), "total_count": total,
                "coverage": "bounded live pages; no transactional pagination guarantee"}

    def discover(self, max_pages: int = 10) -> dict:
        return self._pages("/domains", self._credentials(), max_pages)

    def inspect(self, domain: str) -> dict:
        domain_name(domain)
        data = self._domain(self._request("GET", f"/domains/{domain}", self._credentials()).data, domain)
        return {"provider": self.provider, "domain": domain, "status": "complete", "data": data}

    def nameservers(self, domain: str) -> dict:
        result = self.inspect(domain)
        ns = result["data"].get("nameservers")
        return {**result, "status": "complete" if ns else "unavailable", "data": ns}

    def dns_records(self, domain: str, max_pages: int = 10) -> dict:
        domain_name(domain)
        credentials = self._credentials()
        self._domain(self._request("GET", f"/domains/{domain}", credentials).data, domain)
        return {**self._pages(f"/dns/records/{domain}", credentials, max_pages, dns=True), "domain": domain}

    def _snapshot(self, domain, action, credentials):
        data = self._domain(self._request("GET", f"/domains/{domain}", credentials).data, domain)
        field = {"nameservers": "nameservers", "auto-renew": "autoRenew",
                 "transfer-lock": "transferLocked", "privacy": "privacyLevel"}.get(action)
        if field and field not in data:
            raise CatalogError("required domain readback field is unavailable")
        if action.startswith("dns-"):
            if "nameservers" not in data:
                raise CatalogError("complete nameserver snapshot unavailable")
            listing = self._pages(f"/dns/records/{domain}", credentials, 10, dns=True)
            if listing["status"] != "complete":
                raise CatalogError("complete DNS snapshot unavailable")
            data["records"] = sorted(listing["data"], key=digest)
        return data

    def _dispatch(self, domain, operation, values, credentials):
        domain_name(domain)
        action, values = parameters(operation, values)
        if action in {"dns-replace", "receipt-resolve"}:
            raise CatalogError("operation requires the journal orchestrator")
        if action.startswith("dns-"):
            item = dict(values["record"])
            if action == "dns-update-ttl":
                item["ttl"] = values["ttl"]
            return self._request("DELETE" if action == "dns-delete" else "PUT", f"/dns/records/{domain}",
                                 credentials, [item] if action == "dns-delete" else {"force": False, "items": [item]})
        suffix = {"nameservers": "nameservers", "auto-renew": "autorenew",
                  "transfer-lock": "transfer/lock", "privacy": "privacy/preference"}[action]
        return self._request("PUT", f"/domains/{domain}/{suffix}", credentials, values)

    def operation_status(self, domain: str, operation_id: str) -> dict:
        # The provider ID is taken exclusively from the retained local receipt.
        from .spaceship_domain_operations import observe_operation
        return observe_operation(self, domain, operation_id)
