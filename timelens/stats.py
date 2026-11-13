"""Statistics engine for TimeLens.

Core idea:  work_seconds(range) = wall-clock time during which a tracker
session was open, MINUS every 'idle' and 'paused' period that overlaps the
range (clipped to it).

All functions return plain dicts so the CLI / JSON export stay simple.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from typing import Dict, List, Optional

from .storage import Storage, day_bounds, local_day


# ------------------------------------------------------------------ helpers
def _clip(start: int, end: int, s: int, e: int) -> int:
    """Length of overlap between [start,end) and [s,e)."""
    lo, hi = max(start, s), min(end, e)
    return max(0, hi - lo)


def range_work_seconds(db: Storage, start: int, end: int) -> int:
    """Worked seconds within [start, end)."""
    if end <= start:
        return 0
    total = 0
    now = int(dt.datetime.now().timestamp())
    for s in db.sessions_in(start, end):
        sess_start = max(start, s["started_at"])
        sess_end = min(end, s["stopped_at"] if s["stopped_at"] is not None else now)
        if sess_end > sess_start:
            total += sess_end - sess_start
    for i in db.idles_in(start, end):
        total -= _clip(start, end, i["start_ts"], i["end_ts"])
    return max(0, total)


def range_idle_seconds(db: Storage, start: int, end: int,
                       kind: Optional[str] = None) -> int:
    total = 0
    for i in db.idles_in(start, end):
        if kind and i["kind"] != kind:
            continue
        total += _clip(start, end, i["start_ts"], i["end_ts"])
    return total


def day_work_map(db: Storage, from_day: str, to_day: str) -> "OrderedDict[str, int]":
    """Worked seconds per calendar day (inclusive range of YYYY-MM-DD)."""
    out: OrderedDict[str, int] = OrderedDict()
    d0 = dt.date.fromisoformat(from_day)
    d1 = dt.date.fromisoformat(to_day)
    day = d0
    while day <= d1:
        key = day.isoformat()
        s, e = day_bounds(key)
        out[key] = range_work_seconds(db, s, e)
        day += dt.timedelta(days=1)
    return out


# ------------------------------------------------------------------ periods
def week_bounds(ref: Optional[dt.date] = None) -> tuple[dt.date, dt.date]:
    """ISO week (Mon..Sun) containing ref."""
    ref = ref or dt.date.today()
    monday = ref - dt.timedelta(days=ref.weekday())
    return monday, monday + dt.timedelta(days=6)


def month_bounds(ref: Optional[dt.date] = None) -> tuple[dt.date, dt.date]:
    ref = ref or dt.date.today()
    first = ref.replace(day=1)
    last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
    return first, last


def _period_stats(db: Storage, label: str, d_from: dt.date, d_to: dt.date) -> Dict:
    start = day_bounds(d_from.isoformat())[0]
    # don't count future days of this week/month: clamp end to "now"
    now = int(dt.datetime.now().timestamp())
    end = min(day_bounds(d_to.isoformat())[1], now + 1)
    worked = range_work_seconds(db, start, end)
    idle = range_idle_seconds(db, start, end, "idle")
    paused = range_idle_seconds(db, start, end, "paused")
    days_with_work = {}
    d = d_from
    daily: "OrderedDict[str, int]" = OrderedDict()
    while d <= d_to and day_bounds(d.isoformat())[0] <= end:
        s, e = day_bounds(d.isoformat())
        w = range_work_seconds(db, s, min(e, end))
        daily[d.isoformat()] = w
        if w > 0:
            days_with_work[d.isoformat()] = w
        d += dt.timedelta(days=1)
    n_work_days = len(days_with_work)
    cal_days = max(1, (min(d_to, dt.date.today()) - d_from).days + 1)
    best_day = max(daily.items(), key=lambda kv: kv[1]) if daily else ("", 0)
    apps = app_breakdown(db, start, end)
    return {
        "label": label,
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "worked_seconds": worked,
        "idle_seconds": idle,
        "paused_seconds": paused,
        "at_keyboard_seconds": worked + idle,   # present but not working
        "work_days": n_work_days,
        "calendar_days": cal_days,
        "avg_per_work_day": round(worked / n_work_days) if n_work_days else 0,
        "avg_per_calendar_day": round(worked / cal_days),
        "best_day": {"date": best_day[0], "seconds": best_day[1]},
        "daily": daily,
        "apps": apps,
    }


# --------------------------------------------------------------------- apps
def app_breakdown(db: Storage, start: int, end: int) -> List[Dict]:
    rows = db.app_rows(start, end)
    out = []
    total = sum(r["secs"] for r in rows) or 1
    for r in rows:
        out.append({"app": r["app"], "seconds": r["secs"],
                    "percent": round(100.0 * r["secs"] / total, 1)})
    return out


# ----------------------------------------------------------------- summaries
def today_stats(db: Storage) -> Dict:
    t = dt.date.today()
    return _period_stats(db, "today", t, t)


def week_stats(db: Storage) -> Dict:
    a, b = week_bounds()
    return _period_stats(db, "this-week", a, b)


def month_stats(db: Storage) -> Dict:
    a, b = month_bounds()
    return _period_stats(db, "this-month", a, b)


def history_stats(db: Storage, days: int = 7) -> Dict:
    """Rolling window of the last N calendar days including today."""
    today = dt.date.today()
    start_d = today - dt.timedelta(days=days - 1)
    start = day_bounds(start_d.isoformat())[0]
    end = day_bounds(today.isoformat())[1]
    worked = range_work_seconds(db, start, end)
    daily = day_work_map(db, start_d.isoformat(), today.isoformat())
    wd_days = {k: v for k, v in daily.items() if v > 0}
    best = max(wd_days.items(), key=lambda kv: kv[1]) if wd_days else ("", 0)
    return {
        "window_days": days,
        "from": start_d.isoformat(),
        "to": today.isoformat(),
        "worked_seconds": worked,
        "work_days": len(wd_days),
        "avg_per_work_day": round(worked / len(wd_days)) if wd_days else 0,
        "avg_per_day": round(worked / days),
        "best_day": {"date": best[0], "seconds": best[1]},
        "daily": daily,
        "apps": app_breakdown(db, start, end),
    }


def all_time(db: Storage) -> Dict:
    first = db.first_ts()
    now = int(dt.datetime.now().timestamp())
    if not first:
        return {"worked_seconds": 0, "sessions": 0}
    sessions = db.all_sessions()
    total_wall = 0
    for s in sessions:
        total_wall += (s["stopped_at"] or now) - s["started_at"]
    idle = sum(_clip(first, now, i["start_ts"], i["end_ts"])
               for i in db.idles_in(first, now))
    return {
        "since": local_day(first),
        "worked_seconds": max(0, total_wall - idle),
        "sessions": len(sessions),
        "apps": app_breakdown(db, first, now),
    }


def current_state(db: Storage) -> Dict:
    """Live status used by `timelens status`."""
    now = int(dt.datetime.now().timestamp())
    sess = db.current_session()
    st = {"running": False, "session_started": None,
          "session_worked": 0, "today_worked": today_stats(db)["worked_seconds"]}
    if sess:
        st["running"] = True
        st["session_started"] = sess["started_at"]
        st["session_worked"] = range_work_seconds(db, sess["started_at"], now)
        last_idle = db.db.execute(
            "SELECT * FROM idle_periods WHERE session_id=? ORDER BY end_ts DESC "
            "LIMIT 1", (sess["id"],)).fetchone()
        if last_idle:
            st["last_event"] = {"kind": last_idle["kind"], "ts": last_idle["end_ts"]}
    return st
