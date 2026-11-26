import json
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from stackorion.dashboard import DashboardData, INSPECTION_TARGET, demo_inspection
from stackorion.live_dashboard import collect as collect_live
from stackorion.work_items import WorkItems


class InspectionScheduleTests(unittest.TestCase):
    def test_scoped_plan_reads_only_selected_accounts_and_preserves_other_findings_and_inventory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            credential = root / "fixture.env"
            credential.touch()
            config = root / "services.json"
            names = ("fixture-a", "fixture-b")
            services = [{"id":name, "kind":"python-adapter", "provider":"spaceship",
                         "adapter":"spaceship-management", "enabled":True,
                         "settings":{"credential_file":str(credential)}} for name in names]
            config.write_text(json.dumps({"schema_version":1, "services":services}))
            panel = DashboardData(root / "state", config, False)
            first, second = ("live:spaceship:" + name for name in names)
            enable = {"enabled":True, "interval_minutes":1, "account_ids":[first]}
            self.assertIsNone(panel.configure_inspection_plan({**enable, "account_ids":["live:spaceship:other"]}))
            self.assertEqual(panel.configure_inspection_plan(enable)["account_ids"], [first])
            paused = panel.configure_inspection_plan({"enabled":False,"interval_minutes":1})
            self.assertEqual(paused["account_ids"], [first])
            panel = DashboardData(root / "state", config, False)
            self.assertEqual(panel.inspection_plan()["account_ids"], [first])
            with patch("stackorion.catalog.Catalog.adapter", autospec=True) as adapter, \
                    patch("stackorion.live_dashboard.read_scope", return_value=("complete", [], [], None)):
                report = collect_live(panel.connections(), account_ids=[second])
            self.assertEqual([row["id"] for row in report["accounts"]], [second])
            self.assertEqual([call.args[1].id for call in adapter.call_args_list], [names[1]])

            full = demo_inspection("daily")
            template = next(row for row in full["findings"] if row["kind"] == "ssh")
            full["findings"] = [{**template,"account":account} for account in (first, second)]
            full["accounts"] = [{**full["accounts"][0],"id":account} for account in (first, second)]
            full["assets"] = [{"id":account,"kind":"domain","account":account} for account in (first, second)]
            original = panel.live.record(full)["items"]
            before = {item["evidence"]["account"]:panel.live.detail(item["id"]) for item in original}
            selected = {**report, "observed_at":(datetime.fromisoformat(full["observed_at"])+timedelta(minutes=1)).isoformat()}
            selected["accounts"][0]["checks"]["安全组"] = "complete"
            plan_before = panel.inspection_plan()
            with patch("stackorion.dashboard.collect", return_value=selected) as run:
                self.assertIsNone(panel.run_inspection_summary({"show_mock":False,"account_ids":["live:spaceship:other"]}))
                with self.assertRaises(ValueError):
                    panel.run_inspection_summary({"show_mock":True,"account_ids":[second]})
                summary = panel.run_inspection_summary({"show_mock":False,"account_ids":[second]})
                run.assert_called_once()
                self.assertEqual(run.call_args.kwargs["account_ids"], [second])
            self.assertTrue(summary["recorded"])
            self.assertEqual(summary["selected_accounts"], [second])
            plan_after = panel.inspection_plan()
            self.assertEqual(plan_after, {**plan_before, "last_at":selected["observed_at"]})
            self.assertEqual(panel.live.detail(before[first]["id"]), before[first])
            self.assertEqual(panel.live.detail(before[second]["id"])["status"], "resolved")
            [retained] = panel.live.inventory_report()["assets"]
            self.assertEqual(retained, {**full["assets"][0], "observed_at": full["observed_at"]})
            self.assertEqual(panel.inspection_summary(False)["selected_accounts"], [second])

    def test_inventory_preserves_per_scope_age_and_merges_consecutive_selected_runs(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            store = WorkItems(root)
            base = demo_inspection("daily")
            first, second = "live:cloudflare:first", "live:cloudflare:second"
            scope_a, scope_b = "cf-a:域名:configured", "cf-b:域名:configured"

            def account(account_id, domain_status, complete_scopes=()):
                return {**base["accounts"][0], "id": account_id,
                        "checks": {"域名": domain_status}, "complete_scopes": list(complete_scopes)}

            def domain(name, account_id, scope):
                return {"id": name, "kind": "domain", "account": account_id, "scope": scope}

            def report(minute, accounts, assets, *, scoped=True):
                stamp = (datetime.fromisoformat(base["observed_at"]) + timedelta(minutes=minute)).isoformat()
                row = {**base, "observed_at": stamp, "accounts": accounts, "assets": assets, "findings": []}
                if scoped:
                    row["selected_accounts"] = [item["id"] for item in accounts]
                return row

            old = report(0, [account(first, "complete"), account(second, "complete")], [
                domain("deleted.example", first, scope_a), domain("same.example", first, scope_a),
                domain("same.example", first, scope_b), domain("old.example", second, scope_b),
            ], scoped=False)
            update = report(1, [account(first, "partial", [scope_a])], [
                domain("same.example", first, scope_a),
            ])
            # Seed the format written by the previous runtime after its first scoped run.
            with store.connection() as db:
                db.execute("INSERT INTO last_full_inspection VALUES (1, ?)", (json.dumps(old),))
                db.execute("INSERT INTO inspection VALUES (1, ?)", (json.dumps(update),))
            after_upgrade = store.inventory_report()
            self.assertEqual(len(after_upgrade["assets"]), 3)
            assets = {(row["id"], row["scope"]): row for row in after_upgrade["assets"]}
            self.assertEqual(assets[("same.example", scope_a)]["observed_at"], update["observed_at"])
            lost = assets[("same.example", scope_b)]
            self.assertEqual(lost["observed_at"], old["observed_at"])
            self.assertTrue(lost["inventory_retained"])
            self.assertNotIn("inventory_retained", assets[("old.example", scope_b)])

            second_update = report(2, [account(second, "complete")], [domain("new.example", second, scope_b)])
            self.assertTrue(store.record(second_update)["recorded"])
            self.assertFalse(store.record(update)["recorded"])
            reopened = WorkItems(root).inventory_report()
            self.assertEqual({(row["id"], row["scope"]) for row in reopened["assets"]}, {
                ("same.example", scope_a), ("same.example", scope_b), ("new.example", scope_b),
            })
            self.assertIn(lost, reopened["assets"])
            self.assertEqual({row["id"] for row in reopened["accounts"]}, {first, second})

            gone = report(3, [account(first, "complete")], [], scoped=False)
            store.record(gone)
            self.assertEqual(store.inventory_report()["assets"], [])

    def test_plan_persists_and_uses_shared_results_without_replaying_missed_slots(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "services.json"
            service = {"id":"fixture", "kind":"python-adapter", "provider":"spaceship",
                       "adapter":"spaceship-management", "settings":{}}

            def configure_catalog(enabled):
                config.write_text(json.dumps({"schema_version":1, "services":[{**service, "enabled":enabled}]}))

            configure_catalog(False)
            panel = DashboardData(root / "state", config, False)
            enable = {"enabled":True, "interval_minutes":1}
            self.assertEqual(panel.inspection_plan()["interval_minutes"], 1440)
            self.assertIsNone(panel.configure_inspection_plan(enable))
            configure_catalog(True)

            now = 1_000
            panel.inspection_schedules.clock = lambda: now
            scheduled = panel.configure_inspection_plan(enable)
            self.assertTrue(scheduled["enabled"])
            self.assertIsNone(scheduled["last_at"])
            now += 15
            self.assertEqual(panel.configure_inspection_plan(enable)["next_at"], scheduled["next_at"])

            panel = DashboardData(root / "state", config, False)
            panel.inspection_schedules.clock = lambda: now

            def tick():
                panel.inspection_schedules.run_due((INSPECTION_TARGET,), panel._collect_inspection_slot)

            report = demo_inspection("daily")
            with patch("stackorion.dashboard.collect", return_value=report) as collect:
                tick()
                collect.assert_not_called()
                now = 1_180
                tick()
                tick()
                collect.assert_called_once()
            completed = panel.inspection_plan()
            self.assertEqual(completed["missed"], 2)
            self.assertEqual(completed["passed"], 1)
            self.assertEqual(completed["last_at"], report["observed_at"])
            self.assertEqual(panel.inspection_summary(False)["counts"], report["counts"])
            self.assertEqual(len(panel.snapshot(False)["items"]), 5)
            [run] = panel.live.inspection_history()["runs"]
            self.assertEqual(run["trigger"], "scheduled")

            # Losing the configured scope does not replace the saved observation.
            configure_catalog(False)
            now = 1_240
            tick()
            self.assertEqual(panel.inspection_plan()["missed"], 3)
            self.assertEqual(panel.inspection_plan()["last_at"], completed["last_at"])
            panel.configure_inspection_plan({**enable, "enabled":False})
            now = 1_600
            with patch("stackorion.dashboard.collect") as collect:
                tick()
                collect.assert_not_called()
            self.assertEqual(panel.inspection_plan()["missed"], 3)
            self.assertIsNone(panel.inspection_plan()["next_at"])
            self.assertEqual(panel.live.inspection_history()["runs"], [run])
