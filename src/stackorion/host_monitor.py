"""Registration and normalization shared by all host collectors."""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json

from .catalog import ID, CatalogError
from .host_contract import (
    HOST_STATES, METRIC_LIMITS, PERCENT_METRICS, RATE_METRICS, RESOURCE_ID, TRAFFIC_METRICS,
    HostReading, HostTarget, complete_metrics, metric_point,
)
from .linux_ssh_metrics import LinuxSSHCollector
from .qiniu_host_monitor import QVMHostMonitor, collect_qvm_metrics, host_binding


@dataclass(frozen=True)
class AdapterHostMonitor:
    id: str
    name: str
    service_id: str
    adapter_id: str
    provider: str
    account_id: str
    target: HostTarget
    disk_warn_percent: int | None = None
    ssh_metrics: LinuxSSHCollector | None = None

    @property
    def key(self):
        identity = [self.id, self.name, self.service_id, self.adapter_id, self.provider, self.account_id,
                    self.target.product, self.target.region, self.target.resource_id]
        return "host-adapter:" + sha256(json.dumps(identity).encode()).hexdigest()

    def public(self):
        return {"id": self.id, "name": self.name, "provider": self.provider,
                "product": self.target.product, "disk_warn_percent": self.disk_warn_percent}


DEMO_HOST = QVMHostMonitor("demo-server", "演示服务器", "", "", "", "", "demo", 85)


def host_identity(monitor):
    if isinstance(monitor, AdapterHostMonitor):
        return (f"live:{monitor.provider}:{monitor.account_id}", monitor.target.region, monitor.target.resource_id)
    return (f"live:qiniu:{monitor.account_id}", monitor.region, monitor.instance_id)


def restore_host(binding):
    if "target" in binding:
        return AdapterHostMonitor(**{**binding, "target": HostTarget(**binding["target"])})
    return QVMHostMonitor(**binding)


def host_candidates(catalog, assets):
    services = [catalog.get(row["id"]) for row in catalog.list() if row["enabled"] and row["kind"] == "python-adapter"]
    regions = {}
    for asset in assets:
        region = asset.get("region")
        if (asset.get("data_source") == "live" and asset.get("kind") == "server"
                and asset.get("product") == "七牛云 QVM" and isinstance(region, str) and ID.fullmatch(region)):
            regions.setdefault((asset["account"], asset["id"]), set()).add(region)

    candidates = []
    for service in services:
        if service.provider == "qiniu" and service.adapter == "qiniu-host-capacity" and service.account_id:
            try:
                capacity = catalog.adapter(service)
            except CatalogError:
                continue
            peers = [qvm for qvm in services if qvm.adapter == "qiniu-qvm" and qvm.account_id == service.account_id]
            for alias, instance in capacity.targets.items():
                found = regions.get((f"live:qiniu:{service.account_id}", instance), set())
                if len(found) != 1 or not ID.fullmatch(alias):
                    continue
                for peer in peers:
                    try:
                        catalog.adapter(peer)
                        host_binding(catalog, service.id, peer.id, alias)
                    except CatalogError:
                        continue
                    candidates.append(QVMHostMonitor("", alias, service.id, peer.id, next(iter(found)),
                                                     instance, service.account_id))
            continue
        try:
            adapter = catalog.adapter(service)
            if not all(callable(getattr(adapter, method, None)) for method in ("list_targets", "host_target", "host_reading")):
                continue
            aliases = adapter.list_targets()
            if not isinstance(aliases, (list, tuple)):
                continue
            for alias in aliases:
                if not isinstance(alias, str) or not ID.fullmatch(alias):
                    continue
                target = adapter.host_target(alias)
                if (isinstance(target, HostTarget) and isinstance(target.resource_id, str) and RESOURCE_ID.fullmatch(target.resource_id)
                        and isinstance(target.product, str) and ID.fullmatch(target.product)
                        and (target.region is None or (isinstance(target.region, str) and ID.fullmatch(target.region)))):
                    candidates.append(AdapterHostMonitor("", alias, service.id, service.adapter, service.provider,
                                                         service.account_id or service.id, target))
        except (CatalogError, OSError, TypeError, ValueError):
            continue
    registered = {}
    for row in candidates:
        id_ = "host-" + row.key.split(":", maxsplit=1)[1][:32]
        registered[id_] = replace(row, id=id_)
    return registered


def load_hosts(path, catalog):
    if path is None:
        return {}
    if path.stat().st_size > 65_536:
        raise ValueError("Host monitor file exceeds its size limit")
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or len(rows) > 100:
        raise ValueError("Invalid host monitor list")
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid host monitor definition")
        if "service_id" in row:
            required = {"id", "alias", "service_id", "product", "resource_id"}
            optional = {"disk_warn_percent", "region", "ssh_metrics"}
            id_fields = required - {"resource_id"}
        else:
            required = {"id", "alias", "capacity_service", "qvm_service", "region"}
            optional = {"disk_warn_percent"}
            id_fields = required
        if (required - row.keys() or row.keys() - (required | optional)
                or any(not isinstance(row[key], str) or not ID.fullmatch(row[key]) for key in id_fields)
                or row["id"] in result or row["id"] == DEMO_HOST.id):
            raise ValueError("Invalid or duplicate host monitor definition")
        threshold = row.get("disk_warn_percent")
        if threshold is not None and (type(threshold) is not int or not 1 <= threshold <= 100):
            raise ValueError("Invalid disk warning threshold")
        if "service_id" in row:
            service = catalog.get(row["service_id"])
            region = row.get("region")
            if (service.kind != "python-adapter" or not isinstance(row["resource_id"], str)
                    or not RESOURCE_ID.fullmatch(row["resource_id"])
                    or (region is not None and (not isinstance(region, str) or not ID.fullmatch(region)))):
                raise ValueError("Invalid registered adapter host")
            target = HostTarget(row["resource_id"], row["product"], region)
            ssh = LinuxSSHCollector(row["ssh_metrics"]) if "ssh_metrics" in row else None
            result[row["id"]] = AdapterHostMonitor(row["id"], row["alias"], service.id, service.adapter,
                                                    service.provider, service.account_id or service.id, target, threshold, ssh)
        else:
            capacity, _, instance_id = host_binding(catalog, row["capacity_service"], row["qvm_service"], row["alias"])
            result[row["id"]] = QVMHostMonitor(row["id"], row["alias"], row["capacity_service"], row["qvm_service"],
                                               row["region"], instance_id, capacity.account_id, threshold)
    return result


def collect_adapter(catalog, monitor):
    service = catalog.get(monitor.service_id)
    if (service.adapter != monitor.adapter_id or service.provider != monitor.provider
            or (service.account_id or service.id) != monitor.account_id):
        raise CatalogError("Host monitor account changed")
    adapter = catalog.adapter(service)
    if adapter.host_target(monitor.name) != monitor.target:
        raise CatalogError("Host monitor target changed")
    try:
        metrics, extra = normalize_reading(adapter.host_reading(monitor.name))
    except (CatalogError, ImportError, OSError, AttributeError, KeyError, TypeError, ValueError):
        if monitor.ssh_metrics is None:
            raise
        metrics, extra = {}, {}
    if monitor.ssh_metrics is not None and not complete_metrics(metrics):
        try:
            ssh_metrics, _ = normalize_reading(monitor.ssh_metrics.read(monitor.target.resource_id))
        except (CatalogError, OSError, TypeError, ValueError):
            return metrics, extra
        primary_network_complete = any(
            all(metrics.get(key) is not None for key in pair) for pair in (TRAFFIC_METRICS, RATE_METRICS)
        )
        metrics.update({key: point for key, point in ssh_metrics.items()
                        if metrics.get(key) is None and (not primary_network_complete or key in PERCENT_METRICS)})
    return metrics, extra


def normalize_reading(reading):
    if (not isinstance(reading, HostReading) or not isinstance(reading.values, dict)
            or not isinstance(reading.source, str) or not ID.fullmatch(reading.source)):
        raise CatalogError("Invalid host reading")
    observed = datetime.fromisoformat(reading.observed_at)
    if observed.tzinfo is None:
        raise CatalogError("Host reading timestamp must have a time zone")
    stamp = observed.astimezone(timezone.utc).isoformat()
    metrics = {key: metric_point(reading.values[key], limit, reading.source, stamp)
               for key, limit in METRIC_LIMITS.items() if key in reading.values}
    extra = {}
    if isinstance(reading.state, str) and reading.state in HOST_STATES:
        extra = {"resource_state": reading.state, "state_source": reading.source, "state_observed_at": stamp}
    return metrics, extra


def collect_host(catalog, monitor):
    now = datetime.now(timezone.utc)
    result = {"observed_at": now.isoformat(), "status": "unknown", "metrics": {}}
    try:
        if isinstance(monitor, QVMHostMonitor):
            result["metrics"] = collect_qvm_metrics(catalog, monitor, now)
        else:
            result["metrics"], extra = collect_adapter(catalog, monitor)
            result.update(extra)
    except (CatalogError, ImportError, OSError, AttributeError, KeyError, TypeError, ValueError):
        return result
    if complete_metrics(result["metrics"]):
        result["status"] = "pass"
    return result


def simulate_host(scenario, now=None):
    if scenario not in {"high", "missing", "healthy"}:
        raise ValueError("Unknown host monitoring scenario")
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    values = {
        "high": (61, 64, 91, 3180, 870),
        "missing": (None, 53, None, None, None),
        "healthy": (36, 51, 66, 2040, 650),
    }[scenario]
    return {"observed_at": stamp, "status": "unknown" if scenario == "missing" else "pass",
            "metrics": {key: metric_point(value, METRIC_LIMITS[key], "synthetic", stamp)
                        for key, value in zip((*PERCENT_METRICS, *TRAFFIC_METRICS), values)}}
