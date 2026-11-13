#!/usr/bin/env python3
"""Unit tests for timelens.detect using a fake X11 toolchain."""
import os, sys
sys.path.insert(0, "/workspace")

os.makedirs("/tmp/fakebin", exist_ok=True)
with open("/tmp/fakebin/xdotool", "w") as f:
    f.write('''#!/bin/bash
case "$*" in
  "getactivewindow") echo "0x12345" ;;
  "getactivewindow getwindowpid") echo "4242" ;;
  "getactivewindow getwindowname") echo "project.py - vim" ;;
  "getactivewindow getwindowclassname") echo "Vim" ;;
esac
''')
with open("/tmp/fakebin/xprintidle", "w") as f:
    f.write('#!/bin/bash\ncat /tmp/fakeidle_ms 2>/dev/null || echo 0\n')
os.chmod("/tmp/fakebin/xdotool", 0o755)
os.chmod("/tmp/fakebin/xprintidle", 0o755)

os.environ.update(DISPLAY=":99", XDG_SESSION_TYPE="x11",
                  PATH="/tmp/fakebin:" + os.environ["PATH"])
from timelens import detect

open("/tmp/fakeidle_ms", "w").write("1000")
a = detect.detect()
assert a.source == "x11/xdotool", a
assert a.app == "Vim", a
assert a.window == "project.py - vim", a
assert a.idle_seconds == 1, a
assert a.active is True, a

open("/tmp/fakeidle_ms", "w").write("60000")
a = detect.detect()
assert a.idle_seconds == 60, a
assert a.active is True   # 'active' flag just means backend responded

print("detect unit test OK:", a)
