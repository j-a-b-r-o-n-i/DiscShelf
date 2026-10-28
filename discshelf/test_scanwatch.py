#!/usr/bin/env python3
"""
Tests for scanwatch.py and the live scan in makemkv.py, on the times of a real 4K scan
(Harry Potter and the Sorcerer's Stone, 27 Oct 2026): 5 min 35 s in all, 5 min 18 s of it
spent fetching about 600 keys from MakeMKV's server, about one request a second, with the
drive idle.

What matters: the key step appears only when server requests are seen, and is over once the
title list arrives; a scan with no output and no requests for 3 minutes is "stalled" (10
minutes when the requests can't be seen); MakeMKV's connections are found through /proc;
scan times are kept per kind of disc; and a live scan feeds every line to the watch, returns
the same parse as before, and stops when cancelled.

Run:  python3 discshelf/test_scanwatch.py    (also works under pytest)
"""
import io
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import makemkv, scanui, scanwatch  # noqa: E402
from discshelf.config import Config  # noqa: E402

LIBRE = 'MSG:1011,0,1,"Using LibreDrive mode (v06.3 id=000000000000)","%1","x"'


def run() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # A 4K disc: opened in 4 s, LibreDrive at 6 s, a request each second from 7 s to 324 s,
    # the title list at 325 s, done at 335 s.
    w = scanwatch.ScanWatch(start=0.0, clock=lambda: 0.0)
    w.line('MSG:1005,0,1,"MakeMKV started","%1 started","x"', now=1.0)
    w.line(LIBRE, now=6.0)
    for k, t in enumerate(range(7, 200)):
        w.connections({50000 + k}, now=float(t))
    st = w.status(now=200.0)
    names = [s.name for s in st.steps]
    check(names == ["Opening the disc", "LibreDrive is on, so 4K discs can be read",
                    "Getting this disc's keys from MakeMKV's server", "Reading the titles and their tracks"],
          f"steps while fetching keys: {names}")
    check([s.state for s in st.steps] == ["done", "done", "now", "wait"], f"states: {[s.state for s in st.steps]}")
    check(st.getting_keys and st.requests == 193 and st.sees_traffic, f"keys: {st}")
    check("about 1 a second" in st.steps[2].note and "waiting, not reading" in st.steps[2].note, st.steps[2].note)
    check(sum(st.bins[-3:]) == 29 and len(st.bins) == scanwatch.BINS, f"request bins: {st.bins[-3:]}")
    check(not st.stalled and st.quiet_s == 1.0, f"quiet: {st.quiet_s}")
    check(scanui.headline(st) == "Getting this disc's keys from MakeMKV's server", scanui.headline(st))
    check("about 3 min so far" in scanui.why(st), scanui.why(st))
    check(scanui.requests_note(st) == "about 1 a second", scanui.requests_note(st))

    for k, t in enumerate(range(200, 325)):
        w.connections({60000 + k}, now=float(t))
    w.line("TCOUNT:3", now=325.0)
    w.line('PRGC:5018,0,"Scanning contents"', now=326.0)
    st = w.status(now=330.0)
    check([s.state for s in st.steps] == ["done", "done", "done", "now"], f"after the keys: {[s.state for s in st.steps]}")
    check(abs(st.steps[2].seconds - 318) < 1, f"key step time: {st.steps[2].seconds}")
    check("MakeMKV: Scanning contents" in st.steps[3].note and not st.getting_keys, st.steps[3].note)
    w.finish(now=335.0)
    st = w.status(now=999.0)
    check(st.finished and st.elapsed == 335.0 and all(s.state == "done" for s in st.steps),
          f"finished: {st.elapsed} {[s.state for s in st.steps]}")
    check("keys are saved" in st.steps[2].note, st.steps[2].note)

    # Stalled: requests stop and nothing is printed for 3 minutes.
    w = scanwatch.ScanWatch(start=0.0, clock=lambda: 0.0)
    w.line(LIBRE, now=3.0)
    for k, t in enumerate(range(5, 100)):
        w.connections({k + 1}, now=float(t))
    check(not w.status(now=99 + 179).stalled, "stalled before 3 minutes")
    st = w.status(now=99 + 181)
    check(st.stalled and st.steps[2].state == "stuck" and "then nothing for 3:01" in st.steps[2].note,
          f"stalled: {st.stalled} {st.steps[2]}")
    check(scanui.headline(st) == "The scan has stopped making progress", scanui.headline(st))
    check("made no request to its server for 3 min" in scanui.why(st) and "keys already fetched" in scanui.why(st),
          scanui.why(st))

    # Connections that can't be seen (Disc Shelf's Flatpak): no key step, and 10 minutes before "stalled".
    w = scanwatch.ScanWatch(start=0.0, clock=lambda: 0.0)
    w.connections(None, now=1.0)
    w.line('MSG:1005,0,1,"MakeMKV started","%1","x"', now=2.0)
    check(not w.status(now=2 + 300).stalled, "stalled after 5 min with the connections unseen")
    st = w.status(now=2 + 601)
    check(st.stalled and st.stall_after == scanwatch.STALL_BLIND_S and not st.sees_traffic, f"blind: {st}")
    check([s.name for s in st.steps] == ["Opening the disc", "Reading the titles and their tracks"],
          f"blind steps: {[s.name for s in st.steps]}")
    check("made no request" not in scanui.why(st), scanui.why(st))

    # A Blu-ray with no key lookup: open, then read.
    w = scanwatch.ScanWatch(start=0.0, clock=lambda: 0.0)
    w.connections(set(), now=1.0)
    w.line("TCOUNT:12", now=30.0)
    st = w.status(now=35.0)
    check([(s.name, s.state) for s in st.steps] == [("Opening the disc", "done"),
                                                     ("Reading the titles and their tracks", "now")],
          f"Blu-ray steps: {[(s.name, s.state) for s in st.steps]}")
    check(scanui.headline(st) == "Reading the disc", scanui.headline(st))

    # /proc: makemkvcon two processes below the one started, and its connections to port 443.
    with tempfile.TemporaryDirectory() as proc:
        def mkproc(pid, ppid, comm):
            d = os.path.join(proc, str(pid))
            os.makedirs(os.path.join(d, "fd"))
            os.makedirs(os.path.join(d, "net"))
            with open(os.path.join(d, "comm"), "w") as fh:
                fh.write(comm + "\n")
            with open(os.path.join(d, "stat"), "w") as fh:
                fh.write(f"{pid} ({comm} x) S {ppid} 1 1\n")
        mkproc(100, 1, "bwrap")
        mkproc(101, 100, "bwrap")
        mkproc(102, 101, "makemkvcon")
        mkproc(200, 1, "makemkvcon")
        check(scanwatch.find_makemkvcon(100, proc) == 102, "makemkvcon below flatpak run not found")
        check(scanwatch.find_makemkvcon(200, proc) == 200, "makemkvcon itself not found")
        check(scanwatch.find_makemkvcon(999, proc) is None, "a process that isn't there")
        os.symlink("socket:[555]", os.path.join(proc, "102", "fd", "42"))
        os.symlink("socket:[556]", os.path.join(proc, "102", "fd", "43"))
        os.symlink("/dev/sg2", os.path.join(proc, "102", "fd", "8"))
        head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        with open(os.path.join(proc, "102", "net", "tcp"), "w") as fh:
            fh.write(head)
            # 10.0.0.2:41290 -> 203.0.113.5:443, ours; :8080 is ours but not the server; 557 isn't ours.
            fh.write("   0: 0200000A:A14A 057100CB:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 555\n")
            fh.write("   1: 0200000A:A14B 057100CB:1F90 01 00000000:00000000 00:00000000 00000000  1000        0 556\n")
            fh.write("   2: 0200000A:A14C 057100CB:01BB 01 00000000:00000000 00:00000000 00000000  1000        0 557\n")
        check(scanwatch.server_ports(102, proc) == {41290}, f"ports: {scanwatch.server_ports(102, proc)}")
        check(scanwatch.server_ports(999, proc) is None, "an unreadable process gives None")

    # Scan times per kind of disc.
    with tempfile.TemporaryDirectory() as d:
        times = scanwatch.ScanTimes(os.path.join(d, "sub", "scan-times.json"))
        check(times.usual() == "", "a usual line with no scans")
        for s in (40, 44, 38):
            times.record("Blu-ray", s)
        times.record("4K Blu-ray", 335)
        check(times.usual() == "Usual on this drive: 4K Blu-ray 5:35, Blu-ray 0:40", times.usual())
    mem = scanwatch.ScanTimes(None)
    mem.record("DVD", 20)
    check(mem.usual() == "Usual on this drive: DVD 0:20", mem.usual())
    check(scanwatch.disc_kind(True, "Blu-ray disc") == "4K Blu-ray" and scanwatch.disc_kind(False, "DVD disc") == "DVD",
          "disc kinds")

    # The live scan: every line reaches the watch, and the parse matches the old one.
    out = "\n".join([LIBRE, 'DRV:0,2,999,1,"BD-RE PIONEER","HP","/dev/sr0"', "TCOUNT:1",
                     'CINFO:2,0,"Harry Potter"', 'TINFO:0,9,0,"2:32:21"', 'TINFO:0,11,0,"80492000000"'])

    class FakeProc:
        def __init__(self, text, hold=None):
            self.stdout = io.StringIO(text + "\n") if hold is None else _Slow(text, hold)
            self.pid = 4242
            self.returncode = None
            self.terminated = False
            self._hold = hold
            self.polls = 0

        def poll(self):
            self.polls += 1
            if (self._hold is not None and not self.terminated) or self.polls < 3:
                return None
            self.returncode = 0 if not self.terminated else -15
            return self.returncode

        def wait(self, timeout=None):
            return self.poll()

        def terminate(self):
            self.terminated = True
            if self._hold is not None:
                self._hold.set()

        def kill(self):
            self.terminate()

    seen = []

    class Watch(scanwatch.ScanWatch):
        def line(self, text, now=None):
            seen.append(text)
            super().line(text, now)

    w = Watch()
    parsed, disc = makemkv.scan_disc_parsed(Config(makemkvcon=["makemkvcon"]), 0, watch=w, cancel=threading.Event(),
                                            popen=lambda *a, **k: FakeProc(out), ports_of=lambda pid: {1, 2})
    check(len(seen) == 6 and disc.name == "Harry Potter" and disc.libredrive and disc.titles[0].duration_s == 9141,
          f"live scan: {len(seen)} lines, {disc}")
    check(w.status().finished and w.status().requests == 2, f"watch after a live scan: {w.status()}")

    # Cancelled: MakeMKV is stopped and ScanStopped raised.
    hold = threading.Event()
    cancel = threading.Event()
    w = scanwatch.ScanWatch()
    threading.Timer(0.3, cancel.set).start()
    t0 = time.monotonic()
    try:
        makemkv.scan_disc_parsed(Config(makemkvcon=["makemkvcon"]), 0, watch=w, cancel=cancel,
                                 popen=lambda *a, **k: FakeProc(LIBRE, hold), ports_of=lambda pid: None)
        check(False, "a cancelled scan returned")
    except makemkv.ScanStopped:
        pass
    check(time.monotonic() - t0 < 5 and w.status().finished, "the cancelled scan didn't stop promptly")

    for f in failures:
        print("FAIL:", f)
    print(f"scanwatch: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


class _Slow:
    """A stdout that prints one line, then nothing until the process is stopped."""

    def __init__(self, text, hold):
        self.text, self.hold = text, hold

    def __iter__(self):
        yield self.text + "\n"
        self.hold.wait(10)


def test_scanwatch():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
