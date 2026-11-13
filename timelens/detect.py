"""Activity detection: which application is focused and whether the user
is active (keyboard/mouse input).

Supported desktops
------------------
* X11      -- via ``xdotool`` / ``xprintidle`` (preferred) or ``xprop``
* Wayland  -- KDE Plasma: ``kdotool``;  GNOME: ``gdbus`` + org.gnome.Mutter
              .IdleMonitor (shell-dbus API);  fallback: ``wlrctl`` / swaymsg
* headless -- no display detected: the daemon still counts wall time, it
              simply cannot detect idle automatically.

Everything here degrades gracefully: if a helper binary is missing we try
the next one.  ``detect()`` always returns a result object.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass


@dataclass
class Activity:
    active: bool                  # user interacting with the machine
    app: str                      # focused application name (best effort)
    window: str                   # focused window title  (best effort)
    idle_seconds: int             # seconds since last input (0 if unknown)
    source: str                   # which backend answered


def _run(cmd, timeout=2):
    """Run a command, return stdout string or None on any failure."""
    exe = cmd[0]
    if not shutil.which(exe):
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0:
            return None
        return out.stdout.strip()
    except Exception:
        return None


def _int(text):
    try:
        return int(text.split("\n")[0].strip())
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ session
def session_type() -> str:
    """Return 'wayland', 'x11' or 'none'."""
    xdg = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if xdg in ("wayland", "x11"):
        return xdg
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "none"


# ------------------------------------------------------------------ helpers
def _clean_wm_class(raw: str) -> str:
    """WM_CLASS looks like: <instance>, <class>  -> keep the class."""
    raw = raw.strip()
    if not raw:
        return ""
    parts = [p.strip(' "\t') for p in raw.split(",")]
    name = parts[-1] if len(parts) > 1 else parts[0]
    # WM_CLIENT_MACHINE style quoted strings
    name = name.strip().strip('"').strip("'")
    return name or raw


def _app_from_proc(pid: int) -> str:
    """Best-effort executable name of a pid via /proc."""
    try:
        exe = os.path.realpath(f"/proc/{pid}/exe")
        return os.path.basename(exe) or f"pid:{pid}"
    except OSError:
        try:
            with open(f"/proc/{pid}/comm") as fh:
                return fh.read().strip() or f"pid:{pid}"
        except OSError:
            return f"pid:{pid}"


# ------------------------------------------------------------------ backends
def _x11_detect() -> Activity | None:
    """Detect via xdotool (+xprintidle), falling back to xprop."""
    if shutil.which("xdotool"):
        idle = _int(_run(["xprintidle"])) if shutil.which("xprintidle") else None
        wid = _run(["xdotool", "getactivewindow"])
        app, title = "", ""
        if wid:
            title = _run(["xdotool", "getactivewindow", "getwindowname"]) or ""
        # WM_CLASS is the reliable application name; /proc exe is a fallback
        if wid:
            app = _run(["xdotool", "getactivewindow", "getwindowclassname"]) or ""
        if not app:
            pid = _int(_run(["xdotool", "getactivewindow", "getwindowpid"]))
            if pid:
                app = _app_from_proc(pid)
        if idle is None:
            idle = 0
        return Activity(active=idle < 10**9, app=app, window=title or "",
                        idle_seconds=int(idle // 1000), source="x11/xdotool")
    if shutil.which("xprop"):
        out = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
        wid = None
        if out and "window id #" in out:
            try:
                wid = out.split("window id #")[1].strip().split()[0]
            except IndexError:
                wid = None
        app, title = "", ""
        if wid:
            wm = _run(["xprop", "-id", wid, "WM_CLASS"])
            if wm and "=" in wm:
                app = _clean_wm_class(wm.split("=", 1)[1])
            nm = _run(["xprop", "-id", wid, "_NET_WM_NAME"])
            if nm and "=" in nm:
                title = nm.split("=", 1)[1].strip().strip('"')
            pidv = _run(["xprop", "-id", wid, "_NET_WM_PID"])
            if pidv and "=" in pidv and not app:
                p = _int(pidv.split("=", 1)[1])
                if p:
                    app = _app_from_proc(p)
        return Activity(active=True, app=app, window=title, idle_seconds=0,
                        source="x11/xprop")
    return None


def _gnome_wayland_detect() -> Activity | None:
    """GNOME Wayland: Mutter IdleMonitor + Shell Eval over gdbus."""
    if not shutil.which("gdbus"):
        return None
    bus_name = "org.gnome.Shell"
    base = ["gdbus", "call", "--session", "--dest", bus_name, "--object-path",
            "/org/gnome/Shell", "--method"]
    idle_out = _run(base + ["org.gnome.Mutter.IdleMonitor.GetIdletime"], timeout=3)
    idle_ms = _int(idle_out.split()[0]) if idle_out and idle_out.split() else None
    eval_out = _run(base + ["org.gnome.Shell.Eval",
                           "global.display.focus_window?"
                           "(global.display.focus_window.get_wm_class()+\"|\"+"
                           "global.display.focus_window.get_title()):''"], timeout=3)
    app, title = "", ""
    if eval_out:
        # result looks like: (true, 'firefox|Inbox - Mozilla Firefox')
        if "'" in eval_out:
            inner = eval_out.split("'", 1)[1].rsplit("'", 1)[0]
            if "|" in inner:
                app, title = inner.split("|", 1)
    return Activity(active=(idle_ms is None or idle_ms < 10**12),
                    app=app.lower(), window=title,
                    idle_seconds=int(idle_ms // 1000) if idle_ms is not None else 0,
                    source="wayland/gnome")


def _kde_wayland_detect() -> Activity | None:
    """KDE Wayland: kdotool (kscreenctlerc/kwin scripting)."""
    if not shutil.which("kdotool"):
        return None
    title = _run(["kdotool", "getactivewindow", "getwindowname"]) or ""
    cls = _run(["kdotool", "getactivewindow", "getwindowclassname"]) or ""
    pid = _int(_run(["kdotool", "getactivewindow", "getwindowpid"]))
    app = cls or (_app_from_proc(pid) if pid else "")
    return Activity(active=True, app=app, window=title, idle_seconds=0,
                    source="wayland/kdotool")


def _wlr_detect() -> Activity | None:
    """wlroots compositors (sway, river...): wlrctl or swaymsg."""
    if shutil.which("wlrctl"):
        out = _run(["wlrctl", "toplevel", "list"])
        app, title = "", ""
        if out:
            first = out.splitlines()[0]
            bits = first.split(";", 1)
            app = bits[0].strip()
            if len(bits) > 1:
                title = bits[1].strip()
        return Activity(active=True, app=app, window=title, idle_seconds=0,
                        source="wayland/wlrctl")
    if shutil.which("swaymsg"):
        out = _run(["swaymsg", "-t", "get_tree"])
        if out and '"focused":true' in out:
            # crude parse: find app_id near focused node
            idx = out.find('"focused":true')
            seg = out[max(0, idx - 600):idx]
            app = ""
            for key in ('"app_id":"', '"pid":'):
                pos = seg.rfind(key)
                if pos >= 0:
                    if key == '"pid":':
                        digits = seg[pos + 6:].lstrip(" ")
                        num = ""
                        for ch in digits:
                            if ch.isdigit():
                                num += ch
                            else:
                                break
                        if num:
                            app = _app_from_proc(int(num))
                    else:
                        val = seg[pos + len(key):]
                        app = val.split('"', 1)[0]
                    break
            return Activity(active=True, app=app, window="", idle_seconds=0,
                            source="wayland/sway")
    return None


# ------------------------------------------------------------------- public
def detect() -> Activity:
    """Detect current activity/app/window using the best available backend."""
    stype = session_type()
    backends = []
    if stype == "x11":
        backends = [_x11_detect]
    elif stype == "wayland":
        de = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
        if "GNOME" in de:
            backends = [_gnome_wayland_detect, _kde_wayland_detect, _wlr_detect]
        elif "KDE" in de or "PLASMA" in de:
            backends = [_kde_wayland_detect, _gnome_wayland_detect, _wlr_detect]
        else:
            backends = [_kde_wayland_detect, _gnome_wayland_detect, _wlr_detect,
                        _x11_detect]   # XWayland sessions expose DISPLAY too
    else:
        backends = [_x11_detect]

    for fn in backends:
        try:
            res = fn()
        except Exception:
            res = None
        if res and (res.app or res.window or res.source.startswith("x11/xdotool")):
            return res
    # nothing usable: report "active" with unknown app so wall-time still counts
    return Activity(active=True, app="", window="", idle_seconds=0,
                    source="none")


def has_display() -> bool:
    return session_type() != "none"
