"""Persistent local monitor schedule, with an expiring claim per target."""

from datetime import datetime, timezone
from threading import Event, Thread
import logging
import sqlite3
import time


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


class MonitorSchedules:
    def __init__(self, store, record, *, lease_seconds=30, synthetic=False, clock=time.time):
        self.store = store
        self.record = record
        self.lease_seconds = lease_seconds
        self.synthetic = synthetic
        self.clock = clock
        # Keep the original table name so existing website plans survive upgrades.
        with store.connection() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS website_schedule (
                    target_key TEXT PRIMARY KEY,
                    interval_minutes INTEGER NOT NULL,
                    next_at INTEGER,
                    since INTEGER,
                    running_slot INTEGER,
                    lease_until INTEGER,
                    good INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    missed INTEGER NOT NULL DEFAULT 0
                )
            """)

    def configure(self, key, minutes, enabled):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self._configure(db, key, minutes, enabled)
        return self.describe(key)

    def _configure(self, db, key, minutes, enabled):
        if type(minutes) is not int or not 1 <= minutes <= 1440 or type(enabled) is not bool:
            raise ValueError("Invalid schedule")
        now = int(self.clock())
        next_at = now + minutes * 60 if enabled else None
        if db.execute("SELECT 1 FROM website_condition_revision WHERE target_key = ?", (key,)).fetchone():
            raise ValueError("Website conditions changed before scheduling")
        # Keep the clock position on a no-op save and keep counters across pauses.
        db.execute("""
                INSERT INTO website_schedule (target_key, interval_minutes, next_at, since)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(target_key) DO UPDATE SET
                    next_at = CASE
                        WHEN website_schedule.interval_minutes = excluded.interval_minutes
                        AND (website_schedule.next_at IS NULL) = (excluded.next_at IS NULL)
                        THEN website_schedule.next_at
                        ELSE excluded.next_at END,
                    interval_minutes = excluded.interval_minutes,
                    since = COALESCE(website_schedule.since, excluded.since)
        """, (key, minutes, next_at, now if enabled else None))

    def describe(self, key, *, default_interval=5):
        with self.store.connection() as db:
            return self._describe(db, key, default_interval)

    def _describe(self, db, key, default_interval):
        row = db.execute("SELECT * FROM website_schedule WHERE target_key = ?", (key,)).fetchone()
        if row is None:
            return dict(interval_minutes=default_interval, enabled=False, next_at=None, since=None,
                        passed=0, failed=0, missed=0, running=False)
        return dict(interval_minutes=row["interval_minutes"], enabled=row["next_at"] is not None,
                    next_at=timestamp(row["next_at"]), since=timestamp(row["since"]),
                    passed=row["good"], failed=row["failed"], missed=row["missed"],
                    running=row["running_slot"] is not None and row["lease_until"] > self.clock())

    def claim(self, key):
        now = int(self.clock())
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM website_schedule WHERE target_key = ?", (key,)).fetchone()
            if row is None:
                return None
            if row["running_slot"] is not None:
                if row["lease_until"] > now:
                    return None
                db.execute("""
                    UPDATE website_schedule SET running_slot = NULL, lease_until = NULL, missed = missed + 1
                    WHERE target_key = ?
                """, (key,))
            if row["next_at"] is None or row["next_at"] > now:
                return None
            interval = row["interval_minutes"] * 60
            skipped = (now - row["next_at"]) // interval
            slot = row["next_at"] + interval * skipped
            db.execute("""
                UPDATE website_schedule SET next_at = ?, running_slot = ?, lease_until = ?, missed = missed + ?
                WHERE target_key = ?
            """, (slot + interval, slot, now + self.lease_seconds, skipped, key))
            return slot

    def finish(self, monitor, slot, result):
        with self.store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("""
                UPDATE website_schedule
                SET running_slot = NULL, lease_until = NULL,
                    good = good + ?, failed = failed + ?, missed = missed + ?
                WHERE target_key = ? AND running_slot = ?
            """, (result["status"] == "pass", result["status"] == "fail", result["status"] == "unknown",
                  monitor.key, slot))
            if updated.rowcount:
                minutes = db.execute("SELECT interval_minutes FROM website_schedule WHERE target_key = ?",
                                     (monitor.key,)).fetchone()[0]
                self.record(db, monitor, {**result, "trigger": "scheduled", "interval_minutes": minutes}, self.synthetic)

    def run_due(self, monitors, run):
        for monitor in monitors:
            slot = self.claim(monitor.key)
            if slot is not None:
                self.finish(monitor, slot, run(monitor))

    def start(self, get_monitors, run, *, poll_seconds=1):
        stop = Event()

        def loop():
            while not stop.is_set():
                try:
                    self.run_due(get_monitors(), run)
                except sqlite3.Error:
                    logging.getLogger(__name__).exception("Monitor schedule storage failed")
                stop.wait(poll_seconds)

        thread = Thread(target=loop, name="monitor-schedule", daemon=True)
        thread.start()
        return stop
