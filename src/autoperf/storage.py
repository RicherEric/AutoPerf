from __future__ import annotations

import datetime
import json
import queue
import sqlite3
import threading
from dataclasses import asdict
from contextlib import closing
from pathlib import Path

from .models import Device, MetricSample, RunOrigin, RunStatus, TestEvent, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (serial TEXT PRIMARY KEY, model TEXT, product TEXT, last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS test_runs (id TEXT PRIMARY KEY, device_serial TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, checkpoint TEXT, error TEXT, youtube_scenario TEXT,
  cancel_requested INTEGER NOT NULL DEFAULT 0, campaign_id TEXT,
  app_package TEXT, app_version_name TEXT, app_version_code INTEGER,
  origin TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE IF NOT EXISTS metric_samples (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp TEXT NOT NULL,
  collector TEXT NOT NULL, name TEXT NOT NULL, value REAL NOT NULL, unit TEXT NOT NULL, labels TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_metrics_run_time ON metric_samples(run_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_metrics_run_id ON metric_samples(run_id, id);
CREATE TABLE IF NOT EXISTS test_events (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp TEXT NOT NULL,
  kind TEXT NOT NULL, message TEXT NOT NULL, details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS baselines (device_serial TEXT NOT NULL, scenario TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (device_serial, scenario));
CREATE INDEX IF NOT EXISTS idx_runs_serial_status ON test_runs(device_serial, status);
CREATE TABLE IF NOT EXISTS campaigns (id TEXT PRIMARY KEY, kind TEXT NOT NULL, device_serial TEXT NOT NULL,
  scenario TEXT, tier TEXT, duration REAL NOT NULL, iterations INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS preflight_reports (id TEXT PRIMARY KEY, device_serial TEXT NOT NULL,
  scenario TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  app_package TEXT, app_version_name TEXT, app_version_code INTEGER,
  ok INTEGER, summary TEXT, error TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_preflight_serial ON preflight_reports(device_serial, created_at);
"""
# Deliberately NOT part of SCHEMA: this indexes test_runs.campaign_id, a
# column older databases only gain in the migration block below, and
# executescript(SCHEMA) runs before those migrations. Creating it there
# would work on a fresh database and fail on every existing one.
# `rowid` is not indexable in SQLite (it is the table's implicit key, not a
# column CREATE INDEX accepts), so this indexes campaign_id alone -- enough
# for the campaign_id=? lookups; the ORDER BY rowid then reads in key order.
CAMPAIGN_RUN_INDEX = "CREATE INDEX IF NOT EXISTS idx_runs_campaign ON test_runs(campaign_id)"


def _decode_preflight(row: dict) -> dict:
    row["ok"] = None if row["ok"] is None else bool(row["ok"])
    try:
        row["summary"] = json.loads(row["summary"]) if row["summary"] else None
    except json.JSONDecodeError:
        # Same rule as the event log: a report that cannot be decoded must
        # still be visible as a report that happened, not vanish from the list.
        row["summary"] = None
    return row


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
                # Links each run to the campaign that spawned it (NULL for
                # standalone runs). Must precede CAMPAIGN_RUN_INDEX below.
                if "campaign_id" not in columns:
                    conn.execute("ALTER TABLE test_runs ADD COLUMN campaign_id TEXT")
                # When this run last showed a sign of life. `checkpoint` says
                # how far it got but not when, so a row that stopped moving is
                # indistinguishable from one that is moving slowly -- and a
                # `running` row whose worker died blocks its device for good.
                if "heartbeat_at" not in columns:
                    conn.execute("ALTER TABLE test_runs ADD COLUMN heartbeat_at TEXT")
                # What this run was asked to do, kept on the row so the row is
                # enough to start it. These used to live only inside the queued
                # Celery task, which made a pending row and its task two copies
                # of one intention: lose the task (a purge, a broker restart)
                # and the row stayed `pending` forever with nothing able to
                # rebuild it. Storing them here is what lets a dispatcher pick
                # up work from the database instead of trusting the queue.
                if "duration" not in columns:
                    conn.execute("ALTER TABLE test_runs ADD COLUMN duration REAL")
                if "blind_targets" not in columns:
                    conn.execute("ALTER TABLE test_runs ADD COLUMN blind_targets TEXT")
                # When this run was last handed to the queue. Without it,
                # "has a task already been made for this row" is unanswerable
                # from the database, and a dispatcher that runs on every UI
                # poll re-queues the same row every few seconds -- 1305 copies
                # of two runs, measured. It is a timestamp rather than a flag
                # so a task that was lost (purge, worker killed) becomes
                # eligible again on its own.
                if "queued_at" not in columns:
                    conn.execute("ALTER TABLE test_runs ADD COLUMN queued_at TEXT")
                conn.execute(CAMPAIGN_RUN_INDEX)
                # Which build of the app a run actually measured. Given its
                # own columns rather than folded into a JSON blob because
                # every comparison has to check it: if the app updated
                # between a baseline and its candidate, the delta describes
                # the app's change, not the device's -- and that is the more
                # likely explanation of the two, so it must be impossible to
                # overlook rather than merely available.
                for name, coltype in (("app_package", "TEXT"), ("app_version_name", "TEXT"),
                                      ("app_version_code", "INTEGER")):
                    if name not in columns:
                        conn.execute(f"ALTER TABLE test_runs ADD COLUMN {name} {coltype}")
                # What asked for this run. Rows written before the column
                # existed keep `unknown` rather than being guessed at: a run
                # from the CLI and one from the dashboard were byte-identical
                # in this table, so there is nothing to back-fill from, and
                # inventing an origin would put fabricated provenance next to
                # measured numbers.
                if "origin" not in columns:
                    conn.execute(
                        "ALTER TABLE test_runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'unknown'")
                preflight_columns = {row[1] for row in conn.execute(
                    "PRAGMA table_info(preflight_reports)")}
                if preflight_columns and "cancel_requested" not in preflight_columns:
                    conn.execute("ALTER TABLE preflight_reports "
                                 "ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0")
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
                # Automatically seed only missing baselines from the earliest
                # successful run that actually has metrics. INSERT OR IGNORE
                # preserves every baseline a user explicitly selected.
                conn.execute(
                    """INSERT OR IGNORE INTO baselines(device_serial, scenario, run_id, created_at)
                       SELECT t.device_serial, COALESCE(t.youtube_scenario, ''), t.id, ?
                       FROM test_runs t
                       WHERE t.status='completed'
                         AND EXISTS (SELECT 1 FROM metric_samples m WHERE m.run_id=t.id)
                         AND t.rowid=(
                           SELECT MIN(t2.rowid) FROM test_runs t2
                           WHERE t2.device_serial=t.device_serial
                             AND COALESCE(t2.youtube_scenario, '')=COALESCE(t.youtube_scenario, '')
                             AND t2.status='completed'
                             AND EXISTS (SELECT 1 FROM metric_samples m2 WHERE m2.run_id=t2.id)
                         )""",
                    (utc_now(),),
                )

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

    def create_run(self, run_id: str, serial: str, youtube_scenario: str | None = None,
                   campaign_id: str | None = None, origin: str = RunOrigin.UNKNOWN,
                   duration: float | None = None,
                   blind_targets: list[str] | None = None) -> None:
        """`origin` is who asked, and it is not derivable after the fact.

        A run started from the CLI and one queued from the dashboard produced
        identical rows, so "show me only the runs a person triggered by hand"
        could not be answered at all -- and neither could "is this trend built
        from scheduled runs or from someone poking at it". `campaign_id` was
        the closest thing available and it only separates one of the four.
        """
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO test_runs(id, device_serial, status, youtube_scenario, "
                    "campaign_id, origin, duration, blind_targets) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (run_id, serial, RunStatus.PENDING, youtube_scenario, campaign_id, origin,
                     duration, json.dumps(blind_targets) if blind_targets else None),
                )

    def update_run(self, run_id: str, status: RunStatus, *, checkpoint: str | None = None, error: str | None = None) -> None:
        now = utc_now()
        with closing(self.connect()) as conn:
            with conn:
                # heartbeat_at is stamped on every update, which is what makes
                # "still alive" observable from outside the process: the
                # runner's periodic checkpoint doubles as the heartbeat, so no
                # separate write and no extra work in the sampling loop.
                conn.execute("UPDATE test_runs SET status=?, started_at=CASE WHEN ?='running' THEN COALESCE(started_at, ?) ELSE started_at END, finished_at=CASE WHEN ? IN ('completed','failed','interrupted') THEN ? ELSE finished_at END, checkpoint=COALESCE(?,checkpoint), heartbeat_at=?, error=? WHERE id=?",
                             (status, status, now, status, now, checkpoint, now, error, run_id))

    # How long a `running` row may go without a heartbeat before it is treated
    # as abandoned. TestRunner writes one every `heartbeat_interval` (1s), so
    # this is orders of magnitude above the normal gap -- large enough that a
    # merely slow run is never stolen, small enough that a restart costs one
    # minute of device time rather than a night of it.
    ABANDONED_RUN_SECONDS = 120

    # A preflight walks up to 11 scenarios and dumps the screen for each, which
    # on a slow device is minutes. This ceiling is well past the worst honest
    # case rather than close to it: releasing one that is merely slow would let
    # a second thing drive the same phone.
    ABANDONED_PREFLIGHT_SECONDS = 900

    def _release_abandoned_preflights(self) -> None:
        """The same reclaim, for the other thing that claims a device.

        A preflight has no heartbeat -- it is one long walk through the
        scenarios -- so it is judged by how long it has been running against a
        deliberately generous ceiling. Without this, a preflight whose worker
        was killed holds its device against every later run *and* every later
        preflight, permanently and silently: the page just says `running`.
        """
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(seconds=self.ABANDONED_PREFLIGHT_SECONDS)).isoformat()
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    """UPDATE preflight_reports
                          SET status=?, finished_at=?,
                              error='abandoned: ran past its ceiling, its worker is gone'
                        WHERE status=? AND started_at IS NOT NULL AND started_at < ?""",
                    (RunStatus.FAILED, utc_now(), RunStatus.RUNNING, cutoff),
                )

    def _release_abandoned_runs(self) -> None:
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(seconds=self.ABANDONED_RUN_SECONDS)).isoformat()
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    """UPDATE test_runs
                          SET status=?, finished_at=?,
                              error='abandoned: no heartbeat, its worker is gone'
                        WHERE status=?
                          AND COALESCE(heartbeat_at, started_at) < ?""",
                    (RunStatus.INTERRUPTED, utc_now(), RunStatus.RUNNING, cutoff),
                )

    def try_start_run(self, run_id: str) -> bool:
        """Atomically claims 'running' for run_id iff no *other* run for the
        same device is already 'running' -- lets multiple Celery workers
        process different devices in parallel while still serializing runs
        against the same device, one SQLite UPDATE at a time. Deliberately
        not scoped to any particular prior status of run_id itself, so
        resuming a previously completed/interrupted run_id (see
        TestRunner.run()'s resume handling above) still works -- only a
        *different* run_id racing the same device is refused. Returns
        whether this call was the one that made the claim.

        A running preflight holds the same device just as exclusively: it
        launches apps, taps and dumps. Leaving it out of this claim would let
        a measured run start while something else was driving the phone --
        which pollutes exactly the numbers the run exists to produce.

        Rows that say `running` but have stopped reporting are cleared first.
        A run writes a heartbeat every second; a worker that is killed (a
        restart, a crash) never gets to write a final status, so its row stays
        `running` forever and this claim then refuses every later run on that
        device -- silently, because the row looks like work in progress. Two
        phones sat idle for five hours behind two such rows while the dashboard
        showed them as running. Nothing else can notice this: the process that
        would have cleaned up is the one that died."""
        self._release_abandoned_runs()
        self._release_abandoned_preflights()
        now = utc_now()
        with closing(self.connect()) as conn:
            with conn:
                cur = conn.execute(
                    """UPDATE test_runs SET status=?, started_at=COALESCE(started_at, ?)
                       WHERE id=? AND NOT EXISTS (
                         SELECT 1 FROM test_runs t2
                         WHERE t2.device_serial = (SELECT device_serial FROM test_runs WHERE id=?)
                           AND t2.status=? AND t2.id != ?)
                       AND NOT EXISTS (
                         SELECT 1 FROM preflight_reports p
                         WHERE p.device_serial = (SELECT device_serial FROM test_runs WHERE id=?)
                           AND p.status=?)""",
                    (RunStatus.RUNNING, now, run_id, run_id, RunStatus.RUNNING, run_id,
                     run_id, RunStatus.RUNNING),
                )
                return cur.rowcount == 1

    # ---- preflight -------------------------------------------------------
    # A preflight is not a run: it produces no samples, no baseline and no
    # comparison, and it must never appear in the run list. It is kept here
    # anyway because it competes for the same device and because "when was
    # this selector table last confirmed against this app build" is a
    # question only a durable record can answer.

    def create_preflight(self, preflight_id: str, serial: str,
                         scenario: str | None = None) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO preflight_reports(id, device_serial, scenario, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (preflight_id, serial, scenario, RunStatus.PENDING, utc_now()),
                )

    def request_preflight_cancel(self, preflight_id: str) -> bool:
        """Asks a running preflight to stop at its next scenario boundary."""
        with closing(self.connect()) as conn:
            with conn:
                cur = conn.execute(
                    "UPDATE preflight_reports SET cancel_requested=1 "
                    "WHERE id=? AND status IN ('pending','running')", (preflight_id,))
                return cur.rowcount == 1

    def preflight_cancel_requested(self, preflight_id: str) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM preflight_reports WHERE id=?",
                (preflight_id,)).fetchone()
            return bool(row and row[0])

    def try_start_preflight(self, preflight_id: str) -> bool:
        """The same claim as `try_start_run`, from the other side."""
        self._release_abandoned_runs()
        self._release_abandoned_preflights()
        now = utc_now()
        with closing(self.connect()) as conn:
            with conn:
                cur = conn.execute(
                    """UPDATE preflight_reports SET status=?, started_at=COALESCE(started_at, ?)
                       WHERE id=? AND status=?
                       AND NOT EXISTS (
                         SELECT 1 FROM preflight_reports p2
                         WHERE p2.device_serial = (SELECT device_serial FROM preflight_reports WHERE id=?)
                           AND p2.status=? AND p2.id != ?)
                       AND NOT EXISTS (
                         SELECT 1 FROM test_runs t
                         WHERE t.device_serial = (SELECT device_serial FROM preflight_reports WHERE id=?)
                           AND t.status=?)""",
                    (RunStatus.RUNNING, now, preflight_id, RunStatus.PENDING,
                     preflight_id, RunStatus.RUNNING, preflight_id,
                     preflight_id, RunStatus.RUNNING),
                )
                return cur.rowcount == 1

    def finish_preflight(self, preflight_id: str, status: RunStatus, *,
                         summary: dict | None = None, error: str | None = None,
                         app_version: dict | None = None) -> None:
        """Store the verdict. `summary` is preflight's own output, unmodified.

        `ok` is lifted out of the summary into its own column so a list of
        past preflights can be read without decoding every report -- and so
        the answer to "was the selector table green on this build" stays a
        query rather than an application-side loop.
        """
        version = app_version or {}
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    """UPDATE preflight_reports
                       SET status=?, finished_at=?, ok=?, summary=?, error=?,
                           app_package=COALESCE(?, app_package),
                           app_version_name=COALESCE(?, app_version_name),
                           app_version_code=COALESCE(?, app_version_code)
                       WHERE id=?""",
                    (status, utc_now(),
                     None if summary is None else int(bool(summary.get("ok"))),
                     None if summary is None else json.dumps(summary),
                     error, version.get("package"), version.get("version_name"),
                     version.get("version_code"), preflight_id),
                )

    def get_preflight(self, preflight_id: str) -> dict | None:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM preflight_reports WHERE id=?",
                               (preflight_id,)).fetchone()
        return None if row is None else _decode_preflight(dict(row))

    def list_preflights(self, limit: int = 20, device_serial: str | None = None,
                        with_summary: bool = False) -> list[dict]:
        """Newest first. The summary is dropped unless asked for -- a full
        report carries every observed element of every miss, which is the one
        thing a list of them does not need."""
        clause, params = "", []
        if device_serial:
            clause, params = "WHERE device_serial=?", [device_serial]
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM preflight_reports {clause} ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        reports = [_decode_preflight(dict(row)) for row in rows]
        if not with_summary:
            for report in reports:
                report.pop("summary", None)
        return reports

    def set_run_app_version(self, run_id: str, version: dict | None) -> None:
        """Record which build of the app under test this run measured."""
        if not version:
            return
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    "UPDATE test_runs SET app_package=?, app_version_name=?, app_version_code=? WHERE id=?",
                    (version.get("package"), version.get("version_name"),
                     version.get("version_code"), run_id),
                )

    def count_run_events(self, run_id: str, kinds: tuple[str, ...]) -> dict[str, int]:
        """How many events of each kind a run recorded.

        Used to answer "is this run's data trustworthy" without loading its
        event log: a run carrying `verification_failed` events measured a
        screen it never successfully reached.
        """
        if not kinds:
            return {}
        placeholders = ",".join("?" for _ in kinds)
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"SELECT kind, COUNT(*) FROM test_events WHERE run_id=? AND kind IN ({placeholders}) "
                "GROUP BY kind",
                (run_id, *kinds),
            ).fetchall()
        counts = {kind: 0 for kind in kinds}
        counts.update({kind: count for kind, count in rows})
        return counts

    def list_run_events(self, run_id: str, kinds: tuple[str, ...] = (),
                        limit: int = 500) -> list[dict]:
        """The run's event log, oldest first, with `details` already decoded.

        `count_run_events` answers "can these numbers be trusted"; this answers
        "what actually happened". Both were needed and only the first existed,
        which meant the evidence a run recorded -- which step fell through to a
        coordinate, how many UI dumps it cost, which assertion failed -- was
        written to the database and never readable again from anywhere but
        SQLite itself.

        Ordered by `id` rather than by `timestamp`: events written inside the
        same batch can share a timestamp to the second, and the insertion order
        is the true one.
        """
        clause, params = "run_id=?", [run_id]
        if kinds:
            clause += f" AND kind IN ({','.join('?' for _ in kinds)})"
            params.extend(kinds)
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM test_events WHERE {clause} ORDER BY id ASC LIMIT ?",
                (*params, limit),
            ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            # Stored as JSON text; a malformed row must not take the whole log
            # down, because the log is most valuable exactly when a run went
            # wrong.
            try:
                event["details"] = json.loads(event["details"] or "{}")
            except json.JSONDecodeError:
                event["details"] = {}
            events.append(event)
        return events

    def run_quality_many(self, run_ids: list[str]) -> dict[str, dict]:
        """`run_quality` for many runs in one query.

        The run list needs a verdict per row, and calling `run_quality` per row
        would be one query per run on a page that shows a hundred. One GROUP BY
        over the whole set costs the same as one of them.
        """
        verdict = {run_id: {"verification_failures": 0, "selector_fallbacks": 0,
                            "stale_selectors": 0, "ui_introspections": 0,
                            "verified": True}
                   for run_id in run_ids}
        if not run_ids:
            return verdict
        field = {"verification_failed": "verification_failures",
                 "selector_fallback": "selector_fallbacks",
                 "selector_stale": "stale_selectors",
                 "ui_introspection": "ui_introspections"}
        placeholders = ",".join("?" for _ in run_ids)
        kinds = tuple(field)
        with closing(self.connect()) as conn:
            rows = conn.execute(
                f"SELECT run_id, kind, COUNT(*) FROM test_events "
                f"WHERE run_id IN ({placeholders}) AND kind IN ({','.join('?' for _ in kinds)}) "
                "GROUP BY run_id, kind",
                (*run_ids, *kinds),
            ).fetchall()
        for run_id, kind, count in rows:
            verdict[run_id][field[kind]] = count
            if kind in ("verification_failed", "selector_stale") and count:
                verdict[run_id]["verified"] = False
        return verdict

    def run_quality(self, run_id: str) -> dict:
        """Whether this run's numbers can be trusted.

        A run whose scenario could not be carried out still produces a full
        set of perfectly real metrics -- of a screen it never reached. Before
        verification existed there was no way to tell such a run from a good
        one, which is what made a decayed selector so dangerous: the data kept
        flowing and kept looking healthy.

        `verified` false means "these numbers measure something other than
        what the scenario describes". Two things can make it false:

        - `verification_failures` -- the scenario could not be carried out.
        - `stale_selectors` -- a target that should have resolved fell through
          to its coordinate, so the tap was blind. `selector_fallbacks` counts
          *every* fallback including the handful that are expected (the
          controls the player draws itself); only the unexpected ones
          invalidate the run. Keeping both counts means "this table is
          decaying" and "this run is untrustworthy" stay separate questions.
        """
        counts = self.count_run_events(
            run_id, ("verification_failed", "selector_fallback",
                     "selector_stale", "ui_introspection"))
        failures = counts.get("verification_failed", 0)
        stale = counts.get("selector_stale", 0)
        return {
            "verification_failures": failures,
            "selector_fallbacks": counts.get("selector_fallback", 0),
            "stale_selectors": stale,
            # How much of this run the tool spent reading the screen. Not a
            # failure and not a warning -- it is the cost of locating elements
            # by identity instead of by coordinate, and it lands on the same
            # CPU the collectors are sampling. A comparison between a run with
            # many lookups and one with none is not comparing like with like,
            # and that was previously impossible to notice.
            "ui_introspections": counts.get("ui_introspection", 0),
            "verified": failures == 0 and stale == 0,
        }

    def request_cancel(self, run_id: str) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("UPDATE test_runs SET cancel_requested=1 WHERE id=?", (run_id,))

    def delete_run(self, run_id: str) -> None:
        """Deletes a run and everything keyed to it, baseline included.

        The baseline row goes too, and that is the whole point. The API used
        to refuse to delete a run that was a baseline, telling the caller to
        "set a different baseline first" -- which is impossible in the case
        that actually occurs: a completed run auto-becomes the baseline for
        its device+scenario when that pair has none (dashboard.tasks), so the
        *first* run of every pair is a baseline, and until a second run of the
        same pair exists there is no different baseline to set. Every such run
        was undeletable, permanently, with no way out from the UI.

        Cascading here rather than leaving the row is what keeps the guard
        unnecessary: a baselines row pointing at a run that no longer exists
        would make `compare` fail against a run it cannot read.
        """
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("DELETE FROM metric_samples WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM test_events WHERE run_id=?", (run_id,))
                conn.execute("DELETE FROM baselines WHERE run_id=?", (run_id,))
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

    def list_runs(self, limit: int = 100, device_serial: str | None = None,
                  origin: str | None = None, include_queued: bool = True) -> list[dict]:
        """Recent runs, newest first.

        `include_queued=False` drops a campaign's not-yet-started children.
        They are rows only because the dashboard pre-creates the whole plan up
        front, which is a dispatch decision -- an overnight campaign is 500 of
        them per device, so the run history becomes a list of things that have
        not happened, and the handful that did scroll off the end of it. The
        campaign page is where a plan belongs; this list is for what ran.
        """
        clauses, params = [], []
        if device_serial:
            clauses.append("device_serial=?")
            params.append(device_serial)
        if origin:
            clauses.append("origin=?")
            params.append(origin)
        if not include_queued:
            clauses.append("NOT (status='pending' AND campaign_id IS NOT NULL)")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM test_runs {where} ORDER BY rowid DESC LIMIT ?", params
            ).fetchall()
            return [dict(row) for row in rows]

    def run_origin_counts(self, device_serial: str | None = None) -> dict[str, int]:
        """How many runs came from each source.

        A facet rather than a filter result, because the useful question is
        comparative: a pass rate computed over runs somebody triggered by hand
        while watching the device is different evidence from one computed over
        unattended campaign runs, and until this column existed the two were
        averaged together with no way to notice.
        """
        sql = "SELECT origin, COUNT(*) FROM test_runs"
        params: list = []
        if device_serial:
            sql += " WHERE device_serial=?"
            params.append(device_serial)
        sql += " GROUP BY origin"
        with closing(self.connect()) as conn:
            return {origin: count for origin, count in conn.execute(sql, params)}

    # How long a dispatched-but-not-started run is left alone before it is
    # considered lost and handed out again. Long enough that a worker starting
    # up, or a queue with a couple of items ahead of it, is never mistaken for
    # a failure; short enough that a purge costs a minute, not a night.
    REDISPATCH_AFTER_SECONDS = 90

    def device_has_inflight_run(self, device_serial: str) -> bool:
        """Is something already on its way to this device?

        Running counts, and so does a run handed to the queue that has not
        started yet -- otherwise a poll a second after dispatch sees "nothing
        running" and hands out the next one too, and the queue fills with runs
        competing for a device that can only take one.
        """
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(seconds=self.REDISPATCH_AFTER_SECONDS)).isoformat()
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM test_runs WHERE device_serial=? AND ("
                "  status='running'"
                "  OR (status='pending' AND cancel_requested=0 AND queued_at >= ?)"
                ") LIMIT 1", (device_serial, cutoff)).fetchone()
            return row is not None

    def mark_run_queued(self, run_id: str) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("UPDATE test_runs SET queued_at=? WHERE id=?", (utc_now(), run_id))

    def next_pending_run(self, device_serial: str) -> dict | None:
        """The oldest run waiting on this device, or None.

        Order is row order, which for a campaign is the order it planned --
        iteration-major across a tier. Handing out exactly one at a time is
        what makes that order the order things actually run in: with the whole
        plan queued at once, any run that found the device busy was pushed to
        the back of the queue by its own retry, and a tier came out shuffled.
        """
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(seconds=self.REDISPATCH_AFTER_SECONDS)).isoformat()
            row = conn.execute(
                "SELECT * FROM test_runs WHERE device_serial=? AND status='pending' "
                "AND cancel_requested=0 AND (queued_at IS NULL OR queued_at < ?) "
                "ORDER BY rowid ASC LIMIT 1",
                (device_serial, cutoff),
            ).fetchone()
            return dict(row) if row else None

    def devices_with_pending_runs(self) -> list[str]:
        """Devices that have work waiting and nothing of their own running.

        The self-healing half: a device in this list is one where dispatch
        stopped -- a worker was killed mid-chain, a broker was purged -- and
        nothing would ever start it again on its own.
        """
        with closing(self.connect()) as conn:
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(seconds=self.REDISPATCH_AFTER_SECONDS)).isoformat()
            return [row[0] for row in conn.execute(
                "SELECT DISTINCT device_serial FROM test_runs WHERE status='pending' "
                "AND cancel_requested=0 AND (queued_at IS NULL OR queued_at < ?) "
                "AND device_serial NOT IN "
                "(SELECT device_serial FROM test_runs WHERE status='running')", (cutoff,))]

    def count_baselines(self, device_serial: str | None = None) -> int:
        """How many baselines exist -- one per (device, scenario) that has run.

        Not the same question as "how many runs had no baseline to compare
        against", which is what the stats page's no_baseline bucket counts.
        Zero of the second is the healthy state; zero of the first means
        nothing has ever completed.
        """
        sql = "SELECT count(*) FROM baselines"
        params: tuple = ()
        if device_serial:
            sql += " WHERE device_serial=?"
            params = (device_serial,)
        with closing(self.connect()) as conn:
            return conn.execute(sql, params).fetchone()[0]

    def count_queued_runs(self) -> int:
        """How many runs are waiting to start, straight from the table.

        Celery's own view of its queue is unreliable here (a `--pool=solo`
        worker cannot answer while it works), so this is the number a queue
        page can state without qualification.
        """
        with closing(self.connect()) as conn:
            return conn.execute(
                "SELECT count(*) FROM test_runs WHERE status='pending'").fetchone()[0]

    def list_running_runs(self, limit: int = 100) -> list[dict]:
        """What is on a device right now -- abandoned rows released first.

        Reclaiming only when another run competes for the device was not
        enough: if nothing else is trying to start, a row whose worker died
        keeps saying `running` indefinitely, and every page repeating that is
        repeating something untrue. Asking "what is running" is exactly the
        moment to check, and it is one UPDATE against an indexed status.
        """
        self._release_abandoned_runs()
        self._release_abandoned_preflights()
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

    # ---- campaigns -------------------------------------------------------
    # A campaign is one long-running test programme that spawns many ordinary
    # runs, rather than a new kind of run. Keeping the child runs as plain
    # rows in test_runs means every existing feature -- baselines, the
    # comparison endpoint, live screen, recordings, deletion -- keeps working
    # on them untouched, and a campaign is just an extra grouping layer on
    # top with its own lifecycle.

    def create_campaign(self, campaign_id: str, kind: str, serial: str, duration: float, *,
                        scenario: str | None = None, tier: str | None = None,
                        iterations: int = 1) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO campaigns(id, kind, device_serial, scenario, tier, duration, iterations, "
                    "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (campaign_id, kind, serial, scenario, tier, duration, iterations,
                     RunStatus.PENDING, utc_now()),
                )

    def update_campaign(self, campaign_id: str, status: RunStatus, *, error: str | None = None) -> None:
        now = utc_now()
        with closing(self.connect()) as conn:
            with conn:
                conn.execute(
                    "UPDATE campaigns SET status=?, "
                    "started_at=CASE WHEN ?='running' THEN COALESCE(started_at, ?) ELSE started_at END, "
                    "finished_at=CASE WHEN ? IN ('completed','failed','interrupted') THEN ? ELSE finished_at END, "
                    "error=COALESCE(?, error) WHERE id=?",
                    (status, status, now, status, now, error, campaign_id),
                )

    def request_campaign_cancel(self, campaign_id: str) -> None:
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("UPDATE campaigns SET cancel_requested=1 WHERE id=?", (campaign_id,))

    def cancel_campaign_runs(self, campaign_id: str) -> int:
        """Stops a campaign: queued children are removed, running ones flagged.

        Cancelling used to only set the flag, which left every queued child
        sitting in the list as `pending` -- for an overnight campaign, several
        hundred rows that would never become anything, because their tasks
        only convert them when they fire and the ones that mattered were hours
        away. The only way to clear them from the UI was to delete the
        campaign, and deleting a campaign takes its *completed* runs with it.
        So "stop this and tidy up" cost you the measurements you had already
        collected -- which is the one thing cancelling must never do.

        A queued child holds nothing: no samples, no events, no device. It is
        deleted outright, and its task stops on arrival when it finds no row
        (TestRunner's require_existing). Anything that ran is left exactly as
        it is.

        This is the whole cancellation mechanism for a campaign, and it is
        deliberately one UPDATE rather than a loop of Celery revokes. A
        repeat campaign can have hundreds of queued child tasks, and
        revoking them individually would hit the same `--pool=solo` control
        plane blind spot documented in services._revoke_in_background -- the
        worker cannot answer a revoke while it is synchronously executing a
        task. TestRunner.run() already checks cancel_requested before doing
        any work and converts such a run to 'interrupted' without touching
        the device, so flagging the rows is both sufficient and immediate.
        Returns how many runs were flagged.
        """
        with closing(self.connect()) as conn:
            with conn:
                queued = [row[0] for row in conn.execute(
                    "SELECT id FROM test_runs WHERE campaign_id=? AND status='pending'",
                    (campaign_id,))]
                cur = conn.execute(
                    "UPDATE test_runs SET cancel_requested=1 "
                    "WHERE campaign_id=? AND status='running'",
                    (campaign_id,),
                )
                stopped = cur.rowcount
        for run_id in queued:
            self.delete_run(run_id)
        return stopped + len(queued)

    def get_campaign(self, campaign_id: str) -> dict | None:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            return dict(row) if row else None

    def list_campaigns(self, limit: int = 50, device_serial: str | None = None) -> list[dict]:
        """Campaigns newest-first, each with a live count of its child runs.

        The counts come from one grouped join rather than a per-campaign
        follow-up query, so listing N campaigns stays a single round trip --
        this endpoint is polled while campaigns are running.
        """
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            clause = "WHERE c.device_serial=?" if device_serial else ""
            params = ([device_serial] if device_serial else []) + [limit]
            rows = conn.execute(
                f"""SELECT c.*,
                           COUNT(r.id) AS run_count,
                           SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) AS completed_count,
                           SUM(CASE WHEN r.status IN ('failed','interrupted') THEN 1 ELSE 0 END) AS failed_count
                    FROM campaigns c LEFT JOIN test_runs r ON r.campaign_id = c.id
                    {clause}
                    GROUP BY c.id ORDER BY c.rowid DESC LIMIT ?""",
                params,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_campaign_runs(self, campaign_id: str) -> list[dict]:
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM test_runs WHERE campaign_id=? ORDER BY rowid ASC", (campaign_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def delete_campaign(self, campaign_id: str) -> list[str]:
        """Deletes a campaign and every run it spawned. Returns the deleted run
        ids so the caller can clean up their recordings, which live on disk
        outside this database (mirroring how delete_run is used)."""
        run_ids = [row["id"] for row in self.list_campaign_runs(campaign_id)]
        for run_id in run_ids:
            self.delete_run(run_id)
        with closing(self.connect()) as conn:
            with conn:
                conn.execute("DELETE FROM campaigns WHERE id=?", (campaign_id,))
        return run_ids

    def aggregate_samples(self, run_id: str) -> list[dict]:
        """Per-metric count/mean/stdev/min/max computed entirely inside SQLite.

        Replaces the `compute_stats(list_samples(limit=100_000))` pattern,
        which had two problems that only show up on long runs. It loads every
        row into Python just to reduce them to five numbers, and -- worse --
        the limit silently truncates: at the default collector cadence
        (~3.4 samples/second) 100k rows is only ~8.2 hours, and because
        `list_samples` orders by `id ASC` the rows it drops are the *tail* of
        the run, exactly where a memory leak or thermal throttle would show.
        A soak run long enough to exhibit a regression would have reported
        stats computed from only its healthy opening stretch.

        Variance is computed two-pass (a join against each metric's own mean)
        rather than via the one-pass E[x^2]-E[x]^2 identity. One pass would be
        marginally cheaper, but it subtracts two large nearly-equal numbers --
        for `memory.used`, mean^2 is ~1e13 while the variance may be ~1e6, so
        catastrophic cancellation eats most of the significant digits. SQLite
        still streams both passes, so peak memory stays O(distinct metrics)
        either way. `pstdev` (population, matching analyzer.compute_stats)
        is used so both paths agree.
        """
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT m.name AS name, COUNT(*) AS count, AVG(m.value) AS mean,
                          MIN(m.value) AS minimum, MAX(m.value) AS maximum,
                          AVG((m.value - a.mean) * (m.value - a.mean)) AS variance
                   FROM metric_samples m
                   JOIN (SELECT name, AVG(value) AS mean FROM metric_samples
                         WHERE run_id=? GROUP BY name) a ON a.name = m.name
                   WHERE m.run_id=?
                   GROUP BY m.name""",
                (run_id, run_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def downsample_samples(self, run_id: str, buckets: int = 300) -> list[dict]:
        """Reduce each metric's series to at most `buckets` points for charting.

        A multi-hour run holds ~100k samples; handing those to an SVG line
        chart is what actually breaks the Run Detail page, so the reduction
        has to happen before the rows leave SQLite.

        Buckets are cut by *sample rank* (ROW_NUMBER over the metric's own
        rows) rather than by wall-clock time. Each collector samples on a
        fixed interval, so rank is already proportional to time within a
        metric, and ranking sidesteps parsing the ISO-8601 timestamps -- which
        carry both a `+00:00` offset and microseconds, neither of which
        SQLite's date functions handle as uniformly as `datetime.fromisoformat`
        does on the Python side. Partitioning per metric also means cpu (1s)
        and battery (10s) each get the full bucket budget instead of battery
        being crowded out by cpu's 10x sample count.

        `minimum`/`maximum` are carried alongside `mean` so a spike inside a
        bucket stays visible instead of being averaged away -- the whole point
        of watching a soak run is catching transients.
        """
        buckets = max(1, int(buckets))
        with closing(self.connect()) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """SELECT name, bucket, COUNT(*) AS count, AVG(value) AS mean,
                          MIN(value) AS minimum, MAX(value) AS maximum,
                          MIN(timestamp) AS timestamp, MIN(unit) AS unit
                   FROM (SELECT name, value, timestamp, unit,
                                (ROW_NUMBER() OVER (PARTITION BY name ORDER BY id) - 1) * ?
                                / COUNT(*) OVER (PARTITION BY name) AS bucket
                         FROM metric_samples WHERE run_id=?)
                   GROUP BY name, bucket
                   ORDER BY name, bucket""",
                (buckets, run_id),
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
