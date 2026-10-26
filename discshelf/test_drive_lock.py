#!/usr/bin/env python3
"""
Tests for the controller's MakeMKV lock: MakeMKV can't share a drive between runs, so
while one run (a rip) holds the drive, a drive listing returns the last list instead of
starting makemkvcon, a scan reports the drive is busy, and Disc info reuses the last
scan instead of touching MakeMKV at all.

Run:  python3 discshelf/test_drive_lock.py    (also works under pytest)
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import discinfo, makemkv, pipeline, watch  # noqa: E402
from discshelf.config import Config  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import Disc, Drive, Title  # noqa: E402


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    calls = []
    saved = (makemkv.list_drives, makemkv.scan_disc_parsed, watch.disc_present, discinfo.fetch_meta)
    drive = Drive(0, "/dev/sr9", "DRIVE", "ADVENTURE TIME VOL 2", True)
    disc = Disc(drive=drive, label="ADVENTURE TIME VOL 2", name="ADVENTURE TIME", disc_type="DVD disc",
                titles=[Title(index=i, duration_s=700) for i in range(3)])
    makemkv.list_drives = lambda cfg, run=None: calls.append("list") or [drive]
    makemkv.scan_disc_parsed = lambda cfg, i, run=None, min_seconds=0: calls.append("scan") or ({"sinfo": {}}, disc)
    watch.disc_present = lambda device: True
    discinfo.fetch_meta = lambda *a, **k: discinfo.DiscMeta()
    try:
        with tempfile.TemporaryDirectory() as root:
            ctl = pipeline.Controller(Config(), JobStore(os.path.join(root, "jobs")))
            ctl.cover = lambda i, season=None: ("", "")
            check([d.device for d in ctl.drives()] == ["/dev/sr9"] and calls == ["list"], f"free drive list: {calls}")
            ctl.scan(0)
            check(calls == ["list", "scan"], f"scan: {calls}")

            ctl._mkv.acquire()          # as a rip does for its whole run
            try:
                got = ctl.drives()
                check(calls == ["list", "scan"] and [d.device for d in got] == ["/dev/sr9"],
                      f"a drive listing started MakeMKV during a rip: {calls}")
                rep = ctl.disc_info(0)
                check(calls == ["list", "scan"] and rep.label == "ADVENTURE TIME VOL 2",
                      f"Disc info touched MakeMKV during a rip: {calls}")
                # A scan waits a few seconds for the drive, then says it's busy.
                err = []

                def scan_busy():
                    try:
                        ctl._scan(0)
                    except RuntimeError as e:
                        err.append(str(e))

                th = threading.Thread(target=scan_busy)
                th.start()
                th.join(10)
                check(err and "busy" in err[0] and calls == ["list", "scan"], f"busy scan: {err} {calls}")
            finally:
                ctl._mkv.release()

            # Disc taken out: Disc info scans again (the drive is free now).
            watch.disc_present = lambda device: False
            ctl.disc_info(0)
            check(calls[-1] == "scan", f"Disc info didn't rescan after the disc came out: {calls}")
    finally:
        makemkv.list_drives, makemkv.scan_disc_parsed, watch.disc_present, discinfo.fetch_meta = saved

    for f in failures:
        print("FAIL:", f)
    print(f"drive lock: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_drive_lock():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
