"""Read the existing QVM adapter's metrics for the local operations preview."""

from threading import Lock
from time import monotonic

from .catalog import CatalogError
from .host_monitor import normalize_reading


class OpsLiveHosts:
    def __init__(self, catalog, host_monitors=()):
        self.targets = []
        guest_collectors = {(monitor.account_id, monitor.target): monitor.ssh_metrics
                                 for monitor in host_monitors
                                 if getattr(monitor, "adapter_id", None) == "qiniu-qvm" and monitor.ssh_metrics}
        for row in catalog.list():
            if row["enabled"] and row["adapter"] == "qiniu-qvm":
                service = catalog.get(row["id"])
                adapter = catalog.adapter(service)
                self.targets.extend((adapter, alias, guest_collectors.get((
                    service.account_id or service.id, adapter.host_target(alias)
                ))) for alias in adapter.list_targets())
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
                    "hosts": [{"name": alias, "sample": self._sample(adapter, alias, collector)}
                              for adapter, alias, collector in self.targets],
                }
                self._expires = monotonic() + 60
            return self._cached

    def _sample(self, adapter, alias, collector):
        sample = adapter.metrics(alias, include_history=True)
        if collector is None:
            return sample
        try:
            points, _ = normalize_reading(collector.read(adapter.host_target(alias).resource_id))
        except (CatalogError, OSError, TypeError, ValueError):
            points = {}
        wanted = ("memory_percent", "disk_percent")
        guest = {key: points[key] for key in wanted if points.get(key) is not None}
        if guest:
            sample["values"].update({key: point["value"] for key, point in guest.items()})
            sample["guest_observed_at"] = next(iter(guest.values()))["observed_at"]
        if sample["values"] and (sample["status"] != "observed" or len(guest) != len(wanted)):
            sample["status"] = "partial"
        return sample

    @property
    def resource_ids(self):
        return {adapter.host_target(alias).resource_id for adapter, alias, _ in self.targets}
