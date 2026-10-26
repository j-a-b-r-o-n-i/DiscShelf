#!/usr/bin/env python3
"""
Tests for follow.py. What matters: a file copied while it is still being written ends up
byte-identical on the destination, including when the writer rewrites its start at the
end the way MakeMKV does, or leaves it shorter; a NAS error part-way is retried rather
than ending the copy; a cancel leaves the .partial behind and a later copy resumes from
it; and nothing appears under the final name until the copy has checked out.

Everything runs on temp files with small chunks; no NAS is involved.

Run:  python3 discshelf/test_follow.py    (also works under pytest)
"""
import hashlib
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import follow  # noqa: E402
from discshelf.transfer import Cancelled  # noqa: E402

CHUNK = 4096


def writer(path, bursts, done, rewrite=b"", shrink_to=None, pause=0.01, started=None):
    """Append bursts like a rip, then optionally rewrite the start or truncate, then set done."""
    def go():
        with open(path, "wb") as fh:
            if started is not None:
                started.set()
            for b in bursts:
                fh.write(b)
                fh.flush()
                time.sleep(pause)
            if rewrite:
                fh.seek(0)
                fh.write(rewrite)
            if shrink_to is not None:
                fh.truncate(shrink_to)
        done.set()
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t


def bursts(n, size, seed=0):
    return [bytes((seed + i * 7 + j) % 251 for j in range(size)) for i in range(n)]


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "rip", "Film_t00.mkv")
        os.makedirs(os.path.dirname(src))

        # Following a growing file; MakeMKV rewrites the first 4 KB when it finishes.
        dst = os.path.join(tmp, "nas", "Movies", "Film (2020)", "Film (2020).mkv")
        done = threading.Event()
        seen = []
        w = writer(src, bursts(40, 1500), done, rewrite=b"\xEE" * 4096)
        while not os.path.exists(src):
            time.sleep(0.001)
        res = follow.stream_copy(src, dst, done, lambda *a: seen.append(a), chunk=CHUNK, poll=0.005)
        w.join()
        data = open(src, "rb").read()
        check(open(dst, "rb").read() == data, "copy of a rewritten file is not identical")
        check(res.chunks_resent >= 1, f"rewritten start was not resent: {res}")
        check(res.bytes == len(data), f"size: {res.bytes} vs {len(data)}")
        check(res.sha256 == hashlib.sha256(data).hexdigest(), "whole-file sha256 is not the source's")
        check(not os.path.exists(dst + ".partial"), ".partial left after success")
        check(seen and seen[-1][0] == len(data) and seen[-1][1] == len(data), f"last progress: {seen[-1:]}")
        copied = [s[0] for s in seen]
        check(copied == sorted(copied), "copied bytes went backwards")
        # chunks were copied while the writer was still going, not all at the end
        check(any(s[1] == 0 and s[0] > 0 for s in seen), "nothing was copied before the rip finished")

        # A NAS error part-way through is retried, and the result is still identical.
        dst2 = os.path.join(tmp, "nas", "flaky.mkv")
        state = {"opens": 0, "failed": False}

        class FlakyFile:
            def __init__(self, fh):
                self.fh = fh

            def write(self, b):
                if not state["failed"] and self.fh.tell() >= 3 * CHUNK:
                    state["failed"] = True
                    raise OSError(112, "Host is down")
                return self.fh.write(b)

            def __getattr__(self, name):
                return getattr(self.fh, name)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self.fh.close()

        def flaky_open(path, mode="r"):
            state["opens"] += 1
            return FlakyFile(open(path, mode))
        naps = []
        done = threading.Event()
        w = writer(src, bursts(20, 2000, seed=3), done)
        w.join()
        res = follow.stream_copy(src, dst2, done, lambda *a: None, chunk=CHUNK, poll=0.005,
                                 opener=flaky_open, sleep=lambda s: naps.append(s))
        check(state["failed"], "the flaky opener never failed")
        check(open(dst2, "rb").read() == open(src, "rb").read(), "copy after a NAS error is not identical")
        check(res.chunks_resent == 0, f"nothing changed, yet chunks were resent: {res}")

        # A NAS that never comes back gives up with a clear error, after the time limit.
        clock = [0.0]

        def dead_open(path, mode="r"):
            raise OSError(112, "Host is down")
        try:
            follow.stream_copy(src, os.path.join(tmp, "nas", "dead.mkv"), done, lambda *a: None,
                               chunk=CHUNK, opener=dead_open, now=lambda: clock[0],
                               sleep=lambda s: clock.__setitem__(0, clock[0] + s))
            failures.append("an unreachable NAS did not raise")
        except follow.FollowError as e:
            check("Host is down" in str(e) and "5 minutes" in str(e), f"give-up message: {e}")
            check(clock[0] >= follow.GIVE_UP_S, f"gave up too early, at {clock[0]} s")

        # Cancel: raises, leaves the .partial; the next copy resumes from it.
        dst3 = os.path.join(tmp, "nas", "cancel.mkv")
        done = threading.Event()
        cancel = threading.Event()
        started = threading.Event()
        w = writer(src, bursts(30, 2000, seed=5), done, pause=0.02, started=started)
        started.wait(2)

        def progress(copied, checked, rate):
            if copied >= 2 * CHUNK:
                cancel.set()
        try:
            follow.stream_copy(src, dst3, done, progress, cancel, chunk=CHUNK, poll=0.005)
            failures.append("a cancelled copy did not raise")
        except Cancelled:
            pass
        check(os.path.exists(dst3 + ".partial") and not os.path.exists(dst3), "cancel did not leave only the .partial")
        kept = os.path.getsize(dst3 + ".partial")
        w.join()
        writes = []

        def counting_open(path, mode="r"):
            fh = open(path, mode)
            if "+" in mode or "w" in mode:
                orig = fh.write
                fh.write = lambda b: (writes.append(len(b)), orig(b))[1]
            return fh
        follow.stream_copy(src, dst3, done, lambda *a: None, chunk=CHUNK, poll=0.005, opener=counting_open)
        check(open(dst3, "rb").read() == open(src, "rb").read(), "resumed copy is not identical")
        check(sum(writes) <= os.path.getsize(src) - kept + CHUNK, f"resume rewrote {sum(writes)} bytes; {kept} were kept")

        # The source ends up shorter than what was already copied.
        dst4 = os.path.join(tmp, "nas", "short.mkv")
        done = threading.Event()
        w = writer(src, bursts(20, 2000, seed=9), done, shrink_to=25_000, pause=0.02)
        while not os.path.exists(src):
            time.sleep(0.001)
        res = follow.stream_copy(src, dst4, done, lambda *a: None, chunk=CHUNK, poll=0.005)
        w.join()
        check(os.path.getsize(src) == 25_000, "writer did not shrink the file")
        check(open(dst4, "rb").read() == open(src, "rb").read(), "copy of a shrunk file is not identical")
        check(res.bytes == 25_000, f"shrunk size: {res.bytes}")

    for f in failures:
        print("FAIL:", f)
    print(f"follow: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_follow():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
