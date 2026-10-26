#!/usr/bin/env python3
"""
Tests for taskbar.py: progress is clamped and sent with the .desktop URI, hiding sends
progress-visible false, and repeats of a state that wouldn't change the icon aren't sent.

Run:  python3 discshelf/test_taskbar.py    (also works under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf.taskbar import TaskbarProgress  # noqa: E402


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    sent = []
    t = TaskbarProgress(send=lambda uri, props: sent.append((uri, props)))
    t.update(0.25)
    check(sent == [("application://disc-shelf.desktop", {"progress": 0.25, "progress-visible": True, "urgent": False})],
          f"first update: {sent}")
    t.update(0.2501)
    check(len(sent) == 1, "a change too small to see was sent again")
    t.update(1.7, urgent=True)
    check(sent[-1][1] == {"progress": 1.0, "progress-visible": True, "urgent": True}, f"clamp/urgent: {sent[-1]}")
    t.clear()
    check(sent[-1][1] == {"progress-visible": False, "urgent": False}, f"clear: {sent[-1]}")
    t.clear()
    check(len(sent) == 3, "clearing twice sent twice")

    for f in failures:
        print("FAIL:", f)
    print(f"taskbar: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_taskbar():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
