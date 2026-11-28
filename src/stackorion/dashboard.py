"""Loopback-only dashboard and optional MCP server for synthetic inspections."""

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .dashboard_onboarding import DashboardOnboarding
from .dashboard_operations import DashboardOperations
from .dashboard_storage import BackupError, DashboardStorage
from .domain_observation import HOST, observe
from .inspection import balance_status, expiry_status, inspection_summary, is_public_ssh
from .work_items import WorkItems
from .website_monitor import DEMO_MONITOR, Monitor, certificate_status, load_monitors, probe, simulate
from .host_disk_trend import disk_projection
from .monitor_schedule import MonitorSchedules
from .host_monitor import (
    AdapterHostMonitor, DEMO_HOST, collect_host, host_candidates, host_identity, load_hosts, restore_host, simulate_host,
)
from .catalog import CatalogError
from .live_dashboard import DOMAIN_DELIVERY_ADAPTERS, PROVIDERS, collect, fallback_providers, inspection_targets, load_connections


OBSERVED_AT = "2026-11-23T09:30:00+08:00"
SCENARIOS = {"daily": 0, "repeat": 1, "missed": 2, "empty": 3}
INSPECTION_TARGET = SimpleNamespace(key="inspection:daily")


def asset_route_id(asset, *, by_scope=False):
    identity = [asset["data_source"], asset["kind"], asset["account"], asset.get("region"), asset["id"]]
    if by_scope:
        identity.append(asset.get("scope"))
    return sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


ROOT = files("stackorion").joinpath("web")
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
    "/resource-operation.js": ("resource-operation.js", "text/javascript; charset=utf-8"),
    "/onboarding.js": ("onboarding.js", "text/javascript; charset=utf-8"),
    "/storage.js": ("storage.js", "text/javascript; charset=utf-8"),
    "/host-trend.js": ("host-trend.js", "text/javascript; charset=utf-8"),
    "/uPlot.iife.min.js": ("uPlot.iife.min.js", "text/javascript; charset=utf-8"),
    "/uPlot.min.css": ("uPlot.min.css", "text/css; charset=utf-8"),
    "/stackorion-brand.svg": ("stackorion-brand.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
}


def load_demo_fixture():
    return json.loads(ROOT.joinpath("inspection.json").read_text(encoding="utf-8"))


def demo_assets():
    """Return the synthetic asset catalog independently of inspection observations."""
    fixture = load_demo_fixture()
    assets = [*fixture["assets"], *(
        {**instance, "kind": "server", "server_type": "cloud", "product": "七牛云 QVM"}
        for instance in fixture["instances"]
    )]
    return {"source": "synthetic", "snapshot_at": OBSERVED_AT, "assets": [
        {**asset, "account_label": fixture["accounts"][asset["account"]]["label"]}
        for asset in assets
    ]}


def demo_inspection(scenario="daily"):
    """Return identical synthetic inspection results for Web and MCP."""
    if scenario not in SCENARIOS:
        raise ValueError("Unknown demo scenario")
    observed_at = datetime.fromisoformat(OBSERVED_AT) + timedelta(days=SCENARIOS[scenario])
    fixture = load_demo_fixture()
    accounts = fixture["accounts"]
    if scenario == "missed":
        account = accounts["tencent-prod"]
        account["checks"]["安全组"] = "partial"
        account["reason"] = "生产安全组复查超时"
        account["last_success"] = (observed_at - timedelta(days=1)).isoformat()
    findings = []
    coverage = dict.fromkeys(("complete", "partial", "unavailable", "disabled"), 0)

    def add(kind, item, status):
        account = accounts[item["account"]]
        findings.append({**item, "kind": kind, "status": status,
                         "provider": account["provider"], "account_label": account["label"]})

    for key, account in accounts.items():
        checks = account["checks"]
        if scenario == "empty" and account["enabled"]:
            checks = {name: "complete" for name in checks}
        account["checks"] = checks
        complete = sum(state == "complete" for state in checks.values())
        if not account["enabled"]:
            state = "disabled"
        elif complete == len(checks):
            state = "complete"
        else:
            state = "partial" if complete else "unavailable"
        account["status"] = state
        coverage[state] += 1
        if state in {"partial", "unavailable"}:
            add("coverage", {"id": key, "account": key, "name": account["label"],
                             "reason": account["reason"], "last_success": account["last_success"],
                             "checks": checks}, state)

    if scenario != "empty":
        for item in fixture["balances"]:
            status = balance_status(item["amount"], item["threshold"])
            if status == "low":
                add("balance", item, status)
        for item in fixture["instances"]:
            status = expiry_status(item["expires_at"], item["billing"], observed_at)
            if status in {"expired", "due", "unknown"}:
                add("expiry", item, status)
        for item in fixture["rules"]:
            if scenario != "missed" and is_public_ssh(
                item["direction"], item["action"], item["protocol"],
                item["source"], item["first_port"], item["last_port"],
            ):
                add("ssh", item, "exposed")

    priority = {"ssh": 0, "coverage": 1, "expiry": 2, "balance": 3}
    findings.sort(key=lambda row: priority[row["kind"]])
    return {
        "source": "synthetic", "scenario": scenario, "observed_at": observed_at.isoformat(),
        "expected_accounts": len(accounts) - coverage["disabled"], "coverage": coverage,
        "counts": {kind: sum(row["kind"] == kind for row in findings) for kind in priority},
        "accounts": [{"id": key, **account} for key, account in accounts.items()],
        "findings": findings,
    }


class DashboardData:
    def __init__(self, state_dir, config, development, monitor_config=None, host_monitor_config=None, *, operations=None, onboarding_probe=None):
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.live = WorkItems(state_dir / "live")
        self.demo = WorkItems(state_dir) if development else None
        self.config = config
        self.onboarding = DashboardOnboarding(state_dir, locked=config is not None, probe=onboarding_probe)
        self.operations = operations
        self.development = development
        self._configured_websites = load_monitors(monitor_config) if monitor_config is not None else None
        self.schedules = MonitorSchedules(self.live, self.live._record_monitor)
        self.hosts = load_hosts(host_monitor_config, load_connections(config)) if host_monitor_config else {}
        self.host_schedules = MonitorSchedules(self.live, self.live._record_host, lease_seconds=120)
        self.inspection_schedules = MonitorSchedules(self.live, self._record_inspection_slot, lease_seconds=900)
        self.demo_host_schedules = MonitorSchedules(self.demo, self.demo._record_host, synthetic=True) if self.demo else None
        self.storage = DashboardStorage(self.live, self.demo, monitor_config, host_monitor_config)

    def connections(self):
        return load_connections(self.config) if self.config else self.onboarding.catalog()

    @property
    def monitors(self):
        if self._configured_websites is not None:
            return self._configured_websites
        return self.live.website_monitors()

    @property
    def website_registration_allowed(self):
        return self._configured_websites is None

    def website_targets(self, show_mock):
        targets = [self._website_row(monitor, self.live) for monitor in self.monitors.values()]
        if self.demo is not None and show_mock:
            targets.append(self._website_row(DEMO_MONITOR, self.demo, synthetic=True))
        return {"targets": targets, "editable": self.website_registration_allowed}

    def register_website(self, data):
        if set(data) != {"url", "expected_status", "contains"}:
            raise ValueError("Invalid website registration")
        return self.live.register_website(
            Monitor.from_registration(data["url"], data["expected_status"], data["contains"])
        )

    def update_website_conditions(self, target, data):
        if set(data) != {"revision", "expected_status", "content_action", "contains"}:
            raise ValueError("Invalid website conditions")
        revision, action, text = data["revision"], data["content_action"], data["contains"]
        if (type(revision) is not int or revision < 0 or action not in ("keep", "remove", "replace")
                or (action == "replace" and not isinstance(text, str)) or (action != "replace" and text is not None)):
            raise ValueError("Invalid website conditions")
        return self.live.update_website_conditions(target, revision, data["expected_status"], action,
                                                   text, int(self.schedules.clock()))

    def _website_row(self, monitor, store, synthetic=False):
        history = store.monitor_history(monitor)
        return {**monitor.public(), "source": "synthetic" if synthetic else "live",
                "history": history, "certificate_status": certificate_status(history[0]) if history else None,
                "previous_history": store.previous_website_history(monitor) if monitor.revision else [],
                "schedule": None if synthetic else self.schedules.describe(monitor.key)}

    def run_website(self, target_id, show_mock, scenario):
        synthetic = target_id == DEMO_MONITOR.id and self.demo is not None and show_mock
        monitor = DEMO_MONITOR if synthetic else self.monitors.get(target_id)
        if monitor is None:
            return None
        store = self.demo if synthetic else self.live
        _, target = self._run_website_monitor(monitor, store, synthetic, scenario)
        return target

    def _run_website_monitor(self, monitor, store, synthetic, scenario):
        result = simulate(monitor, scenario) if synthetic else probe(monitor)
        store.record_monitor(monitor, result, synthetic)
        return result, self._website_row(monitor, store, synthetic)

    def configure_website_schedule(self, target_id, minutes, enabled):
        monitor = self.monitors.get(target_id)
        if monitor is None:
            return None
        self.schedules.configure(monitor.key, minutes, enabled)
        return self._website_row(monitor, self.live)

    def registered_hosts(self):
        manual = {host_identity(monitor) for monitor in self.hosts.values()}
        local = (restore_host(row) for row in self.live.host_registrations())
        return {**{monitor.id: monitor for monitor in local if host_identity(monitor) not in manual}, **self.hosts}

    def _host_candidates(self, catalog):
        inspection = self.live.inventory_report()
        return host_candidates(catalog, inspection.get("assets", []))

    def available_hosts(self):
        catalog = self.connections()
        candidates = self._host_candidates(catalog)
        registered = {host_identity(monitor) for monitor in self.registered_hosts().values()}
        labels = {row["id"]: row["label"] for row in catalog.accounts()}
        targets = []
        for monitor in candidates.values():
            account, region, instance = host_identity(monitor)
            if (account, region, instance) not in registered:
                service = monitor.service_id if isinstance(monitor, AdapterHostMonitor) else monitor.qvm_service
                targets.append({**monitor.public(), "account": account, "account_label": labels.get(monitor.account_id, service),
                                "region": region, "resource_id": instance, "service_id": service})
        return {"targets": targets}

    def register_host(self, data):
        id_ = data.get("id")
        if set(data) != {"id"} or not isinstance(id_, str) or not re.fullmatch(r"host-[0-9a-f]{32}", id_):
            raise ValueError("Invalid host monitor registration")
        monitor = self._host_candidates(self.connections()).get(id_)
        if monitor is None:
            return None
        identity = host_identity(monitor)
        existing = [row for row in self.hosts.values() if host_identity(row) == identity]
        if existing:
            return {"id": existing[0].id, "status": "existing"} if len(existing) == 1 else {"status": "ambiguous"}
        return self.live.register_host(identity, asdict(monitor))

    def host_targets(self, show_mock):
        targets = [self._host_row(monitor, False) for monitor in self.registered_hosts().values()]
        if self.demo and show_mock:
            targets.append(self._host_row(DEMO_HOST, True))
        return {"targets": targets}

    def _host_row(self, monitor, synthetic):
        store = self.demo if synthetic else self.live
        schedules = self.demo_host_schedules if synthetic else self.host_schedules
        return {**monitor.public(), "source": "synthetic" if synthetic else "live",
                "history": store.monitor_history(monitor), "schedule": schedules.describe(monitor.key)}

    def host_history(self, target_id, show_mock, hours):
        synthetic = target_id == DEMO_HOST.id and self.demo is not None and show_mock
        monitor = DEMO_HOST if synthetic else self.registered_hosts().get(target_id)
        if monitor is None:
            return None
        now = datetime.now(timezone.utc)
        since = (now - timedelta(hours=hours)).isoformat()
        history = (self.demo if synthetic else self.live).host_history(monitor, since)
        history["disk_projection"] = disk_projection(history["history"], monitor.disk_warn_percent, now,
                                                     truncated=history["truncated"])
        return history

    def collect_server(self, monitor):
        try:
            catalog = self.connections()
        except (CatalogError, OSError):
            return {"observed_at": datetime.now(timezone.utc).isoformat(), "status": "unknown", "metrics": {}}
        return collect_host(catalog, monitor)

    def run_host(self, target_id, show_mock, scenario):
        synthetic = target_id == DEMO_HOST.id and self.demo is not None and show_mock
        monitor = DEMO_HOST if synthetic else self.registered_hosts().get(target_id)
        if monitor is None:
            return None
        _, target = self._run_host_monitor(monitor, synthetic, scenario)
        return target

    def _run_host_monitor(self, monitor, synthetic, scenario):
        store = self.demo if synthetic else self.live
        result = simulate_host(scenario) if synthetic else self.collect_server(monitor)
        store.record_host(monitor, result, synthetic)
        return result, self._host_row(monitor, synthetic)

    def configure_host_schedule(self, target_id, minutes, enabled, show_mock):
        synthetic = target_id == DEMO_HOST.id and self.demo is not None and show_mock
        monitor = DEMO_HOST if synthetic else self.registered_hosts().get(target_id)
        if monitor is None:
            return None
        schedules = self.demo_host_schedules if synthetic else self.host_schedules
        schedules.configure(monitor.key, minutes, enabled)
        return self._host_row(monitor, synthetic)

    def run(self, scenario, show_mock, *, account_ids=None):
        catalog = self.connections()
        if account_ids is not None and not set(account_ids) <= self._inspection_accounts(catalog).keys():
            return None
        demo_recorded = False
        if self.demo and show_mock:
            demo_recorded = self.demo.record(demo_inspection(scenario))["recorded"]
        elif scenario != "daily" and not self.development:
            raise ValueError("Demo scenario unavailable")
        live = self.live.record(collect(catalog, starter=self.config is None, account_ids=account_ids))
        snapshot = self.snapshot(show_mock, catalog)
        snapshot["recorded"] = (live["recorded"] and bool(live["report"]["expected_accounts"])) or demo_recorded
        return snapshot

    def _inspection_accounts(self, catalog):
        return {key: {"id": key, "label": label, "provider": PROVIDERS[service.provider]}
                for service, key, label in inspection_targets(catalog, starter=self.config is None)}

    @staticmethod
    def _validate_inspection_scope(chosen):
        if chosen is not None and (not isinstance(chosen, list) or not chosen or
                any(not isinstance(key, str) or len(key) > 135 for key in chosen) or len(set(chosen)) != len(chosen)):
            raise ValueError("Invalid inspection account scope")

    def inspection_plan(self, include_accounts=False):
        with self.live.connection() as db:
            db.execute("BEGIN")
            plan = self.inspection_schedules._describe(db, INSPECTION_TARGET.key, 1440)
            plan["account_ids"] = self.live.inspection_account_ids(db)
        plan["last_at"] = self.live.latest_inspection_time()
        if include_accounts:
            try:
                plan["accounts"] = list(self._inspection_accounts(self.connections()).values())
            except (CatalogError, OSError):
                plan["accounts"] = []
        return plan

    def configure_inspection_plan(self, data):
        required = {"interval_minutes", "enabled"}
        if (set(data) not in (required, required | {"account_ids"}) or type(data["interval_minutes"]) is not int
                or not 1 <= data["interval_minutes"] <= 1440 or type(data["enabled"]) is not bool):
            raise ValueError("Invalid inspection plan")
        chosen = data.get("account_ids")
        self._validate_inspection_scope(chosen)
        available = self._inspection_accounts(self.connections()) if data["enabled"] else {}
        with self.live.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if "account_ids" not in data:
                chosen = self.live.inspection_account_ids(db)
            if data["enabled"] and (not available or (chosen is not None and not set(chosen) <= available.keys())):
                return None
            self.inspection_schedules._configure(db, INSPECTION_TARGET.key, data["interval_minutes"], data["enabled"])
            if "account_ids" in data:
                self.live.save_inspection_account_ids(db, chosen)
        return self.inspection_plan(include_accounts=True)

    def _collect_inspection_slot(self, _target):
        with self.live.connection() as db:
            chosen = self.live.inspection_account_ids(db)
        try:
            report = collect(self.connections(), starter=self.config is None, account_ids=chosen)
        except (CatalogError, OSError):
            return {"status": "unknown"}
        if not report["expected_accounts"]:
            return {"status": "unknown"}
        return {"status": "pass", "report": report}

    def _record_inspection_slot(self, db, _target, result, _synthetic):
        if result["status"] == "pass":
            self.live._record_inspection(db, result["report"], trigger="scheduled")

    def start_inspection_plan(self):
        return self.inspection_schedules.start(lambda: (INSPECTION_TARGET,), self._collect_inspection_slot, poll_seconds=5)

    def _inspection_configured(self, catalog):
        return next(inspection_targets(catalog, starter=self.config is None), None) is not None

    def _demo_providers(self, catalog):
        return {PROVIDERS[name] for name in fallback_providers(catalog, starter=self.config is None)}

    def snapshot(self, show_mock, catalog=None):
        if catalog is None:
            catalog = self.connections()
        state = self.live.snapshot()
        state["development"] = self.development
        state["recorded"] = False
        state["inspection_setup"] = {
            "configured": self._inspection_configured(catalog),
            "editable": not self.onboarding.locked,
        }
        if self.demo is None or not show_mock:
            return state
        blocked = self._demo_providers(catalog)
        demo = self.demo.snapshot()
        state["items"].extend({**row, "evidence": {**row["evidence"], "data_source": "synthetic"}}
                              for row in demo["items"] if row["evidence"]["provider"] not in blocked)
        if not demo["report"]:
            return state
        report = state["report"]
        if report is None or "selected_accounts" in report:
            return state
        observed = demo["report"]
        simulated_accounts = [{**row, "data_source": "synthetic"} for row in observed["accounts"]
                              if row["provider"] not in blocked]
        simulated_findings = [{**row, "data_source": "synthetic"} for row in observed["findings"]
                              if row["provider"] not in blocked]
        has_live = bool(report["accounts"])
        report["accounts"].extend(simulated_accounts)
        report["findings"].extend(simulated_findings)
        report["expected_accounts"] += sum(row["enabled"] for row in simulated_accounts)
        for row in simulated_accounts:
            report["coverage"][row["status"]] += 1
        for row in simulated_findings:
            report["counts"][row["kind"]] += 1
        if simulated_accounts:
            report["scenario"] = observed["scenario"]
            report["simulated_at"] = observed["observed_at"]
            if not has_live:
                report["observed_at"] = observed["observed_at"]
        return state

    def inspection_summary(self, show_mock):
        return inspection_summary(self.snapshot(show_mock)["report"])

    def run_inspection_summary(self, data):
        required = {"show_mock"}
        if set(data) not in (required, required | {"account_ids"}) or type(data["show_mock"]) is not bool:
            raise ValueError("Invalid inspection summary request")
        chosen = data.get("account_ids")
        self._validate_inspection_scope(chosen)
        if "account_ids" in data and (chosen is None or data["show_mock"]):
            raise ValueError("Selected account inspection requires real data and explicit selection")
        snapshot = self.run("daily", data["show_mock"], account_ids=chosen)
        if snapshot is None:
            return None
        return {**inspection_summary(snapshot["report"]), "recorded": snapshot["recorded"]}

    def inspection_history(self, source, show_mock, before):
        store = self.live if source == "live" else self.demo if show_mock else None
        return {"source": source, **store.inspection_history(before)} if store is not None else None

    def work_item_index(self, show_mock):
        items = []
        for item in self.snapshot(show_mock)["items"]:
            evidence = item["evidence"]
            items.append({"id": item["id"], "status": item["status"],
                          "exception_active": item.get("exception", {}).get("active", False),
                          "last_seen": item["last_seen"], "kind": evidence["kind"], "name": evidence["name"],
                          "account": evidence["account"], "data_source": evidence.get("data_source", "live")})
        return {"items": items}

    def host_link(self, resource, hosts=None):
        identity = resource.get("account"), resource.get("region"), resource.get("id")
        if not identity[1]:
            return None
        hosts = self.registered_hosts() if hosts is None else hosts
        matches = [monitor.id for monitor in hosts.values() if host_identity(monitor) == identity]
        return matches[0] if len(matches) == 1 else None

    def assets(self, show_mock):
        snapshot = self.live.inventory_report()
        rows = snapshot.get("assets", [])
        try:
            connections = self.connections()
        except (CatalogError, OSError):
            connections = None
        if self.demo is not None and show_mock and connections is not None:
            blocked = self._demo_providers(connections)
            fixture = load_demo_fixture()
            demo = demo_assets()
            rows.extend({**row, "data_source": "synthetic", "observed_at": demo["snapshot_at"]} for row in demo["assets"]
                        if fixture["accounts"][row["account"]]["provider"] not in blocked)
        hosts = self.registered_hosts()
        linked = []
        for row in rows:
            if row["kind"] == "server":
                monitor_id = self.host_link(row, hosts)
                row = {**row, "monitor_id": monitor_id,
                       "monitor_key": hosts[monitor_id].key if monitor_id else None}
            linked.append(row)
        rows = linked
        if self.operations:
            catalog = self.operations.catalog()
            rows = [{**row, "operation_preview": self.operations.for_asset(catalog, row)} for row in rows]
        routes = [asset_route_id(row) for row in rows]
        counts = Counter(routes)
        routes = [asset_route_id(row, by_scope=True) if counts[route] > 1 and row["kind"] in {"domain", "certificate"}
                  else route for row, route in zip(rows, routes)]
        counts = Counter(routes)
        rows = [{**row, "route_id": route if counts[route] == 1 else None} for row, route in zip(rows, routes)]
        return dict(source="live", snapshot_at=snapshot.get("observed_at"), assets=rows,
                    backup_inventory_status=self._backup_inventory_status(connections, snapshot))

    def check_domain_delivery(self, data):
        if (set(data) != {"service_id", "domain"} or not isinstance(data["service_id"], str)
                or not isinstance(data["domain"], str) or not HOST.fullmatch(data["domain"])):
            raise ValueError("Invalid domain delivery target")
        snapshot = self.live.inventory_report()
        matches = [row for row in snapshot.get("assets", []) if row["kind"] == "domain"
                   and row.get("service_id") == data["service_id"] and row["name"] == data["domain"]
                   and row.get("data_source") == "live" and "provider_nameservers" in row]
        if len(matches) != 1:
            return None
        asset, = matches
        current = next((service for service, account, _ in inspection_targets(
            self.connections(), starter=self.config is None)
            if service.id == asset["service_id"] and account == asset["account"]), None)
        if current is None or current.adapter not in DOMAIN_DELIVERY_ADAPTERS:
            return None
        return {**observe(asset["name"], asset["provider_nameservers"]), "provider": asset["provider"],
                "provider_observed_at": asset["observed_at"],
                "provider_nameservers": asset["provider_nameservers"]}

    @staticmethod
    def _backup_inventory_status(connections, snapshot):
        if connections is None:
            return "unavailable"
        accounts = {
            "live:tencent:" + (row.get("account_id") or row["id"])
            for row in connections.list()
            if row["enabled"] and row["adapter"] == "tencent-management"
            and connections.get(row["id"]).settings.get("backup_inventory") is True
        }
        if not accounts:
            return "disabled"
        checks = {row["id"]: row["checks"].get("云盘备份") for row in snapshot.get("accounts", [])}
        states = [checks.get(account) for account in accounts]
        if all(state is None for state in states):
            return "pending"
        if all(state == "complete" for state in states):
            return "complete"
        if any(state in {"complete", "partial"} for state in states):
            return "partial"
        return "unavailable"

    def _work_item(self, key, show_mock):
        store = self.live
        item = store.detail(key)
        if item is None and self.demo is not None and show_mock:
            store = self.demo
            item = self.demo.detail(key)
            if item and item["evidence"]["provider"] not in self._demo_providers(self.connections()):
                item["evidence"]["data_source"] = "synthetic"
            else:
                return store, None
        return store, item

    def update_work_item_exception(self, key, data):
        if type(data.get("show_mock")) is not bool:
            raise ValueError
        if set(data) == {"show_mock", "remove"} and data["remove"] is True:
            reason, review_on = None, None
        elif set(data) == {"show_mock", "reason", "review_on"} and isinstance(data["reason"], str):
            reason, review_on = data["reason"], data["review_on"]
        else:
            raise ValueError
        store, item = self._work_item(key, data["show_mock"])
        return store.update_exception(key, reason, review_on) if item else "missing"

    def _website_monitor_for_item(self, evidence, synthetic):
        monitors = {DEMO_MONITOR.id: DEMO_MONITOR} if synthetic else self.monitors
        if evidence["kind"] == "website":
            return next((monitor for monitor in monitors.values() if monitor.key == evidence["id"]), None)
        if evidence["kind"] == "website-certificate":
            monitor = monitors.get(evidence["id"])
            return monitor if monitor and monitor.url == evidence["scope"] else None
        return None

    def _host_monitor_for_item(self, evidence, synthetic):
        hosts = (DEMO_HOST,) if synthetic else self.registered_hosts().values()
        return next((host for host in hosts if host.key == evidence["id"] and host.account_id == evidence["account"]), None)

    @staticmethod
    def _disk_detection(reading, threshold):
        disk = reading["metrics"].get("disk_percent")
        return {"observed_at": reading["observed_at"], "disk_percent": disk["value"] if disk else None,
                "threshold": threshold}

    def recheck_work_item(self, key, data):
        if (set(data) != {"show_mock", "scenario"} or type(data["show_mock"]) is not bool
                or not isinstance(data["scenario"], str)):
            raise ValueError("Invalid work item recheck")
        store, item = self._work_item(key, data["show_mock"])
        if item is None:
            return None
        if item["status"] != "open":
            return {"status": "unavailable"}
        evidence = item["evidence"]
        synthetic = store is self.demo
        if evidence["kind"] == "host-disk":
            monitor = self._host_monitor_for_item(evidence, synthetic)
            if monitor is None or monitor.disk_warn_percent != evidence["threshold"]:
                return {"status": "unavailable"}
            reading, target = self._run_host_monitor(monitor, synthetic, data["scenario"])
            result = self._disk_detection(reading, monitor.disk_warn_percent)
        else:
            monitor = self._website_monitor_for_item(evidence, synthetic)
            if monitor is None:
                return {"status": "unavailable"}
            reading, target = self._run_website_monitor(monitor, store, synthetic, data["scenario"])
            result = {**reading, "certificate_status": certificate_status(reading)["status"]}
        return {"status": "recorded", "result": result, "target": target,
                "item": self.detail(key, data["show_mock"])}

    def detail(self, key, show_mock):
        store, item = self._work_item(key, show_mock)
        if item is None:
            return None
        evidence = item["evidence"]
        synthetic = store is self.demo
        if evidence["kind"] == "expiry" and not evidence.get("resource_kind") and not synthetic:
            item["monitor_target"] = self.host_link(evidence)
        elif evidence["kind"] == "host-disk":
            monitor = self._host_monitor_for_item(evidence, synthetic)
            item["monitor_target"] = monitor.id if monitor else None
            same_threshold = monitor is not None and monitor.disk_warn_percent == evidence["threshold"]
            item["can_recheck"] = same_threshold and item["status"] == "open"
            history = store.monitor_history(monitor, limit=1) if same_threshold else []
            if history:
                item["latest_detection"] = self._disk_detection(history[0], monitor.disk_warn_percent)
        elif evidence["kind"] in {"website", "website-certificate"}:
            monitor = self._website_monitor_for_item(evidence, synthetic)
            item["monitor_target"] = monitor.id if monitor else None
            item["can_recheck"] = monitor is not None and item["status"] == "open"
            history = store.monitor_history(monitor, limit=1) if monitor else []
            if history:
                latest = history[0]
                item["latest_detection"] = {
                    "observed_at": latest["observed_at"], "status": latest["status"],
                    "http_status": latest["http_status"], "content": latest["content"],
                    "certificate_status": certificate_status(latest)["status"],
                }
        if evidence["kind"] == "website":
            monitors = {DEMO_MONITOR.id: DEMO_MONITOR} if synthetic else self.monitors
            previous = store.previous_website_condition(evidence["id"]) if not item["monitor_target"] else None
            if previous and previous["monitor_id"] in monitors and monitors[previous["monitor_id"]].url == evidence["name"]:
                item["monitor_target"] = previous["monitor_id"]
            item["failed_observation"] = store.failed_observation(evidence["id"], item["last_seen"])
            if item["failed_observation"] and previous:
                item["failed_observation"].update(expected_status=previous["expected_status"],
                                                  checks_content=bool(previous["checks_content"]))
        if not synthetic and self.operations:
            binding = self._operation_binding_for_item(item)
            if binding:
                item["operation_binding"] = binding
        return item

    def _operation_binding_for_item(self, item):
        evidence = item["evidence"]
        if item["status"] != "open" or evidence.get("kind") != "expiry" or evidence.get("resource_kind"):
            return None
        assets = self.live.inventory_report().get("assets", [])
        matches = [asset for asset in assets if asset["kind"] == "server" and
                   all(asset.get(key) == evidence.get(key) for key in ("id", "account", "region"))]
        if len(matches) == 1:
            return self.operations.for_asset(self.operations.catalog(), matches[0])
        return None

    def preview_handoff(self, data):
        data = dict(data)
        work_item_id = data.pop("work_item_id", None)
        if work_item_id is not None:
            if not isinstance(work_item_id, str) or not re.fullmatch(r"[a-f0-9]{64}", work_item_id):
                raise ValueError("Invalid work item")
            item = self.live.detail(work_item_id)
            binding = self._operation_binding_for_item(item) if item else None
            if binding is None or binding["id"] != data.get("binding"):
                raise ValueError("Work item and operation refer to different resources")
        return self.operations.preview(data, handoff=True, work_item_id=work_item_id)


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, data, **kwargs):
        self.data = data
        super().__init__(*args, **kwargs)

    def storage_request_allowed(self):
        port = self.server.server_port
        return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def do_GET(self):
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        show_mock = query.get("mock", ["1"])[0] != "0"
        if url.path == "/api/onboarding":
            if not self.storage_request_allowed():
                self.send_error(403)
                return
            try:
                self.send_json(self.data.onboarding.summary(self.data.connections()))
            except (CatalogError, OSError):
                self.send_error(503, "Service onboarding unavailable")
        elif url.path == "/api/resource-operations/targets":
            if not self.storage_request_allowed():
                self.send_error(403)
                return
            try:
                operations = self.data.operations
                self.send_json({"status": "available" if operations else "unconfigured",
                                "targets": operations.preview_targets() if operations else []})
            except (CatalogError, OSError):
                self.send_error(503, "Operation targets unavailable")
        elif url.path == "/api/resource-operations/history":
            if not self.storage_request_allowed():
                self.send_error(403)
                return
            if not self.data.operations:
                self.send_json({"records": [], "resources": [], "truncated": False, "available": False})
                return
            try:
                self.send_json(self.data.operations.ledger(query))
            except (sqlite3.Error, CatalogError, OSError):
                self.send_error(503, "Operation history unavailable")
            except ValueError:
                self.send_error(400, "Invalid operation filters")
        elif match := re.fullmatch(r"/api/resource-operations/record/([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", url.path):
            if not self.storage_request_allowed():
                self.send_error(403)
                return
            if not self.data.operations:
                self.send_error(404)
                return
            try:
                self.send_json(self.data.operations.record(match[1]))
            except (sqlite3.Error, OSError):
                self.send_error(503, "Operation record unavailable")
            except ValueError:
                self.send_error(404, "Operation record unavailable")
        elif match := re.fullmatch(r"/api/resource-operations/([a-z][a-z0-9-]{1,63})/history", url.path):
            if not self.storage_request_allowed():
                self.send_error(403)
                return
            if not self.data.operations:
                self.send_error(404)
                return
            try:
                self.send_json({"records": self.data.operations.records(match[1])})
            except (sqlite3.Error, CatalogError, OSError):
                self.send_error(503, "Operation history unavailable")
            except ValueError:
                self.send_error(404)
        elif url.path == "/api/storage":
            if not self.storage_request_allowed():
                self.send_error(403)
                return
            try:
                self.send_json(self.data.storage.summary())
            except (OSError, sqlite3.Error):
                self.send_error(503, "Local dashboard storage unavailable")
        elif url.path == "/api/host-monitors/available":
            if not self.storage_request_allowed():
                self.send_error(403)
                return
            try:
                self.send_json(self.data.available_hosts())
            except (OSError, sqlite3.Error, CatalogError):
                self.send_error(503, "Host monitoring sources unavailable")
        elif url.path.startswith("/api/"):
            try:
                if url.path == "/api/website-monitors":
                    self.send_json(self.data.website_targets(show_mock))
                elif url.path == "/api/host-monitors":
                    self.send_json(self.data.host_targets(show_mock))
                elif match := re.fullmatch(r"/api/host-monitors/([a-z][a-z0-9-]{1,63})/history", url.path):
                    hours = query.get("hours", ["6"])[0]
                    if hours not in {"1", "6", "24"}:
                        self.send_error(400, "Invalid history range")
                        return
                    history = self.data.host_history(match[1], show_mock, int(hours))
                    if history is None:
                        self.send_error(404, "Unknown monitor")
                    else:
                        self.send_json(history)
                elif url.path == "/api/assets":
                    self.send_json(self.data.assets(show_mock))
                elif url.path == "/api/work-items":
                    if query.get("view") == ["index"]:
                        if not self.storage_request_allowed():
                            self.send_error(403)
                            return
                        self.send_json(self.data.work_item_index(show_mock))
                    else:
                        self.send_json(self.data.snapshot(show_mock))
                elif url.path == "/api/inspection/summary":
                    if not self.storage_request_allowed():
                        self.send_error(403)
                        return
                    self.send_json(self.data.inspection_summary(query.get("mock") == ["1"]))
                elif url.path == "/api/inspection/plan":
                    if not self.storage_request_allowed():
                        self.send_error(403)
                        return
                    self.send_json(self.data.inspection_plan(include_accounts=query.get("accounts") == ["1"]))
                elif url.path == "/api/inspection/history":
                    if not self.storage_request_allowed():
                        self.send_error(403)
                        return
                    source = query.get("source", ["live"])
                    cursor = query.get("before", [])
                    if source not in (["live"], ["synthetic"]) or (cursor and
                            (len(cursor) != 1 or not re.fullmatch(r"[1-9][0-9]{0,17}", cursor[0]))):
                        self.send_error(400, "Invalid inspection history request")
                        return
                    history = self.data.inspection_history(source[0], query.get("mock") == ["1"],
                                                           int(cursor[0]) if cursor else None)
                    if history is None:
                        self.send_error(404, "Inspection history source unavailable")
                    else:
                        self.send_json(history)
                elif match := re.fullmatch(r"/api/work-items/([a-f0-9]{64})", url.path):
                    item = self.data.detail(match[1], show_mock)
                    if item is None:
                        self.send_error(404)
                    else:
                        self.send_json(item)
                else:
                    self.send_error(404)
            except (CatalogError, OSError):
                self.send_error(503, "Local service catalog unavailable")
        elif url.path in ASSETS:
            filename, content_type = ASSETS[url.path]
            self.send_body(ROOT.joinpath(filename).read_bytes(), content_type)
        else:
            self.send_error(404)

    def do_POST(self):
        monitor = re.fullmatch(r"/api/(website|host)-monitors/([a-z][a-z0-9-]{1,63})/(run|schedule)", self.path)
        run_inspection_summary = self.path == "/api/inspection/summary"
        inspection_plan = self.path == "/api/inspection/plan"
        work_action = re.fullmatch(r"/api/work-items/([a-f0-9]{64})/(exception|recheck)", self.path)
        check_connection = self.path == "/api/onboarding/check"
        check_domain_delivery = self.path == "/api/domain-delivery/check"
        register_host = self.path == "/api/host-monitors"
        register_website = self.path == "/api/website-monitors"
        edit_website = re.fullmatch(r"/api/website-monitors/([a-z][a-z0-9-]{1,63})/conditions", self.path)
        website_write = register_website or edit_website
        if website_write and not self.data.website_registration_allowed:
            self.send_error(403)
            return
        enrollment = self.path in {"/api/onboarding/test", "/api/onboarding/save"}
        connection_state = self.path == "/api/onboarding/state"
        credential_update = self.path == "/api/onboarding/credential"
        onboarding_write = enrollment or connection_state or credential_update
        if onboarding_write and self.data.onboarding.locked:
            self.send_error(403)
            return
        storage_action = self.path in {"/api/storage/retention", "/api/storage/backup"}
        operation = re.fullmatch(r"/api/resource-operations/(preview|handoff|execute|verify)", self.path)
        if operation and (self.data.operations is None or
                          (operation[1] in {"execute", "verify"} and self.data.operations.offline_registry is None)):
            self.send_error(404)
            return
        if (storage_action or operation or onboarding_write or check_connection or check_domain_delivery or website_write or register_host or
                work_action or run_inspection_summary or inspection_plan) and not self.storage_request_allowed():
            self.send_error(403)
            return
        if not (monitor or storage_action or operation or onboarding_write or check_connection or check_domain_delivery or website_write or register_host or work_action
                or run_inspection_summary or inspection_plan or self.path == "/api/inspection"):
            self.send_error(404)
            return
        origin = self.headers.get("Origin")
        port = self.server.server_port
        if (origin or onboarding_write or check_connection or check_domain_delivery or website_write or register_host or work_action or run_inspection_summary or inspection_plan or
                (operation and operation[1] == "handoff")) and origin not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
            self.send_error(403)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_error(415)
            return
        try:
            size = int(self.headers.get("Content-Length", 0))
            max_size = 131072 if inspection_plan or run_inspection_summary else 4096 if enrollment or website_write or work_action else 1024
            if not 0 < size <= max_size:
                raise ValueError
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError
            show_mock = data.get("show_mock", True) is True
            if inspection_plan:
                try:
                    plan = self.data.configure_inspection_plan(data)
                    if plan is None:
                        self.send_json({"status":"no_accounts"}, status=409)
                    else:
                        self.send_json(plan)
                except sqlite3.Error:
                    self.send_error(503, "Inspection plan storage unavailable")
                return
            if run_inspection_summary:
                summary = self.data.run_inspection_summary(data)
                if summary is None:
                    self.send_json({"status":"accounts_unavailable"}, status=409)
                else:
                    self.send_json(summary)
                return
            if work_action:
                try:
                    key, action = work_action.groups()
                    if action == "exception":
                        outcome = self.data.update_work_item_exception(key, data)
                        self.send_json({"status": outcome}, status={"missing":404, "closed":409}.get(outcome, 200))
                    else:
                        result = self.data.recheck_work_item(key, data)
                        if result is None:
                            self.send_error(404, "Unknown work item")
                        else:
                            self.send_json(result, status=409 if result["status"] == "unavailable" else 200)
                except sqlite3.Error:
                    self.send_error(503, "Work item storage unavailable")
                return
            if register_host:
                try:
                    registration = self.data.register_host(data)
                    if registration is None:
                        self.send_error(404, "Unknown host monitoring source")
                    else:
                        self.send_json(registration)
                except sqlite3.Error:
                    self.send_error(503, "Host registration storage unavailable")
                return
            if website_write:
                try:
                    if edit_website:
                        outcome = self.data.update_website_conditions(edit_website[1], data)
                        self.send_json({"status": outcome}, status={"missing":404, "stale":409}.get(outcome, 200))
                    else:
                        self.send_json(self.data.register_website(data))
                except sqlite3.Error:
                    self.send_error(503, "Website registration storage unavailable")
                return
            if check_connection:
                self.send_json(self.data.onboarding.check(self.data.connections(), data))
                return
            if connection_state:
                connection = self.data.onboarding.change_state(data)
                if connection is None:
                    self.send_error(404, "Managed connection not found")
                else:
                    self.send_json(connection)
                return
            if credential_update:
                result = self.data.onboarding.replace_credential(data)
                if result is None:
                    self.send_error(404, "Active managed connection not found")
                else:
                    self.send_json(result)
                return
            if check_domain_delivery:
                result = self.data.check_domain_delivery(data)
                if result is None:
                    self.send_error(404, "Domain or connection unavailable")
                else:
                    self.send_json(result)
                return
            if enrollment:
                action = self.data.onboarding.test if self.path.endswith("/test") else self.data.onboarding.save
                self.send_json(action(data))
                return
            if operation:
                handler = {"preview": self.data.operations.preview, "handoff": self.data.preview_handoff,
                           "execute": self.data.operations.execute,
                           "verify": self.data.operations.verify}[operation[1]]
                try:
                    self.send_json(handler(data))
                except sqlite3.Error:
                    self.send_error(503, "Operation history unavailable")
                return
            if storage_action:
                self.handle_storage_action(data)
                return
            if monitor:
                kind, target, action = monitor.groups()
                if action == "schedule":
                    required = {"interval_minutes", "enabled"} | ({"show_mock"} if kind == "host" else set())
                    if set(data) != required:
                        raise ValueError
                    if kind == "host":
                        snapshot = self.data.configure_host_schedule(target, data["interval_minutes"], data["enabled"], show_mock)
                    else:
                        snapshot = self.data.configure_website_schedule(target, data["interval_minutes"], data["enabled"])
                else:
                    scenario = data.get("scenario", "mismatch" if kind == "website" else "healthy")
                    if not isinstance(scenario, str) or set(data) - {"scenario", "show_mock"}:
                        raise ValueError
                    run = self.data.run_website if kind == "website" else self.data.run_host
                    snapshot = run(target, show_mock, scenario)
                if snapshot is None:
                    self.send_error(404, "Unknown monitor")
                    return
            else:
                if not isinstance(data.get("scenario"), str):
                    raise ValueError
                snapshot = self.data.run(data["scenario"], show_mock)
        except (CatalogError, OSError):
            self.send_error(503, "Local service catalog unavailable")
            return
        except ValueError:
            self.send_error(400, "Invalid observation request")
            return
        self.send_json(snapshot)

    def handle_storage_action(self, data):
        try:
            if self.path == "/api/storage/retention" and set(data) == {"days"}:
                removed = self.data.storage.configure(data["days"])
                self.send_json({"retention_days": data["days"], "removed": removed})
            elif self.path == "/api/storage/backup" and set(data) == {"recipient"}:
                encrypted = self.data.storage.encrypted_backup(data["recipient"])
                day = datetime.now(timezone.utc).date().isoformat()
                self.send_body(encrypted, "application/octet-stream",
                               filename=f"stackorion-{day}.tar.gz.age")
            else:
                self.send_error(400, "Invalid storage request")
        except BackupError as exc:
            code = str(exc)
            status = {"invalid_recipient": 400, "backup_too_large": 413, "backup_busy": 409}.get(code, 503)
            self.send_json({"error": code}, status=status)
        except (sqlite3.Error, OSError):
            self.send_json({"error": "storage_unavailable"}, status=503)

    def send_json(self, value, *, status=200):
        self.send_body(json.dumps(value, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", status=status)

    def send_body(self, body, content_type, *, status=200, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if filename is not None:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)


def build_demo_mcp():
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations

    server = MCPServer("StackOrion Inspection Demo", title="StackOrigin 巡检演示")

    @server.tool(structured_output=True, annotations=ToolAnnotations(read_only_hint=True))
    def run_demo_inspection(scenario: str = "daily") -> dict[str, Any]:
        """Evaluate synthetic data for the daily or empty StackOrigin dashboard scenario."""
        return demo_inspection(scenario)

    return server


class OpsPreviewHandler(DashboardHandler):
    """Serve a fixed, synthetic snapshot without exposing workbench actions."""

    assets = {
        "/": ("ops-preview.html", "text/html; charset=utf-8"),
        "/ops-preview.css": ("ops-preview.css", "text/css; charset=utf-8"),
        "/ops-preview.js": ("ops-preview.js", "text/javascript; charset=utf-8"),
        **{route: ASSETS[route] for route in ("/dashboard.css", "/stackorion-brand.svg", "/favicon.ico")},
    }

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/ops-preview":
            if not self.storage_request_allowed():
                self.send_error(403)
                return
            self.send_json(self.data)
        elif path in self.assets:
            filename, content_type = self.assets[path]
            self.send_body(ROOT.joinpath(filename).read_bytes(), content_type)
        else:
            self.send_error(404)

    def do_POST(self):
        self.send_error(405)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mcp", action="store_true", help="Start the synthetic MCP server over stdio")
    parser.add_argument("--state-dir", type=Path, default=os.environ.get("STACKORION_STATE_DIR", ".stackorion"))
    parser.add_argument("--config", type=Path, default=os.environ.get("STACKORION_CONFIG"))
    parser.add_argument("--development", action=argparse.BooleanOptionalAction,
                        default=os.environ.get("STACKORION_DEVELOPMENT") == "1")
    parser.add_argument("--monitor-config", type=Path, default=os.environ.get("STACKORION_MONITOR_CONFIG"))
    parser.add_argument("--host-monitor-config", type=Path, default=os.environ.get("STACKORION_HOST_MONITOR_CONFIG"))
    parser.add_argument("--operation-preview-config", type=Path)
    parser.add_argument("--ops-preview", action="store_true", help="Serve the read-only synthetic operations dashboard")
    args = parser.parse_args()
    if args.mcp:
        build_demo_mcp().run()
        return
    from functools import partial

    if args.ops_preview:
        from tempfile import TemporaryDirectory

        args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="ops-preview-", dir=args.state_dir) as seed:
            snapshot = WorkItems(seed).record(demo_inspection())
        observed = datetime.fromisoformat(snapshot["report"]["observed_at"])
        website = simulate(DEMO_MONITOR, "timeout", now=observed)
        payload = {
            "inspection": inspection_summary(snapshot["report"]),
            "items": snapshot["items"],
            "inventory": demo_assets(),
            "monitoring": {
                "host": {"name": DEMO_HOST.name, "observation": simulate_host("healthy", now=observed)},
                "website": {"domain": urlsplit(DEMO_MONITOR.url).hostname, "observation": website,
                            "certificate": certificate_status(website)},
            },
        }
        handler = partial(OpsPreviewHandler, data=payload)
        with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
            print(f"StackOrigin 运营大屏 · http://127.0.0.1:{server.server_port}/", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        return

    data = DashboardData(args.state_dir, args.config, args.development, args.monitor_config, args.host_monitor_config)
    if args.operation_preview_config:
        data.operations = DashboardOperations.from_file(lambda: load_connections(args.config),
                                                        args.operation_preview_config,
                                                        args.state_dir / "operation-records")
    handler = partial(DashboardHandler, data=data)
    with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
        stops = [data.start_inspection_plan(), data.schedules.start(lambda: data.monitors.values(), probe),
                 data.host_schedules.start(lambda: data.registered_hosts().values(), data.collect_server), data.storage.start()]
        if data.demo_host_schedules:
            stops.append(data.demo_host_schedules.start(lambda: (DEMO_HOST,), lambda _: simulate_host("healthy")))
        print(f"StackOrigin（源栈） · http://127.0.0.1:{server.server_port}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            for stop in stops:
                stop.set()


if __name__ == "__main__":
    main()
