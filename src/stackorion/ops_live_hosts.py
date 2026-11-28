"""Read the existing QVM adapter's metrics for the local operations preview."""

from threading import Lock
from time import monotonic

from .catalog import CatalogError


class OpsLiveHosts:
    def __init__(self, catalog):
        self.targets = []
        for row in catalog.list():
            if row["enabled"] and row["adapter"] == "qiniu-qvm":
                adapter = catalog.adapter(catalog.get(row["id"]))
                self.targets.extend((adapter, alias) for alias in adapter.list_targets())
        if not self.targets:
            raise CatalogError("Configure at least one enabled QVM metrics target")
        self._lock = Lock()
        self._cached = None
        self._expires = 0

    def snapshot(self):
        with self._lock:
            if self._cached is None or monotonic() >= self._expires:
                self._cached = {
                    "mode": "live-hosts",
                    "hosts": [{"name": alias, "sample": adapter.metrics(alias)}
                              for adapter, alias in self.targets],
                }
                self._expires = monotonic() + 60
            return self._cached
