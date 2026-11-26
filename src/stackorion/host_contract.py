"""Provider-independent contracts for read-only server monitoring."""

from dataclasses import dataclass
import re

RESOURCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")

METRIC_LIMITS = {
    "cpu_percent": 100,
    "memory_percent": 100,
    "disk_percent": 100,
    "network_rx_kbit": 10**15,
    "network_tx_kbit": 10**15,
    "network_rx_kbps": 10**15,
    "network_tx_kbps": 10**15,
}
PERCENT_METRICS = ("cpu_percent", "memory_percent", "disk_percent")
TRAFFIC_METRICS = ("network_rx_kbit", "network_tx_kbit")
RATE_METRICS = ("network_rx_kbps", "network_tx_kbps")
HOST_STATES = frozenset({"available", "running", "stopped", "starting", "stopping", "unavailable"})


@dataclass(frozen=True)
class HostTarget:
    resource_id: str
    product: str
    region: str | None = None


@dataclass(frozen=True)
class HostReading:
    observed_at: str
    source: str
    values: dict[str, float | int]
    state: str | None = None


def metric_point(value, maximum, source, observed_at):
    if type(value) in {int, float} and 0 <= value <= maximum:
        return {"value": value, "source": source, "observed_at": observed_at}
    return None


def complete_metrics(metrics):
    return all(metrics.get(key) is not None for key in PERCENT_METRICS) and any(
        all(metrics.get(key) is not None for key in pair) for pair in (TRAFFIC_METRICS, RATE_METRICS)
    )
