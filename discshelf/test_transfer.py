#!/usr/bin/env python3
"""
Tests for transfer.py. What matters: rsync is called with flags that suit a CIFS
mount (no owners or modes, resumable, partial files kept out of the final name),
its progress lines become readable status, a failure or cancel raises instead of
looking like success, the verify modes catch a damaged copy, and the clean-up
policy never deletes a rip that was not copied. One test runs the real rsync.

Run:  python3 discshelf/test_transfer.py    (also works under pytest)
"""
import datetime as dt
import io
import os
import shutil
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import transfer as tr  # noqa: E402

# Real rsync 3.5 --info=progress2 output for a 30 MB file (carriage-return separated).
RSYNC_OUT = (b"\r         32,768   0%    0.00kB/s    0:00:00  "
             b"\r     21,004,288  70%   19.55MB/s    0:00:01  "
             b"\r     30,000,000 100%   19.93MB/s    0:00:01 (xfr#1, to-chk=0/1)\n")


class FakeProc:
    def __init__(self, out=b"", err=b"", rc=0, block=False):
        self.stdout, self.stderr, self.rc, self.block = io.BytesIO(out), io.BytesIO(err), rc, block
        self.terminated = threading.Event()

    def wait(self):
        if self.block:
            self.terminated.wait(5)
            return -15
        return self.rc

    def terminate(self):
        self.terminated.set()


class BlockingStdout(io.RawIOBase):
    """Never produces output until the process is terminated, like a stalled rsync."""

    def __init__(self, proc):
        self.proc = proc

    def read(self, n=-1):
        self.proc.terminated.wait(5)
        return b""


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # Progress parsing.
    f, text = tr.parse_progress("     21,004,288  70%   19.55MB/s    0:00:01  ", 30_000_000)
    check(abs(f - 0.7001) < 0.001 and text == "21.0 MB of 30.0 MB · 20 MB/s · 0:01 left", f"progress: {f} {text!r}")
    f, text = tr.parse_progress(" 1,234,000,000  30%   95.00MB/s    0:00:30", 4_000_000_000)
    check(text == "1.2 GB of 4.0 GB · 100 MB/s · 0:30 left", f"GB progress: {text!r}")
    f, text = tr.parse_progress("     30,000,000 100%   19.93MB/s    0:00:01 (xfr#1, to-chk=0/1)", 30_000_000)
    check(f == 1.0 and "left" not in text, f"finished line: {text!r}")
    check(tr.parse_progress("sending incremental file list") is None, "non-progress line parsed")
    _, text = tr.parse_progress("  5,000,000,000  50%  110.00MB/s    1:15:30")
    check(text.endswith("1:15:30 left") and "10.0 GB" in text, f"no total / hours: {text!r}")

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "a.mkv")
        with open(src, "wb") as fh:
            fh.write(b"x" * 30_000_000)
        dest = os.path.join(tmp, "nas", "Show", "Season 01", "a.mkv")

        # Flags and progress with a fake rsync.
        calls, seen = [], []

        def popen(cmd, **kw):
            calls.append(cmd)
            return FakeProc(RSYNC_OUT)
        tr.copy(src, dest, lambda f, t: seen.append((f, t)), popen=popen)
        cmd = calls[0]
        check(os.path.isdir(os.path.dirname(dest)), "parent folders not created")
        for flag in ("--no-perms", "--no-owner", "--no-group", "--info=progress2", "--no-whole-file"):
            check(flag in cmd, f"missing {flag}: {cmd}")
        check(any(c.startswith("--partial-dir=.") for c in cmd), f"partial files not kept hidden: {cmd}")
        check(not any(c in ("-a", "--archive", "--inplace") for c in cmd), f"flag unsuited to CIFS: {cmd}")
        check(cmd[-2:] == [src, dest], f"src/dest: {cmd}")
        check(len(seen) >= 3 and seen[1][0] > 0.6 and seen[-1][0] == 1.0, f"progress calls: {seen}")

        # Failure raises with rsync's stderr.
        try:
            tr.copy(src, dest, lambda f, t: None,
                    popen=lambda cmd, **kw: FakeProc(err=b"rsync: write failed: No space left on device (28)", rc=11))
            check(False, "rsync failure did not raise")
        except tr.CopyError as e:
            check("No space left" in str(e) and "exit 11" in str(e), f"error text: {e}")

        # Cancel terminates a stalled rsync.
        cancel = threading.Event()
        procs = []

        def stalled(cmd, **kw):
            p = FakeProc(block=True)
            p.stdout = BlockingStdout(p)
            procs.append(p)
            return p
        threading.Timer(0.3, cancel.set).start()
        t0 = time.monotonic()
        try:
            tr.copy(src, dest, lambda f, t: None, cancel=cancel, popen=stalled)
            check(False, "cancel did not raise")
        except tr.Cancelled:
            check(procs[0].terminated.is_set() and time.monotonic() - t0 < 3, "rsync not terminated on cancel")

        # Real rsync, then both verify modes.
        if shutil.which("rsync"):
            real_dest = os.path.join(tmp, "real", "Film (2020)", "a.mkv")
            seen = []
            tr.copy(src, real_dest, lambda f, t: seen.append(f))
            check(os.path.getsize(real_dest) == 30_000_000 and seen and seen[-1] == 1.0, "real rsync copy")
            check(not os.path.exists(os.path.join(os.path.dirname(real_dest), tr.PARTIAL_DIR)), "partial dir left")
            h = tr.sha256(src)
            check(tr.verify(src, real_dest, "readback") and tr.verify(src, real_dest, "readback", src_hash=h),
                  "readback verify failed on a good copy")
            check(tr.verify(src, real_dest, "size"), "size verify failed on a good copy")
            with open(real_dest, "r+b") as fh:
                fh.seek(1000)
                fh.write(b"y")
            check(not tr.verify(src, real_dest, "readback", src_hash=h), "readback missed a changed byte")
            check(tr.verify(src, real_dest, "size"), "size mode should only compare sizes")
            with open(real_dest, "ab") as fh:
                fh.write(b"z")
            check(not tr.verify(src, real_dest, "size"), "size mode missed a size change")
            check(not tr.verify(src, real_dest + ".missing", "size"), "a missing copy verified")
        else:
            failures.append("rsync is not installed, so the real copy test did not run")

        # sha256 progress and value.
        small = os.path.join(tmp, "s")
        with open(small, "wb") as fh:
            fh.write(b"abc")
        prog = []
        check(tr.sha256(small, lambda f, t: prog.append(f), chunk=1) ==
              "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad", "sha256 value")
        check(prog == [1 / 3, 2 / 3, 1.0], f"sha256 progress: {prog}")

    # Clean-up policy.
    now = dt.datetime(2026, 10, 26, 12, 0, tzinfo=dt.timezone.utc)
    check(tr.due_for_cleanup("2026-10-26T11:00:00+00:00", "delete", now), "delete policy")
    check(not tr.due_for_cleanup("", "delete", now), "never-copied rip cleaned up")
    check(not tr.due_for_cleanup("2026-01-01T00:00:00+00:00", "keep", now), "keep policy")
    check(not tr.due_for_cleanup("2026-10-20T12:00:01+00:00", "7days", now), "7days too early")
    check(tr.due_for_cleanup("2026-10-19T12:00:00+00:00", "7days", now), "7days due")
    check(isinstance(tr.due_for_cleanup("2026-10-01T00:00:00", "7days", now), bool), "naive timestamp")

    for f in failures:
        print("FAIL:", f)
    print("ok" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


def test_transfer():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
