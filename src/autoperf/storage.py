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
  started_at TEXT, finished_at TEXT, checkpoint TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS metric_samples (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp TEXT NOT NULL,
  collector TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, labels TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_metrics_run_time ON metric_samples(run_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_metrics_run_id ON metric_samples(run_id, id);
CREATE TABLE IF NOT EXISTS test_events (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp TEXT NOT NULL,
  kind TEXT NOT NULL, message TEXT NOT NULL, details TEXT NOT NULL);
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

    def register_device(self, device: Device) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("INSERT INTO devices VALUES (?, ?, ?, ?) ON CONFLICT(serial) DO UPDATE SET model=excluded.model, product=excluded.product, last_seen=excluded.last_seen",
                             (device.serial, device.model, device.product, utc_now()))

    def create_run(self, run_id: str, serial: str) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("INSERT INTO test_runs(id, device_serial, status) VALUES (?, ?, ?)", (run_id, serial, RunStatus.PENDING))

    def update_run(self, run_id: str, status: RunStatus, *, checkpoint: str | None = None, error: str | None = None) -> None:
        now = utc_now()
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("UPDATE test_runs SET status=?, started_at=CASE WHEN ?='running' THEN COALESCE(started_at, ?) ELSE started_at END, finished_at=CASE WHEN ? IN ('completed','failed','interrupted') THEN ? ELSE finished_at END, checkpoint=COALESCE(?,checkpoint), error=? WHERE id=?",
                             (status, status, now, status, now, checkpoint, error, run_id))

    def get_run(self, run_id: str) -> dict | None:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM test_runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def list_devices(self) -> list[dict]:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM devices ORDER BY last_seen DESC").fetchall()
            return [dict(row) for row in rows]

    def list_runs(self, limit: int = 100) -> list[dict]:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM test_runs ORDER BY rowid DESC LIMIT ?", (limit,)
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
