"""Configuration for TimeLens.

Stored at ``~/.config/timelens/config.json`` (honours ``$TIMELENS_HOME``
and ``$XDG_CONFIG_HOME`` so it can be relocated for testing).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

DEFAULTS: Dict[str, Any] = {
    # How often the daemon samples activity (seconds)
    "sample_interval": 15,
    # No keyboard/mouse activity for this many seconds => you are "away"
    "idle_timeout": 300,
    # A gap of manual-pause shorter than this is merged into work time
    # (useful if you pause only briefly and forget to resume)
    "short_pause_merge": 0,
    # Count time even when the tracker is stopped?  (always False: stopped
    # means stopped -- kept here for clarity/future options)
    "count_when_paused": False,
    # Ignore these processes when computing per-application stats
    "ignore_apps": [
        "Xorg", "Xwayland", "wayland-session", "pipewire", "pipewire-pulse",
        "wireplumber", "dbus-daemon", "systemd", "init", "kworker",
        "gnome-shell", "plasmashell", "mutter", "xfwm4", "openbox",
        "pulseaudio", "appindicator-watchd", "xdg-desktop-portal",
    ],
    # Minimum share (seconds) an app must have been seen to appear in stats
    "min_app_seconds": 30,
}


def config_dir() -> Path:
    base = os.environ.get("TIMELENS_HOME")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(xdg) / "timelens"


def data_dir() -> Path:
    base = os.environ.get("TIMELENS_HOME")
    if base:
        return Path(base)
    xdg = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return Path(xdg) / "timelens"


def db_path() -> Path:
    return data_dir() / "timelens.db"


def config_path() -> Path:
    return config_dir() / "config.json"


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    p = config_path()
    if p.exists():
        try:
            user = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update(user)
        except Exception as exc:  # corrupt file -> keep defaults but warn
            print(f"[timelens] warning: bad config file {p}: {exc}")
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(p)


def ensure_dirs() -> None:
    config_dir().mkdir(parents=True, exist_ok=True)
    data_dir().mkdir(parents=True, exist_ok=True)
