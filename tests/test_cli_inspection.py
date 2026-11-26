from contextlib import redirect_stdout
from functools import partial
from http.server import ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from stackorion import cli
from stackorion.dashboard import DashboardData, DashboardHandler, collect
from stackorion.dashboard_bridge import get_inspection


class InspectionCLITests(unittest.TestCase):
    def test_cli_run_and_reads_share_the_panel_state_and_hide_demo_by_default(self):
        with TemporaryDirectory() as temporary:
            data = DashboardData(Path(temporary), None, True)
            handler = partial(DashboardHandler, data=data)
            with ThreadingHTTPServer(("127.0.0.1", 0), handler) as panel:
                worker = Thread(target=panel.serve_forever, daemon=True)
                worker.start()
                origin = f"http://127.0.0.1:{panel.server_port}"
                try:
                    with patch.dict(os.environ, {"STACKORION_DASHBOARD_URL":origin}, clear=True), \
                            patch("stackorion.dashboard.collect", wraps=collect) as reader:
                        def command(*args):
                            output = StringIO()
                            with patch("sys.argv", ["stackorion", *args]), redirect_stdout(output):
                                cli.main()
                            return json.loads(output.getvalue())

                        self.assertEqual(command("inspection")["status"], "empty")
                        self.assertEqual(command("work-items"), {"items":[]})

                        def attempt(host, request_origin, body):
                            headers = {"Content-Type":"application/json", "Host":host}
                            if request_origin is not None:
                                headers["Origin"] = request_origin
                            request = Request(origin+"/api/inspection/summary", json.dumps(body).encode(), headers)
                            with self.assertRaises(HTTPError) as rejected:
                                build_opener(ProxyHandler({})).open(request, timeout=3)
                            rejected.exception.close()
                            return rejected.exception.code

                        authority = origin.removeprefix("http://")
                        self.assertEqual(attempt(authority, None, {"show_mock":True}), 403)
                        self.assertEqual(attempt(authority, "https://example.test", {"show_mock":True}), 403)
                        self.assertEqual(attempt("example.test", origin, {"show_mock":True}), 403)
                        self.assertEqual(attempt(authority, origin, {"show_mock":"true"}), 400)
                        reader.assert_not_called()

                        run = command("inspection", "run", "--include-demo")
                        self.assertTrue(run.pop("recorded"))
                        self.assertEqual(run, get_inspection(True))
                        self.assertEqual(run, command("inspection", "latest", "--include-demo"))
                        self.assertEqual(command("inspection")["expected_accounts"], 0)
                        self.assertEqual(command("work-items"), {"items":[]})
                        [finding] = [row for row in command("work-items", "--include-demo")["items"] if row["kind"] == "ssh"]
                        self.assertEqual(finding["data_source"], "synthetic")
                        self.assertNotIn("evidence", finding)
                        detail = command("work-item", finding["id"], "--include-demo")
                        self.assertEqual(detail["id"], finding["id"])
                        self.assertTrue(detail["history"])
                        with patch("sys.argv", ["stackorion", "work-item", finding["id"]]), redirect_stdout(StringIO()):
                            with self.assertRaises(SystemExit) as hidden:
                                cli.main()
                        self.assertEqual(hidden.exception.code, 2)
                        reader.assert_called_once()
                finally:
                    panel.shutdown()
                    worker.join(timeout=5)
