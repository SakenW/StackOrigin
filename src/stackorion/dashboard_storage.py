"""Local dashboard storage policy and recoverable encrypted backups."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
import json
import logging
import re
import shutil
import sqlite3
import subprocess
import tarfile
from threading import Event, Lock, Thread


MAX_RETENTION_DAYS = 3650
MAX_BACKUP_BYTES = 64 * 1024 * 1024
MAX_ENCRYPTED_BYTES = MAX_BACKUP_BYTES + 1024 * 1024
AGE_RECIPIENT = re.compile(r"age1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{58}")


class BackupError(RuntimeError):
    pass


class DashboardStorage:
    def __init__(self, live, demo, website_config, host_config):
        self.live = live
        self.demo = demo
        self.configs = {"website-monitors.json": website_config, "host-monitors.json": host_config}
        self._backup_lock = Lock()

    @staticmethod
    def _retention_days(db):
        table = db.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'storage_policy'").fetchone()
        if table is None:
            return None
        row = db.execute("SELECT monitor_days FROM storage_policy WHERE singleton = 1").fetchone()
        return row["monitor_days"] if row else None

    @staticmethod
    def _database_size(store):
        total = 0
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                total += store.path.with_name(store.path.name + suffix).stat().st_size
            except FileNotFoundError:
                if not suffix:
                    raise
        return total

    def summary(self):
        with self.live.connection() as db:
            days = self._retention_days(db)
            evidence = self._backup_evidence(db)
        return {
            "state_dir": str(self.live.path.parent.parent.resolve()),
            "live": {"bytes": self._database_size(self.live), "encrypted": False},
            "demo": {"bytes": self._database_size(self.demo)} if self.demo else None,
            "retention_days": days,
            "backup_available": hasattr(sqlite3.Connection, "serialize") and shutil.which("age") is not None,
            "backup_evidence": evidence,
        }

    @staticmethod
    def _backup_evidence(db):
        result = {"generated": None, "restore_drill": None}
        if db.execute("SELECT 1 FROM sqlite_schema WHERE name = 'storage_backup' AND type = 'table'").fetchone():
            for field, column in (("generated", "generated_at"), ("restore_drill", "verified_at")):
                row = db.execute(f"""
                    SELECT digest, {column} AS observed_at FROM storage_backup
                    WHERE {column} IS NOT NULL ORDER BY {column} DESC LIMIT 1
                """).fetchone()
                result[field] = dict(row) if row else None
        return result

    def configure(self, days):
        if days is not None and (type(days) is not int or not 1 <= days <= MAX_RETENTION_DAYS):
            raise ValueError("Invalid retention days")
        with self.live.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""
                CREATE TABLE IF NOT EXISTS storage_policy (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    monitor_days INTEGER CHECK (monitor_days BETWEEN 1 AND 3650)
                )
            """)
            db.execute("INSERT OR REPLACE INTO storage_policy VALUES (1, ?)", (days,))
            return self._prune(db, days)

    @staticmethod
    def _prune(db, days):
        if days is None:
            return 0
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return db.execute("""
            DELETE FROM monitor_run
            WHERE julianday(json_extract(observation, '$.observed_at')) < julianday(?)
        """, (cutoff,)).rowcount

    def start(self):
        stop = Event()

        def loop():
            while not stop.is_set():
                try:
                    with self.live.connection() as db:
                        if self._retention_days(db) is not None:
                            # Recheck under the write lock in case the user just changed the policy.
                            db.execute("BEGIN IMMEDIATE")
                            self._prune(db, self._retention_days(db))
                except sqlite3.Error:
                    logging.getLogger(__name__).exception("Dashboard retention failed")
                stop.wait(3600)

        Thread(target=loop, name="dashboard-retention", daemon=True).start()
        return stop

    def encrypted_backup(self, recipient):
        if not isinstance(recipient, str) or not AGE_RECIPIENT.fullmatch(recipient):
            raise BackupError("invalid_recipient")
        age = shutil.which("age")
        if age is None or not hasattr(sqlite3.Connection, "serialize"):
            raise BackupError("backup_unavailable")
        if not self._backup_lock.acquire(blocking=False):
            raise BackupError("backup_busy")
        try:
            archive = self._backup_archive()
            try:
                encrypted = subprocess.run([age, "--encrypt", "-r", recipient], input=archive,
                                           capture_output=True, check=True, timeout=20).stdout
            except (OSError, subprocess.SubprocessError):
                raise BackupError("encryption_failed") from None
            if not encrypted.startswith(b"age-encryption.org/v1\n"):
                raise BackupError("encryption_failed")
            with self.live.connection() as db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS storage_backup (
                        digest TEXT PRIMARY KEY, generated_at TEXT NOT NULL, verified_at TEXT
                    )
                """)
                db.execute("INSERT OR IGNORE INTO storage_backup (digest, generated_at) VALUES (?, ?)",
                           (sha256(encrypted).hexdigest(), datetime.now(timezone.utc).isoformat()))
            return encrypted
        finally:
            self._backup_lock.release()

    def verify_restore(self, source, identity):
        age = shutil.which("age")
        if age is None or not hasattr(sqlite3.Connection, "deserialize"):
            raise BackupError("restore_drill_unavailable")
        if not source.is_file() or not identity.is_file():
            raise BackupError("restore_drill_input_unavailable")
        with source.open("rb") as stream:
            encrypted = stream.read(MAX_ENCRYPTED_BYTES + 1)
        if len(encrypted) > MAX_ENCRYPTED_BYTES:
            raise BackupError("backup_too_large")
        digest = sha256(encrypted).hexdigest()
        with self.live.connection() as db:
            has_evidence = db.execute("SELECT 1 FROM sqlite_schema WHERE name = 'storage_backup' AND type = 'table'").fetchone()
            if not has_evidence or not db.execute("SELECT 1 FROM storage_backup WHERE digest = ?", (digest,)).fetchone():
                raise BackupError("backup_not_recorded")
        try:
            plain = subprocess.run([age, "--decrypt", "-i", str(identity.absolute())], input=encrypted,
                                   capture_output=True, check=True, timeout=20).stdout
        except (OSError, subprocess.SubprocessError):
            raise BackupError("decryption_failed") from None
        try:
            with tarfile.open(fileobj=BytesIO(plain), mode="r:gz") as archive:
                members = archive.getmembers()
                names = {member.name for member in members}
                if (names - {"live/work-items.db", *self.configs} or "live/work-items.db" not in names
                        or len(names) != len(members) or any(not member.isfile() for member in members)):
                    raise BackupError("invalid_backup_archive")
                for name in names - {"live/work-items.db"}:
                    with archive.extractfile(name) as stream:
                        if not isinstance(json.load(stream), list):
                            raise BackupError("invalid_backup_archive")
                with archive.extractfile("live/work-items.db") as stream:
                    snapshot = stream.read(MAX_BACKUP_BYTES + 1)
                if len(snapshot) > MAX_BACKUP_BYTES or not snapshot.startswith(b"SQLite format 3\x00"):
                    raise BackupError("invalid_backup_archive")
            with closing(sqlite3.connect(":memory:")) as recovered:
                recovered.deserialize(snapshot)
                recovered.execute("PRAGMA trusted_schema=OFF")
                recovered.execute("PRAGMA query_only=ON")
                if recovered.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise BackupError("invalid_backup_archive")
                for table in ("monitor_run", "work_item", "website_schedule"):
                    recovered.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        except (tarfile.TarError, EOFError, OSError, sqlite3.Error, ValueError):
            raise BackupError("invalid_backup_archive") from None
        stamp = datetime.now(timezone.utc).isoformat()
        with self.live.connection() as db:
            db.execute("UPDATE storage_backup SET verified_at = ? WHERE digest = ?", (stamp, digest))
        return {"digest": digest, "observed_at": stamp}

    def _backup_archive(self):
        with self.live.connection() as source, closing(sqlite3.connect(":memory:")) as copy:
            source.execute("BEGIN")
            pages = source.execute("PRAGMA page_count").fetchone()[0]
            page_size = source.execute("PRAGMA page_size").fetchone()[0]
            if pages * page_size > MAX_BACKUP_BYTES:
                raise BackupError("backup_too_large")
            source.backup(copy)
            snapshot = copy.serialize()

        members = {"live/work-items.db": snapshot}
        for name, path in self.configs.items():
            if path is not None:
                with path.open("rb") as handle:
                    members[name] = handle.read(65_537)
                if len(members[name]) > 65_536:
                    raise BackupError("backup_too_large")

        buffer = BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz", compresslevel=3) as archive:
            for name, content in members.items():
                info = tarfile.TarInfo(name)
                info.mode = 0o600
                info.size = len(content)
                archive.addfile(info, BytesIO(content))
        return buffer.getvalue()
