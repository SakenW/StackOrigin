"""Fixed read-only Linux probe sent to a registered host through SSH."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time


def cpu_counters():
    fields = Path("/proc/stat").read_text().splitlines()[0].split()
    if fields[0] != "cpu" or len(fields) < 6:
        raise ValueError("invalid aggregate CPU counters")
    counters = [int(value) for value in fields[1:9]]
    return sum(counters), counters[3] + counters[4]


def cpu_percent(first, second):
    total, idle = (end - start for start, end in zip(first, second))
    if total > 0 and 0 <= idle <= total:
        return round(100 * (total - idle) / total, 2)
    return None


def memory_percent():
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in {"MemTotal", "MemAvailable"}:
            memory[key] = int(value.split()[0])
    total, available = memory["MemTotal"], memory["MemAvailable"]
    if total > 0 and 0 <= available <= total:
        return round(100 * (total - available) / total, 2)
    return None


def disk_percent():
    fs = os.statvfs("/")
    used = fs.f_blocks - fs.f_bfree
    usable = used + fs.f_bavail
    return round(100 * used / usable, 2) if usable > 0 and used >= 0 else None


def default_interface():
    routes = []
    for line in Path("/proc/net/route").read_text().splitlines()[1:]:
        route = line.split()
        if len(route) >= 8 and route[1] == route[7] == "00000000" and int(route[3], 16) & 1:
            if route[0] != "lo":
                routes.append((int(route[6]), route[0]))
    if routes:
        return min(routes)[1]
    return None


def network_counters(interface):
    if not interface or interface == "lo":
        return None
    for entry in Path("/proc/net/dev").read_text().splitlines()[2:]:
        name, _, fields = entry.partition(":")
        if name.strip() == interface:
            counters = fields.split()
            return int(counters[0]), int(counters[8])
    return None


def network_rates(first, second, seconds):
    if seconds <= 0:
        return {}
    return {name: round((end - start) * 8 / seconds / 1000, 2)
            for name, start, end in zip(("network_rx_kbps", "network_tx_kbps"), first, second)
            if end >= start}


def try_read(reader, *args):
    try:
        return reader(*args)
    except (OSError, ValueError, IndexError, KeyError):
        return None


def verify_identity(kind, expected):
    if kind == "machine-id-sha256":
        actual = Path("/etc/machine-id").read_text().strip()
        return bool(actual) and sha256(actual.encode()).hexdigest() == expected
    if kind == "cloud-init":
        return Path("/var/lib/cloud/data/instance-id").read_text().strip() == expected
    return False


def main():
    kind, expected, interface = sys.argv[1:]
    if not try_read(verify_identity, kind, expected):
        raise SystemExit(2)

    interface = interface or try_read(default_interface)
    cpu_before = try_read(cpu_counters)
    network_before = try_read(network_counters, interface)
    start = time.monotonic()
    time.sleep(1)
    network_after = try_read(network_counters, interface)
    elapsed = time.monotonic() - start
    cpu_after = try_read(cpu_counters)
    reading = {"memory_percent": try_read(memory_percent), "disk_percent": try_read(disk_percent)}
    if cpu_before is not None and cpu_after is not None:
        reading["cpu_percent"] = cpu_percent(cpu_before, cpu_after)
    if network_before is not None and network_after is not None:
        reading.update(network_rates(network_before, network_after, elapsed))
    print(json.dumps({"observed_at": datetime.now(timezone.utc).isoformat(), "metrics": reading}))


if __name__ == "__main__":
    main()
