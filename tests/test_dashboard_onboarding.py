"""Credential enrollment tests with fixed synthetic provider responses."""

from datetime import datetime, timedelta, timezone
from functools import partial
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import stat
import sys
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from stackorion.catalog import CatalogError
from stackorion.dashboard import DashboardData, DashboardHandler, INSPECTION_TARGET


ZONE = "a" * 32
ACCOUNT = "b" * 32
CLOUDFLARE = {"provider":"cloudflare", "account_id":ACCOUNT, "zone_id":ZONE,
              "domain":"example.com", "token":"fixture-cloudflare-token"}
SPACESHIP = {"provider":"spaceship", "key":"fixture-spaceship-key", "secret":"fixture-spaceship-secret"}


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.enterContext(patch("stackorion.cloudflare.urllib.request.urlopen", side_effect=self.cloudflare))
        self.enterContext(patch("stackorion.spaceship.SpaceshipDomainsAdapter._get_json", side_effect=self.spaceship))
        self.data = DashboardData(self.root / "state", None, False)

    def cloudflare(self, request, timeout):
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://api.cloudflare.com/client/v4/zones/" + ZONE)
        if request.get_header("Authorization") != "Bearer " + CLOUDFLARE["token"]:
            raise HTTPError(request.full_url, 403, "synthetic denied", {}, BytesIO())
        return BytesIO(json.dumps({"success":True,"result":{"id":ZONE,"name":"example.com",
                    "account":{"id":ACCOUNT},"status":"active"}}).encode())

    def spaceship(self, url, key, secret):
        self.assertEqual((key, secret), (SPACESHIP["key"], SPACESHIP["secret"]))
        if url.startswith("https://spaceship.dev/api/v1/domains?"):
            return {"items":[{"name":"other.example.com"}], "total":1}
        self.assertEqual(url, "https://spaceship.dev/api/v1/dns/records/other.example.com?take=100&skip=0")
        return {"items":[{"name":f"r{i}", "type":"TXT", "ttl":300,
                          "value":"fixture-record-content"} for i in range(7)], "total":7}

    def test_denied_connection_is_not_saved(self):
        bad = {**CLOUDFLARE, "token":"fixture-denied"}
        result = self.data.onboarding.test(bad)
        self.assertEqual(result["status"], "denied")
        self.assertEqual(result["required_scope"], "Zone: Read")
        self.assertNotIn(bad["token"], json.dumps(result))
        self.assertFalse(self.data.onboarding.save(bad)["saved"])
        self.assertFalse(self.data.onboarding.path.exists())
        self.assertEqual(list(self.data.onboarding.root.iterdir()), [])

    def test_rejects_newline_credentials(self):
        with self.assertRaises(ValueError):
            self.data.onboarding.test({**SPACESHIP, "key":"key\ninjection=yes"})

    def test_both_connectors_save_reload_and_feed_read_only_inventory(self):
        before = self.data.snapshot(False)
        self.assertEqual(before["inspection_setup"], {"configured": False, "editable": True})
        self.assertIsNone(before["report"])
        for payload in (CLOUDFLARE, SPACESHIP):
            result = self.data.onboarding.test(payload)
            self.assertEqual(result["status"], "observed")
            self.assertEqual(result["count"], 1)
            if payload is CLOUDFLARE:
                self.assertFalse(self.data.onboarding.path.exists())
            self.assertTrue(self.data.onboarding.save(payload)["saved"])
        reopened = DashboardData(self.root / "state", None, False)
        ready = reopened.snapshot(False)
        self.assertEqual(ready["inspection_setup"], {"configured": True, "editable": True})
        self.assertIsNone(ready["report"])
        self.assertEqual(len(reopened.onboarding.summary()["connections"]), 2)
        catalog = reopened.connections()
        declared = [catalog.get(row["id"]) for row in catalog.list()]
        for entry in declared:
            secret = Path(entry.settings["credential_file"])
            self.assertEqual(stat.S_IMODE(secret.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(secret.parent.stat().st_mode), 0o700)
        for value in [CLOUDFLARE["token"], SPACESHIP["key"], SPACESHIP["secret"]]:
            self.assertNotIn(value, reopened.onboarding.path.read_text())
            self.assertNotIn(value, json.dumps(reopened.onboarding.summary()))
        self.assertEqual(reopened.onboarding.save(CLOUDFLARE)["status"], "already_configured")
        self.assertEqual(reopened.run("daily", False)["report"]["expected_accounts"], 2)
        self.assertEqual({a["name"] for a in reopened.assets(False)["assets"]},
                         {"example.com", "other.example.com"})

    def test_multiple_connections_keep_existing_identity_and_credentials(self):
        first_cf = {**CLOUDFLARE, "label": "个人官网"}
        next_cf = {**CLOUDFLARE, "zone_id": "c" * 32, "account_id": "d" * 32, "domain": "project.example",
                   "token": "fixture-project-token", "label": "项目官网"}
        next_ship = {**SPACESHIP, "key": "fixture-project-key", "secret": "fixture-project-secret",
                     "label": "项目域名"}

        def cloudflare(request, timeout):
            if request.full_url.endswith(ZONE):
                return self.cloudflare(request, timeout)
            self.assertEqual(request.get_method(), "GET")
            self.assertTrue(request.full_url.endswith(next_cf["zone_id"]))
            self.assertEqual(request.get_header("Authorization"), "Bearer " + next_cf["token"])
            return BytesIO(json.dumps({"success": True, "result": {
                "id": next_cf["zone_id"], "name": next_cf["domain"], "status": "active",
                "account": {"id": next_cf["account_id"]},
            }}).encode())

        def spaceship(url, key, secret):
            if key == SPACESHIP["key"]:
                return self.spaceship(url, key, secret)
            self.assertEqual((key, secret), (next_ship["key"], next_ship["secret"]))
            self.assertTrue(url.startswith("https://spaceship.dev/api/v1/domains?"))
            return {"items": [{"name": "project-registry.example"}], "total": 1}

        onboarding = self.data.onboarding
        self.assertEqual(onboarding.save(first_cf)["service_id"], "starter-cloudflare")
        self.assertEqual(onboarding.save(SPACESHIP)["service_id"], "starter-spaceship")
        originals = {Path(onboarding.catalog().get(row["id"]).settings["credential_file"])
                     for row in onboarding.catalog().list()}
        original_bytes = {path: path.read_bytes() for path in originals}

        with patch("stackorion.cloudflare.urllib.request.urlopen", side_effect=cloudflare), \
                patch("stackorion.spaceship.SpaceshipDomainsAdapter._get_json", side_effect=spaceship):
            for payload in (next_cf, next_ship):
                self.assertEqual(onboarding.test(payload)["status"], "observed")
                self.assertEqual(onboarding.save(payload)["service_id"], f"starter-{payload['provider']}-2")
            reopened = DashboardData(self.root / "state", None, False)
            self.assertEqual(reopened.run("daily", False)["report"]["expected_accounts"], 4)
            self.assertEqual({asset["name"]: asset["account_label"] for asset in reopened.assets(False)["assets"]}, {
                "example.com": "个人官网", "project.example": "项目官网",
                "other.example.com": "starter-spaceship", "project-registry.example": "项目域名",
            })
        self.assertEqual({path: path.read_bytes() for path in originals}, original_bytes)
        config_before = onboarding.path.read_bytes()
        with patch.object(onboarding, "probe", side_effect=AssertionError("Duplicates must be rejected before probing")):
            self.assertEqual(onboarding.test({**first_cf, "token": "rotated-fixture"})["status"], "already_configured")
            self.assertEqual(onboarding.save({**next_ship, "secret": "rotated-fixture"})["status"], "already_configured")
            self.assertEqual(onboarding.test({**next_cf, "zone_id": "e" * 32, "label": " 个人官网 "})["status"],
                             "connection_name_used")
        self.assertEqual(onboarding.path.read_bytes(), config_before)
        summary = onboarding.summary()
        self.assertEqual(len(summary["connections"]), 4)
        text = config_before.decode() + json.dumps(summary)
        for secret in (next_ship["key"], next_ship["secret"], next_cf["token"]):
            self.assertNotIn(secret, text)

    def test_save_rechecks_duplicate_after_read_only_validation(self):
        onboarding = self.data.onboarding
        validate = onboarding._validate_connection

        def validate_after_other_save(provider, payload):
            result = validate(provider, payload)
            with patch.object(onboarding, "_validate_connection", side_effect=validate):
                other = onboarding.save(payload)
            self.assertTrue(other["saved"])
            return result

        with patch.object(onboarding, "_validate_connection", side_effect=validate_after_other_save):
            result = onboarding.save({**CLOUDFLARE, "label": "项目官网"})
        self.assertEqual(result, {"status": "already_configured", "saved": False})
        self.assertEqual(len(onboarding.catalog().list()), 1)
        self.assertEqual(len(list(onboarding.root.glob("starter-cloudflare-*/credential.env"))), 1)

    def test_pause_and_resume_only_change_local_connection_state(self):
        data = DashboardData(self.root / "state", None, True)
        for payload in (CLOUDFLARE, SPACESHIP):
            self.assertTrue(data.onboarding.save(payload)["saved"])
        service_id = "starter-spaceship"
        account_id = "live:spaceship:" + service_id
        credential = Path(data.connections().get(service_id).settings["credential_file"])
        old_credential = credential.read_bytes()
        expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

        def approaching(url, key, secret):
            result = self.spaceship(url, key, secret)
            for item in result["items"]:
                item["expirationDate"] = expires
            return result

        with patch("stackorion.spaceship.SpaceshipDomainsAdapter._get_json", side_effect=approaching):
            snapshot = data.run("daily", False)
        [due] = [item for item in snapshot["items"] if item["evidence"]["kind"] == "expiry"]
        request = {"service_id": service_id}
        self.assertEqual(data.onboarding.check(data.connections(), request)["status"], "observed")
        plan = data.configure_inspection_plan({"enabled": True, "interval_minutes": 1440, "account_ids": [account_id]})
        with patch("stackorion.spaceship.SpaceshipDomainsAdapter._get_json") as spaceship:
            paused = data.onboarding.change_state({**request, "enabled": False})
            self.assertFalse(paused["enabled"])
            self.assertFalse(paused["can_check"])
            self.assertNotIn("last_check", paused)
            with self.assertRaises(ValueError):
                data.onboarding.check(data.connections(), request)
            self.assertEqual(data._collect_inspection_slot(INSPECTION_TARGET), {"status": "unknown"})
            self.assertIsNone(data.run_inspection_summary({"account_ids": [account_id], "show_mock": False}))
            after = data.run("daily", True)
            self.assertIn("starter-cloudflare", {row["label"] for row in after["report"]["accounts"]})
            self.assertNotIn("Spaceship", {row["provider"] for row in after["report"]["accounts"]})
            self.assertEqual(data.detail(due["id"], False)["status"], "open")
            spaceship.assert_not_called()
        self.assertEqual(data.inspection_plan()["account_ids"], plan["account_ids"])
        self.assertEqual(data.inspection_plan()["next_at"], plan["next_at"])
        saved = data.onboarding.path.read_bytes()
        self.assertFalse(data.onboarding.change_state({**request, "enabled": False})["enabled"])
        self.assertEqual(data.onboarding.path.read_bytes(), saved)
        reopened = DashboardData(self.root / "state", None, True)
        self.assertFalse(reopened.connections().get(service_id).enabled)
        resumed = reopened.onboarding.change_state({**request, "enabled": True})
        self.assertTrue(resumed["enabled"])
        self.assertTrue(resumed["can_check"])
        self.assertEqual(reopened.onboarding.check(reopened.connections(), request)["status"], "observed")
        self.assertEqual(reopened.inspection_plan()["account_ids"], [account_id])
        self.assertEqual(credential.read_bytes(), old_credential)
        self.assertEqual(reopened.detail(due["id"], False)["status"], "open")

    def test_cloudflare_credential_replacement_validates_original_zone_before_commit(self):
        onboarding = self.data.onboarding
        self.assertTrue(onboarding.save({**CLOUDFLARE, "label": "项目官网"})["saved"])
        catalog = self.data.connections()
        service = catalog.get("starter-cloudflare")
        credential = Path(service.settings["credential_file"])
        previous = credential.read_bytes()
        config = onboarding.path.read_bytes()
        onboarding.check(catalog, {"service_id": service.id})

        def respond(request, timeout):
            self.assertEqual(request.get_method(), "GET")
            self.assertTrue(request.full_url.endswith(ZONE))
            token = request.get_header("Authorization")
            if token == "Bearer fixture-denied":
                raise HTTPError(request.full_url, 403, "synthetic denied", {}, BytesIO())
            account = ACCOUNT if token == "Bearer fixture-rotated" else "c" * 32
            return BytesIO(json.dumps({"success": True, "result": {
                "id": ZONE, "name": "example.com", "account": {"id": account},
            }}).encode())

        with patch("stackorion.cloudflare.urllib.request.urlopen", side_effect=respond):
            for token, expected in (("fixture-denied", "denied"), ("fixture-other", "mismatch")):
                result = onboarding.replace_credential({"service_id": service.id, "credential": token})
                self.assertEqual(result, {"saved": False, "status": expected})
                self.assertEqual(credential.read_bytes(), previous)
            self.assertEqual(onboarding.replace_credential({"service_id": service.id, "credential": "fixture-rotated"}),
                             {"saved": True, "status": "observed"})
            self.assertNotIn("last_check", onboarding.summary()["connections"][0])
            self.assertEqual(onboarding.check(self.data.connections(), {"service_id":service.id})["status"], "observed")
        self.assertEqual(onboarding.path.read_bytes(), config)
        self.assertEqual(stat.S_IMODE(credential.stat().st_mode), 0o600)
        self.assertFalse(list(credential.parent.glob("credential-check-*")))

    def test_spaceship_secret_replacement_uses_original_key_and_protects_newer_file(self):
        onboarding = self.data.onboarding
        self.assertTrue(onboarding.save(SPACESHIP)["saved"])
        service_id = "starter-spaceship"
        credential = Path(self.data.connections().get(service_id).settings["credential_file"])
        previous = credential.read_bytes()
        later_secret = "fixture-later-secret"

        def respond(url, key, secret):
            self.assertEqual(key, SPACESHIP["key"])
            self.assertTrue(url.startswith("https://spaceship.dev/api/v1/domains?"))
            if secret == "fixture-partial":
                return {"items": [], "total": 1}
            if secret == "fixture-raced":
                credential.write_bytes(previous.replace(SPACESHIP["secret"].encode(), later_secret.encode()))
            return {"items": [{"name": "other.example.com"}], "total": 1}

        with patch("stackorion.spaceship.SpaceshipDomainsAdapter._get_json", side_effect=respond):
            result = onboarding.replace_credential({"service_id": service_id,"credential": "fixture-partial"})
            self.assertEqual(result, {"status": "partial", "saved": False})
            self.assertEqual(credential.read_bytes(), previous)
            result = onboarding.replace_credential({"service_id": service_id,"credential": "fixture-raced"})
            self.assertEqual(result, {"status": "connection_changed", "saved": False})
            self.assertIn(later_secret.encode(), credential.read_bytes())
            result = onboarding.replace_credential({"service_id": service_id,"credential": "fixture-key-unchanged"})
            self.assertTrue(result["saved"])
        self.assertIn(SPACESHIP["key"].encode(), credential.read_bytes())
        self.assertNotIn(later_secret.encode(), credential.read_bytes())
        self.assertNotIn("fixture-key-unchanged", json.dumps(onboarding.summary()))

    def test_registered_connections_use_existing_files_and_keep_safe_check_results(self):
        for payload in (CLOUDFLARE, SPACESHIP):
            self.assertTrue(self.data.onboarding.save(payload)["saved"])
        path = self.data.onboarding.path
        configured = DashboardData(self.root / "external", path, False)
        original = path.read_bytes()
        services = configured.connections()
        tokens = [CLOUDFLARE["token"], SPACESHIP["key"], SPACESHIP["secret"]]
        for row in configured.onboarding.summary(services)["connections"]:
            self.assertTrue(row["can_check"])
            result = configured.onboarding.check(services, {"service_id":row["id"]})
            self.assertEqual(result["status"], "observed")
            self.assertEqual(result["count"], 1)
            self.assertIn("checked_at", result)
            if row["provider"] == "spaceship":
                self.assertEqual(result["domains"], ["other.example.com"])
                self.assertNotIn("dns", result)
                with self.assertRaisesRegex(ValueError, "not visible"):
                    configured.onboarding.check(services, {"service_id":row["id"], "domain":"unlisted.example"})
                result = configured.onboarding.check(services, {"service_id":row["id"], "domain":"other.example.com"})
                self.assertEqual(result["dns"], {"target":"other.example.com", "scope":"dnsrecords:read",
                    "status":"observed", "count":7,
                    "records":[{"name":f"r{i}", "type":"TXT", "ttl":300} for i in range(7)]})
                self.assertNotIn("fixture-record-content", json.dumps(result))
        summary = configured.onboarding.summary(services)
        self.assertFalse(summary["editable"])
        self.assertTrue(all(row["last_check"]["status"] == "observed" for row in summary["connections"]))
        for token in tokens:
            self.assertNotIn(token, json.dumps(summary))
        self.assertEqual(original, path.read_bytes())
        self.assertFalse(configured.onboarding.root.exists())

    def test_spaceship_dns_denial_does_not_hide_readable_domains(self):
        self.assertTrue(self.data.onboarding.save(SPACESHIP)["saved"])
        def respond(url, key, secret):
            if "/dns/records/" in url:
                raise CatalogError("Spaceship API HTTP 403")
            return self.spaceship(url, key, secret)
        with patch("stackorion.spaceship.SpaceshipDomainsAdapter._get_json", side_effect=respond):
            result = self.data.onboarding.check(self.data.connections(), {"service_id":"starter-spaceship",
                                                                      "domain":"other.example.com"})
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["dns"], {"target":"other.example.com", "scope":"dnsrecords:read", "status":"denied"})

    def test_spaceship_checks_selected_domain_and_reads_each_dns_page(self):
        self.assertTrue(self.data.onboarding.save(SPACESHIP)["saved"])
        seen = []

        def respond(url, key, secret):
            if "/domains?" in url:
                return {"items": [{"name": "first.example.com"}, {"name": "other.example.com"}], "total": 2}
            offset = [0, 100][len(seen)]
            self.assertTrue(url.endswith(f"/other.example.com?take=100&skip={offset}"))
            seen.append(offset)
            return {"items": [{"name": f"r{i}", "type": "TXT"} for i in range(offset, min(101, offset + 100))],
                    "total": 101}

        with patch("stackorion.spaceship.SpaceshipDomainsAdapter._get_json", side_effect=respond):
            catalog = self.data.connections()
            connection = {"service_id": "starter-spaceship"}
            self.assertEqual(self.data.onboarding.check(catalog, connection)["domains"],
                             ["first.example.com", "other.example.com"])
            self.assertEqual(seen, [])
            dns = self.data.onboarding.check(catalog, {**connection, "domain": "other.example.com"})["dns"]
        self.assertEqual(seen, [0, 100])
        self.assertEqual((dns["status"], dns["count"], len(dns["records"])), ("observed", 101, 101))
        self.assertEqual(dns["records"][-1], {"name": "r100", "type": "TXT", "ttl": None})

    def test_existing_catalog_and_foreign_origins_are_protected(self):
        original = self.root / "existing.json"
        data = b'{"schema_version":1,"services":[]}\n'
        original.write_bytes(data)
        existing = DashboardData(self.root / "locked-state", original, False)
        self.assertFalse(existing.onboarding.summary()["editable"])
        self.assertEqual(existing.snapshot(False)["inspection_setup"], {"configured": False, "editable": False})
        with self.assertRaises(ValueError):
            existing.onboarding.save(CLOUDFLARE)
        with self.assertRaises(ValueError):
            existing.onboarding.change_state({"service_id": "starter-cloudflare", "enabled": False})
        with self.assertRaises(ValueError):
            existing.onboarding.replace_credential({"service_id": "starter-cloudflare", "credential": "fixture-denied"})
        self.assertEqual(original.read_bytes(), data)

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(DashboardHandler, data=self.data))
        self.addCleanup(server.server_close)
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        client = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
        self.addCleanup(client.close)
        for host, origin in (("foreign.example", f"http://127.0.0.1:{server.server_port}"),
                             (f"127.0.0.1:{server.server_port}", "https://foreign.example")):
            client.request("POST", "/api/onboarding/save", json.dumps(CLOUDFLARE),
                           {"Content-Type":"application/json", "Host":host, "Origin":origin})
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            client.request("POST", "/api/onboarding/state", json.dumps({"service_id":"starter-cloudflare","enabled":False}),
                           {"Content-Type":"application/json", "Host":host, "Origin":origin})
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            client.request("POST", "/api/onboarding/credential",
                           json.dumps({"service_id":"starter-cloudflare","credential":"fixture-denied"}),
                           {"Content-Type":"application/json", "Host":host, "Origin":origin})
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        self.assertFalse(self.data.onboarding.path.exists())


if __name__ == "__main__":
    fixture = OnboardingTests()
    try:
        fixture.setUp()
        with ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])),
                                 partial(DashboardHandler, data=fixture.data)) as server:
            server.serve_forever()
    finally:
        fixture.doCleanups()
