#!/usr/bin/env python3
"""
Take the screenshots the Flathub listing and README use, from the window in sample mode
(no drive, no network), into packaging/screenshots/.

Run:  python3 scripts/screenshots.py
The metainfo points at these files under the release tag, so take them before tagging.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from discshelf import gui  # noqa: E402
from discshelf.config import Config  # noqa: E402
from discshelf.fake import FakeController  # noqa: E402

OUT = os.path.join(ROOT, "packaging", "screenshots")
SIZE = (1440, 900)


def main() -> int:
    app = gui.make_app()
    ctl = FakeController(Config(), delay=0.02)
    win = gui.MainWindow(ctl)
    win.resize(*SIZE)
    win.show()

    def settle(seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)

    os.makedirs(OUT, exist_ok=True)
    settle(4)                       # the drive list, the scan of the sample disc and its cover

    win.show_page("rip")
    settle(1)
    win.grab().save(os.path.join(OUT, "rip.png"))

    win.show_page("match")
    settle(2)
    win.grab().save(os.path.join(OUT, "match.png"))

    win.show_page("queue")
    win.queue.refresh()
    settle(2)
    win.grab().save(os.path.join(OUT, "queue.png"))

    for name in ("rip", "match", "queue"):
        print(os.path.join(OUT, f"{name}.png"))
    win.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
