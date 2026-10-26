#!/usr/bin/env python3
"""
Tests for watch.py: the drive-status answers map to disc in / no disc / can't tell,
and a device that can't be opened or answered reads as "can't tell", never as empty.

Run:  python3 discshelf/test_watch.py    (also works under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import watch  # noqa: E402


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    r, w = os.pipe()        # real file descriptors for disc_present to close
    os.close(w)

    def opener(path, flags):
        check(flags & os.O_NONBLOCK, "opened without O_NONBLOCK, which fails or waits on an empty drive")
        return os.dup(r)

    for status, want in ((watch.CDS_DISC_OK, True), (watch.CDS_NO_DISC, False), (watch.CDS_TRAY_OPEN, False),
                         (watch.CDS_DRIVE_NOT_READY, None), (0, None)):
        got = watch.disc_present("/dev/srX", opener=opener, ioctl=lambda fd, req, arg, s=status: s)
        check(got is want, f"status {status} read as {got}")

    def refuse(path, flags):
        raise PermissionError(13, "denied")

    check(watch.disc_present("/dev/srX", opener=refuse) is None, "an unopenable drive read as empty")

    def broken(fd, req, arg):
        raise OSError(25, "not a CD-ROM")

    check(watch.disc_present("/dev/srX", opener=opener, ioctl=broken) is None, "a failed ioctl read as empty")
    os.close(r)

    for f in failures:
        print("FAIL:", f)
    print(f"watch: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_watch():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
