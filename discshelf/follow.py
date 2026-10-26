"""Copy a file to the NAS while MakeMKV is still writing it.

A film is one 30-70 GB file, so copying it only after the rip adds most of an hour.
This copier follows the growing file instead: each whole chunk is sent once the source
has grown past it, flushed, read back through the mount and compared. MakeMKV rewrites
the start of the file (and possibly other parts) when it finishes, so once the rip is
done every chunk is hashed again and any that changed is sent again. The copy sits
under "<name>.partial" until all of that has checked out, so Plex never sees half a file.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .transfer import Cancelled, CopyError

FollowProgress = Callable[[int, int, float], None]

PARTIAL_SUFFIX = ".partial"
GIVE_UP_S = 300.0            # a NAS that stays unreachable this long is reported, not waited on
MAX_BACKOFF_S = 30.0


class FollowError(CopyError):
    """The NAS copy could not be completed; the message says why."""


@dataclass
class FollowResult:
    bytes: int               # size of the finished file
    chunks_resent: int       # chunks that changed after they were first copied
    seconds: float
    sha256: str              # of the whole finished source file, which the copy matches


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _Follower:
    def __init__(self, src, dst, done, progress, cancel, chunk, poll, verify, opener, sleep, now):
        self.src, self.part = src, dst + PARTIAL_SUFFIX
        self.dst = dst
        self.done, self.progress, self.cancel = done, progress, cancel
        self.chunk, self.poll, self.verify = chunk, poll, verify
        self.opener, self.sleep, self.now = opener, sleep, now
        self.hashes: list[str] = []      # source sha256 of each chunk that is on the NAS and read back
        self.fh = None                   # the .partial, open for writing
        self.copied = 0                  # bytes on the NAS that read back correctly
        self.checked = 0                 # bytes confirmed against the finished source
        self.t_last = now()
        self.bytes_since = 0

    # --- NAS side, every call retried -------------------------------------------------

    def _check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled("copy cancelled")

    def _nap(self, seconds: float):
        # Sleep in short steps so a cancel during a long backoff is noticed.
        end = self.now() + seconds
        while True:
            self._check_cancel()
            left = end - self.now()
            if left <= 0:
                return
            self.sleep(min(left, self.poll))

    def _close(self):
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None

    def _retry(self, what: str, fn):
        """Run fn until it succeeds; NAS errors are retried with backoff for GIVE_UP_S."""
        first = None
        delay = 1.0
        while True:
            self._check_cancel()
            try:
                return fn()
            except (OSError, _Mismatch) as e:
                self._close()      # the handle may be dead; reopen from scratch next time
                t = self.now()
                first = t if first is None else first
                if t - first >= GIVE_UP_S:
                    raise FollowError(f"The NAS copy of {os.path.basename(self.dst)} stopped while "
                                      f"{what}, and the NAS was still failing after "
                                      f"{GIVE_UP_S / 60:.0f} minutes: {e}") from e
                self._nap(delay)
                delay = min(delay * 2, MAX_BACKOFF_S)

    def _open(self):
        if self.fh is None:
            os.makedirs(os.path.dirname(os.path.abspath(self.part)), exist_ok=True)
            try:
                self.fh = self.opener(self.part, "r+b")
            except FileNotFoundError:
                self.fh = self.opener(self.part, "w+b")
        return self.fh

    def _read_back(self, offset: int, length: int) -> bytes:
        with self.opener(self.part, "rb") as rf:
            try:
                # Drop cached pages so the read through the CIFS mount comes from the NAS,
                # not from what this machine just wrote.
                os.posix_fadvise(rf.fileno(), offset, length, os.POSIX_FADV_DONTNEED)
            except (AttributeError, OSError, ValueError):
                pass
            rf.seek(offset)
            return rf.read(length)

    def _put(self, index: int, data: bytes, digest: str):
        """Write chunk `index` at its offset, fsync it, and read it back."""
        offset = index * self.chunk

        def attempt():
            fh = self._open()
            fh.seek(offset)
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            if self.verify:
                back = self._read_back(offset, len(data))
                if _sha(back) != digest:
                    raise _Mismatch(f"chunk at {offset} bytes read back differently")
        self._retry("writing", attempt)

    def _truncate(self, size: int):
        def attempt():
            fh = self._open()
            fh.truncate(size)
            fh.flush()
            os.fsync(fh.fileno())
        self._retry("trimming the file", attempt)

    # --- source side ------------------------------------------------------------------

    def _src_size(self) -> int | None:
        try:
            return os.path.getsize(self.src)
        except FileNotFoundError:
            return None

    def _src_chunk(self, index: int, length: int | None = None) -> bytes:
        with open(self.src, "rb") as fh:
            fh.seek(index * self.chunk)
            return fh.read(self.chunk if length is None else length)

    def _report(self, nbytes: int):
        t = self.now()
        self.bytes_since += nbytes
        dt = t - self.t_last
        rate = self.bytes_since / dt if dt > 0 else 0.0
        if dt >= 1.0:
            self.t_last, self.bytes_since = t, 0
        self.progress(self.copied, self.checked, rate)

    # --- the copy ---------------------------------------------------------------------

    def _resume(self):
        """Keep the leading whole chunks of an earlier .partial that still match the source."""
        try:
            have = os.path.getsize(self.part)
        except FileNotFoundError:
            return
        except OSError:
            have = 0
        src_size = self._src_size() or 0
        i = 0
        while (i + 1) * self.chunk <= min(have, src_size):
            self._check_cancel()
            try:
                back = self._read_back(i * self.chunk, self.chunk)
            except OSError:
                break
            digest = _sha(self._src_chunk(i))
            if _sha(back) != digest:
                break
            self.hashes.append(digest)
            i += 1
        self.copied = len(self.hashes) * self.chunk
        if have != self.copied:
            self._truncate(self.copied)

    def _send_next(self, length: int):
        index = len(self.hashes)
        data = self._src_chunk(index, length)
        if len(data) != length:
            raise FollowError(f"{os.path.basename(self.src)} shrank while it was being copied")
        digest = _sha(data)
        self._put(index, data, digest)
        self.hashes.append(digest)
        self.copied = index * self.chunk + length
        self._report(length)

    def run(self) -> FollowResult:
        start = self.now()
        self._resume()

        # Follow the growing file, sending only whole chunks: the last one may still change.
        while True:
            self._check_cancel()
            finished = self.done.is_set()   # read before the size, so the size is final when it says so
            size = self._src_size()
            if size is None:
                if finished:
                    raise FollowError(f"{self.src} is missing; MakeMKV finished without leaving it")
            elif self.copied + self.chunk <= size:
                self._send_next(self.chunk)
                continue
            if finished:
                break
            self.sleep(self.poll)

        # The rip is finished: hash every chunk again, resend what changed, add the tail.
        size = self._src_size() or 0
        if size < self.copied:
            self._truncate(size)
            self.copied = size
        whole = hashlib.sha256()
        resent = 0
        n_chunks = (size + self.chunk - 1) // self.chunk
        for i in range(n_chunks):
            self._check_cancel()
            data = self._src_chunk(i)
            whole.update(data)
            digest = _sha(data)
            if i < len(self.hashes) and self.hashes[i] == digest:
                self.checked += len(data)
                self._report(0)
                continue
            if i < len(self.hashes):
                resent += 1
            self._put(i, data, digest)
            if i < len(self.hashes):
                self.hashes[i] = digest
            else:
                self.hashes.append(digest)
            self.copied = max(self.copied, i * self.chunk + len(data))
            self.checked += len(data)
            self._report(len(data))
        del self.hashes[n_chunks:]
        if self.copied != size:
            self._truncate(size)
            self.copied = size

        def finish():
            self._open()
            self._close()
            if os.path.getsize(self.part) != size:
                raise _Mismatch(f"the copy is {os.path.getsize(self.part)} bytes, not {size}")
            os.replace(self.part, self.dst)
        self._retry("finishing", finish)
        return FollowResult(bytes=size, chunks_resent=resent, seconds=self.now() - start,
                            sha256=whole.hexdigest())


class _Mismatch(Exception):
    """What was read back from the NAS is not what was written; treated like a network error."""


def stream_copy(src: str, dst: str, done: threading.Event, progress: FollowProgress,
                cancel: threading.Event | None = None, *, chunk: int = 64 * 1024 * 1024,
                poll: float = 1.0, verify: bool = True, opener=open, sleep=time.sleep,
                now=time.monotonic) -> FollowResult:
    """Copy src to dst while another process may still be writing src.

    done: set once the writer has finished (makemkvcon exited successfully).
    progress(copied_bytes, checked_bytes, rate_bytes_per_s) after each chunk.
    copied_bytes is what is on the NAS and read back correctly; checked_bytes stays 0
    while following and counts up during the final pass, as each chunk is confirmed
    against the finished source. opener opens the NAS-side files (it is open() outside
    tests). Raises transfer.Cancelled when cancel is set, leaving dst + ".partial" for a
    later call to resume from, and FollowError when the NAS stays unreachable.
    """
    f = _Follower(src, dst, done, progress, cancel, chunk, poll, verify, opener, sleep, now)
    try:
        return f.run()
    finally:
        f._close()
