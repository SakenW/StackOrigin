import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from stackorion.doctor import diagnose


class DoctorTests(unittest.TestCase):
    def test_initial_setup_reports_attention_without_creating_state_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "new-state"
            with patch.dict(os.environ, {"STACKORION_STATE_DIR": str(state)}, clear=True), \
                    patch("stackorion.doctor.request_dashboard", return_value=("", {"editable":True, "connections":[]})) as request:
                report = diagnose()
            checks = {row["check"]:row for row in report["checks"]}
            self.assertEqual(report["status"], "attention")
            self.assertEqual(checks["dashboard"]["enabled_connections"], 0)
            self.assertEqual(checks["state_dir"]["status"], "attention")
            self.assertFalse(state.exists())
            request.assert_called_once_with("/api/onboarding", address="http://127.0.0.1:8765", timeout=2)

    def test_missing_dependency_is_an_error_only_for_an_enabled_connector(self):
        panel = {"editable":True,"connections":[{"enabled":True,"adapter":"spaceship-domains",
                                                 "id":"private-account-label","credential":"private-value"}]}
        with patch.dict(os.environ, {}, clear=True), \
                patch("stackorion.doctor.request_dashboard", return_value=("", panel)), \
                patch("stackorion.doctor.which", return_value=None):
            report = diagnose()
        checks = {row["check"]:row for row in report["checks"]}
        self.assertEqual(report["status"], "error")
        self.assertEqual(checks["curl"]["status"], "error")
        self.assertEqual(checks["ssh"]["status"], "optional_missing")
        self.assertNotIn("private-", json.dumps(report))

    def test_invalid_catalog_and_insecure_state_directory_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "public"
            state.mkdir(mode=0o755)
            state.chmod(0o755)
            with patch.dict(os.environ, {"STACKORION_STATE_DIR":str(state),"STACKORION_CONFIG":"relative.json",
                                         "STACKORION_DASHBOARD_URL":"http://example.test:8765"}, clear=True), \
                    patch("stackorion.dashboard_bridge.build_opener") as opener:
                report = diagnose()
            checks = {row["check"]:row for row in report["checks"]}
            self.assertEqual(checks["catalog"]["status"], "error")
            self.assertEqual(checks["state_dir"]["status"], "error")
            self.assertEqual(checks["dashboard"]["status"], "attention")
            self.assertEqual(state.stat().st_mode & 0o777, 0o755)
            opener.assert_not_called()
