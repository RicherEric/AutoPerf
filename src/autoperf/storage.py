from __future__ import annotations

import json
import queue
import sqlite3
import threading
from dataclasses import asdict
from contextlib import closing
from pathlib import Path

from .models import Device, MetricSample, RunStatus, TestEvent, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (serial TEXT PRIMARY KEY, model TEXT, product TEXT, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS test_runs (id TEXT PRIMARY KEY, device_serial TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, checkpoint TEXT, error TEXT, youtube_scenario TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS metric_samples (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp TEXT NOT NULL,
  collector TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, labels TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_metrics_run_time ON metric_samples(run_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_metrics_run_id ON metric_samples(run_id, id);
CREATE TABLE IF NOT EXISTS test_events (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp TEXT NOT NULL,
  kind TEXT NOT NULL, message TEXT NOT NULL, details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS baselines (device_serial TEXT NOT NULL, scenario TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (device_serial, scenario));
CREATE INDEX IF NOT EXISTS idx_runs_serial_status ON test_runs(device_serial, status);
"""


class Storage:
    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def initialize(self) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.executescript(SCHEMA)
                # Migration for databases created before youtube_scenario existed --
                # CREATE TABLE IF NOT EXISTS above is a no-op on an existing table,
                # so older files need the column added explicitly. Idempotent: a
                # fresh database already has it via SCHEMA and this is skipped.
                columns = {row[1] for row in conn.execute("PRAGMA table_info(test_runs)")}
                if "youtube_scenario" not in columns:
                    conn.execute("ALTER TABLE test_runs ADD COLUMN youtube_scenario TEXT")
                if "cancel_requested" not in columns:
                    conn.execute("ALTER TABLE test_runs ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
                device_columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
                for name in ("nickname", "android_version", "battery_level", "connection", "extra_info"):
                    if name not in device_columns:
                        coltype = "REAL" if name == "battery_level" else "TEXT"
                        conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {coltype}")
                # Migration: baselines used to be one row per device, shared
                # across every scenario -- comparing a heavy scenario's CPU
                # against a light scenario's baseline produced meaningless
                # deltas (e.g. "+778%") that were really just "this scenario
                # does more work", not a real regression. Rebuild the table
                # with each row scoped to (device, scenario), backfilling
                # each existing baseline's scenario from the run it actually
                # points to via a join, rather than dropping history.
                baseline_columns = {row[1] for row in conn.execute("PRAGMA table_info(baselines)")}
                if baseline_columns and "scenario" not in baseline_columns:
                    conn.execute("ALTER TABLE baselines RENAME TO baselines_old")
                    conn.execute(
                        "CREATE TABLE baselines (device_serial TEXT NOT NULL, scenario TEXT NOT NULL DEFAULT '', "
                        "run_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (device_serial, scenario))"
                    )
                    conn.execute(
                        "INSERT INTO baselines(device_serial, scenario, run_id, created_at) "
                        "SELECT b.device_serial, COALESCE(t.youtube_scenario, ''), b.run_id, b.created_at "
                        "FROM baselines_old b LEFT JOIN test_runs t ON t.id = b.run_id"
                    )
                    conn.execute("DROP TABLE baselines_old")

    def register_device(
        self, device: Device, *,
        android_version: str | None = None, battery_level: float | None = None, connection: str | None = None,
        device_name: str | None = None, extra_info: dict | None = None,
    ) -> None:
        # `device_name` is the phone's own user-set name (Settings > About
        # phone > Device name -- the same name shown when pairing Bluetooth/
        # WiFi Direct), used as a free default nickname so a classroom demo
        # with many phones doesn't need everyone typed in by hand. It only
        # fills a NULL nickname (COALESCE keeps whatever a user already typed
        # via set_device_nickname) -- a manual nickname always wins and
        # survives a later devices/refresh.
        #
        # `extra_info` is a free-form dict of additional identity fields
        # (manufacturer, sdk version, build id, CPU ABI, WiFi IP, a
        # constructed User-Agent string, ...) stored as one JSON blob rather
        # than one column each, so adding more fields later never needs
        # another migration. Unlike nickname it's fully auto-detected, so it
        # always reflects the latest scan rather than being preserved.
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO devices(serial, model, product, last_seen, android_version, battery_level, connection, nickname, extra_info) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(serial) DO UPDATE SET model=excluded.model, product=excluded.product, "
                    "last_seen=excluded.last_seen, android_version=excluded.android_version, "
                    "battery_level=excluded.battery_level, connection=excluded.connection, "
                    "nickname=COALESCE(devices.nickname, excluded.nickname), extra_info=excluded.extra_info",
                    (
                        device.serial, device.model, device.product, utc_now(), android_version, battery_level,
                        connection, device_name, json.dumps(extra_info or {}),
                    ),
                )

    def set_device_nickname(self, serial: str, nickname: str) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("UPDATE devices SET nickname=? WHERE serial=?", (nickname, serial))

    def create_run(self, run_id: str, serial: str, youtube_scenario: str | None = None) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO test_runs(id, device_serial, status, youtube_scenario) VALUES (?, ?, ?, ?)",
                    (run_id, serial, RunStatus.PENDING, youtube_scenario),
                )

    def update_run(self, run_id: str, status: RunStatus, *, checkpoint: str | None = None, error: str | None = None) -> None:
        now = utc_now()
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("UPDATE test_runs SET status=?, started_at=CASE WHEN ?='running' THEN COALESCE(started_at, ?) ELSE started_at END, finished_at=CASE WHEN ? IN ('completed','failed','interrupted') THEN ? ELSE finished_at END, checkpoint=COALESCE(?,checkpoint), error=? WHERE id=?",
                             (status, status, now, status, now, checkpoint, error, run_id))

    def try_start_run(self, run_id: str) -> bool:
        """Atomically claims 'running' for run_id iff no *other* run for the
        same device is already 'running' -- lets multiple Celery workers
        process different devices in parallel while still serializing runs
        against the same device, one SQLite UPDATE at a time. Deliberately
        not scoped to any particular prior status of run_id itself, so
        resuming a previously completed/interrupted run_id (see
        TestRunner.run()'s resume handling above) still works -- only a
        *different* run_id racing the same device is refused. Returns
        whether this call was the one that made the claim."""
        now = utc_now()
        with closing(self.connect()) as conn:
            with conn:
                cur = conn.execute(
                    """UPDATE test_runs SET status=?, started_at=COALESCE(started_at, ?)
                       WHERE id=? AND NOT EXISTS (
                         SELECT 1 FROM test_runs t2
                         WHERE t2.device_serial = (SELECT device_serial FROM test_runs WHERE id=?)
                           AND t2.status=? AND t2.id != ?)""",
                    (RunStatus.RUNNING, now, run_id, run_id, RunStatus.RUNNING, run_id),
                )
                return cur.rowcount == 1

    def request_cancel(self, run_id: str) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("UPDATE test_runs SET cancel_requested=1 WHERE id=?", (run_id,))

    def delete_run(self, run_id: str) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("DELETE FROM metric_samples WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM test_events WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM test_runs WHERE id=?", (run_id,))

    def get_run(self, run_id: str) -> dict | None:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM test_runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_devices(self) -> list[dict]:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
            results = []
            for row in rows:
                device = dict(row)
                device.update(json.loads(device.pop("extra_info") or "{}"))
                results.append(device)
            return results

    def list_runs(self, limit: int = 100, device_serial: str | None = None) -> list[dict]:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            if device_serial:
                rows = conn.execute(
                    "SELECT * FROM test_runs WHERE device_serial=? ORDER BY rowid DESC LIMIT ?",
                    (device_serial, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM test_runs ORDER BY rowid DESC LIMIT ?", (limit,)
                ).fetchall()
            return [dict(row) for row in rows]

    def list_running_runs(self, limit: int = 100) -> list[dict]:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM test_runs WHERE status='running' ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(row) for row in rows]

    def list_samples(self, run_id: str, since_id: int = 0, limit: int = 1000) -> list[dict]:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM metric_samples WHERE run_id=? AND id>? ORDER BY id ASC LIMIT ?",
                (run_id, since_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def set_baseline(self, device_serial: str, run_id: str) -> None:
        # Scenario is derived from the run itself (never passed separately)
        # so a baseline is always scoped to whichever scenario the chosen
        # run actually used -- see get_baseline's docstring for why this
        # scoping matters. '' means "the plain/no-scenario baseline".
        run = self.get_run(run_id)
        scenario = (run["youtube_scenario"] if run else None) or ""
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO baselines(device_serial, scenario, run_id, created_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(device_serial, scenario) DO UPDATE SET run_id=excluded.run_id, created_at=excluded.created_at",
                    (device_serial, scenario, run_id, utc_now()),
                )

    def get_baseline(self, device_serial: str, scenario: str | None = None) -> dict | None:
        """Baselines are scoped per (device, scenario) -- a scenario that
        drives more on-screen interaction than another will naturally use
        more CPU/memory regardless of any real regression, so comparing it
        against a *different* scenario's baseline produces meaningless
        deltas. `scenario=None` (or '') looks up the plain/no-scenario
        baseline; pass a run's own `youtube_scenario` to get the baseline
        that's actually comparable to it.
        """
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM baselines WHERE device_serial=? AND scenario=?",
                (device_serial, scenario or ""),
            ).fetchone()
            return dict(row) if row else None


class BatchWriter:
    def __init__(self, storage: Storage, batch_size: int = 100, flush_seconds: float = 1.0,
                 queue_size: int = 10_000, put_timeout: float = 2.0):
        self.storage, self.batch_size, self.flush_seconds = storage, batch_size, flush_seconds
        self.put_timeout = put_timeout
        self._queue: queue.Queue[MetricSample | TestEvent | None] = queue.Queue(maxsize=queue_size)
        self._thread: threading.Thread | None = None
        self.error: Exception | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="autoperf-writer", daemon=False)
        self._thread.start()

    def put(self, item: MetricSample | TestEvent) -> None:
        if self.error:
            raise RuntimeError("Batch writer failed") from self.error
        try:
            self._queue.put(item, timeout=self.put_timeout)
        except queue.Full as exc:
            raise RuntimeError("Metrics queue is full; writer cannot keep up") from exc

    def close(self) -> None:
        self._queue.put(None, timeout=self.put_timeout)
        if self._thread:
            self._thread.join()
        if self.error:
            raise RuntimeError("Batch writer failed") from self.error

    def _run(self) -> None:
        pending: list[MetricSample | TestEvent] = []
        try:
            with closing(self.storage.connect()) as conn:
                stopping = False
                while not stopping:
                    try:
                        item = self._queue.get(timeout=self.flush_seconds)
                        if item is None:
                            stopping = True
                        else:
                            pending.append(item)
                    except queue.Empty:
                        pass
                    if pending and (stopping or len(pending) >= self.batch_size or self._queue.empty()):
                        for entry in pending:
                            if isinstance(entry, MetricSample):
                                conn.execute("INSERT INTO metric_samples(run_id,timestamp,collector,name,value,unit,labels) VALUES (?,?,?,?,?,?,?)",
                                             (entry.run_id, entry.timestamp, entry.collector, entry.name, entry.value, entry.unit, json.dumps(entry.labels)))
                            else:
                                conn.execute("INSERT INTO test_events(run_id,timestamp,kind,message,details) VALUES (?,?,?,?,?)",
                                             (entry.run_id, entry.timestamp, entry.kind, entry.message, json.dumps(entry.details)))
                        conn.commit()
                        pending.clear()
        except Exception as exc:
            self.error = exc
