"""First-use, local-only enrollment for two read-only adapters."""

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .catalog import Catalog, CatalogError, Service, set_enabled
from .credentials_setup import _private_directory, _validate_value
from .operation_runtime import resource_lock
from .spaceship import DOMAIN, _read_credentials


PROVIDERS = {
    "cloudflare": ("Cloudflare", "cloudflare-zone", {"token", "account_id", "zone_id", "domain"}, "Zone: Read", "DNS: Read"),
    "spaceship": ("Spaceship", "spaceship-domains", {"key", "secret"}, "domains:read", "dnsrecords:read"),
}


CHECKABLE_ADAPTERS = {"cloudflare-zone", "cloudflare-domains", "spaceship-domains"}


def _credential_revision(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CatalogError("Credential must be a private regular file")
    return info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size


class DashboardOnboarding:
    def __init__(self, state_dir, *, locked=False, probe=None):
        self.root = state_dir / "service-onboarding"
        self.path = self.root / "services.json"
        self.locked = locked
        self.probe = probe or self._probe
        self.reports = {}
        self.check_generations = {}

    def catalog(self):
        return Catalog.load(self.path) if self.path.exists() else Catalog([])

    def summary(self, catalog=None):
        catalog = self.catalog() if catalog is None else catalog
        labels = {account["id"]: account["label"] for account in catalog.accounts()}
        connections = []
        for row in catalog.list():
            if row["provider"] not in PROVIDERS:
                continue
            available = row["enabled"] and row["adapter"] in CHECKABLE_ADAPTERS
            item = {**row, "can_check": available,
                    "can_manage": not self.locked and row["adapter"] == PROVIDERS[row["provider"]][1]}
            if row.get("account_id") in labels:
                item["label"] = labels[row["account_id"]]
            prior = self.reports.get(row["id"])
            if available and prior and prior[:2] == (catalog.get(row["id"]), self.check_generations.get(row["id"], 0)):
                item["last_check"] = prior[2]
            connections.append(item)
        return {"editable": not self.locked, "providers": [
            {"id": key, "label": label, "required_scope": required, "optional_scope": optional}
            for key, (label, _, _, required, optional) in PROVIDERS.items()
        ], "connections": connections}

    @staticmethod
    def _probe_status(error):
        reason = str(error)
        if reason.endswith("HTTP 401"):
            return "unauthorized"
        if reason.endswith(("HTTP 403", "HTTP 404")):
            return "denied"
        if reason == "Cloudflare API target mismatch":
            return "mismatch"
        return "unavailable"

    def check(self, catalog, data):
        if set(data) not in ({"service_id"}, {"service_id", "domain"}) or not isinstance(data["service_id"], str):
            raise ValueError("Invalid connection check")
        service = catalog.get(data["service_id"])
        if service.provider not in PROVIDERS or service.adapter not in CHECKABLE_ADAPTERS or not service.enabled:
            raise ValueError("Connection check unavailable")
        domain = data.get("domain")
        if "domain" in data and (service.adapter != "spaceship-domains" or not isinstance(domain, str)
                                 or len(domain) > 253 or not DOMAIN.fullmatch(domain)):
            raise ValueError("Invalid Spaceship domain")
        generation = self.check_generations.get(service.id, 0)
        result = {**self._check_registered(catalog, service, domain.lower() if domain is not None else None),
                  "checked_at": datetime.now(timezone.utc).isoformat()}
        self.reports[service.id] = (service, generation, result)
        return result

    def _invalidate_check(self, service_id):
        self.check_generations[service_id] = self.check_generations.get(service_id, 0) + 1
        self.reports.pop(service_id, None)

    def change_state(self, data):
        if self.locked:
            raise ValueError("Existing catalog is managed separately")
        if (set(data) != {"service_id", "enabled"} or not isinstance(data["service_id"], str)
                or type(data["enabled"]) is not bool):
            raise ValueError("Invalid connection state")
        if not self.path.exists():
            return None
        with resource_lock(self.root, "starter-connections"):
            catalog = self.catalog()
            connection = next((row for row in self.summary(catalog)["connections"]
                               if row["id"] == data["service_id"] and row["can_manage"]), None)
            if connection is None:
                return None
            enabled = data["enabled"]
            if connection["enabled"] != enabled:
                set_enabled(self.path, connection["id"], enabled)
                self._invalidate_check(connection["id"])
                connection.pop("last_check", None)
                connection.update(enabled=enabled, can_check=enabled)
            return connection

    def replace_credential(self, data):
        if self.locked:
            raise ValueError("Existing catalog is managed separately")
        if (set(data) != {"service_id", "credential"} or not isinstance(data["service_id"], str)
                or not isinstance(data["credential"], str)):
            raise ValueError("Invalid credential request")
        secret = data["credential"]
        _validate_value(secret)
        catalog = self.catalog()
        if not any(row["id"] == data["service_id"] and row["can_manage"] and row["enabled"]
                   for row in self.summary(catalog)["connections"]):
            return None
        service = catalog.get(data["service_id"])
        credential = Path(service.settings["credential_file"])
        _private_directory(self.root)
        if credential.parent.parent != self.root.resolve() or credential.name != "credential.env":
            raise CatalogError("Credential is outside the panel enrollment directory")
        _private_directory(credential.parent)
        revision = _credential_revision(credential)
        if service.provider == "cloudflare":
            values = {"token": secret}
        else:
            try:
                key, _ = _read_credentials(credential)
            except (CatalogError, OSError):
                return {"status": "key_unavailable", "saved": False}
            values = {"key": key, "secret": secret}
        with tempfile.TemporaryDirectory(prefix="credential-check-", dir=credential.parent) as temporary:
            pending = Path(temporary) / credential.name
            self._write_secret(pending, service.provider, values)
            candidate = replace(service, settings={**service.settings, "credential_file": str(pending)})
            result = self._check_registered(Catalog([candidate], accounts=catalog.accounts()), candidate, None)
            if result["status"] != "observed":
                return {"status": result["status"], "saved": False}
            with resource_lock(self.root, "starter-connections"):
                _private_directory(credential.parent)
                if self.catalog().get(service.id) != service or _credential_revision(credential) != revision:
                    return {"status": "connection_changed", "saved": False}
                os.replace(pending, credential)
                self._invalidate_check(service.id)
        return {"status": "observed", "saved": True}

    def _check_registered(self, catalog, service, domain):
        provider = service.provider
        _, _, _, required, optional = PROVIDERS[provider]
        output = {"provider": provider, "service_id": service.id, "required_scope": required,
                  "unverified_scope": optional, "count": None, "names": []}
        try:
            adapter = catalog.adapter(service)
            if service.adapter == "cloudflare-zone":
                checked = [adapter.inspect(alias)["data"] for alias in adapter.list_targets()]
                if not checked:
                    return {**output, "status": "unavailable"}
                return {**output, "status": "observed", "count": len(checked),
                        "names": [row["name"] for row in checked[:10]]}
            if service.adapter == "spaceship-domains":
                result = adapter.discover()
                names = sorted(row["name"].lower() for row in result["data"])
                output.update(status=result["status"], count=result["returned_count"], names=names[:10], domains=names)
                if domain is not None:
                    if domain not in names:
                        raise ValueError("Domain is not visible to this Spaceship connection")
                    dns = {"target": domain, "scope": optional}
                    try:
                        summary = adapter.dns_records_summary(domain)
                        dns.update(summary, status="observed" if len(summary["records"]) == summary["count"] else "partial")
                    except CatalogError as exc:
                        reason = str(exc)
                        dns["status"] = ("denied" if reason.endswith("HTTP 403") else
                                         "unauthorized" if reason.endswith("HTTP 401") else "unavailable")
                    except OSError:
                        dns["status"] = "unavailable"
                    output.update(dns=dns, unverified_scope=None)
                return output
            result = adapter.discover()
            rows = result["items"]
            if result["status"] not in {"complete", "partial"}:
                return {**output, "status": "unavailable"}
            output.update(status="observed" if result["status"] == "complete" else "partial",
                          count=len(rows), names=[row["name"] for row in rows[:10]], resource_unit=" 个可见 Zone")
            if rows:
                zone = rows[0]
                dns = {"target": zone["name"], "status": "unavailable"}
                try:
                    records = adapter.records(zone["id"], zone["name"])
                    dns_status = records["status"]
                    if dns_status in {"complete", "partial"}:
                        dns.update(status="observed" if dns_status == "complete" else "partial",
                                   count=len(records["items"]))
                except (CatalogError, OSError):
                    pass
                output.update(dns=dns, unverified_scope=None)
            return output
        except CatalogError as exc:
            return {**output, "status": self._probe_status(exc)}
        except OSError:
            return {**output, "status": "unavailable"}

    def _input(self, data):
        if self.locked:
            raise ValueError("Existing catalog is managed separately")
        provider = data.get("provider")
        if not isinstance(provider, str) or provider not in PROVIDERS:
            raise ValueError("Unsupported provider")
        _, _, fields, _, _ = PROVIDERS[provider]
        required = fields | {"provider"}
        if set(data) not in (required, required | {"label"}) or any(not isinstance(data[k], str) for k in fields):
            raise ValueError("Invalid provider form")
        label = data.get("label")
        if "label" in data and (not isinstance(label, str) or not label.strip() or
                                len(label) > 48 or not label.isprintable()):
            raise ValueError("Invalid connection label")
        if provider == "cloudflare":
            _validate_value(data["token"])
            if (not DOMAIN.fullmatch(data["domain"]) or len(data["domain"]) > 253
                    or any(not re.fullmatch(r"[0-9a-fA-F]{32}", data[k]) for k in ("account_id", "zone_id"))):
                raise ValueError("Invalid Cloudflare target")
        else:
            _validate_value(data["key"])
            _validate_value(data["secret"])
        return provider

    @staticmethod
    def _service(provider, data, credential, identifier=None):
        _, adapter, _, _, _ = PROVIDERS[provider]
        targets = [{
            "alias": "onboarding-zone", "zone_id": data["zone_id"].lower(),
            "zone_name": data["domain"].lower(), "account_id": data["account_id"].lower(),
        }] if provider == "cloudflare" else []
        return Service(identifier or "starter-" + provider, "python-adapter", provider, True,
                       {"credential_file": str(credential), "targets": targets}, adapter,
                       identifier if data.get("label") else None)

    @staticmethod
    def _conflict(catalog, provider, data):
        labels = {account["id"]: account["label"] for account in catalog.accounts()}
        label = data.get("label", "").strip().casefold()
        adapter = PROVIDERS[provider][1]
        for row in catalog.list():
            if row["provider"] != provider or row["adapter"] != adapter:
                continue
            service = catalog.get(row["id"])
            if provider == "cloudflare":
                if any((target["account_id"], target["zone_id"]) ==
                       (data["account_id"].lower(), data["zone_id"].lower())
                       for target in service.settings["targets"]):
                    return "already_configured"
            else:
                try:
                    key, _ = _read_credentials(Path(service.settings["credential_file"]))
                except (CatalogError, OSError):
                    # Old credentials can be missing. They are never rewritten by enrollment.
                    pass
                else:
                    if key == data["key"]:
                        return "already_configured"
            if label and labels.get(service.account_id, "").casefold() == label:
                return "connection_name_used"
        return None

    @staticmethod
    def _write_secret(path, provider, data):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as out:
            if provider == "cloudflare":
                out.write("CLOUDFLARE_API_TOKEN=" + data["token"] + "\n")
            else:
                out.write("SPACESHIP_API_KEY=" + data["key"] + "\nSPACESHIP_API_SECRET=" + data["secret"] + "\n")
            out.flush()
            os.fsync(out.fileno())

    @staticmethod
    def _probe(service):
        adapter = Catalog([service]).adapter(service)
        if service.provider == "cloudflare":
            observed = adapter.inspect("onboarding-zone")
            return {"status": observed["status"], "count": 1, "names": [observed["data"]["name"]]}
        observed = adapter.discover()
        return {"status": observed["status"], "count": observed["returned_count"],
                "names": [row["name"] for row in observed["data"][:10]]}

    def _validate_connection(self, provider, data):
        self.root.mkdir(mode=0o700, exist_ok=True)
        _private_directory(self.root)
        with tempfile.TemporaryDirectory(prefix="check-", dir=self.root) as temporary:
            credential = Path(temporary).resolve() / "credential.env"
            self._write_secret(credential, provider, data)
            try:
                result = self.probe(self._service(provider, data, credential))
            except CatalogError as exc:
                result = {"status": self._probe_status(exc), "count": None, "names": []}
            except OSError:
                result = {"status": "unavailable", "count": None, "names": []}
        _, _, _, required, optional = PROVIDERS[provider]
        return {"provider": provider, **result, "required_scope": required, "unverified_scope": optional}

    def test(self, data):
        provider = self._input(data)
        conflict = self._conflict(self.catalog(), provider, data)
        return {"status": conflict} if conflict else self._validate_connection(provider, data)

    def save(self, data):
        provider = self._input(data)
        conflict = self._conflict(self.catalog(), provider, data)
        if conflict:
            return {"status": conflict, "saved": False}
        result = self._validate_connection(provider, data)
        if result["status"] not in {"observed", "partial"}:
            return {**result, "saved": False}
        with resource_lock(self.root, "starter-connections"):
            catalog = self.catalog()
            conflict = self._conflict(catalog, provider, data)
            if conflict:
                return {"status": conflict, "saved": False}
            used = {row["id"] for row in catalog.list()} | {row["id"] for row in catalog.accounts()}
            base = "starter-" + provider
            sequence = 1
            identifier = base
            while identifier in used:
                sequence += 1
                identifier = f"{base}-{sequence}"
            contents = json.loads(self.path.read_text()) if self.path.exists() else {"schema_version": 1, "services": []}
            # Publish the catalog only after both files validate. A failed attempt
            # owns its private directory and can remove it without touching a prior account.
            directory = Path(tempfile.mkdtemp(prefix=identifier + "-", dir=self.root)).resolve()
            try:
                credential = directory / "credential.env"
                self._write_secret(credential, provider, data)
                service = self._service(provider, data, credential, identifier)
                if service.account_id:
                    contents.setdefault("accounts", []).append({
                        "id": service.account_id, "provider": provider, "label": data["label"].strip(),
                        "scope": "unknown", "verification": "declared",
                    })
                contents["services"].append({**service.public(), "settings": service.settings})
                with tempfile.TemporaryDirectory(prefix="catalog-", dir=self.root) as staging:
                    pending = Path(staging) / "services.json"
                    with pending.open("w", encoding="utf-8") as output:
                        os.fchmod(output.fileno(), 0o600)
                        json.dump(contents, output, ensure_ascii=False, indent=2)
                        output.flush()
                        os.fsync(output.fileno())
                    Catalog.load(pending)
                    os.replace(pending, self.path)
            except BaseException:
                shutil.rmtree(directory)
                raise
        return {**result, "saved": True, "service_id": identifier}
