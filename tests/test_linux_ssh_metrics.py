from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from stackorion.catalog import CatalogError
from stackorion.host_contract import complete_metrics
from stackorion.linux_ssh_metrics import LinuxSSHAdapter, LinuxSSHCollector
from stackorion.linux_ssh_probe import cpu_percent, network_rates, verify_identity


class LinuxSSHMetricsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        key = root / "identity"
        key.touch(mode=0o600)
        known_hosts = root / "verified hosts"
        known_hosts.touch()
        self.profile = {"host": "fixture.example", "user": "metrics", "key_file": str(key),
                        "known_hosts": str(known_hosts), "host_key_alias": "pinned.example",
                        "machine_id_sha256": "a" * 64}

    def test_default_ssh_transport_runs_fixed_probe_using_private_binding(self):
        stamp = "2026-11-23T00:00:00Z"
        result = {"observed_at": stamp, "metrics": {"cpu_percent": 8, "network_rx_kbps": 1.5}}
        adapter = LinuxSSHAdapter({"alias": "fixture", "resource_id": "inventory-one",
                                   "product": "bare-metal", "ssh": self.profile})
        with patch("stackorion.linux_ssh_metrics.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, json.dumps(result).encode())) as run:
            with self.assertRaises(CatalogError):
                adapter.host_reading("wrong-host")
            run.assert_not_called()
            reading = adapter.host_reading("fixture")
            args = run.call_args.args[0]
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("UpdateHostKeys=no", args)
            self.assertIn("HostKeyAlias=pinned.example", args)
            self.assertIn("python3 -I -B - machine-id-sha256", args[-1])
            self.assertIn(b"/proc/stat", run.call_args.kwargs["input"])
        self.assertEqual(reading.source, "linux-ssh")
        self.assertEqual(reading.values, result["metrics"])

    def test_identity_failure_discards_output(self):
        with patch("stackorion.linux_ssh_metrics.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 2, b'{"metrics":{"cpu_percent":22}}')):
            with self.assertRaisesRegex(CatalogError, "unavailable"):
                LinuxSSHCollector(self.profile).read("inventory-one")
        with patch("stackorion.linux_ssh_probe.Path.read_text", return_value="machine\n"):
            self.assertTrue(verify_identity("machine-id-sha256", sha256(b"machine").hexdigest()))
            self.assertFalse(verify_identity("machine-id-sha256", "a" * 64))

    def test_measured_counter_deltas_handle_resets_and_do_not_mix_units(self):
        self.assertEqual(cpu_percent((100, 50), (200, 125)), 25)
        self.assertIsNone(cpu_percent((100, 50), (50, 25)))
        self.assertEqual(network_rates((1000, 700), (1500, 50), 2.0), {"network_rx_kbps": 2.0})
        self.assertEqual(network_rates((1000, 700), (1000, 700), 1.0),
                         {"network_rx_kbps": 0.0, "network_tx_kbps": 0.0})
        partial = {"cpu_percent": 0, "memory_percent": 50, "disk_percent": 50,
                   "network_rx_kbit": 3, "network_tx_kbps": 3}
        self.assertFalse(complete_metrics(partial))
        self.assertTrue(complete_metrics({**partial, "network_rx_kbps": 0}))
