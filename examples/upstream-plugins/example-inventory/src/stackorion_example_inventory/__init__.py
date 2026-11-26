"""Offline catalog adapter with two explicitly registered queries."""

from stackorion.plugin_api import CatalogError


class InventoryAdapter:
    provider = "example"
    query_methods = {"inventory": "discover", "target": "inspect"}
    query_upstreams = {"inventory": "local", "target": "local"}

    def __init__(self, settings: dict):
        if set(settings) != {"targets"}:
            raise CatalogError("example inventory requires only the targets setting")
        targets = settings["targets"]
        if (not isinstance(targets, list) or not 1 <= len(targets) <= 20
                or any(not isinstance(alias, str) or not 1 <= len(alias) <= 64 for alias in targets)
                or len(targets) != len(set(targets))):
            raise CatalogError("example inventory requires 1–20 unique target aliases")
        self.targets = tuple(targets)

    def list_targets(self) -> list[str]:
        return list(self.targets)

    def discover(self) -> dict:
        return {"status": "observed", "source": "synthetic", "targets": self.list_targets()}

    def inspect(self, alias: str) -> dict:
        if not isinstance(alias, str) or alias not in self.targets:
            raise CatalogError("target alias is not configured")
        return {"status": "observed", "source": "synthetic", "target": alias}
