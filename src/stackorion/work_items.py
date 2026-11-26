"""Persist findings and their observation history in the local dashboard."""

from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import stat

from .inspection import inspection_summary
from .website_monitor import MAX_MONITORS, Monitor, certificate_status


CHECKS = {"balance": "账务", "expiry": "实例", "ssh": "安全组"}
INVENTORY_CHECKS = {"server": "实例", "security_group": "安全组", "domain": "域名",
                    "certificate": "库存证书", "backup": "云盘备份"}
WEBSITE_DEFINITIONS = """
    SELECT id, url, expected_status, contains,
        COALESCE((SELECT MAX(revision) + 1 FROM website_condition_revision WHERE monitor_id = id), 0) AS revision
    FROM website_monitor
"""
WORK_ITEM_ROWS = """
    SELECT item.*, exception.reason AS exception_reason, exception.review_on AS exception_review_on
    FROM work_item AS item LEFT JOIN work_item_exception AS exception ON exception.item_id = item.id
"""
REVIEW_TIMEZONE = timezone(timedelta(hours=8))


def review_today():
    return datetime.now(REVIEW_TIMEZONE).date()


def _work_item(row, today):
    item = dict(row)
    reason = item.pop("exception_reason")
    review_on = item.pop("exception_review_on")
    item["evidence"] = json.loads(item["evidence"])
    if review_on is not None:
        item["exception"] = {"reason": reason, "review_on": review_on,
                             "active": item["status"] == "open" and review_on > today}
    return item


def item_id(finding):
    kind = finding["kind"]
    identity = [kind, finding["account"], finding.get("region"), finding["id"]]
    if finding.get("scope"):
        identity.append(finding["scope"])
    if kind == "balance":
        identity.extend((finding["metric"], finding["currency"]))
    elif kind == "coverage":
        identity.extend(sorted(check for check, state in finding["checks"].items() if state != "complete"))
    return sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def scope_complete(finding, accounts):
    account = accounts.get(finding["account"])
    if not account or not account["enabled"]:
        return False
    if finding.get("scope"):
        return finding["scope"] in account.get("complete_scopes", ())
    if finding["kind"] == "coverage":
        checks = [check for check, state in finding["checks"].items() if state != "complete"]
    else:
        checks = [CHECKS[finding["kind"]]]
    return all(account["checks"].get(check) == "complete" for check in checks)


def _asset_identity(asset):
    return asset["account"], asset["kind"], asset.get("region"), asset["id"]


def _merge_inventory(previous, report):
    observed_at = report["observed_at"]
    current = {row["id"]: row for row in report["accounts"]}
    scoped = "selected_accounts" in report
    accounts = {row["id"]: row for row in previous.get("accounts", [])} if scoped else {}
    accounts.update(current)
    assets = [{**row, "observed_at": observed_at} for row in report.get("assets", [])]
    matches = {}
    for asset in assets:
        matches.setdefault(_asset_identity(asset), []).append(asset.get("scope"))
    for asset in previous.get("assets", []):
        account_id = asset["account"]
        if account_id not in accounts:
            continue
        if account_id not in current:
            assets.append(asset)
            continue
        account = current[account_id]
        scope = asset.get("scope")
        check = INVENTORY_CHECKS[asset["kind"]]
        if account["checks"].get(check) == "complete" or scope in account.get("complete_scopes", ()):
            continue
        scopes = matches.get(_asset_identity(asset), ())
        if scope in scopes or (scope is None and len(scopes) == 1):
            continue
        assets.append({**asset, "inventory_retained": True})
    return {"observed_at": observed_at, "accounts": list(accounts.values()), "assets": assets}


class WorkItems:
    def __init__(self, state_dir):
        directory = Path(state_dir)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("Use a private state directory owned by the current user (0700)")
        path = directory / "work-items.db"
        fd = os.open(path, os.O_CREAT | os.O_RDONLY | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError("Work item database must be a private regular file (0600)")
        finally:
            os.close(fd)
        self.path = path
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS inspection (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inspection_run (
                    seq INTEGER PRIMARY KEY,
                    summary TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inspection_plan_scope (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    account_ids TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS last_full_inspection (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_inventory (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    report TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_item (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    resolved_at TEXT
                );
                CREATE TABLE IF NOT EXISTS work_item_exception (
                    item_id TEXT PRIMARY KEY REFERENCES work_item(id),
                    reason TEXT NOT NULL,
                    review_on TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_run (
                    seq INTEGER PRIMARY KEY,
                    target_key TEXT NOT NULL,
                    observation TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS monitor_run_target ON monitor_run(target_key, seq);
                CREATE TABLE IF NOT EXISTS website_monitor (
                    id TEXT PRIMARY KEY,
                    url TEXT NOT NULL UNIQUE,
                    expected_status INTEGER NOT NULL,
                    contains TEXT
                );
                CREATE TABLE IF NOT EXISTS website_condition_revision (
                    monitor_id TEXT NOT NULL REFERENCES website_monitor(id),
                    revision INTEGER NOT NULL,
                    target_key TEXT NOT NULL UNIQUE,
                    expected_status INTEGER NOT NULL,
                    checks_content INTEGER NOT NULL,
                    PRIMARY KEY (monitor_id, revision)
                );
                CREATE TABLE IF NOT EXISTS host_registration (
                    id TEXT PRIMARY KEY,
                    identity TEXT NOT NULL UNIQUE,
                    definition TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS history (
                    seq INTEGER PRIMARY KEY,
                    item_id TEXT NOT NULL REFERENCES work_item(id),
                    observed_at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT NOT NULL
                );
            """)
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("""
                SELECT report FROM inspection WHERE NOT EXISTS (SELECT 1 FROM inspection_run)
            """).fetchone()
            if prior:
                self._save_inspection_run(db, json.loads(prior["report"]), "legacy")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _snapshot(db):
        stored = db.execute("SELECT report FROM inspection").fetchone()
        rows = db.execute(WORK_ITEM_ROWS + " ORDER BY item.status, item.first_seen, item.rowid")
        today = review_today().isoformat()
        return {
            "report": json.loads(stored["report"]) if stored else None,
            "items": [_work_item(row, today) for row in rows],
        }

    def snapshot(self):
        with self.connection() as db:
            db.execute("BEGIN")
            return self._snapshot(db)

    def detail(self, key):
        with self.connection() as db:
            db.execute("BEGIN")
            row = db.execute(WORK_ITEM_ROWS + " WHERE item.id = ?", (key,)).fetchone()
            if row is None:
                return None
            history = db.execute("""
                SELECT observed_at, event, detail FROM history WHERE item_id = ? ORDER BY seq DESC
            """, (key,))
            return {**_work_item(row, review_today().isoformat()),
                    "history": [dict(event) for event in history]}

    def update_exception(self, key, reason, review_on):
        if reason is None and review_on is not None:
            raise ValueError("Review date requires an exception reason")
        if reason is not None:
            if (not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 240
                    or not isinstance(review_on, str)):
                raise ValueError("Invalid exception reason or review date")
            day = date.fromisoformat(review_on)
            if day.isoformat() != review_on or day <= review_today():
                raise ValueError("Review date must be later than today")
            reason = reason.strip()
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(WORK_ITEM_ROWS + " WHERE item.id = ?", (key,)).fetchone()
            if prior is None:
                return "missing"
            if prior["status"] != "open":
                return "closed"
            if (reason, review_on) == (prior["exception_reason"], prior["exception_review_on"]):
                return "unchanged"
            stamp = datetime.now(timezone.utc).isoformat()
            if reason is None:
                db.execute("DELETE FROM work_item_exception WHERE item_id = ?", (key,))
                self._event(db, key, stamp, "exception_removed", "已取消例外，恢复待处理")
            else:
                db.execute("INSERT OR REPLACE INTO work_item_exception VALUES (?, ?, ?)", (key, reason, review_on))
                self._event(db, key, stamp, "exception_set", f"{review_on} 复查（UTC+8） · {reason}")
        return "saved"

    def record(self, report):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            recorded = self._record_inspection(db, report)
            return {**self._snapshot(db), "recorded": recorded}

    def latest_inspection_time(self):
        with self.connection() as db:
            row = db.execute("SELECT json_extract(report, '$.observed_at') FROM inspection").fetchone()
            return row[0] if row else None

    def inventory_report(self):
        with self.connection() as db:
            db.execute("BEGIN")
            return self._inventory_report(db)

    @staticmethod
    def _inventory_report(db, latest=None):
        inventory = db.execute("SELECT report FROM asset_inventory").fetchone()
        if inventory:
            return json.loads(inventory["report"])
        if latest is None:
            row = db.execute("SELECT report FROM inspection").fetchone()
            if not row:
                return {}
            latest = json.loads(row["report"])
        if "selected_accounts" not in latest:
            return _merge_inventory({}, latest)
        previous = db.execute("SELECT report FROM last_full_inspection").fetchone()
        baseline = _merge_inventory({}, json.loads(previous["report"])) if previous else {}
        return _merge_inventory(baseline, latest)

    @staticmethod
    def inspection_account_ids(db):
        row = db.execute("SELECT account_ids FROM inspection_plan_scope").fetchone()
        return json.loads(row["account_ids"]) if row else None

    @staticmethod
    def save_inspection_account_ids(db, account_ids):
        db.execute("INSERT OR REPLACE INTO inspection_plan_scope VALUES (1, ?)", (json.dumps(account_ids),))

    @staticmethod
    def _save_inspection_run(db, report, trigger):
        if not report["expected_accounts"]:
            return
        summary = {**inspection_summary(report), "trigger": trigger}
        db.execute("INSERT INTO inspection_run (summary) VALUES (?)", (json.dumps(summary, ensure_ascii=False),))

    def inspection_history(self, before=None):
        with self.connection() as db:
            rows = db.execute("""
                SELECT seq, summary FROM inspection_run WHERE ? IS NULL OR seq < ?
                ORDER BY seq DESC LIMIT 21
            """, (before, before)).fetchall()
        page = rows[:20]
        return {"runs": [json.loads(row["summary"]) for row in page],
                "before": page[-1]["seq"] if len(rows) > len(page) else None}

    def _record_inspection(self, db, report, *, trigger="manual"):
        stamp = report["observed_at"]
        accounts = {account["id"]: account for account in report["accounts"]}
        confirmed_dates = {item_id({**asset, "kind": "expiry"}) for asset in report.get("assets", [])
                           if asset["kind"] in {"domain", "certificate"} and asset.get("expiry_status") == "normal"}
        scoped = "selected_accounts" in report
        selected = set(report["selected_accounts"]) if scoped else None
        previous = db.execute("SELECT report FROM inspection").fetchone()
        previous_report = None
        if previous:
            previous_report = json.loads(previous["report"])
            latest = datetime.fromisoformat(previous_report["observed_at"])
            if datetime.fromisoformat(stamp) <= latest:
                return False
        inventory = _merge_inventory(self._inventory_report(db, previous_report) if previous_report else {}, report)
        db.execute("INSERT OR REPLACE INTO asset_inventory VALUES (1, ?)",
                   (json.dumps(inventory, ensure_ascii=False),))

        rows = db.execute("SELECT * FROM work_item")
        existing = {row["id"]: row for row in rows}
        hits = {item_id(finding): finding for finding in report["findings"]}
        for key, finding in hits.items():
            self._apply_hit(db, key, existing.get(key), stamp, finding, "本轮巡检命中")

        for key, prior in existing.items():
            if key in hits or prior["status"] != "open":
                continue
            finding = json.loads(prior["evidence"])
            if finding["kind"] in {"website", "website-certificate", "host-disk"}:
                continue
            if selected is not None and finding["account"] not in selected:
                continue
            if finding["kind"] == "billing":
                comparisons = accounts.get(finding["account"], {}).get("billing_checks", [])
                if any(row["scope"] == finding["scope"] and finding["id"] in row["compared"]
                       for row in comparisons):
                    self._resolve(db, key, stamp, "同一费用口径复查后未再达到提醒线")
                else:
                    self._event(db, key, stamp, "retained", "本轮缺少同口径账期对照，保留上次费用证据")
                continue
            if finding["kind"] == "expiry" and finding.get("resource_kind") in {"domain", "certificate"}:
                if key in confirmed_dates:
                    self._resolve(db, key, stamp, "同一资源复查到期时间已超出提醒范围")
                else:
                    self._event(db, key, stamp, "retained", "同一资源未取得可确认的到期时间，保留上次证据")
                continue
            if scope_complete(finding, accounts):
                self._resolve(db, key, stamp, "原检查范围已查全，本轮未再命中")
            else:
                self._event(db, key, stamp, "retained", "原检查范围未查全，保留上次命中依据")

        db.execute("INSERT OR REPLACE INTO inspection VALUES (1, ?)",
                   (json.dumps(report, ensure_ascii=False),))
        self._save_inspection_run(db, report, trigger)
        return True

    def website_monitors(self):
        with self.connection() as db:
            rows = db.execute(WEBSITE_DEFINITIONS + " ORDER BY rowid")
            return {row["id"]: Monitor(**dict(row)) for row in rows}

    def register_website(self, monitor):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM website_monitor WHERE url = ?", (monitor.url,)).fetchone()
            if existing is not None:
                return {"id": existing["id"], "status": "existing"}
            if db.execute("SELECT count(*) FROM website_monitor").fetchone()[0] >= MAX_MONITORS:
                return {"status": "limit"}
            db.execute("INSERT INTO website_monitor VALUES (?, ?, ?, ?)",
                       (monitor.id, monitor.url, monitor.expected_status, monitor.contains))
            return {"id": monitor.id, "status": "registered"}

    def update_website_conditions(self, target_id, revision, expected_status, content_action, contains, now):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(WEBSITE_DEFINITIONS + " WHERE id = ?", (target_id,)).fetchone()
            if row is None:
                return "missing"
            current = Monitor(**dict(row))
            if revision != current.revision:
                return "stale"
            text = current.contains if content_action == "keep" else contains
            updated = replace(current, expected_status=expected_status, contains=text)
            if updated == current:
                return "unchanged"
            updated = replace(updated, revision=current.revision + 1)
            db.execute("INSERT INTO website_condition_revision VALUES (?, ?, ?, ?, ?)",
                       (current.id, current.revision, current.key, current.expected_status, current.contains is not None))
            db.execute("UPDATE website_monitor SET expected_status = ?, contains = ? WHERE id = ?",
                       (updated.expected_status, updated.contains, updated.id))
            db.execute("""
                INSERT INTO website_schedule (target_key, interval_minutes, next_at, since)
                SELECT ?, interval_minutes,
                    CASE WHEN next_at IS NOT NULL THEN ? + interval_minutes * 60 END,
                    CASE WHEN next_at IS NOT NULL THEN ? END
                FROM website_schedule WHERE target_key = ?
            """, (updated.key, now, now, current.key))
            db.execute("UPDATE website_schedule SET next_at = NULL WHERE target_key = ?", (current.key,))
            key = item_id({"kind": "website", "account": "website-monitor", "id": current.key})
            stamp = datetime.now(timezone.utc).isoformat()
            closed = db.execute("""
                UPDATE work_item SET status = 'superseded', resolved_at = ? WHERE id = ? AND status = 'open'
            """, (stamp, key))
            if closed.rowcount:
                db.execute("DELETE FROM work_item_exception WHERE item_id = ?", (key,))
                self._event(db, key, stamp, "superseded", "检测条件已更新，保留旧条件下的异常证据")
            return "updated"

    def previous_website_history(self, monitor):
        with self.connection() as db:
            rows = db.execute("""
                SELECT observation, expected_status, checks_content, revision
                FROM website_condition_revision JOIN monitor_run USING (target_key)
                WHERE monitor_id = ? AND revision < ? ORDER BY revision DESC, seq DESC LIMIT 30
            """, (monitor.id, monitor.revision))
            return [{**json.loads(row["observation"]), "expected_status": row["expected_status"],
                     "checks_content": bool(row["checks_content"]), "revision": row["revision"]} for row in rows]

    def previous_website_condition(self, key):
        with self.connection() as db:
            row = db.execute("""
                SELECT monitor_id, expected_status, checks_content FROM website_condition_revision WHERE target_key = ?
            """, (key,)).fetchone()
            return dict(row) if row else None

    def host_registrations(self):
        with self.connection() as db:
            return [json.loads(row["definition"]) for row in db.execute("SELECT definition FROM host_registration ORDER BY rowid")]

    def register_host(self, identity, binding):
        identity = json.dumps(identity)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM host_registration WHERE identity = ?", (identity,)).fetchone()
            if existing:
                return {"id": existing["id"], "status": "existing"}
            if db.execute("SELECT count(*) FROM host_registration").fetchone()[0] >= 100:
                return {"status": "limit"}
            db.execute("INSERT INTO host_registration VALUES (?, ?, ?)", (binding["id"], identity, json.dumps(binding)))
            return {"id": binding["id"], "status": "registered"}

    def monitor_history(self, monitor, limit=30):
        with self.connection() as db:
            return self._monitor_history(db, monitor.key, limit)

    @staticmethod
    def _monitor_history(db, key, limit=30):
        rows = db.execute("""
            SELECT observation FROM monitor_run WHERE target_key = ? ORDER BY seq DESC LIMIT ?
        """, (key, limit))
        return [json.loads(row["observation"]) for row in rows]

    def failed_observation(self, key, stamp):
        with self.connection() as db:
            row = db.execute("""
                SELECT observation FROM monitor_run WHERE target_key = ?
                  AND json_extract(observation, '$.observed_at') = ?
                  AND json_extract(observation, '$.status') = 'fail'
            """, (key, stamp)).fetchone()
        return json.loads(row["observation"]) if row else None

    def record_monitor(self, monitor, result, synthetic):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._record_monitor(db, monitor, result, synthetic)
            return self._monitor_history(db, monitor.key)

    def _record_monitor(self, db, monitor, result, synthetic):
        stamp = result["observed_at"]
        recorded, _ = self._append_observation(db, monitor, result)
        if not recorded or db.execute("SELECT 1 FROM website_condition_revision WHERE target_key = ?",
                                      (monitor.key,)).fetchone():
            return
        self._record_certificate(db, monitor, result, synthetic)
        finding = {
            "kind": "website", "account": "website-monitor", "id": monitor.key,
            "name": monitor.url, "account_label": "网站监控", "provider": "网站监控",
            "data_source": "synthetic" if synthetic else "live",
            "http_status": result["http_status"], "content": result["content"],
            "status": result["status"], "monitor_id": monitor.id,
        }
        key = item_id(finding)
        prior = db.execute("SELECT * FROM work_item WHERE id = ?", (key,)).fetchone()
        if result["status"] == "fail":
            self._apply_hit(db, key, prior, stamp, finding, "网站检测未通过")
        elif prior and prior["status"] == "open":
            if result["status"] == "pass":
                self._resolve(db, key, stamp, "同一 URL 复查通过")
            else:
                self._event(db, key, stamp, "retained", "本轮检测不完整，保留上次异常证据")

    def _record_certificate(self, db, monitor, result, synthetic):
        certificate = certificate_status(result)
        status = certificate["status"]
        stamp = result["observed_at"]
        finding = {"kind": "website-certificate", "account": "website-monitor", "id": monitor.id,
                   "scope": monitor.url, "name": monitor.url, "account_label": "网站监控", "provider": "网站监控",
                   "data_source": "synthetic" if synthetic else "live", **certificate}
        key = item_id(finding)
        prior = db.execute("SELECT status, last_seen, resolved_at FROM work_item WHERE id = ?", (key,)).fetchone()
        if prior and datetime.fromisoformat(stamp) <= datetime.fromisoformat(prior["resolved_at"] or prior["last_seen"]):
            return
        if status in {"due", "urgent", "expired"}:
            detail = "取得的线上证书已到期" if status == "expired" else f"线上证书剩余 {certificate['days_remaining']} 天"
            self._apply_hit(db, key, prior, stamp, finding, detail)
        elif prior and prior["status"] == "open":
            if status == "valid":
                self._resolve(db, key, stamp, f"同一 URL 复查证书有效，剩余 {certificate['days_remaining']} 天")
            else:
                self._event(db, key, stamp, "retained", "本轮未取得可校验证书，保留上次到期证据")

    def _append_observation(self, db, monitor, result):
        row = db.execute("""
            SELECT observation FROM monitor_run WHERE target_key = ? ORDER BY seq DESC LIMIT 1
        """, (monitor.key,)).fetchone()
        previous = json.loads(row["observation"]) if row else None
        if previous and datetime.fromisoformat(result["observed_at"]) <= datetime.fromisoformat(previous["observed_at"]):
            return False, previous
        db.execute("INSERT INTO monitor_run (target_key, observation) VALUES (?, ?)",
                   (monitor.key, json.dumps(result, ensure_ascii=False)))
        return True, previous

    def host_history(self, monitor, since):
        with self.connection() as db:
            rows = db.execute("""
                SELECT observation FROM monitor_run
                WHERE target_key = ? AND json_extract(observation, '$.observed_at') >= ?
                ORDER BY seq DESC LIMIT 1501
            """, (monitor.key, since)).fetchall()
        return {"history": [json.loads(row["observation"]) for row in rows[:1500]],
                "truncated": len(rows) > 1500}

    def record_host(self, monitor, result, synthetic):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._record_host(db, monitor, result, synthetic)

    def _record_host(self, db, monitor, result, synthetic):
        recorded, previous = self._append_observation(db, monitor, result)
        threshold = monitor.disk_warn_percent
        if not recorded or threshold is None:
            return
        point = result["metrics"].get("disk_percent")
        previous_point = previous["metrics"].get("disk_percent") if previous else None
        interval = result.get("interval_minutes")
        if previous_point and interval:
            elapsed = datetime.fromisoformat(result["observed_at"]) - datetime.fromisoformat(previous["observed_at"])
            if elapsed.total_seconds() > interval * 90:
                previous_point = None
        finding = {"kind": "host-disk", "account": monitor.account_id, "id": monitor.key,
                   "name": monitor.name, "account_label": "服务器监控", "provider": "服务器监控",
                   "data_source": "synthetic" if synthetic else "live", "threshold": threshold,
                   "used_percent": point["value"] if point else None, "status": "fail"}
        key = item_id(finding)
        prior = db.execute("SELECT * FROM work_item WHERE id = ?", (key,)).fetchone()
        opened = prior and prior["status"] == "open"
        stamp = result["observed_at"]
        if point is None:
            if opened:
                self._event(db, key, stamp, "retained", "本轮未取得磁盘使用率，保留上次异常证据")
        elif point["value"] < threshold:
            if opened:
                self._resolve(db, key, stamp, "同一服务器磁盘使用率复查低于阈值")
        elif opened or (previous_point and previous_point["value"] >= threshold):
            self._apply_hit(db, key, prior, stamp, finding, "根分区磁盘使用率达到预警阈值")

    def _apply_hit(self, db, key, prior, stamp, finding, detail):
        if prior and finding["status"] == "unknown":
            self._event(db, key, stamp, "retained", "本轮缺少可确认的资源字段")
            return
        evidence = json.dumps(finding, ensure_ascii=False)
        if prior:
            db.execute("""
                UPDATE work_item SET status = 'open', evidence = ?, last_seen = ?,
                    resolved_at = NULL WHERE id = ?
            """, (evidence, stamp, key))
            event = "reopened" if prior["status"] == "resolved" else "confirmed"
        else:
            db.execute("INSERT INTO work_item VALUES (?, 'open', ?, ?, ?, NULL)",
                       (key, evidence, stamp, stamp))
            event = "opened"
        self._event(db, key, stamp, event, detail)

    def _resolve(self, db, key, stamp, detail):
        db.execute("UPDATE work_item SET status = 'resolved', resolved_at = ? WHERE id = ?", (stamp, key))
        db.execute("DELETE FROM work_item_exception WHERE item_id = ?", (key,))
        self._event(db, key, stamp, "resolved", detail)

    @staticmethod
    def _event(db, key, stamp, event, detail):
        db.execute("INSERT INTO history (item_id, observed_at, event, detail) VALUES (?, ?, ?, ?)",
                   (key, stamp, event, detail))
