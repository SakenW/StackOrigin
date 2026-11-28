import unittest
from unittest.mock import patch

from stackorion.ops_live_websites import OpsLiveWebsites
from stackorion.website_monitor import Monitor


class OpsLiveWebsitesTests(unittest.TestCase):
    def test_returns_only_preview_fields_and_reuses_recent_probe(self):
        monitor = Monitor("test-site", "https://example.com/", 200)
        result = {
            "observed_at": "2026-11-28T00:00:00+00:00",
            "status": "unknown",
            "phases": {"dns": {"ok": True}, "tls": {"ok": False}},
            "reason": "certificate_verification_failed",
            "http_status": None,
            "address": "192.0.2.10",
        }
        live = OpsLiveWebsites([monitor])
        with patch("stackorion.ops_live_websites.probe", return_value=result) as probe, \
             patch("stackorion.ops_live_websites.monotonic", side_effect=[0, 10, 61, 62]):
            snapshot = live.snapshot()
            self.assertEqual(snapshot, live.snapshot())
            probe.assert_called_once_with(monitor)
            live.snapshot()
            self.assertEqual(probe.call_count, 2)
        row, = snapshot["websites"]
        self.assertEqual(row["domain"], "example.com")
        self.assertEqual(row["certificate"], {"status": "unknown"})
        self.assertEqual(row["observation"]["reason"], "certificate_verification_failed")
        self.assertNotIn("address", row["observation"])

    def test_rejects_multiple_definitions_for_the_same_domain(self):
        with self.assertRaisesRegex(ValueError, "one website monitor per domain"):
            OpsLiveWebsites([
                Monitor("root", "https://example.com/", 200),
                Monitor("health", "https://example.com/health", 204),
            ])
