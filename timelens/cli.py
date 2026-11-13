"""TimeLens command line interface.

    timelens start|stop|pause|resume|status        -- control the tracker
    timelens today|week|month|history|apps|report  -- statistics
    timelens add ...                                -- log time manually
    timelens config | autostart                     -- setup helpers
    timelens export | sessions | edit | forget      -- data management
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import time
from typing import List, Optional

from . import daemon as dm
from . import stats
from .config import (DEFAULTS, config_path, data_dir, db_path, ensure_dirs,
                     load_config, save_config)
from .storage import Storage, day_bounds, local_day
from .__init__ import __version__

# ------------------------------------------------------------------ formatting
def fmt_hms(seconds: float) -> str:
    seconds = int(round(seconds))
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{sign}{h:d}:{m:02d}:{s:02d}"


def fmt_hm(seconds: float) -> str:
    """Compact form used in tables: 1h23m."""
    seconds = int(round(seconds))
    h, rem = divmod(abs(seconds), 3600)
    m = rem // 60
    if h and m:
        return f"{h}h{m:02d}m"
    if h:
        return f"{h}h"
    if m:
        return f"{m}m"
    return f"{seconds}s"


WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def bar(seconds: int, total: int, width: int = 24) -> str:
    if total <= 0 or seconds <= 0:
        return ""
    n = max(1, round(width * seconds / total)) if seconds else 0
    return "#" * min(n, width)


def open_db() -> Storage:
    ensure_dirs()
    return Storage(str(db_path()))


def die(msg: str, code: int = 2):
    print(f"timelens: {msg}", file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------- control
def cmd_start(args) -> int:
    db = open_db()
    if dm.daemon_running():
        ctl = dm.read_control()
        if ctl == "paused":
            dm.write_control("running")
            print("Tracker resumed (daemon was already running).")
        else:
            print("Tracker is already running.")
        return 0
    # daemon not running: start it; adopt/reopen a session happens inside
    if dm.spawn_daemon():
        dm.write_control("running")
        print("Tracking started. Use 'timelens pause' when you take a break.")
    else:
        # headless fallback without spawn?  still report
        die("could not start the background daemon; see log: %s" % dm.logfile(), 1)
    return 0


def cmd_stop(args) -> int:
    db = open_db()
    pid = dm.read_pid()
    was_paused = (dm.read_control() == "paused")
    now = int(time.time())
    stopped = False
    if pid:
        try:
            os.kill(pid, 15)          # SIGTERM -> daemon closes session cleanly
            for _ in range(60):
                time.sleep(0.1)
                if not dm.daemon_running():
                    stopped = True
                    break
        except OSError:
            pass
    if not stopped:
        # safety net: close an orphaned open session directly
        sess = db.current_session()
        if sess:
            sid = sess["id"]
            row = db.db.execute(
                "SELECT MAX(GREATEST(end_ts, start_ts)) m FROM idle_periods "
                "WHERE session_id=?", (sid,)).fetchone()
            anchors = [x for x in (row["m"] if row and row["m"] else None,
                                   sess["started_at"]) if x is not None]
            # from the last known non-work event until now you were away
            if was_paused or not anchors or now - max(anchors) >= 0:
                anchor = max(anchors) if anchors else sess["started_at"]
                if now > anchor:
                    db.add_idle(sid, "paused", anchor, now)
            db.close_session(now)
    dm.write_control("paused")   # remember user intent for next autostart
    st = stats.today_stats(db)
    print("Tracking stopped. Today's counted work: %s" % fmt_hm(st["worked_seconds"]))
    return 0


def cmd_pause(args) -> int:
    if not dm.daemon_running():
        die("tracker is not running (use: timelens start)")
    dm.write_control("paused")
    st = stats.today_stats(open_db())
    print("Timer paused — time will NOT be counted until 'timelens resume'.")
    print("Work counted so far today: %s" % fmt_hm(st["worked_seconds"]))
    return 0


def cmd_resume(args) -> int:
    db = open_db()
    if dm.daemon_running():
        dm.write_control("running")
        print("Timer resumed.")
    else:
        if dm.spawn_daemon():
            dm.write_control("running")
            print("Daemon was not running — started and timer resumed.")
        else:
            die("could not start the background daemon")
    return 0


def cmd_status(args) -> int:
    db = open_db()
    running = dm.daemon_running()
    ctl = dm.read_control()
    st = stats.current_state(db)
    today = stats.today_stats(db)
    week = stats.week_stats(db)
    month = stats.month_stats(db)
    state = "RUNNING" if (running and ctl != "paused") else \
            ("PAUSED" if (running and ctl == "paused") else
             ("STOPPED (session left open)" if db.current_session() else "STOPPED"))
    print(f"Status: {state}")
    if running:
        print(f"Daemon PID: {dm.read_pid()}   log: {dm.logfile()}")
    if st["session_started"]:
        print(f"Current session since: "
              f"{dt.datetime.fromtimestamp(st['session_started']):%Y-%m-%d %H:%M}")
        print(f"Session work time:     {fmt_hms(st['session_worked'])}")
    print(f"Today:  {fmt_hms(today['worked_seconds'])}  "
          f"(idle {fmt_hm(today['idle_seconds'])}, paused {fmt_hm(today['paused_seconds'])})")
    print(f"Week:   {fmt_hms(week['worked_seconds'])} over {week['work_days']} day(s)")
    print(f"Month:  {fmt_hms(month['worked_seconds'])} over {month['work_days']} day(s)")
    last = db.get_meta("last_sample")
    if last:
        age = int(time.time()) - int(last)
        note = "OK" if age < 3 * max(2, int(load_config()["sample_interval"])) \
             else "stale (daemon may have died)"
        print(f"Last sample: {age}s ago [{note}]")
    if not running and state.startswith("STOPPED"):
        print("Hint: run `timelens start` to begin tracking.")
    return 0


# -------------------------------------------------------------------- reports
def _print_apps(apps: List[dict], title: str, limit: int = 12):
    if not apps:
        print(f"\n{title}: no application data recorded.")
        return
    print(f"\n{title} (top {min(limit, len(apps))}):")
    total = max(a["seconds"] for a in apps)
    for a in apps[:limit]:
        print(f"  {a['app'][:28]:<28} {fmt_hm(a['seconds']):>8}  "
              f"{a['percent']:>5.1f}%  {bar(a['seconds'], total, 18)}")


def _print_daily(daily: dict, title: str):
    print(f"\n{title}:")
    mx = max(daily.values()) if daily else 0
    for day, secs in daily.items():
        d = dt.date.fromisoformat(day)
        wd = WEEKDAYS[d.weekday()]
        print(f"  {day} {wd}  {fmt_hms(secs):>10}  {bar(secs, mx, 30)}")


def _summary_lines(p: dict) -> List[str]:
    out = []
    out.append(f"Total work:      {fmt_hms(p['worked_seconds'])}")
    out.append(f"Idle (away):     {fmt_hms(p['idle_seconds'])}")
    out.append(f"Paused by user:  {fmt_hms(p['paused_seconds'])}")
    out.append(f"Days with work:  {p['work_days']} of {p['calendar_days']}")
    out.append(f"Avg per work day:{fmt_hms(p['avg_per_work_day']):>10}")
    out.append(f"Avg per day:     {fmt_hms(p['avg_per_calendar_day']):>10}")
    if p["best_day"]["seconds"]:
        out.append(f"Best day:        {p['best_day']['date']} "
                   f"({fmt_hms(p['best_day']['seconds'])})")
    return out


def cmd_today(args) -> int:
    db = open_db()
    t = stats.today_stats(db)
    print(f"=== Today ({t['from']}) ===")
    for ln in _summary_lines(t):
        print(ln)
    sess = db.current_session()
    if sess:
        now = int(time.time())
        live = stats.range_work_seconds(db, sess["started_at"], now)
        print(f"(live session: {fmt_hms(live)} so far)")
    _print_apps(t["apps"], "Apps today")
    return 0


def cmd_week(args) -> int:
    db = open_db()
    w = stats.week_stats(db)
    print(f"=== This week ({w['from']} .. {w['to']}) ===")
    for ln in _summary_lines(w):
        print(ln)
    _print_daily(w["daily"], "Daily breakdown")
    _print_apps(w["apps"], "Apps this week")
    return 0


def cmd_month(args) -> int:
    db = open_db()
    m = stats.month_stats(db)
    print(f"=== This month ({m['from']} .. {m['to']}) ===")
    for ln in _summary_lines(m):
        print(ln)
    _print_daily(m["daily"], "Daily breakdown")
    _print_apps(m["apps"], "Apps this month")
    return 0


def cmd_history(args) -> int:
    db = open_db()
    h = stats.history_stats(db, days=args.days)
    print(f"=== Last {args.days} days ({h['from']} .. {h['to']}) ===")
    print(f"Total work:       {fmt_hms(h['worked_seconds'])}")
    print(f"Days with work:   {h['work_days']}")
    print(f"Avg per work day: {fmt_hms(h['avg_per_work_day'])}")
    print(f"Avg per calendar: {fmt_hms(h['avg_per_day'])}")
    if h["best_day"]["seconds"]:
        print(f"Best day:         {h['best_day']['date']} "
              f"({fmt_hms(h['best_day']['seconds'])})")
    _print_daily(h["daily"], "Daily breakdown")
    _print_apps(h["apps"], f"Apps in last {args.days} days")
    return 0


def cmd_apps(args) -> int:
    db = open_db()
    end = int(time.time())
    start = end - args.days * 86400
    rows = stats.app_breakdown(db, start, end)
    print(f"=== Application usage, last {args.days} day(s) ===")
    if not rows:
        print("No data yet.")
        return 0
    total = sum(r["seconds"] for r in rows)
    mx = rows[0]["seconds"]
    for r in rows[: args.limit]:
        print(f"  {r['app'][:30]:<30} {fmt_hm(r['seconds']):>8}  "
              f"{r['percent']:>5.1f}%  {bar(r['seconds'], mx, 20)}")
    print(f"  {'-'*30} {'-'*8}")
    print(f"  {'TOTAL':<30} {fmt_hm(total):>8}")
    return 0


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def cmd_report(args) -> int:
    db = open_db()
    if args.day:
        if not DATE_RE.match(args.day):
            die("--day must be YYYY-MM-DD")
        s, e = day_bounds(args.day)
        label = f"Day report {args.day}"
    elif args.week:
        monday = dt.date.fromisoformat(args.week)
        sunday = monday + dt.timedelta(days=6)
        s = day_bounds(monday.isoformat())[0]
        e = day_bounds(sunday.isoformat())[1]
        label = f"Week report {monday} .. {sunday}"
    elif args.month:
        first = dt.date.fromisoformat(args.month + "-01")
        last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        s = day_bounds(first.isoformat())[0]
        e = day_bounds(last.isoformat())[1]
        label = f"Month report {args.month}"
    else:
        die("report needs --day YYYY-MM-DD | --week YYYY-MM-DD | --month YYYY-MM")

    worked = stats.range_work_seconds(db, s, e)
    idle = stats.range_idle_seconds(db, s, e, "idle")
    paused = stats.range_idle_seconds(db, s, e, "paused")
    print(f"=== {label} ===")
    print(f"Work:   {fmt_hms(worked)}")
    print(f"Idle:   {fmt_hms(idle)}")
    print(f"Paused: {fmt_hms(paused)}")
    if not args.day:
        d0 = dt.date.fromisoformat(day_bounds_local(s))
        daily = stats.day_work_map(db, d0, day_bounds_local(e - 1))
        _print_daily(daily, "Daily breakdown")
        nz = [v for v in daily.values() if v > 0]
        if nz:
            print(f"\nWork days: {len(nz)}   avg/work-day: {fmt_hms(sum(nz)/len(nz))}")
            bd = max(daily.items(), key=lambda kv: kv[1])
            print(f"Max day:   {bd[0]}  {fmt_hms(bd[1])}")
    _print_apps(stats.app_breakdown(db, s, e), "Applications")
    return 0


def day_bounds_local(ts: int) -> str:
    return local_day(ts)


# --------------------------------------------------------------------- manual
def parse_len(text: str) -> int:
    text = text.strip().lower()
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)\s*h)?\s*(?:(\d+(?:\.\d+)?)\s*m)?", text)
    if not m or (m.group(1) is None and m.group(2) is None):
        m2 = re.fullmatch(r"(\d+):(\d{1,2})(?::(\d{1,2}))?", text)
        if m2:
            h, mi, se = int(m2.group(1)), int(m2.group(2)), int(m2.group(3) or 0)
            return h * 3600 + mi * 60 + se
        raise ValueError(f"bad duration: {text!r} (use e.g. '2h30m' or '90m')")
    h = float(m.group(1) or 0)
    mi = float(m.group(2) or 0)
    return int(h * 3600 + mi * 60)


def cmd_add(args) -> int:
    db = open_db()
    day = args.day or local_day(time.time())
    if not DATE_RE.match(day):
        die("--day must be YYYY-MM-DD")
    length = parse_len(args.length)
    if length <= 0:
        die("length must be positive")
    start_str = args.start or "09:00"
    if re.fullmatch(r"\d{1,2}:\d{2}", start_str):
        hh, mm = map(int, start_str.split(":"))
        base = dt.datetime.fromisoformat(day)
        ts = int(dt.datetime(base.year, base.month, base.day, hh, mm).timestamp())
    else:
        try:
            ts = int(dt.datetime.fromisoformat(start_str).timestamp())
        except ValueError:
            die("--start must be HH:MM or YYYY-MM-DDTHH:MM")
    if ts >= int(time.time()):
        die("cannot log time in the future")
    if ts + length > int(time.time()):
        die("entry would extend into the future")
    sid = db.open_session(ts, note="manual")
    db.close_session(ts + length)
    msg = f"Logged {fmt_hm(length)} on {local_day(ts)}"
    if args.app:
        db.add_app_usage(sid, args.app, ts, ts + length)
        msg += f" (app: {args.app})"
    if args.note:
        msg += f" — {args.note}"
    print(msg)
    return 0


# ----------------------------------------------------------------- data mgmt
def cmd_sessions(args) -> int:
    db = open_db()
    rows = db.all_sessions()
    if not rows:
        print("No sessions recorded yet.")
        return 0
    now = int(time.time())
    print(f"{'id':>4}  {'start':<17} {'end':<17} {'wall':>8} {'work':>9}  note")
    for s in rows[-args.limit:]:
        end = s["stopped_at"]
        wall = (end or now) - s["started_at"]
        work = stats.range_work_seconds(db, s["started_at"], end or now)
        print(f"{s['id']:>4}  "
              f"{dt.datetime.fromtimestamp(s['started_at']):%Y-%m-%d %H:%M}  "
              f"{(dt.datetime.fromtimestamp(end).strftime('%Y-%m-%d %H:%M') if end else '(open)'):<17} "
              f"{fmt_hm(wall):>8} {fmt_hm(work):>9}  {s['note'] or ''}")
    return 0


def cmd_edit(args) -> int:
    db = open_db()
    sess = db.db.execute("SELECT * FROM sessions WHERE id=?", (args.id,)).fetchone()
    if not sess:
        die(f"no session with id {args.id}")
    changed = False
    if args.stop_ago is not None:
        new_end = int(time.time()) - args.stop_ago
        if new_end <= sess["started_at"]:
            die("--stop-ago too large; end must be after session start")
        db.db.execute("UPDATE sessions SET stopped_at=? WHERE id=?",
                      (new_end, args.id))
        changed = True
        print(f"session {args.id}: stop time set to "
              f"{dt.datetime.fromtimestamp(new_end):%Y-%m-%d %H:%M}")
    if args.drop_pauses:
        kind = "paused" if args.drop_pauses == "paused" else None
        q = "DELETE FROM idle_periods WHERE session_id=?"
        p: list = [args.id]
        if kind:
            q += " AND kind=?"
            p.append(kind)
        cur = db.db.execute(q, p)
        changed = True
        print(f"session {args.id}: removed {cur.rowcount} non-work period(s)")
    if changed:
        db.db.commit()
    else:
        print("nothing to change (pass --stop-ago SECONDS or --drop-pauses)")
    return 0


def cmd_forget(args) -> int:
    db = open_db()
    if args.session:
        db.delete_session(args.session)
        print(f"Deleted session {args.session}.")
        return 0
    if args.confirm != "yes":
        die("this erases ALL data. Re-run with --confirm yes")
    for t in ("sessions", "idle_periods", "samples", "app_usage"):
        db.db.execute(f"DELETE FROM {t}")
    db.db.commit()
    print("All TimeLens data erased.")
    return 0


def cmd_export(args) -> int:
    db = open_db()
    now = int(time.time())
    start = now - args.days * 86400
    rows = []
    d = dt.date.fromisoformat(local_day(start))
    while day_bounds(d.isoformat())[0] < now:
        s, e = day_bounds(d.isoformat())
        rows.append({"day": d.isoformat(),
                     "work_seconds": stats.range_work_seconds(db, s, min(e, now)),
                     "idle_seconds": stats.range_idle_seconds(db, s, min(e, now), "idle"),
                     "paused_seconds": stats.range_idle_seconds(db, s, min(e, now), "paused")})
        d += dt.timedelta(days=1)
    if args.format == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["day", "work_seconds", "idle_seconds",
                                            "paused_seconds"])
        w.writeheader()
        w.writerows(rows)
        out = buf.getvalue()
    else:
        apps = stats.app_breakdown(db, start, now)
        out = json.dumps({"days": rows, "apps": apps}, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out)
        print(f"Wrote {args.output}")
    else:
        sys.stdout.write(out)
    return 0


# ------------------------------------------------------------- config & setup
def cmd_config(args) -> int:
    cfg = load_config()
    if args.set:
        for pair in args.set:
            if "=" not in pair:
                die(f"expected key=value, got {pair!r}")
            k, v = pair.split("=", 1)
            if k not in DEFAULTS:
                die(f"unknown key {k!r}; known: {', '.join(DEFAULTS)}")
            default = DEFAULTS[k]
            if isinstance(default, bool):
                cfg[k] = v.lower() in ("1", "true", "yes", "on")
            elif isinstance(default, list):
                cfg[k] = [x.strip() for x in v.split(",") if x.strip()]
            elif isinstance(default, int):
                try:
                    cfg[k] = int(v)
                except ValueError:
                    die(f"{k} needs an integer")
            else:
                cfg[k] = v
        save_config(cfg)
        print("Saved:", ", ".join(p.split("=")[0] for p in args.set))
        if dm.daemon_running():
            print("(restart the daemon to apply: timelens stop && timelens start)")
        return 0
    if args.reset:
        save_config(dict(DEFAULTS))
        print("Config reset to defaults.")
        return 0
    print(f"# {config_path()}")
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    return 0


AUTOSTART_UNIT = """\
[Unit]
Description=TimeLens activity tracker
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart=%s -m timelens.daemon --run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=graphical-session.target
"""


def cmd_autostart(args) -> int:
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    unit = os.path.join(unit_dir, "timelens.service")
    py = sys.executable
    pkg_root = str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if args.remove:
        subprocess.run(["systemctl", "--user", "disable", "--now", "timelens.service"],
                       check=False)
        if os.path.exists(unit):
            os.unlink(unit)
        print("Autostart disabled.")
        return 0
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") and \
            not subprocess.run(["which", "systemctl"], capture_output=True).returncode == 0:
        die("systemctl not available; add this to your desktop autostart instead:\n"
            f"  {py} -m timelens.cli start   (or: timelens start)")
    os.makedirs(unit_dir, exist_ok=True)
    with open(unit, "w") as fh:
        fh.write(AUTOSTART_UNIT % py)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    if args.enable:
        subprocess.run(["systemctl", "--user", "enable", "--now", "timelens.service"],
                       check=False)
        print(f"Installed and enabled {unit}")
    else:
        print(f"Installed {unit}\nEnable with: systemctl --user enable --now timelens.service")
    print(f"(package dir: {pkg_root}; set PYTHONPATH={pkg_root} if installed elsewhere)")
    return 0


# ----------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="timelens",
        description="TimeLens — measure how much you actually work at your "
                    "computer (Linux). Free software, GPL-3.0.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  timelens start          begin tracking (also auto-starts via systemd)
  timelens pause          stop counting during breaks
  timelens resume         continue counting
  timelens status         what is going on right now
  timelens today          hours worked today + app breakdown
  timelens week           weekly stats: totals, averages, best day, per day
  timelens month          monthly stats
  timelens history -n 14  rolling window of the last 14 days
  timelens apps           application usage stats
  timelens report --week 2026-11-09
  timelens add 2h30m --day 2026-11-12 --start 14:00 --app code --note "meeting prep"
""")
    p.add_argument("--version", action="version", version=f"timelens {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("start", help="start tracking").set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="stop tracking completely").set_defaults(fn=cmd_stop)
    sub.add_parser("pause", help="pause the timer (breaks)").set_defaults(fn=cmd_pause)
    sub.add_parser("resume", help="resume the timer").set_defaults(fn=cmd_resume)
    sub.add_parser("status", help="current state + quick totals").set_defaults(fn=cmd_status)

    sub.add_parser("today", help="stats for today").set_defaults(fn=cmd_today)

    sub.add_parser("week", help="stats for this ISO week").set_defaults(fn=cmd_week)
    sub.add_parser("month", help="stats for this month").set_defaults(fn=cmd_month)

    h = sub.add_parser("history", help="rolling window of recent days")
    h.add_argument("-n", "--days", type=int, default=7)
    h.set_defaults(fn=cmd_history)

    a = sub.add_parser("apps", help="application usage")
    a.add_argument("-n", "--days", type=int, default=7)
    a.add_argument("--limit", type=int, default=20)
    a.set_defaults(fn=cmd_apps)

    r = sub.add_parser("report", help="detailed report for a day/week/month")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--day", metavar="YYYY-MM-DD")
    g.add_argument("--week", metavar="YYYY-MM-DD", help="any date inside the week")
    g.add_argument("--month", metavar="YYYY-MM")
    r.set_defaults(fn=cmd_report)

    ad = sub.add_parser("add", help="log time manually")
    ad.add_argument("length", help="e.g. 2h30m, 90m, 1:45")
    ad.add_argument("--day", help="YYYY-MM-DD (default: today)")
    ad.add_argument("--start", help="HH:MM or ISO datetime (default 09:00)")
    ad.add_argument("--app", help="attribute the time to an application")
    ad.add_argument("--note", help="free-text note")
    ad.set_defaults(fn=cmd_add)

    s = sub.add_parser("sessions", help="list recorded sessions")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_sessions)

    e = sub.add_parser("edit", help="fix a recorded session")
    e.add_argument("id", type=int)
    e.add_argument("--stop-ago", type=int, metavar="SECONDS",
                   help="set the session end time to N seconds before now")
    e.add_argument("--drop-pauses", choices=["paused", "all"],
                   help="remove non-work periods (make them count as work)")
    e.set_defaults(fn=cmd_edit)

    f = sub.add_parser("forget", help="delete data")
    f.add_argument("--session", type=int)
    f.add_argument("--confirm", help="pass 'yes' to erase everything")
    f.set_defaults(fn=cmd_forget)

    x = sub.add_parser("export", help="export stats as JSON/CSV")
    x.add_argument("--days", type=int, default=90)
    x.add_argument("--format", choices=["json", "csv"], default="json")
    x.add_argument("-o", "--output")
    x.set_defaults(fn=cmd_export)

    c = sub.add_parser("config", help="show/edit configuration")
    c.add_argument("--set", nargs="+", metavar="KEY=VALUE")
    c.add_argument("--reset", action="store_true")
    c.set_defaults(fn=cmd_config)

    au = sub.add_parser("autostart", help="install systemd user service")
    au.add_argument("--enable", action="store_true")
    au.add_argument("--remove", action="store_true")
    au.set_defaults(fn=cmd_autostart)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
