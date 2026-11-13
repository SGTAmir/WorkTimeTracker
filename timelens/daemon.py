"""TimeLens background daemon.

One process per desktop session.  It:
  * opens a tracker "session" when started and closes it on clean shutdown,
  * samples the active window / application every `sample_interval` sec,
  * marks 'idle' periods automatically (no keyboard/mouse for idle_timeout),
  * honours pause/resume requests from the CLI through a control file,
  * records per-application focus time into app_usage.

Control file: ``<config dir>/control.json``:
    {"state": "running"|"paused", "updated": <epoch>, "nonce": "<random>"}

The daemon polls the file each tick; if its own state differs it writes an
idle/paused period accordingly.  This makes start/stop work even when the
CLI is run from another terminal/tty without any IPC server needed.

Crash recovery: sessions are opened immediately at daemon start and closed
only on clean shutdown (SIGTERM/SIGINT).  If the daemon dies uncleanly the
orphaned open session is adopted by the next daemon instance (so no time is
lost); the CLI's `stop` command also closes it directly as a safety net.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from . import detect
from .config import config_dir, data_dir, ensure_dirs, load_config
from .storage import Storage, local_day

PIDFILE_NAME = "daemon.pid"
CONTROL_NAME = "control.json"
LOG_NAME = "daemon.log"


def pidfile() -> Path:
    return data_dir() / PIDFILE_NAME


def control_file() -> Path:
    return config_dir() / CONTROL_NAME


def logfile() -> Path:
    return data_dir() / LOG_NAME


# ------------------------------------------------------------------ helpers
def read_pid() -> Optional[int]:
    try:
        pid = int(pidfile().read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    # verify the process exists and is really our daemon
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            cmd = fh.read().decode(errors="replace")
        if "timelens" not in cmd:
            return None
    except OSError:
        pass
    return pid


def daemon_running() -> bool:
    return read_pid() is not None


def write_control(state: str) -> None:
    ensure_dirs()
    tmp = control_file().with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"state": state, "updated": int(time.time()),
                               "nonce": os.urandom(4).hex()}))
    tmp.replace(control_file())


def read_control() -> Optional[str]:
    try:
        return json.loads(control_file().read_text()).get("state")
    except Exception:
        return None


def spawn_daemon() -> bool:
    """Start the daemon detached.  Returns True if it came up."""
    import subprocess
    ensure_dirs()
    log = open(logfile(), "ab")
    subprocess.Popen(
        [sys.executable, "-m", "timelens.daemon", "--run"],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True, cwd=str(Path(__file__).resolve().parents[1]))
    for _ in range(50):
        time.sleep(0.1)
        if daemon_running():
            return True
    return False


# ------------------------------------------------------------------- daemon
class Daemon:
    def __init__(self):
        self.cfg = load_config()
        self.db = Storage(str(data_dir() / "timelens.db"))
        self.interval = max(2, int(self.cfg.get("sample_interval", 15)))
        self.idle_timeout = max(10, int(self.cfg.get("idle_timeout", 300)))
        self.ignore = {a.lower() for a in self.cfg.get("ignore_apps", [])}
        self.running = True
        self.paused = False
        self.session_id: Optional[int] = None
        self.last_app = ""
        self.last_span_start: Optional[int] = None
        self.wake_ts: Optional[int] = None    # start of current idle stretch
        self.pause_start: Optional[int] = None

    # ------------------------------------------------------------- lifecycle
    def setup(self):
        pidfile().write_text(str(os.getpid()))

        def _sig(signum, frame):
            self.running = False
        signal.signal(signal.SIGTERM, _sig)
        signal.signal(signal.SIGINT, _sig)

    def teardown(self):
        now = int(time.time())
        if self.session_id:
            # close an open app span so no seconds are lost
            if (not self.paused and self.last_app and self.last_span_start
                    and now > self.last_span_start):
                self.db.add_app_usage(self.session_id, self.last_app,
                                      self.last_span_start, now)
            # close an open idle stretch (away until shutdown)
            if not self.paused and self.wake_ts is not None and now > self.wake_ts:
                self.db.add_idle(self.session_id, "idle", self.wake_ts, now)
            # close an open pause (not working until shutdown)
            if self.paused and self.pause_start is not None and now > self.pause_start:
                self.db.add_idle(self.session_id, "paused",
                                 self.pause_start, now)
            self.db.close_session(now)
        try:
            pidfile().unlink()
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------ bookkeeping
    def _open_session(self, ts: int):
        sess = self.db.current_session()          # adopt orphan after crash
        self.session_id = sess["id"] if sess else self.db.open_session(ts)

    # --------------------------------------------------------------- main loop
    def run(self):
        self.setup()
        now = int(time.time())
        self._open_session(now)
        ctl = read_control()
        self.paused = (ctl == "paused")
        if self.paused:
            self.pause_start = now
        print(f"[timelens-daemon] started pid={os.getpid()} "
              f"interval={self.interval}s idle_timeout={self.idle_timeout}s "
              f"display={detect.session_type()} paused={self.paused}", flush=True)

        while self.running:
            try:
                self.tick()
            except Exception as exc:                      # never die silently
                print(f"[timelens-daemon] error: {exc!r}", flush=True)
            # sleep in small slices so SIGTERM/control changes act fast
            slept = 0.0
            while self.running and slept < self.interval:
                time.sleep(0.5)
                slept += 0.5
        self.teardown()
        print("[timelens-daemon] stopped", flush=True)

    # ---------------------------------------------------------------- sampling
    def tick(self):
        now = int(time.time())
        ctl = read_control()
        want_paused = (ctl == "paused")
        if want_paused != self.paused:
            if want_paused:                     # running -> paused
                # freeze open app span and idle stretch at `now`
                if self.last_app and self.last_span_start and now > self.last_span_start:
                    self.db.add_app_usage(self.session_id, self.last_app,
                                          self.last_span_start, now)
                if self.wake_ts is not None and now > self.wake_ts:
                    self.db.add_idle(self.session_id, "idle", self.wake_ts, now)
                self.last_app, self.last_span_start, self.wake_ts = "", None, None
                self.pause_start = now
                self.paused = True
                print("[timelens-daemon] paused by user", flush=True)
            else:                               # paused -> running
                if self.pause_start is not None and now > self.pause_start:
                    self.db.add_idle(self.session_id, "paused",
                                     self.pause_start, now)
                self.pause_start = None
                self.paused = False
                self.last_span_start = now
                print("[timelens-daemon] resumed by user", flush=True)

        if self.paused:
            return

        act = detect.detect()

        # ---- idle detection (only when the backend reports idleness)
        can_idle = act.source.startswith(("x11/xdotool", "wayland/gnome"))
        if can_idle and act.idle_seconds >= self.idle_timeout:
            if self.wake_ts is None:            # entering idle state
                self.wake_ts = now - act.idle_seconds
                # freeze current app span at the moment activity stopped
                if self.last_app and self.last_span_start:
                    end = max(self.last_span_start, min(now, self.wake_ts))
                    if end > self.last_span_start:
                        self.db.add_app_usage(self.session_id, self.last_app,
                                              self.last_span_start, end)
                    self.last_span_start = end
            self.db.add_sample(now, self.session_id, False, act.window, act.app)
            return

        if self.wake_ts is not None:            # was idle, user came back
            idle_end = now
            row = self.db.db.execute(
                "SELECT id, end_ts FROM idle_periods WHERE session_id=? "
                "AND kind='idle' ORDER BY end_ts DESC LIMIT 1",
                (self.session_id,)).fetchone()
            if row and abs(row["end_ts"] - self.wake_ts) <= self.interval + 2:
                self.db.extend_idle(row["id"], idle_end)   # grow existing record
            else:
                self.db.add_idle(self.session_id, "idle", self.wake_ts, idle_end)
            self.wake_ts = None
            self.last_span_start = idle_end

        # ---- application tracking: charge the previous app until now
        app = (act.app or "").strip()
        if app.lower() in self.ignore:
            app = ""
        if app != self.last_app:
            if self.last_app and self.last_span_start and now > self.last_span_start:
                self.db.add_app_usage(self.session_id, self.last_app,
                                      self.last_span_start, now)
            self.last_app = app
            self.last_span_start = now
        elif self.last_span_start is None:
            self.last_span_start = now

        # keep raw sample (bounded size via prune below)
        self.db.add_sample(now, self.session_id, True, act.window[:200], app)

        # daily housekeeping
        last_prune = self.db.get_meta("last_prune")
        today = local_day(now)
        if last_prune != today:
            self.db.prune_samples(keep_days=90)
            self.db.set_meta("last_prune", today)
        self.db.set_meta("last_sample", str(now))


def main(argv=None):
    d = Daemon()
    d.run()


if __name__ == "__main__":
    main()
