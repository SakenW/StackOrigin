"""Non-secret service catalog and adapter discovery."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ID = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
ALLOWED_KINDS = {"python-adapter", "external-mcp", "client-mcp", "unconnected", "dormant"}


class CatalogError(ValueError):
    pass


@dataclass(frozen=True)
class Service:
    id: str
    kind: str
    provider: str
    enabled: bool
    settings: dict[str, Any]
    adapter: str | None = None
    account_id: str | None = None

    def public(self) -> dict[str, Any]:
        """Return catalog metadata without credential paths or provider settings."""
        result = {
            "id": self.id,
            "kind": self.kind,
            "provider": self.provider,
            "enabled": self.enabled,
            "adapter": self.adapter,
        }
        if self.account_id is not None:
            result["account_id"] = self.account_id
        if self.kind == "client-mcp":
            result["execution_scope"] = "client-direct"
        return result


class Catalog:
    def __init__(self, services: list[Service], resources: list[dict[str, Any]] | None = None,
                 relations: list[dict[str, str]] | None = None,
                 accounts: list[dict[str, str]] | None = None):
        ids = [service.id for service in services]
        if len(ids) != len(set(ids)):
            raise CatalogError("duplicate service id")
        self._services = {service.id: service for service in services}
        self._accounts = {row["id"]: row.copy() for row in (accounts or [])}
        if len(self._accounts) != len(accounts or []):
            raise CatalogError("duplicate account id")
        for service in services:
            if service.account_id is not None:
                account = self._accounts.get(service.account_id)
                if account is None or account["provider"] != service.provider:
                    raise CatalogError("service references unknown or mismatched account")
        self._resources = resources or []
        self._relations = relations or []
        resource_ids = [row["id"] for row in self._resources]
        if len(resource_ids) != len(set(resource_ids)):
            raise CatalogError("duplicate resource id")
        identities = [(row["service_id"], row["account"], row["region"], row["type"], row["provider_id"]) for row in self._resources]
        if len(identities) != len(set(identities)):
            raise CatalogError("duplicate provider resource identity")
        for row in self._resources:
            if row["service_id"] not in self._services:
                raise CatalogError("resource references unknown service")
            service = self._services[row["service_id"]]
            if service.account_id is not None and row["account"] != service.account_id:
                raise CatalogError("resource account does not match service account")
        for row in self._relations:
            if row["from"] not in resource_ids or row["to"] not in resource_ids:
                raise CatalogError("relation references unknown resource")

    @classmethod
    def load(cls, path: Path) -> Catalog:
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
            raise CatalogError("catalog must be a regular file")
        if path.stat().st_size > 1_000_000:
            raise CatalogError("catalog is too large")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CatalogError("invalid catalog JSON") from exc
        if not isinstance(data, dict) or not {"schema_version", "services"} <= set(data) or set(data) - {"schema_version", "services", "accounts", "resources", "relations"}:
            raise CatalogError("invalid catalog structure")
        if data["schema_version"] != 1 or not isinstance(data["services"], list):
            raise CatalogError("unsupported catalog schema")
        services: list[Service] = []
        for row in data["services"]:
            if not isinstance(row, dict) or not {"id", "kind", "provider", "enabled", "settings"} <= set(row):
                raise CatalogError("invalid service entry")
            if set(row) - {"id", "kind", "provider", "enabled", "settings", "adapter", "account_id"}:
                raise CatalogError("unknown service fields")
            if not isinstance(row["id"], str) or not ID.fullmatch(row["id"]):
                raise CatalogError("invalid service id")
            if not isinstance(row["provider"], str) or not ID.fullmatch(row["provider"]):
                raise CatalogError("invalid provider id")
            if not isinstance(row["kind"], str) or row["kind"] not in ALLOWED_KINDS or type(row["enabled"]) is not bool:
                raise CatalogError("invalid service kind or enabled flag")
            if not isinstance(row["settings"], dict):
                raise CatalogError("settings must be an object")
            adapter = row.get("adapter")
            account_id = row.get("account_id")
            if account_id is not None and (not isinstance(account_id, str) or not ID.fullmatch(account_id)):
                raise CatalogError("invalid service account id")
            if row["kind"] == "python-adapter" and (not isinstance(adapter, str) or not ID.fullmatch(adapter)):
                raise CatalogError("python service requires an adapter id")
            if row["kind"] != "python-adapter" and adapter is not None:
                raise CatalogError("only python services have an adapter id")
            if row["kind"] in {"dormant", "unconnected"} and (row["enabled"] or row["settings"]):
                raise CatalogError("service without a connector cannot be enabled or configured")
            if row["kind"] == "client-mcp" and (row["enabled"] or row["settings"]):
                settings = row["settings"]
                if set(settings) != {"client", "server_id", "enabled_tools"} or settings["client"] != "codex":
                    raise CatalogError("client MCP requires a known client binding")
                if not isinstance(settings["server_id"], str) or not ID.fullmatch(settings["server_id"]):
                    raise CatalogError("invalid client MCP server id")
                tools = settings["enabled_tools"]
                if (not isinstance(tools, list) or not tools or len(tools) > 32
                        or any(not isinstance(tool, str) or not ID.fullmatch(tool) for tool in tools)
                        or len(tools) != len(set(tools))):
                    raise CatalogError("invalid client MCP tool allowlist")
            if row["kind"] == "external-mcp" and (row["enabled"] or row["settings"]):
                if set(row["settings"]) != {"url"}:
                    raise CatalogError("external MCP settings must contain only url")
                url = row["settings"].get("url")
                if not isinstance(url, str):
                    raise CatalogError("external MCP requires an HTTPS URL")
                try:
                    parsed = urlsplit(url)
                except ValueError as exc:
                    raise CatalogError("external MCP requires an HTTPS URL") from exc
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise CatalogError("external MCP requires an HTTPS URL")
            services.append(Service(**row))
        accounts = data.get("accounts", [])
        resources = data.get("resources", [])
        relations = data.get("relations", [])
        if not isinstance(accounts, list) or not isinstance(resources, list) or not isinstance(relations, list):
            raise CatalogError("accounts, resources and relations must be lists")
        for row in accounts:
            if not isinstance(row, dict) or set(row) != {"id", "provider", "label", "scope", "verification"}:
                raise CatalogError("invalid account entry")
            if not isinstance(row["id"], str) or not ID.fullmatch(row["id"]):
                raise CatalogError("invalid account id")
            if not isinstance(row["provider"], str) or not ID.fullmatch(row["provider"]):
                raise CatalogError("invalid account provider")
            if not isinstance(row["label"], str) or not row["label"].strip() or len(row["label"]) > 120:
                raise CatalogError("invalid account label")
            if (not isinstance(row["scope"], str) or row["scope"] not in {"personal", "company", "project", "unknown"}
                    or not isinstance(row["verification"], str) or row["verification"] not in {"declared", "provider-verified"}):
                raise CatalogError("invalid account scope or verification")
        for row in resources:
            if not isinstance(row, dict) or set(row) != {"id", "service_id", "account", "region", "type", "provider_id", "verification"}:
                raise CatalogError("invalid resource entry")
            if any(not isinstance(row[key], str) or not row[key] for key in row):
                raise CatalogError("resource fields must be non-empty strings")
            if not ID.fullmatch(row["id"]) or row["verification"] not in {"declared", "verified"}:
                raise CatalogError("invalid resource id or verification")
        for row in relations:
            if not isinstance(row, dict) or set(row) != {"from", "to", "type"}:
                raise CatalogError("invalid relation entry")
            if any(not isinstance(value, str) or not value for value in row.values()):
                raise CatalogError("invalid relation fields")
        return cls(services, resources, relations, accounts)

    def list(self) -> list[dict[str, Any]]:
        return [service.public() for service in self._services.values()]

    def accounts(self) -> list[dict[str, Any]]:
        return [
            {**row.copy(),
             "service_ids": [service.id for service in self._services.values() if service.account_id == row["id"]],
             "enabled_service_ids": [service.id for service in self._services.values()
                                     if service.account_id == row["id"] and service.enabled]}
            for row in self._accounts.values()
        ]

    def resources(self, service_id: str | None = None) -> list[dict[str, Any]]:
        if service_id is not None and service_id not in self._services:
            raise CatalogError("unknown service")
        return [row.copy() for row in self._resources if service_id is None or row["service_id"] == service_id]

    def relations(self, resource_id: str) -> list[dict[str, str]]:
        if resource_id not in {row["id"] for row in self._resources}:
            raise CatalogError("unknown resource")
        return [row.copy() for row in self._relations if row["from"] == resource_id or row["to"] == resource_id]

    def get(self, service_id: str) -> Service:
        try:
            return self._services[service_id]
        except KeyError as exc:
            raise CatalogError("unknown service") from exc

    def adapter(self, service: Service):
        if not service.enabled:
            raise CatalogError("service is disabled")
        if service.kind != "python-adapter":
            raise CatalogError("service has no local adapter")
        matches = [entry for entry in entry_points(group="stackorion.adapters") if entry.name == service.adapter]
        if len(matches) != 1:
            raise CatalogError("adapter is missing or ambiguous")
        try:
            factory = matches[0].load()
        except Exception:
            raise CatalogError("adapter could not be loaded") from None
        declared_provider = getattr(factory, "provider", None)
        if isinstance(declared_provider, str) and declared_provider != service.provider:
            raise CatalogError("query adapter provider does not match service")
        try:
            adapter = factory(service.settings)
        except CatalogError:
            raise
        except Exception:
            raise CatalogError("adapter could not be initialized") from None
        if getattr(adapter, "provider", service.provider) != service.provider:
            raise CatalogError("query adapter provider does not match service")
        return adapter


def set_enabled(path: Path, service_id: str, enabled: bool) -> None:
    catalog = Catalog.load(path)
    service = catalog.get(service_id)
    if service.kind in {"dormant", "unconnected"}:
        raise CatalogError("service requires a connector before enabling")
    if service.kind == "client-mcp":
        raise CatalogError("client MCP must be managed in its owning client")
    if enabled and not service.settings:
        raise CatalogError("service requires connector settings before enabling")
    data = json.loads(path.read_text(encoding="utf-8"))
    for row in data["services"]:
        if row["id"] == service_id:
            row["enabled"] = enabled
            break
    mode = path.stat().st_mode & 0o777
    fd, name = tempfile.mkstemp(prefix=".services-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        Catalog.load(Path(name))
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def configured_path() -> Path:
    raw = os.environ.get("STACKORION_CONFIG")
    if not raw:
        raise CatalogError("STACKORION_CONFIG is required")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise CatalogError("STACKORION_CONFIG must be absolute")
    return path
