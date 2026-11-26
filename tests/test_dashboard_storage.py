from datetime import datetime, timedelta, timezone
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from threading import Thread
import time
import unittest
from unittest.mock import patch

from stackorion.dashboard import DashboardData, DashboardHandler
from stackorion.dashboard_storage import BackupError
from stackorion.host_monitor import DEMO_HOST, simulate_host
from stackorion.website_monitor import DEMO_MONITOR, simulate


class DashboardStorageTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config = self.root / "website-config.json"
        self.config.write_text('[{"id":"fixture-site","url":"https://example.com/","expected_status":200}]')
        self.data = DashboardData(self.root / "state", None, True, self.config)
        self.site = self.data.monitors["fixture-site"]
        self.now = datetime.now(timezone.utc)

    def seed_history(self):
        reading = simulate(self.site, "mismatch")
        old = (self.now - timedelta(days=40)).isoformat()
        self.data.live.record_monitor(self.site, {**reading, "observed_at": old}, False)
        self.data.live.record_monitor(self.site, reading, False)
        self.data.live.record_host(DEMO_HOST, simulate_host("high", self.now - timedelta(days=40)), False)
        self.data.live.record_host(DEMO_HOST, simulate_host("high", self.now), False)
        self.data.demo.record_monitor(DEMO_MONITOR, {**simulate(DEMO_MONITOR, "mismatch"), "observed_at": old}, True)
        self.data.schedules.configure(self.site.key, 5, False)

    def test_opening_existing_storage_without_a_policy_leaves_database_unchanged(self):
        self.seed_history()
        path = self.data.live.path
        original = path.read_bytes()
        reopened = DashboardData(self.root / "state", None, True, self.config)
        self.assertIsNone(reopened.storage.summary()["retention_days"])
        self.assertEqual(reopened.storage.summary()["backup_evidence"],
                         {"generated": None, "restore_drill": None})
        stop = reopened.storage.start()
        self.addCleanup(stop.set)
        self.assertEqual(path.read_bytes(), original)
        with reopened.live.connection() as db:
            self.assertIsNone(db.execute(
                "SELECT 1 FROM sqlite_schema WHERE name = 'storage_policy'"
            ).fetchone())

    def test_retention_is_opt_in_and_preserves_work_and_schedules(self):
        self.seed_history()
        storage = self.data.storage
        summary = storage.summary()
        self.assertIsNone(summary["retention_days"])
        self.assertFalse(summary["live"]["encrypted"])
        self.assertGreater(summary["live"]["bytes"], 0)
        self.assertGreater(summary["demo"]["bytes"], 0)
        items = self.data.live.snapshot()["items"]
        self.assertTrue(items)
        history = self.data.live.detail(items[0]["id"])
        schedule = self.data.schedules.describe(self.site.key)

        for value in (True, 0, 3651, 3.5, "30"):
            with self.assertRaises(ValueError):
                storage.configure(value)
        self.assertEqual(len(self.data.live.monitor_history(self.site)), 2)
        self.assertEqual(storage.configure(30), 2)
        self.assertEqual(storage.configure(30), 0)
        self.assertEqual(len(self.data.live.monitor_history(self.site)), 1)
        self.assertEqual(len(self.data.live.monitor_history(DEMO_HOST)), 1)
        self.assertEqual(len(self.data.demo.monitor_history(DEMO_MONITOR)), 1)
        self.assertEqual(self.data.live.detail(items[0]["id"]), history)
        self.assertEqual(self.data.schedules.describe(self.site.key), schedule)

        reopened = DashboardData(self.root / "state", None, False, self.config)
        self.assertEqual(reopened.storage.summary()["retention_days"], 30)
        self.assertEqual(reopened.storage.configure(None), 0)
        self.assertIsNone(reopened.storage.summary()["retention_days"])

    def test_background_retention_applies_when_the_dashboard_starts(self):
        self.data.storage.configure(30)
        self.seed_history()
        stop = self.data.storage.start()
        self.addCleanup(stop.set)
        deadline = time.monotonic() + 3
        while len(self.data.live.monitor_history(self.site)) == 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.data.live.monitor_history(self.site)), 1)
        self.assertEqual(len(self.data.demo.monitor_history(DEMO_MONITOR)), 1)

    def test_missing_age_cannot_fall_back_to_a_plaintext_download(self):
        with patch("stackorion.dashboard_storage.shutil.which", return_value=None):
            self.assertFalse(self.data.storage.summary()["backup_available"])
            with self.assertRaisesRegex(BackupError, "backup_unavailable"):
                self.data.storage.encrypted_backup("age1" + "q" * 58)

    def test_storage_endpoints_reject_foreign_host_and_origin(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(DashboardHandler, data=self.data))
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        self.addCleanup(client.close)

        def request(method, path, payload=None, headers=None):
            client.request(method, path, payload, headers or {})
            response = client.getresponse()
            return response.status, response.read()

        self.assertEqual(request("GET", "/api/storage", headers={"Host": "external.example"})[0], 403)
        self.assertEqual(request("POST", "/api/storage/retention", '{"days":1}', {
            "Content-Type": "application/json", "Origin": "http://external.example",
        })[0], 403)
        status, body = request("GET", "/api/storage")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["retention_days"])
        self.assertEqual(request("POST", "/api/storage/backup", '{"recipient":"-R"}',
                                 {"Content-Type": "application/json"})[0], 400)

    @unittest.skipUnless(shutil.which("age") and shutil.which("age-keygen")
                         and hasattr(sqlite3.Connection, "serialize"), "age and SQLite serialization required")
    def test_backup_decrypts_only_with_identity_and_restores_a_consistent_snapshot(self):
        self.seed_history()
        self.data.storage.configure(30)
        key = self.root / "backup.agekey"
        other = self.root / "other.agekey"
        for path in (key, other):
            subprocess.run(["age-keygen", "-o", str(path)], capture_output=True, check=True)
        recipient = subprocess.check_output(["age-keygen", "-y", str(key)], text=True).strip()
        encrypted = self.data.storage.encrypted_backup(recipient)
        self.assertTrue(encrypted.startswith(b"age-encryption.org/v1\n"))
        self.assertNotIn(b"SQLite format 3", encrypted)
        saved = self.root / "snapshot.tar.gz.age"
        saved.write_bytes(encrypted)
        before = self.data.storage.summary()["backup_evidence"]
        self.assertIsNotNone(before["generated"])
        self.assertIsNone(before["restore_drill"])
        with self.assertRaisesRegex(BackupError, "decryption_failed"):
            self.data.storage.verify_restore(saved, other)
        self.assertEqual(self.data.storage.summary()["backup_evidence"], before)
        drill = self.data.storage.verify_restore(saved, key)
        self.assertEqual(drill["digest"], before["generated"]["digest"])
        self.data.storage.encrypted_backup(recipient)
        reopened = DashboardData(self.root / "state", None, False, self.config)
        evidence = reopened.storage.summary()["backup_evidence"]
        self.assertNotEqual(evidence["generated"]["digest"], drill["digest"])
        self.assertEqual(evidence["restore_drill"], drill)
        self.assertNotEqual(subprocess.run(["age", "-d", "-i", str(other)], input=encrypted,
                                           capture_output=True).returncode, 0)
        plain = subprocess.run(["age", "-d", "-i", str(key)], input=encrypted,
                               capture_output=True, check=True).stdout
        with tarfile.open(fileobj=BytesIO(plain), mode="r:gz") as archive:
            self.assertEqual(archive.getnames(), ["live/work-items.db", "website-monitors.json"])
            self.assertEqual(archive.extractfile("website-monitors.json").read(), self.config.read_bytes())
            destination = self.root / "restored.db"
            destination.write_bytes(archive.extractfile("live/work-items.db").read())
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(db.execute("SELECT count(*) FROM monitor_run").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT count(*) FROM website_schedule").fetchone()[0], 1)
            self.assertGreater(db.execute("SELECT count(*) FROM history").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT monitor_days FROM storage_policy").fetchone()[0], 30)
