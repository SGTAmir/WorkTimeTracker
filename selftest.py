#!/usr/bin/env python3
"""Self-test for TimeLens: exercises the CLI end-to-end in a sandbox."""
import datetime as dt
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(ROOT, "bin", "timelens")


def run(args, env=None, check=True):
    proc = subprocess.run([sys.executable, BIN] + args, capture_output=True,
                          text=True, env=env, cwd=ROOT)
    if check and proc.returncode != 0:
        print("FAILED:", args, proc.returncode)
        print(proc.stdout)
        print(proc.stderr)
        sys.exit(1)
    return proc


def main():
    sandbox = tempfile.mkdtemp(prefix="timelens-selftest-")
    env = dict(os.environ, TIMELENS_HOME=sandbox)
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env["XDG_SESSION_TYPE"] = ""

    yday = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    steps = [
        ["config", "--set", "sample_interval=2", "idle_timeout=15"],
        ["add", "2h30m", "--day", yday, "--start", "09:00", "--app", "code",
         "--note", "deep work"],
        ["add", "45m", "--day", yday, "--start", "14:00", "--app", "firefox"],
        ["today"],
        ["week"],
        ["month"],
        ["history", "-n", "7"],
        ["apps", "-n", "7"],
        ["report", "--day", yday],
        ["sessions"],
        ["status"],
        ["export", "--days", "7", "--format", "json"],
        ["export", "--days", "7", "--format", "csv"],
        ["config"],
    ]
    for s in steps:
        p = run(s, env=env)
        print(f"\n$ timelens {' '.join(s)}\n{p.stdout.strip()}")

    # pause without daemon must fail gracefully
    p = run(["pause"], env=env, check=False)
    assert p.returncode != 0, "pause should fail when daemon not running"
    print("\n$ timelens pause (no daemon) -> correctly refused:",
          p.stderr.strip())

    # forget session 1 then verify week total shrank
    p = run(["sessions", "--limit", "5"], env=env)
    sid = p.stdout.splitlines()[1].split()[0]
    run(["forget", "--session", sid], env=env)
    p = run(["report", "--day", yday], env=env)
    print(f"\nafter forgetting session {sid}:\n{p.stdout.strip()}")
    assert "Work:   0:45:00" in p.stdout, p.stdout

    # erase everything
    run(["forget", "--confirm", "yes"], env=env)
    p = run(["week"], env=env)
    assert "Total work:      0:00:00" in p.stdout, p.stdout

    print("\nSELF-TEST OK ✔")


if __name__ == "__main__":
    main()
