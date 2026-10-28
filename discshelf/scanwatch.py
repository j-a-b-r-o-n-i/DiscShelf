"""What a disc scan is doing while MakeMKV works, for the Rip page.

A Blu-ray scan takes under a minute, but a 4K disc's first scan can take several: MakeMKV
looks up the disc's keys on its own server, one small request at a time, and the drive sits
idle meanwhile. MakeMKV prints little or nothing during that, so its output alone can't tell
a slow scan from a stuck one. ScanWatch combines the two things that can be seen: each line
MakeMKV prints, and the connections it opens to its server (read from /proc, where the
makemkvcon process is visible). It turns them into a list of steps with times, a count of
server requests, and whether the scan has stopped making progress.

Nothing here stops a scan. `stalled` only says there has been no output and no server
traffic for a while; the window asks before stopping anything.

ScanTimes keeps how long each kind of disc took to scan on this machine, so the clock can
say what's usual.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
from dataclasses import dataclass, field

from .makemkv import MSG_LIBREDRIVE, parse_line

STALL_S = 180.0          # no output and no server traffic for this long: ask whether to stop
STALL_BLIND_S = 600.0    # the same, when MakeMKV's connections can't be seen (inside the Flatpak)
KEYS_GAP_S = 10.0        # the key step is over once there have been no new requests for this long
BIN_S = 10.0             # the request chart counts requests per 10 s
BINS = 30                # and shows the last 5 minutes
SERVER_PORT = 443


@dataclass
class ScanStep:
    name: str
    state: str                   # "done" | "now" | "stuck" | "wait"
    seconds: float = 0.0
    note: str = ""


@dataclass
class ScanStatus:
    elapsed: float = 0.0
    steps: list[ScanStep] = field(default_factory=list)
    requests: int = 0            # connections to MakeMKV's server seen so far
    bins: list[int] = field(default_factory=list)    # requests per BIN_S, oldest first, the last BINS
    sees_traffic: bool = False   # False when the makemkvcon process can't be looked at
    quiet_s: float = 0.0         # seconds since the last output or server request
    stalled: bool = False
    stall_after: float = STALL_S
    task: str = ""               # MakeMKV's own name for what it is doing, if it printed one
    percent: float | None = None
    libredrive: bool = False
    getting_keys: bool = False
    finished: bool = False


class ScanWatch:
    """Fed from the thread running MakeMKV; status() may be called from any thread."""

    def __init__(self, start: float | None = None, clock=time.monotonic):
        self.clock = clock
        self.t0 = clock() if start is None else start
        self._lock = threading.Lock()
        self.last_output = self.t0
        self.first_output: float | None = None
        self.libredrive_at: float | None = None
        self.titles_at: float | None = None      # when MakeMKV started listing titles
        self.ports: set[int] = set()
        self.request_times: list[float] = []
        self.sees_traffic = False
        self.task = ""
        self.percent: float | None = None
        self.end: float | None = None

    # -- feeding -----------------------------------------------------------
    def line(self, text: str, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        parsed = parse_line(text)
        with self._lock:
            self.last_output = now
            if not parsed:
                return
            kind, f = parsed
            if self.first_output is None and kind not in ("DRV",):
                self.first_output = now
            if kind == "MSG" and len(f) >= 4 and _int(f[0]) == MSG_LIBREDRIVE and "LibreDrive" in f[3]:
                self.libredrive_at = self.libredrive_at or now
            elif kind in ("TCOUNT", "CINFO", "TINFO") and self.titles_at is None:
                self.titles_at = now
            elif kind in ("PRGC", "PRGT") and len(f) >= 3 and f[2]:
                self.task = f[2]
            elif kind == "PRGV" and len(f) >= 3 and _int(f[2]) > 0:
                self.percent = min(100.0, 100.0 * _int(f[0]) / _int(f[2]))

    def connections(self, ports: set[int] | None, now: float | None = None) -> None:
        """The local ports of makemkvcon's connections to its server right now, or None when the
        process can't be looked at. Each new port is one request."""
        now = self.clock() if now is None else now
        with self._lock:
            if ports is None:
                return
            self.sees_traffic = True
            for p in ports - self.ports:
                self.request_times.append(now)
            self.ports |= ports

    def finish(self, now: float | None = None) -> None:
        with self._lock:
            self.end = self.clock() if now is None else now

    # -- reading -----------------------------------------------------------
    def status(self, now: float | None = None) -> ScanStatus:
        now = self.clock() if now is None else now
        with self._lock:
            if self.end is not None:
                now = self.end
            el = now - self.t0
            last_req = self.request_times[-1] if self.request_times else None
            quiet = now - max(self.last_output, last_req or self.t0)
            stall_after = STALL_S if self.sees_traffic else STALL_BLIND_S
            stalled = self.end is None and quiet >= stall_after
            reqs = list(self.request_times)
            first_req = reqs[0] if reqs else None
            keys_end = None
            if first_req is not None:
                if self.titles_at is not None and self.titles_at >= first_req:
                    keys_end = min(self.titles_at, last_req + 1.0)
                elif self.end is not None or (now - last_req >= KEYS_GAP_S and self.last_output > last_req):
                    keys_end = last_req + 1.0     # MakeMKV printed something after its last request
            getting_keys = first_req is not None and keys_end is None
            steps: list[ScanStep] = []
            # Opening the disc: until the first request, LibreDrive, or the title list.
            marks = [t for t in (first_req, self.libredrive_at, self.titles_at) if t is not None]
            open_end = min(marks) if marks else (self.end if self.end is not None else None)
            steps.append(ScanStep("Opening the disc", "done" if open_end is not None else ("stuck" if stalled else "now"),
                                  (open_end if open_end is not None else now) - self.t0))
            if self.libredrive_at is not None:
                steps.append(ScanStep("LibreDrive is on, so 4K discs can be read", "done", 0.0))
            if first_req is not None:
                n = len(reqs)
                span = (keys_end if keys_end is not None else now) - first_req
                if keys_end is not None:
                    state, note = "done", f"MakeMKV sent {n} requests. The keys are saved, so scanning this disc again is quicker."
                elif stalled:
                    state = "stuck"
                    note = f"MakeMKV sent {n} requests, then nothing for {fmt_clock(quiet)}: no output and no requests."
                else:
                    state = "now"
                    rate = n / span if span > 5 else 0
                    every = f", about {rate:.0f} a second" if rate >= 0.95 else ""
                    note = f"MakeMKV has sent {n} requests so far{every}. The drive is waiting, not reading."
                steps.append(ScanStep("Getting this disc's keys from MakeMKV's server", state, span, note))
            read_from = keys_end if keys_end is not None else open_end
            if first_req is not None and keys_end is None:
                steps.append(ScanStep("Reading the titles and their tracks", "wait"))
            elif read_from is not None:
                done = self.end is not None
                bits = []
                if self.task and not done:
                    bits.append(f"MakeMKV: {self.task}")
                if self.percent is not None and not done:
                    bits.append(f"{self.percent:.0f}%")
                steps.append(ScanStep("Reading the titles and their tracks",
                                      "done" if done else ("stuck" if stalled else "now"),
                                      (self.end if done else now) - read_from, " · ".join(bits)))
            else:
                steps.append(ScanStep("Reading the titles and their tracks", "wait"))
            bins = [0] * BINS
            for t in reqs:
                k = int((now - t) // BIN_S)
                if 0 <= k < BINS:
                    bins[BINS - 1 - k] += 1
            return ScanStatus(elapsed=el, steps=steps, requests=len(reqs), bins=bins,
                              sees_traffic=self.sees_traffic, quiet_s=quiet, stalled=stalled,
                              stall_after=stall_after, task=self.task, percent=self.percent,
                              libredrive=self.libredrive_at is not None, getting_keys=getting_keys,
                              finished=self.end is not None)


# --------------------------------------------------------------------------- /proc


def _children(proc: str) -> dict[int, list[int]]:
    kids: dict[int, list[int]] = {}
    try:
        names = os.listdir(proc)
    except OSError:
        return kids
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc, name, "stat")) as fh:
                stat = fh.read()
        except OSError:
            continue
        # The command name is in parentheses and may hold spaces; the parent pid follows it.
        rest = stat[stat.rfind(")") + 2:].split()
        if len(rest) >= 2 and rest[1].isdigit():
            kids.setdefault(int(rest[1]), []).append(int(name))
    return kids


def find_makemkvcon(pid: int, proc: str = "/proc") -> int | None:
    """The makemkvcon process started by pid: pid itself, or a descendant (`flatpak run` starts
    it a few processes down). None when it isn't visible, as from inside a Flatpak, where it
    runs on the host through flatpak-spawn."""
    def comm(p: int) -> str:
        try:
            with open(os.path.join(proc, str(p), "comm")) as fh:
                return fh.read().strip()
        except OSError:
            return ""

    if comm(pid) == "makemkvcon":
        return pid
    kids = _children(proc)
    todo = list(kids.get(pid, []))
    while todo:
        p = todo.pop()
        if comm(p) == "makemkvcon":
            return p
        todo.extend(kids.get(p, []))
    return None


def server_ports(pid: int, proc: str = "/proc", port: int = SERVER_PORT) -> set[int] | None:
    """Local ports of pid's TCP connections to a remote port 443, or None if pid can't be read."""
    fd_dir = os.path.join(proc, str(pid), "fd")
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return None
    inodes = set()
    for fd in fds:
        try:
            target = os.readlink(os.path.join(fd_dir, fd))
        except OSError:
            continue
        if target.startswith("socket:["):
            inodes.add(target[8:-1])
    out: set[int] = set()
    if not inodes:
        return out
    for name in ("tcp", "tcp6"):
        try:
            with open(os.path.join(proc, str(pid), "net", name)) as fh:
                rows = fh.read().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            if len(cols) < 10 or cols[9] not in inodes:
                continue
            local, remote = cols[1], cols[2]
            if int(remote.rsplit(":", 1)[1], 16) == port:
                out.add(int(local.rsplit(":", 1)[1], 16))
    return out


# --------------------------------------------------------------------------- history


class ScanTimes:
    """How long scans took on this machine, by kind of disc ("4K Blu-ray", "Blu-ray", "DVD")."""

    KEEP = 20

    def __init__(self, path: str | None):
        self.path = path            # None keeps the times in memory only
        self._mem: dict[str, list[float]] = {}

    def _load(self) -> dict[str, list[float]]:
        if self.path is None:
            return {k: list(v) for k, v in self._mem.items()}
        try:
            with open(self.path) as fh:
                data = json.load(fh)
            return {k: [float(x) for x in v] for k, v in data.items() if isinstance(v, list)}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def record(self, kind: str, seconds: float) -> None:
        data = self._load()
        data[kind] = (data.get(kind, []) + [round(seconds, 1)])[-self.KEEP:]
        if self.path is None:
            self._mem = data
            return
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass                # only used for the "usual" line; never worth an error

    def usual(self) -> str:
        """'Usual on this drive: Blu-ray 0:40, 4K Blu-ray 5:35', or '' with no scans yet."""
        data = self._load()
        parts = [f"{k} {fmt_clock(statistics.median(v))}" for k, v in sorted(data.items()) if v]
        return "Usual on this drive: " + ", ".join(parts) if parts else ""


def disc_kind(uhd: bool, disc_type: str) -> str:
    if uhd:
        return "4K Blu-ray"
    t = (disc_type or "").lower()
    return "DVD" if "dvd" in t else "Blu-ray" if "blu" in t else (disc_type or "Disc")


def fmt_clock(s: float) -> str:
    s = max(0, int(round(s)))
    return f"{s // 60}:{s % 60:02d}"


def _int(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return 0
