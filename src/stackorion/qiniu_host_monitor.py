"""Compatibility transport for existing QVM host-monitor registrations."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import importlib.util
import json

from .catalog import CatalogError
from .host_contract import metric_point
from .qiniu import _regular_owned

@dataclass(frozen=True)
class QVMHostMonitor:
    id: str
    name: str
    capacity_service: str
    qvm_service: str
    region: str
    instance_id: str
    account_id: str
    disk_warn_percent: int | None = None

    @property
    def key(self):
        identity = [self.id, self.name, self.capacity_service, self.qvm_service, self.region,
                    self.instance_id, self.account_id]
        return "host:" + sha256(json.dumps(identity).encode()).hexdigest()

    def public(self):
        return {"id": self.id, "name": self.name, "provider": "qiniu", "product": "qvm",
                "disk_warn_percent": self.disk_warn_percent}


def host_binding(catalog, capacity_service, qvm_service, alias):
    capacity, qvm = catalog.get(capacity_service), catalog.get(qvm_service)
    if (capacity.provider != "qiniu" or qvm.provider != "qiniu"
            or capacity.adapter != "qiniu-host-capacity" or qvm.adapter != "qiniu-qvm"
            or not capacity.account_id or capacity.account_id != qvm.account_id):
        raise CatalogError("host monitor requires QVM and capacity services for the same account")
    capacity_ids = [target["instance_id"] for target in capacity.settings.get("targets", [])
                    if target.get("alias") == alias]
    qvm_ids = [target["instance_id"] for target in qvm.settings.get("targets", [])]
    if len(capacity_ids) != 1 or qvm_ids.count(capacity_ids[0]) != 1:
        raise CatalogError("host monitor requires an exact instance in both service registrations")
    return capacity, qvm, capacity_ids[0]


def qvm_metrics(adapter, monitor, now):
    _regular_owned(adapter.script, secret=False)
    _regular_owned(adapter.credential, secret=True)
    spec = importlib.util.spec_from_file_location("stackorion_qvm_monitor_transport", adapter.script)
    if spec is None or spec.loader is None:
        raise CatalogError("QVM monitor helper is unavailable")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    if helper.HOST != "api-qvm.qiniu.com":
        raise CatalogError("Unexpected QVM monitor host")
    credentials = helper.load_dotenv(str(adapter.credential))
    client = helper.QiniuQvm(credentials["QINIU_AK"], credentials["QINIU_SK"])
    response = client.request("GET", "/v1/vm/monitor/instance", {
        "instance_id": monitor.instance_id, "start_time": int(now.timestamp()) - 900,
        "end_time": int(now.timestamp()),
    }, extra_headers={"x-qiniu-regionid": monitor.region})
    if response.get("ok") is not True:
        raise CatalogError("QVM instance metrics unavailable")
    rows = response["data"]["data"]["monitor_data"]["instance_monitor_data"]
    if not isinstance(rows, list) or len(rows) > 900:
        raise CatalogError("Unexpected QVM monitor response")
    samples = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("time_stamp"), str):
            continue
        try:
            stamp = datetime.fromisoformat(row["time_stamp"])
        except ValueError:
            continue
        if stamp.tzinfo is not None and now - timedelta(minutes=5) <= stamp <= now:
            samples.append((stamp, row))
    if not samples:
        return {}
    stamp, row = max(samples, key=lambda sample: sample[0])
    stamp = stamp.astimezone(timezone.utc).isoformat()
    return {
        "cpu_percent": metric_point(row.get("cpu"), 100, "qvm", stamp),
        "network_rx_kbit": metric_point(row.get("intranet_rx"), 10**15, "qvm", stamp),
        "network_tx_kbit": metric_point(row.get("intranet_tx"), 10**15, "qvm", stamp),
    }


def capacity_metrics(adapter, monitor):
    result = adapter.inspect(monitor.name)
    observed = datetime.fromisoformat(result["observed_at"])
    if observed.tzinfo is None:
        raise CatalogError("Host capacity timestamp has no time zone")
    stamp = observed.astimezone(timezone.utc).isoformat()
    data = result["data"]
    system = data["system"]
    total, available = system.get("memory_total_bytes"), system.get("memory_available_bytes")
    used = (round((1 - available / total) * 100, 2)
            if type(total) is int and type(available) is int and 0 <= available <= total and total > 0
            else None)
    return {
        "memory_percent": metric_point(used, 100, "host", stamp),
        "disk_percent": metric_point(data["filesystem"].get("used_percent"), 100, "host", stamp),
    }


def collect_qvm_metrics(catalog, monitor, now):
    capacity, qvm, instance_id = host_binding(catalog, monitor.capacity_service, monitor.qvm_service, monitor.name)
    if instance_id != monitor.instance_id or capacity.account_id != monitor.account_id:
        return {}
    metrics = {}
    for service, collect, args in ((qvm, qvm_metrics, (now,)), (capacity, capacity_metrics, ())):
        try:
            metrics.update(collect(catalog.adapter(service), monitor, *args))
        except (CatalogError, ImportError, OSError, AttributeError, KeyError, TypeError, ValueError):
            # Each source can be unavailable without discarding the other source.
            continue
    return metrics
