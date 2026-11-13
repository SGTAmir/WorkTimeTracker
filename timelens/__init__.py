"""TimeLens — a free (GPL-3.0) activity/time tracker for Linux.

Modules:
    config  -- user configuration (~/.config/timelens/config.json)
    storage -- SQLite database layer (sessions, idle segments, app usage)
    detect  -- active-window / process sampling (X11 & Wayland)
    daemon  -- background service that records work time
    stats   -- daily/weekly/monthly statistics engine
    cli     -- command line interface (`timelens`)
"""

__version__ = "1.0.0"

APP_NAME = "TimeLens"
