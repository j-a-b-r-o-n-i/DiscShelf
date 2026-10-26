"""Copy rips to the NAS share with rsync, check the copy, and decide when the local rip can go.

The NAS shares are CIFS mounts, which has three consequences for the rsync flags:
CIFS cannot store Unix owners or modes (so those are not sent), a dropped
connection must resume rather than start a 60 GB file again, and a half-copied
file must never sit under its final name where Plex would scan it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import re
import subprocess
import threading
from typing import Callable

Progress = Callable[[float, str], None]

PARTIAL_DIR = ".discshelf-partial"   # hidden, so Plex skips it; rsync removes it when empty

RSYNC_FLAGS = [
    "-t",                            # keep modification times; nothing else CIFS can store
    # Always compare contents: rsync otherwise skips a file whose size and time match one
    # already there, which a different film of the same name and size could. With
    # --no-whole-file only the differing blocks are sent, so a resumed copy stays cheap.
    "--ignore-times",
    "--no-perms", "--no-owner", "--no-group",
    # An interrupted copy is kept in PARTIAL_DIR beside the destination and used as the
    # basis next time, instead of appearing half-written under the real name.
    f"--partial-dir={PARTIAL_DIR}",
    # rsync defaults to whole-file copies between local paths, which would ignore that
    # partial file; the delta algorithm lets a resumed copy skip what is already there.
    "--no-whole-file",
    "--timeout=300",                 # fail instead of hanging forever when the share goes away
    "--info=progress2",
]

CHUNK = 8 * 1024 * 1024

_PROGRESS = re.compile(r"^\s*([\d,]+)\s+(\d+)%\s+([\d.]+)([kMGT]?B)/s\s+(\d+:\d\d:\d\d)")
_UNITS = {"B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}   # rsync's are binary


class Cancelled(Exception):
    """The copy was cancelled by the user."""


class CopyError(RuntimeError):
    """rsync failed; the message carries what it printed on stderr."""


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} TB"


def _eta(hms: str) -> str:
    h, m, s = (int(x) for x in hms.split(":"))
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_progress(line: str, total: int | None = None) -> tuple[float, str] | None:
    """One rsync --info=progress2 line as (fraction, "1.2 GB of 4.0 GB · 95 MB/s · 0:30 left")."""
    m = _PROGRESS.match(line)
    if not m:
        return None
    done, pct = int(m.group(1).replace(",", "")), int(m.group(2))
    rate = float(m.group(3)) * _UNITS[m.group(4)]
    if not total:
        total = int(done * 100 / pct) if pct else 0
    frac = min(done / total, 1.0) if total else pct / 100
    finished = "xfr#" in line or frac >= 1.0
    rate_txt = f"{rate / 1e6:.0f} MB/s" if rate >= 1e6 else f"{rate / 1e3:.0f} KB/s"
    text = f"{human_bytes(done)} of {human_bytes(total)} · {rate_txt}"
    if not finished:
        text += f" · {_eta(m.group(5))} left"
    return frac, text


def copy(src: str, dest_path: str, progress: Progress, cancel: threading.Event | None = None,
         popen=subprocess.Popen, rsync: str = "rsync") -> None:
    """Copy one file to dest_path (a full file path on the mounted share), resuming if possible."""
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    total = os.path.getsize(src)
    cmd = [rsync, *RSYNC_FLAGS, src, dest_path]
    proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    err_chunks: list[bytes] = []
    # stderr is drained on its own thread so a chatty rsync can never block on a full pipe.
    err_thread = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)
    err_thread.start()
    stop = threading.Event()
    if cancel is not None:
        def watch():
            while not stop.wait(0.2):
                if cancel.is_set():
                    proc.terminate()
                    return
        threading.Thread(target=watch, daemon=True).start()
    try:
        buf = b""
        while True:
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            # progress2 rewrites one line with carriage returns.
            *lines, buf = re.split(rb"[\r\n]", buf)
            for raw in lines:
                parsed = parse_progress(raw.decode(errors="replace"), total)
                if parsed:
                    progress(*parsed)
        rc = proc.wait()
    finally:
        stop.set()
    err_thread.join(timeout=5)
    if cancel is not None and cancel.is_set():
        raise Cancelled("copy cancelled")
    if rc != 0:
        msg = b"".join(err_chunks).decode(errors="replace").strip()
        raise CopyError(f"rsync failed (exit {rc}): {msg[-2000:] or 'no error message'}")
    progress(1.0, f"{human_bytes(total)} copied")


def sha256(path: str, progress: Progress | None = None, chunk: int = CHUNK) -> str:
    total = os.path.getsize(path) or 1
    h = hashlib.sha256()
    done = 0
    with open(path, "rb", buffering=0) as fh:
        try:
            # Drop cached pages so a read through the CIFS mount fetches from the NAS
            # rather than handing back what this machine just wrote.
            os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
            done += len(block)
            if progress:
                progress(done / total, f"Checking {human_bytes(done)} of {human_bytes(total)}")
    return h.hexdigest()


def verify(src: str, dst: str, mode: str, src_hash: str | None = None,
           progress: Progress | None = None) -> bool:
    """"size": the sizes match. "readback": also the NAS copy, read back through the mount, hashes the same."""
    try:
        if os.path.getsize(src) != os.path.getsize(dst):
            return False
    except FileNotFoundError:
        return False
    if mode == "size":
        return True
    if mode != "readback":
        raise ValueError(f"unknown verify mode {mode!r}")
    if not src_hash:
        src_hash = sha256(src, (lambda f, t: progress(f / 2, t)) if progress else None)
        part = (lambda f, t: progress(0.5 + f / 2, t)) if progress else None
    else:
        part = progress
    return sha256(dst, part) == src_hash


def due_for_cleanup(copied_at_iso: str, policy: str, now: dt.datetime | None = None) -> bool:
    """Whether a rip copied (and verified) at copied_at_iso may be deleted locally under this policy."""
    if not copied_at_iso or policy == "keep":
        return False
    if policy == "delete":
        return True
    if policy != "7days":
        raise ValueError(f"unknown clean-up policy {policy!r}")
    copied = dt.datetime.fromisoformat(copied_at_iso)
    now = now or dt.datetime.now(copied.tzinfo)
    if (copied.tzinfo is None) != (now.tzinfo is None):
        # Mixing naive and aware times raises; treat naive ones as local time.
        copied, now = copied.astimezone(), now.astimezone()
    return now - copied >= dt.timedelta(days=7)
