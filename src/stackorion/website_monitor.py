"""Registered HTTPS probes using bounded reads and the observed public address."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import http.client
import io
import ipaddress
import json
import math
import re
import socket
import ssl
import time
from urllib.parse import urlsplit

from .catalog import CatalogError
from .domain_observation import dig, hostname


MAX_BODY = 65536
MAX_MONITORS = 100
CERTIFICATE_WARNING_DAYS = 14
CERTIFICATE_URGENT_DAYS = 7
TARGET_ID = re.compile(r"[a-z][a-z0-9-]{1,63}")


@dataclass(frozen=True)
class Monitor:
    id: str
    url: str
    expected_status: int
    contains: str | None = None
    revision: int = 0

    def __post_init__(self):
        if not isinstance(self.id, str) or not TARGET_ID.fullmatch(self.id):
            raise ValueError("Invalid monitor ID")
        if (not isinstance(self.url, str) or len(self.url) > 2048
                or not re.fullmatch(r"https://[a-z0-9.-]+(?:/[A-Za-z0-9._~/%+-]*)?", self.url)):
            raise ValueError("Monitor URL must be canonical HTTPS on port 443")
        hostname(urlsplit(self.url).hostname)
        if type(self.expected_status) is not int or not 100 <= self.expected_status <= 599:
            raise ValueError("Invalid expected HTTP status")
        if self.contains is not None and (not isinstance(self.contains, str) or not self.contains
                                          or len(self.contains.encode("utf-8")) > 512):
            raise ValueError("Invalid content assertion")

    @property
    def key(self):
        definition = [self.id, self.url, self.expected_status, self.contains]
        if self.revision:
            definition.append(self.revision)
        return sha256(json.dumps(definition).encode()).hexdigest()

    def public(self):
        return {"id": self.id, "url": self.url, "expected_status": self.expected_status,
                "checks_content": self.contains is not None, "revision": self.revision}

    @classmethod
    def from_registration(cls, url, expected_status, contains):
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError("Invalid monitor URL")
        scheme, separator, address = url.strip().partition("://")
        if scheme.lower() != "https" or not separator:
            raise ValueError("Invalid monitor URL")
        host, _, path = address.partition("/")
        url = f"https://{host.lower()}/{path}"
        try:
            return cls("web-" + sha256(url.encode()).hexdigest()[:32], url, expected_status, contains)
        except CatalogError as exc:
            raise ValueError("Invalid monitor hostname") from exc


DEMO_MONITOR = Monitor("demo-website", "https://example.com/health", 200, "stackorion-ok")


def load_monitors(path):
    if path is None:
        return {}
    if path.stat().st_size > 65536:
        raise ValueError("Website monitor file exceeds its size limit")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(data, list) or len(data) > MAX_MONITORS
            or not all(isinstance(row, dict) and "revision" not in row for row in data)):
        raise ValueError("Invalid website monitor list")
    try:
        monitors = [Monitor(**row) for row in data]
    except (CatalogError, TypeError) as exc:
        raise ValueError("Invalid website monitor definition") from exc
    result = {monitor.id: monitor for monitor in monitors}
    if len(result) != len(monitors) or DEMO_MONITOR.id in result:
        raise ValueError("Duplicate or reserved monitor ID")
    return result


def observation(observed_at, phases, certificate=None, *, status="unknown", **fields):
    return {"observed_at": observed_at, "status": status, "phases": phases,
            "certificate": certificate, "http_status": None, "content": "unchecked", **fields}

def certificate_status(run):
    certificate = run.get("certificate")
    if certificate:
        try:
            expires = datetime.fromisoformat(certificate["expires_at"])
            observed = datetime.fromisoformat(run["observed_at"])
            if expires.tzinfo is not None and observed.tzinfo is not None:
                remaining = (expires - observed).total_seconds() / 86400
                if remaining <= 0:
                    status = "expired"
                elif remaining <= CERTIFICATE_URGENT_DAYS:
                    status = "urgent"
                elif remaining <= CERTIFICATE_WARNING_DAYS:
                    status = "due"
                else:
                    status = "valid"
                return {**certificate, "status": status, "days_remaining": max(0, math.ceil(remaining))}
        except (KeyError, TypeError, ValueError):
            pass
    return {"status": "unknown"}


def evaluate(monitor, result, code, matched):
    result.update(http_status=code, content=matched,
                  status="fail" if code != monitor.expected_status or matched == "mismatch"
                  else "unknown" if matched in {"incomplete", "unchecked"} else "pass")
    return result


def simulate(monitor, scenario):
    if scenario not in {"mismatch", "recovered", "timeout", "certificate-due", "certificate-urgent", "certificate-missing"}:
        raise ValueError("Unknown website simulation")
    now = datetime.now(timezone.utc)
    phases = {"dns": {"ok": True, "ms": 4}, "tcp": {"ok": True, "ms": 12},
              "tls": {"ok": True, "ms": 38}}
    if scenario == "certificate-missing":
        phases["tls"] = {"ok": False, "ms": 5000}
        return observation(now.isoformat(), phases, probe="synthetic", address="192.0.2.10")
    days = {"certificate-due": 12, "certificate-urgent": 5}.get(scenario, 90)
    certificate = {"expires_at": (now + timedelta(days=days)).isoformat(),
                   "issuer": "StackOrigin Demo CA", "protocol": "TLSv1.3"}
    result = observation(now.isoformat(), phases, certificate, probe="synthetic", address="192.0.2.10")
    if scenario == "timeout":
        phases["http"] = {"ok": False, "ms": 5000}
        return result
    phases["http"] = {"ok": True, "ms": 81}
    result.update(ttfb_ms=126, total_ms=135)
    return evaluate(monitor, result, 200, "mismatch" if scenario == "mismatch" else "matched")


class DeadlineRead(io.RawIOBase):
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline

    def readable(self):
        return True

    def readinto(self, buffer):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("HTTP probe deadline")
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)


class PinnedResponseSocket:
    def __init__(self, connection, deadline):
        self.connection = connection
        self.deadline = deadline

    def makefile(self, mode):
        return io.BufferedReader(DeadlineRead(self.connection, self.deadline))


def probe(monitor):
    target = urlsplit(monitor.url)
    host = target.hostname
    started = time.monotonic()
    deadline = started + 10
    phases = {}
    stage, phase_start = "dns", started
    result = observation(datetime.now(timezone.utc).isoformat(), phases, probe="local")

    def elapsed(begin):
        return round((time.monotonic() - begin) * 1000)

    def advance(next_stage):
        nonlocal stage, phase_start
        phases[stage] = {"ok": True, "ms": elapsed(phase_start)}
        stage, phase_start = next_stage, time.monotonic()

    def remaining():
        budget = deadline - time.monotonic()
        if budget <= 0:
            raise TimeoutError("Website probe deadline")
        return budget

    try:
        addresses = dig(host, "A")["values"]
        address = next((str(ip) for value in addresses if (ip := ipaddress.ip_address(value)).is_global), None)
        if not address:
            phases["dns"] = {"ok": False, "ms": elapsed(phase_start)}
            result["reason"] = "no_public_ipv4"
            return result
        result["address"] = address
        advance("tcp")
        with socket.create_connection((address, 443), timeout=remaining()) as raw:
            advance("tls")
            raw.settimeout(remaining())
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as connection:
                cert = connection.getpeercert()
                issuer = next((value for part in cert.get("issuer", ()) for key, value in part
                               if key == "commonName"), None)
                result["certificate"] = {
                    "expires_at": datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc).isoformat(),
                    "issuer": issuer, "protocol": connection.version(),
                }
                advance("http")
                path = target.path or "/"
                connection.settimeout(remaining())
                request = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\n"
                           "Connection: close\r\nAccept-Encoding: identity\r\nUser-Agent: StackOrion\r\n\r\n")
                connection.sendall(request.encode("ascii"))
                connection.settimeout(remaining())
                # Peek through a buffered wrapper so the HTTP parser receives the same first byte.
                response_socket = PinnedResponseSocket(connection, deadline)
                with http.client.HTTPResponse(response_socket) as response:
                    if response.fp.peek(1):
                        result["ttfb_ms"] = elapsed(started)
                    response.begin()
                    result["http_status"] = response.status
                    check = "not_configured"
                    if monitor.contains is not None and response.status == monitor.expected_status:
                        if response.getheader("Content-Encoding", "identity").lower() != "identity":
                            check = "incomplete"
                        else:
                            body = response.read(MAX_BODY + 1)
                            if monitor.contains.encode("utf-8") in body[:MAX_BODY]:
                                check = "matched"
                            else:
                                check = ("incomplete" if len(body) > MAX_BODY or response.length not in (None, 0)
                                         else "mismatch")
                    phases[stage] = {"ok": True, "ms": elapsed(phase_start)}
                    return evaluate(monitor, result, response.status, check)
    except ssl.SSLCertVerificationError:
        phases[stage] = {"ok": False, "ms": elapsed(phase_start)}
        result.update(status="fail", reason="certificate_verification_failed")
        return result
    except (CatalogError, OSError, http.client.HTTPException, ValueError, KeyError):
        phases[stage] = {"ok": False, "ms": elapsed(phase_start)}
        return result
    finally:
        result["total_ms"] = elapsed(started)
