"""Read public website checks for the local operations preview."""

from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from .website_monitor import certificate_status, probe


class OpsLiveWebsites:
    def __init__(self, monitors):
        self.monitors = tuple(monitors)
        self.domains = tuple(urlsplit(monitor.url).hostname for monitor in self.monitors)
        if not self.domains:
            raise ValueError("Configure at least one website monitor")
        if len(set(self.domains)) != len(self.domains):
            raise ValueError("Configure one website monitor per domain in the operations preview")
        self._lock = Lock()
        self._cached = None
        self._expires = 0

    @staticmethod
    def _check(monitor):
        result = probe(monitor)
        return {
            "domain": urlsplit(monitor.url).hostname,
            "observation": {key: result.get(key) for key in ("observed_at", "status", "phases", "http_status", "reason")},
            "certificate": {"status": certificate_status(result)["status"]},
        }

    def snapshot(self):
        with self._lock:
            if self._cached is None or monotonic() >= self._expires:
                with ThreadPoolExecutor(max_workers=6) as pool:
                    self._cached = {"websites": list(pool.map(self._check, self.monitors))}
                self._expires = monotonic() + 60
            return self._cached
