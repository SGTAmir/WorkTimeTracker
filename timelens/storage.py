"""SQLite storage for TimeLens.

Schema
------
sessions        one row per start/stop cycle of the tracker
                  id, started_at, stopped_at(NULL => running), note
idle_periods    time inside a session that was NOT counted as work
                  id, session_id, kind('idle'|'paused'), start_ts, end_ts
samples         raw activity samples written by the daemon
                  ts, session_id, active(bool), window, app
app_usage       aggregated per-application seconds
                  id, session_id, app, day, start_ts, end_ts, seconds
meta            key/value store (last sample ts, schema version ...)

All timestamps are unix epoch seconds (UTC).  A *day* is stored as
``YYYY-MM-DD`` in the machine's **local** timezone so that "today" and
"this week" match what the user sees on their clock.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    stopped_at  INTEGER,
    note        TEXT
);
CREATE TABLE IF NOT EXISTS idle_periods (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('idle','paused')),
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idle_session ON idle_periods(session_id);
CREATE INDEX IF NOT EXISTS idx_idle_range   ON idle_periods(start_ts, end_ts);
CREATE TABLE IF NOT EXISTS samples (
    ts          INTEGER NOT NULL,
    session_id  INTEGER,
    active      INTEGER NOT NULL,
    window      TEXT,
    app         TEXT
);
CREATE INDEX IF NOT EXISTS idx_samples_ts ON samples(ts);
CREATE TABLE IF NOT EXISTS app_usage (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER,
    app         TEXT NOT NULL,
    day         TEXT NOT NULL,
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL,
    seconds     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_app_day  ON app_usage(day);
CREATE INDEX IF NOT EXISTS idx_app_name ON app_usage(app);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
PRAGMA journal_mode=WAL;
"""


def local_day(ts: float) -> str:
    """Return the local calendar day (YYYY-MM-DD) of an epoch timestamp."""
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def day_bounds(day: str) -> Tuple[int, int]:
    """Local [start, end) epoch bounds of a YYYY-MM-DD string."""
    d = dt.date.fromisoformat(day)
    start = int(dt.datetime.combine(d, dt.time.min).timestamp())
    return start, start + 86400


class Storage:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)
        self.db.execute(
            "INSERT OR REPLACE INTO meta(key,value) VALUES('schema',?)",
            (str(SCHEMA_VERSION),))
        self.db.commit()

    # ------------------------------------------------------------------ util
    def close(self) -> None:
        self.db.close()

    def get_meta(self, key: str, default: Optional[str] = None) -> Optional[str]:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
                        (key, value))
        self.db.commit()

    # -------------------------------------------------------------- sessions
    def open_session(self, ts: int, note: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO sessions(started_at, note) VALUES(?,?)", (ts, note))
        self.db.commit()
        return cur.lastrowid

    def current_session(self) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM sessions WHERE stopped_at IS NULL "
            "ORDER BY id DESC LIMIT 1").fetchone()

    def close_session(self, ts: int) -> Optional[int]:
        row = self.current_session()
        if not row:
            return None
        self.db.execute("UPDATE sessions SET stopped_at=? WHERE id=?",
                        (ts, row["id"]))
        self.db.commit()
        return row["id"]

    def delete_session(self, session_id: int) -> None:
        self.db.execute("DELETE FROM idle_periods WHERE session_id=?", (session_id,))
        self.db.execute("DELETE FROM app_usage  WHERE session_id=?", (session_id,))
        self.db.execute("DELETE FROM samples     WHERE session_id=?", (session_id,))
        self.db.execute("DELETE FROM sessions    WHERE id=?", (session_id,))
        self.db.commit()

    # ----------------------------------------------------------- idle/pauses
    def add_idle(self, session_id: int, kind: str, start_ts: int, end_ts: int) -> None:
        if end_ts <= start_ts:
            return
        self.db.execute(
            "INSERT INTO idle_periods(session_id,kind,start_ts,end_ts) "
            "VALUES(?,?,?,?)", (session_id, kind, int(start_ts), int(end_ts)))
        self.db.commit()

    def mergeable_idle(self, session_id: int, kind: str, start_ts: int,
                       max_gap: int) -> Optional[sqlite3.Row]:
        """Find an open-ended-ish idle period ending within max_gap of start_ts."""
        return self.db.execute(
            "SELECT * FROM idle_periods WHERE session_id=? AND kind=? "
            "AND ? - end_ts <= ? ORDER BY end_ts DESC LIMIT 1",
            (session_id, kind, int(start_ts), int(max_gap))).fetchone()

    def extend_idle(self, idle_id: int, end_ts: int) -> None:
        self.db.execute("UPDATE idle_periods SET end_ts=? WHERE id=?",
                        (int(end_ts), idle_id))
        self.db.commit()

    # --------------------------------------------------------------- samples
    def add_sample(self, ts: int, session_id: Optional[int], active: bool,
                   window: str, app: str) -> None:
        self.db.execute(
            "INSERT INTO samples(ts,session_id,active,window,app) VALUES(?,?,?,?,?)",
            (int(ts), session_id, 1 if active else 0, window or "", app or ""))
        self.db.commit()

    def recent_sample(self, within: int, now: int) -> Optional[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM samples WHERE ts>=? ORDER BY ts DESC LIMIT 1",
            (now - within,)).fetchone()

    def prune_samples(self, keep_days: int = 90) -> int:
        cutoff = int(dt.datetime.now().timestamp()) - keep_days * 86400
        cur = self.db.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
        self.db.commit()
        return cur.rowcount

    # ------------------------------------------------------------- app usage
    def add_app_usage(self, session_id: Optional[int], app: str, start_ts: int,
                      end_ts: int) -> None:
        """Record that `app` was the focused application between two stamps.

        Spans crossing midnight are split so daily stats stay correct.
        """
        seconds = int(end_ts - start_ts)
        if not app or seconds <= 0:
            return
        ts = int(start_ts)
        while ts < int(end_ts):
            day = local_day(ts)
            _, d_end = day_bounds(day)
            seg_end = min(int(end_ts), d_end)
            self.db.execute(
                "INSERT INTO app_usage(session_id,app,day,start_ts,end_ts,seconds) "
                "VALUES(?,?,?,?,?,?)",
                (session_id, app, day, ts, seg_end, seg_end - ts))
            ts = seg_end
        self.db.commit()

    def app_rows(self, start: int, end: int) -> List[sqlite3.Row]:
        """Per-app totals overlapping [start, end), clipped to the range."""
        rows = self.db.execute(
            """SELECT app,
                      SUM(MIN(end_ts, ?) - MAX(start_ts, ?)) AS secs
               FROM app_usage
               WHERE start_ts < ? AND end_ts > ? AND app IS NOT NULL AND app != ''
               GROUP BY app HAVING secs > 0 ORDER BY secs DESC""",
            (end, start, end, start)).fetchall()
        return rows

    def app_rows_for_day(self, day: str) -> List[sqlite3.Row]:
        s, e = day_bounds(day)
        return self.app_rows(s, e)

    # ---------------------------------------------------------------- ranges
    def sessions_in(self, start: int, end: int) -> List[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM sessions WHERE started_at < ? "
            "AND (stopped_at IS NULL OR stopped_at > ?) ORDER BY started_at",
            (end, start)).fetchall()

    def idles_in(self, start: int, end: int) -> List[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM idle_periods WHERE start_ts < ? AND end_ts > ?",
            (end, start)).fetchall()

    def all_sessions(self) -> List[sqlite3.Row]:
        return self.db.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()

    def first_ts(self) -> Optional[int]:
        row = self.db.execute("SELECT MIN(started_at) m FROM sessions").fetchone()
        return row["m"] if row and row["m"] is not None else None
