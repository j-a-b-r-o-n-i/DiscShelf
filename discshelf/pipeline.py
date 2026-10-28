"""The controller the GUI and CLI drive: scan → rip → match → copy → Plex, with jobs kept on disk.

Every method blocks; the GUI runs them on worker threads. progress callbacks take
(fraction 0..1, one-line status) and may be called from any thread.

While rip() runs, finished titles are post-processed and copied to the NAS by one
background worker, and the UI thread may call suggest_matches, update_matches and
confirm_matches at the same time. Controller.job_lock guards a job's matches, files
and titles_progress: this module holds it whenever it changes them, and the GUI holds
it while reading them. No callback (progress, samples) is ever called with it held.
"""
from __future__ import annotations

import collections
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy as _copy
import dataclasses
from dataclasses import dataclass, field
import datetime as _dt
import inspect
import math
import os
import posixpath
import queue
import re
import shutil
import threading
import time
import traceback
from typing import Callable

from . import (av1, caseocr, classify, config, discdb, discinfo, hdr, makemkv, match, naming, ocr, theme, titlecard,
               transfer, watch)
from . import nasfix, play, ripstats, scanwatch, season
from .config import Config
from .jobs import JobStore, now_iso
from .models import Disc, Drive, EncodeJob, Episode, Job, Match, RippedFile, Sample, Span, Title, TitleProgress
from .plex import Plex, PlexError
from .tvmaze import TvMaze, TvMazeError

try:
    from . import follow
except ImportError:     # without it a film is copied after its rip, like any other title
    follow = None

Progress = Callable[[float, str], None]
SCAN_MIN_SECONDS = 120     # shorter titles are menus and logos; scan and rip must agree on this
BLURAY_1X = 4.5e6          # bytes per second at 1× Blu-ray speed
RIP_WINDOW = 20.0          # seconds the rip speed is averaged over; MakeMKV writes in 32 MB blocks
RIP_SHARE = 0.9            # of the overall progress bar; the rest is what is left to do after the rip
LOCKED_STATES = ("copying", "checking", "verified")      # the match can no longer change
WAITING_STATES = ("ripped", "waiting_match", "skipped")  # ripped and not on the NAS: may be queued for copying
ENCODE_ACTIVE = ("waiting", "encoding", "paused", "checking", "copying")   # an AV1 version still to be made
ENCODE_RUNNING = ("encoding", "checking", "copying")
ENCODE_FINISHED = ("verified", "skipped")    # the remux may be deleted; a failed one keeps it until given up
NIGHT_HOURS = (1, 7)                         # av1_when "night": encodes run from 01:00 to 07:00 local time


def _resolve_tool(name: str) -> str:
    """The installer puts dovi_tool in ~/.local/bin, which a desktop launcher's PATH may not include."""
    if shutil.which(name):
        return name
    local = os.path.expanduser(f"~/.local/bin/{name}")
    return local if os.access(local, os.X_OK) else name


def _guess(label: str, pattern: str) -> int | None:
    m = re.search(pattern, label, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _scaled(progress: Progress, lo: float, hi: float) -> Progress:
    return lambda f, text: progress(lo + (hi - lo) * max(0.0, min(1.0, f)), text)


def _rip_accepts(name: str, fn=None) -> bool:
    """Whether makemkv.rip_titles (or fn) takes this keyword, so an older MakeMKV wrapper still works."""
    try:
        params = inspect.signature(fn or makemkv.rip_titles).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


def _tp(job: Job, ti: int) -> TitleProgress | None:
    return next((p for p in job.titles_progress if p.title_index == ti), None)


def _file(job: Job, ti: int) -> RippedFile | None:
    return next((f for f in job.files if f.title_index == ti), None)


def _enc(job: Job, ti: int) -> EncodeJob | None:
    return next((e for e in job.encodes if e.title_index == ti), None)


def _encode_holds(job: Job, ti: int) -> bool:
    """The title's AV1 version isn't finished, so its local remux must stay: it's encoded from it."""
    e = _enc(job, ti)
    return e is not None and e.state not in ENCODE_FINISHED


def _width_of(resolution: str) -> int:
    try:
        return int((resolution or "").lower().split("x")[0])
    except ValueError:
        return 0


def _video_width(job: Job, ti: int) -> int:
    """The title's video width, from the scan's video stream or else the ripped file's probe."""
    t = next((t for t in job.titles if t.index == ti), None)
    for st in t.streams if t else []:
        if st.kind == "video" and _width_of(st.resolution):
            return _width_of(st.resolution)
    f = _file(job, ti)
    return _width_of(f.hdr.resolution) if f is not None and f.hdr is not None else 0


def _score(score: float, threshold: float) -> str:
    """89.4 -> "89"; 92.6 against 93 -> "92.6", so a near miss doesn't read as a pass."""
    text = f"{score:.0f}"
    return f"{score:.1f}" if float(text) >= threshold > score else text


def _hours(seconds: float) -> str:
    """"3 h 05 min", "40 min"."""
    m = max(1, int(round(seconds / 60)))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _remove_av1_files(e: EncodeJob) -> None:
    """The local AV1 file and the partial files av1.encode writes beside it."""
    for p in (e.path, e.path + ".encode.partial", e.path + ".partial") if e.path else ():
        try:
            os.remove(p)
        except OSError:
            pass


def _mark_waiting(tp: TitleProgress, m: Match | None, card_pending: bool = False) -> bool:
    """Say why a ripped title isn't being copied. Returns whether anything changed."""
    if card_pending and m is not None and m.ready:
        state, note = "waiting_match", "It waits while its title card is read, which may change its match."
    elif m is not None and m.target == "skip":
        state, note = "skipped", "It's set to skip, so it isn't copied."
    elif m is None:
        state, note = "waiting_match", "It waits until it's matched to an episode, the film or an extra."
    elif m.held:
        state, note = "waiting_match", f"{m.held} It waits until that's settled on the Match page."
    else:
        state, note = "waiting_match", "It waits until you confirm its match."
    changed = (tp.state, tp.note) != (state, note)
    tp.state, tp.note = state, note
    return changed


def _unit(total: float) -> tuple[float, str]:
    for size, name in ((1e12, "TB"), (1e9, "GB"), (1e6, "MB"), (1e3, "KB")):
        if total >= size:
            return size, name
    return 1, "B"


def _of(done: float, total: float) -> str:
    """"9.8 of 22.6 GB", both in the total's unit."""
    size, name = _unit(total)
    if size == 1:
        return f"{int(done)} of {int(total)} B"
    return f"{done / size:.1f} of {total / size:.1f} {name}"


def _clock(seconds: float) -> str:
    s = int(round(seconds))
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _copy_text(done: float, total: float, rate: float) -> str:
    """"9.8 of 22.6 GB · 111 MB/s · 1:55 left"."""
    text = _of(done, total)
    if rate > 0:
        text += f" · {rate / 1e6:.0f} MB/s" if rate >= 1e6 else f" · {rate / 1e3:.0f} KB/s"
        if total > done:
            text += f" · {_clock((total - done) / rate)} left"
    return text


class _Meter:
    """A byte counter that works out the rate over the last few seconds and emits a Sample about once an interval."""

    def __init__(self, kind: str, sink: Callable[[Sample], None] | None, interval: float, t0: float,
                 window: float = 3.0):
        self.kind, self.sink, self.interval, self.t0, self.window = kind, sink, interval, t0, window
        self.points: collections.deque[tuple[float, int]] = collections.deque()
        self.rate = 0.0
        self.total = 0
        self._last: float | None = None
        self._lock = threading.Lock()

    def restart(self) -> None:
        """Forget the readings so far, so time spent with nothing to read isn't averaged in."""
        with self._lock:
            self.points.clear()
            self.rate = 0.0

    def update(self, total: int) -> None:
        now = time.monotonic()
        with self._lock:
            self.total = total = max(int(total), self.total)
            self.points.append((now, total))
            while len(self.points) > 2 and now - self.points[1][0] >= self.window:
                self.points.popleft()
            t_old, b_old = self.points[0]
            if now - t_old > 0:
                self.rate = (total - b_old) / (now - t_old)
            due = self.sink is not None and (self._last is None or now - self._last >= self.interval)
            if due:
                self._last = now
                sample = Sample(t=now - self.t0, kind=self.kind, rate=self.rate, total=total)
        if due:
            self.sink(sample)


class _Emitter:
    """The rip's overall progress callback, kept from going backwards when two threads report."""

    def __init__(self, job: Job, progress: Progress):
        self.job, self.progress, self.last = job, progress, 0.0
        self._lock = threading.Lock()

    def __call__(self, f: float, text: str) -> None:
        with self._lock:
            self.last = f = max(self.last, min(1.0, f))
            self.job.progress, self.job.status = f, text
        self.progress(f, text)


class _Run:
    """One rip in progress: how it copies, and the queue its post-process-and-copy worker reads."""

    def __init__(self, job: Job, mode: str, cancel: threading.Event, interval: float,
                 samples: Callable[[Sample], None] | None, progress: Progress):
        self.job, self.mode, self.cancel = job, mode, cancel
        self.t0 = t0 = time.monotonic()
        self.rip_meter = _Meter("rip", samples, interval, t0, window=RIP_WINDOW)
        self.copy_meter = _Meter("copy", samples, interval, t0)
        self.sink = samples
        self.spans: dict[tuple[str, int], Span] = {}    # the rip timeline, by (kind, title)
        self._span_lock = threading.Lock()
        self.emit = _Emitter(job, progress)
        self.q: queue.Queue = queue.Queue()      # ("post" | "copy", title_index), None to finish
        self.thread: threading.Thread | None = None
        self.seen: set[int] = set()              # titles MakeMKV has finished
        self.posted: set[int] = set()            # titles whose language and HDR steps are done
        self.queued: set[int] = set()            # titles with a copy waiting in the queue
        self.started = 0
        self.bytes_seen = False
        self.rip_over = False
        self.tail_total = 1                      # work items left when the rip ended, for the progress bar
        self.tail_done = 0
        self.post_errors: list[Exception] = []
        self.copy_errors: list[tuple[int, Exception]] = []
        # A film copied while it rips:
        self.stream_done = threading.Event()     # MakeMKV has finished writing the file
        self.follow_cancel = threading.Event()
        self.follow_thread: threading.Thread | None = None
        self.follow_result = None
        self.follow_error: BaseException | None = None
        # Title cards, read on their own thread so OCR never holds up a copy that's ready:
        self.cards_q: queue.Queue = queue.Queue()    # (title_index, path), None to finish
        self.cards_thread: threading.Thread | None = None
        self.card_pending: set[int] = set()          # ripped titles whose card isn't read yet

    def span(self, kind: str, ti: int, end: bool = False, size: int = 0) -> None:
        """Start (or with end=True, finish) one step of a title on the timeline, and send it on."""
        now = time.monotonic() - self.t0
        with self._span_lock:
            sp = self.spans.get((kind, ti))
            if end:
                if sp is None or sp.end is not None:
                    return
                sp = dataclasses.replace(sp, end=now, bytes=size or sp.bytes)
            else:
                sp = Span(kind, ti, now)
            self.spans[(kind, ti)] = sp
        if self.sink is not None:
            try:
                self.sink(sp)
            except Exception:  # noqa: BLE001 -- a failing display never stops a rip
                traceback.print_exc()

    def timeline(self) -> list[Span]:
        with self._span_lock:
            return list(self.spans.values())


@dataclass
class CardStatus:
    """Where one title's card reading is, for the Match page."""
    state: str = "waiting"       # "waiting" | "reading" | "read" | "none" (read, no card found) | "failed" | "stopped"
    started: float = 0.0         # time.monotonic() when reading began
    seconds: float = 0.0         # how long the reading took
    text: str = ""               # what the card read, as OCR saw it
    sure: bool = False           # the card names one episode clearly
    note: str = ""               # why it failed
    t: float | None = None       # seconds into the title where the card naming its episode was on screen


@dataclass
class CheckRow:
    """One title waiting for the user to decide its match."""
    title_index: int
    read: str                    # what its title card read ("" when no card was read)
    note: str                    # the match's note, one sentence
    current: Episode | None      # the episode it's matched to now
    options: list[tuple[Episode, str]]   # candidate episodes, current first, each with a one-sentence reason
    held: str = ""               # it shares its episode with another title (Match.held); settled on the Match page


@dataclass
class QueueItem:
    job: Job
    section: str                 # "needs" | "working" | "done"
    reason: str                  # one sentence (or two) for the card
    checks: list[CheckRow]       # section "needs": titles waiting for a decision
    failed: list[tuple[int, str]]    # (title_index, why) copies that failed
    ready_to_copy: int           # titles matched and ready but not copied
    local_bytes: int             # size of this job's files still in its rip folder, AV1 versions included
    safe_bytes: int              # of those, already verified on the NAS (a remux only once its AV1 version is done)
    done_at: str                 # ISO time it reached Plex, "" if not (with AV1 versions: when the last one did)
    encodes: list[EncodeJob] = field(default_factory=list)   # the job's AV1 versions, the live objects


@dataclass
class EncodeStatus:
    """The AV1 encoder across every disc, for the window."""
    job_id: str = ""             # the disc whose encode is running (or paused), "" when none
    title_index: int | None = None
    state: str = ""              # that encode's state, "" when none
    waiting: int = 0             # encodes waiting their turn, every disc
    gate_open: bool = True       # the "when to encode" setting lets an encode run now
    note: str = ""               # why encodes wait, when that setting holds them back


@dataclass
class SeasonEp:
    number: int
    title: str
    state: str                   # "plex" (verified on the NAS) | "ripping" | "ripped" (not on the NAS yet) | "todo"
    disc: int | None             # disc number it came from, when known


@dataclass
class SeasonView:
    show: str
    show_id: int | None
    season: int
    episodes: list[SeasonEp]
    in_plex: int
    next_disc: int | None        # the disc to insert next, None when every episode is on a disc already, or unknown
    next_text: str               # one sentence, e.g. "Next: disc 3. E07–E09 aren't ripped yet."


@dataclass
class QueueView:
    items: list[QueueItem]       # needs first, then working, then done (newest first within each)
    seasons: list[SeasonView]    # seasons with some but not all episodes in Plex
    local_bytes: int             # everything in the rip folder (cfg.rip_path)
    safe_bytes: int              # of that, verified on the NAS and safe to delete


DONE_HOURS = 24              # a finished disc stays in the queue this long, then only History shows it
RECENT_DAYS = 30             # only seasons touched this recently are looked up on TVmaze for the season view
EPISODES_RETRY = 300.0       # seconds before an episode list that failed to load (offline) is asked for again
REVERIFY_NOTE = "Checked again"
_STOPWORDS = {"the", "and", "of", "a", "an", "to", "in", "on", "for", "with"}


def _plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _isnt(n: int) -> str:
    return "isn't" if n == 1 else "aren't"


def _ep_code(e: Episode) -> str:
    return f"S{e.season:02d}E{e.number:02d}" if e.season is not None else f"E{e.number:02d}"


def _title_label(ti: int, m: Match | None) -> str:
    """How a title is named in a sentence: "S03E10 “A Benihana Christmas”", "The film", "Title 4"."""
    if m is not None and m.target == "episode" and m.episode is not None:
        name = f" “{m.episode.title}”" if m.episode.title else ""
        return _ep_code(m.episode) + name
    if m is not None and m.target == "feature":
        return "The film"
    if m is not None and m.target == "extra" and m.extra_name:
        return f"The extra “{m.extra_name}”"
    return f"Title {ti}"


def _ranges(numbers: list[int]) -> str:
    """[7, 8, 9, 11] -> "E07–E09 and E11"."""
    runs: list[list[int]] = []
    for n in sorted(numbers):
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    parts = [f"E{r[0]:02d}" if len(r) == 1 else f"E{r[0]:02d}–E{r[-1]:02d}" for r in runs]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _hours_since(iso: str) -> float | None:
    try:
        t = _dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    now = _dt.datetime.now(t.tzinfo) if t.tzinfo else _dt.datetime.now()
    return (now - t).total_seconds() / 3600


def _label_stem(label: str) -> str:
    """"SEVERANCE_S1_D2" -> "severance"; "" when the label carries no season or disc number,
    since then a matching label is the same disc again, not the next one."""
    parts = re.split(r"[_ ](?:S\d|D\d|SEASON|DISC|VOL)", label or "", maxsplit=1, flags=re.IGNORECASE)
    return parts[0].strip(" _").casefold() if len(parts) == 2 else ""


def _current_reason(m: Match) -> str:
    return {"card": "Its title card is closest to this episode.",
            "runtime": "Its running time fits this episode.",
            "list": "This is its place in the episode list from the case.",
            "discdb": "TheDiscDB lists this title as this episode.",
            "manual": "You picked this episode."}.get(m.source, "It's matched to this episode now.")


class QueueViews:
    """The Queue page's view of the jobs, shared by Controller and FakeController. Each supplies
    job_lock, _layout, _locked, _card_text, _show_episodes, _local_size and _rip_folder_bytes."""

    def queue(self, jobs: list[Job], running: set[str]) -> QueueView:
        running = set(running)
        # Episode lists first and outside job_lock, since they may need TVmaze: one per show, cached.
        shows = set()
        for job in jobs:
            if job.show_id is None or self._layout(job) == "movie":
                continue
            recent = (_hours_since(job.updated) or 0) <= RECENT_DAYS * 24 if job.updated else False
            if recent or job.id in running or job.stage in ("match", "failed", "copy"):
                shows.add(job.show_id)
        episodes = {sid: self._show_episodes(sid) for sid in shows}
        # A failed attempt at a disc stops needing you once a newer job for the same disc got further
        # (a rip retried after an error is a new job; the old one would otherwise wait forever).
        later: dict[str, str] = {}
        for j in jobs:
            if j.stage not in ("failed", "rip") and j.disc_label:
                later[j.disc_label] = max(later.get(j.disc_label, ""), j.created)
        stale = {j.id for j in jobs if j.stage == "failed" and j.id not in running and not j.files
                 and later.get(j.disc_label, "") > j.created}
        with self.job_lock:
            encoding = {j.id for j in jobs if any(e.state in ("encoding", "paused", "checking", "copying")
                                                  and not e.held for e in j.encodes)}
            items = [it for it in (self._queue_item(j, running, episodes.get(j.show_id), encoding) for j in jobs
                                   if j.id not in stale) if it]
            # A job that's ripping or copying can't be freed until it stops, so it isn't counted as safe.
            safe = sum(self._local_size(f) for j in jobs if j.id not in running
                       for f in j.files if self._safe_local(j, f))
            safe += sum(self._av1_size(e) for j in jobs if j.id not in running
                        for e in j.encodes if self._safe_av1(j, e))
            seasons = self._season_views(jobs, running, episodes)
        order = {"needs": 0, "working": 1, "done": 2}
        items.sort(key=lambda it: it.done_at if it.section == "done" else it.job.created, reverse=True)
        items.sort(key=lambda it: order[it.section])
        return QueueView(items=items, seasons=seasons, local_bytes=self._rip_folder_bytes(), safe_bytes=safe)

    def _safe_local(self, job: Job, f: RippedFile, check_nas: bool = False) -> bool:
        """Whether the local rip of this file may be deleted: its NAS copy was verified, its AV1
        version (if any) is finished, and with check_nas the copy is still there at the same size."""
        if f.state != "verified" or f.local_deleted or not f.dest_rel or self._local_size(f) <= 0:
            return False
        if _encode_holds(job, f.title_index):
            return False
        if not check_nas:
            return True
        try:
            return os.path.getsize(os.path.join(job.destination, f.dest_rel)) == self._local_size(f)
        except OSError:
            return False

    @staticmethod
    def _av1_size(e: EncodeJob) -> int:
        if e.local_deleted or not e.path:
            return 0
        try:
            return os.path.getsize(e.path)
        except OSError:
            return 0

    def _safe_av1(self, job: Job, e: EncodeJob, check_nas: bool = False) -> bool:
        """Whether a local AV1 file may be deleted: its NAS copy was verified, and with check_nas
        the copy is still there at the same size."""
        if e.state != "verified" or e.local_deleted or not e.dest_rel or self._av1_size(e) <= 0:
            return False
        if not check_nas:
            return True
        try:
            return os.path.getsize(os.path.join(job.destination, e.dest_rel)) == self._av1_size(e)
        except OSError:
            return False

    def _encode_line(self, job: Job, encoding: set[str]) -> str:
        """The working card's sentence while a disc's AV1 version is made."""
        encs = [e for e in job.encodes if e.state in ENCODE_ACTIVE]
        e = next((e for e in encs if e.state != "waiting"), None) or (encs[0] if encs else None)
        if e is None:
            return job.status or "Working…"
        pct = int(e.fraction * 100)
        if e.state == "encoding":
            text = f"Encoding the AV1 version · {pct}%"
            if e.fps:
                text += f" · {e.fps:.1f} fps"
            if e.eta_seconds:
                text += f" · about {_hours(e.eta_seconds)} left"
        elif e.state == "paused":
            text = e.note or f"The AV1 version is paused at {pct}%."
        elif e.state == "checking":
            text = "Checking the quality of the AV1 version."
        elif e.state == "copying":
            text = "Copying the AV1 version to the NAS."
        elif e.note:
            text = e.note
        elif encoding - {job.id}:
            text = "The AV1 version waits for the encode running now to finish."
        else:
            text = "The AV1 version waits its turn to encode."
        if not text.endswith("."):
            text += "."
        more = len(encs) - 1
        if more:
            text += f" {_plural(more, 'more title')} after this one."
        return text

    def _queue_item(self, job: Job, running: set[str], episodes: list[Episode] | None,
                    encoding: set[str] = frozenset()) -> QueueItem | None:
        """Where a job goes in the queue, or None when it isn't there. Hold job_lock."""
        is_running = job.id in running
        enc_active = any(e.state in ENCODE_ACTIVE for e in job.encodes)
        enc_failed = [e for e in job.encodes if e.state == "failed"]
        if job.dismissed and not is_running and not enc_active:
            return None
        by = {m.title_index: m for m in job.matches}
        tps = {tp.title_index: tp for tp in job.titles_progress}
        files = {f.title_index: f for f in job.files}
        titles = sorted(set(tps) | set(files))
        locked = self._locked(job)

        failed = []
        for ti in titles:
            f, tp = files.get(ti), tps.get(ti)
            if (f is not None and f.state == "failed") or (tp is not None and tp.state == "failed"):
                failed.append((ti, self._failed_why(job, ti, by.get(ti), f, tp)))
        failed_ids = {ti for ti, _ in failed}

        checks = []
        for ti in titles:
            if ti in locked or ti in failed_ids:
                continue
            m, tp, f = by.get(ti), tps.get(ti), files.get(ti)
            if is_running:
                # A title waiting only while its card is read has a ready match; it needs nobody.
                waiting = tp is not None and tp.state == "waiting_match" and not (m and m.ready)
            else:
                waiting = (job.stage == "match" and f is not None and bool(job.matches)
                           and (m is None or (m.target != "skip" and not m.ready)))
            if waiting:
                checks.append(self._check_row(job, ti, m, episodes))

        ready = 0
        if not is_running:
            for ti, f in files.items():
                m = by.get(ti)
                if (m is not None and m.ready and f.state not in ("verified", "failed", "copying")
                        and self._local_size(f) > 0):
                    ready += 1

        base_needs = bool(checks or failed) or job.stage == "failed" or (
            not is_running and (job.stage == "match" or (job.stage == "copy" and ready > 0)))
        needs = base_needs or bool(enc_failed)
        done_at = job.copied_at if job.stage in ("done", "plex") else ""
        if done_at and job.encodes:
            done_at = max([done_at] + [e.finished for e in job.encodes if e.finished])
        age = _hours_since(done_at) if done_at else None
        if needs:
            section = "needs"
        elif is_running or enc_active:
            section = "working"
        elif age is not None and age < DONE_HOURS:
            section = "done"
        else:
            return None

        local = sum(self._local_size(f) for f in job.files) + sum(self._av1_size(e) for e in job.encodes)
        safe = sum(self._local_size(f) for f in job.files if self._safe_local(job, f)) + \
            sum(self._av1_size(e) for e in job.encodes if self._safe_av1(job, e))
        if section == "needs":
            parts = [self._needs_reason(job, titles, by, files, checks, failed, ready)] if base_needs else []
            if len(enc_failed) == 1:
                parts.append(enc_failed[0].note or "The AV1 version failed.")
            elif enc_failed:
                parts.append(f"{len(enc_failed)} AV1 versions weren't copied. {enc_failed[0].note}".strip())
            reason = " ".join(parts)
        elif section == "working":
            reason = (job.status or "Working…") if is_running else self._encode_line(job, encoding)
        else:
            verified = [f.title_index for f in job.files if f.state == "verified"]
            reason = f"{self._what(job, verified, by)} copied to {job.library or 'the NAS'} and checked."
            n_av1 = sum(1 for e in job.encodes if e.state == "verified")
            if n_av1:
                reason += (" The AV1 version is next to it." if n_av1 == 1 and len(verified) == 1
                           else f" {_plural(n_av1, 'AV1 version')} next to them.")
        return QueueItem(job=job, section=section, reason=reason, checks=checks, failed=failed,
                         ready_to_copy=ready, local_bytes=local, safe_bytes=safe, done_at=done_at,
                         encodes=list(job.encodes))

    def _what(self, job: Job, tis: list[int], by: dict[int, Match]) -> str:
        """"6 episodes", "1 film and 2 extras", for the titles given."""
        counts = collections.Counter()
        for ti in tis:
            m = by.get(ti)
            kind = {"episode": "episode", "feature": "film", "extra": "extra"}.get(m.target if m else "", "title")
            counts[kind] += 1
        parts = [_plural(n, k) for k, n in counts.most_common()] or ["0 titles"]
        return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]

    def _failed_why(self, job: Job, ti: int, m: Match | None, f: RippedFile | None,
                    tp: TitleProgress | None) -> str:
        label = _title_label(ti, m)
        note = (tp.note if tp is not None else "") or ""
        if f is None and tp is not None and tp.state == "failed" and not note.startswith("Copy"):
            return f"{label} failed. {note}".strip() if note else f"{label} failed."
        if note.startswith("Copy failed: "):
            text = f"{label} didn't copy: {note[len('Copy failed: '):]}"
            text += "" if text.endswith(".") else "."
        elif note.startswith(REVERIFY_NOTE):
            text = f"{label} needs copying again. {note}"
        else:
            text = f"{label} didn't copy." + (f" {note}" if note else "")
        if f is not None and self._local_size(f) <= 0:
            text += " The local copy is gone, so rip the disc again to copy it."
        return text

    def _needs_reason(self, job: Job, titles: list[int], by: dict[int, Match], files: dict[int, RippedFile],
                      checks: list[CheckRow], failed: list[tuple[int, str]], ready: int) -> str:
        if job.stage == "failed" and not job.files:
            return job.status or "The rip failed."
        tv = self._layout(job) != "movie"
        # On a TV disc the count is of episodes, so extras and skipped titles are left out of it.
        if tv:
            wanted = [ti for ti in titles if by.get(ti) is None or by[ti].target == "episode"]
        else:
            wanted = [ti for ti in titles if not (by.get(ti) and by[ti].target == "skip")]
        on_nas = sum(1 for ti in wanted if files.get(ti) and files[ti].state == "verified")
        parts = []
        if job.stage == "match" and not job.matches:
            parts.append(f"Ripped {_plural(len(titles), 'title')}. Match them to episodes next.")
        elif wanted and (len(wanted) > 1 or tv):
            parts.append(f"{on_nas} of {len(wanted)} {'episodes' if tv else 'titles'} in Plex.")
        held = ""
        if any(m.held for m in job.matches) and hasattr(self, "season_others"):
            problems = season.find_problems(season.title_holders(job), self.season_others(job)[0],
                                            self._season_names(job))
            held = season.held_summary(job.matches, problems)
        if held:
            parts.append(held)
        checks = [c for c in checks if not c.held]
        if checks:
            n = len(checks)
            if all(c.read for c in checks):
                parts.append(f"{n} {'waits' if n == 1 else 'wait'} for you to check what "
                             f"{'its title card' if n == 1 else 'their title cards'} read.")
            else:
                parts.append(f"{n} {'waits' if n == 1 else 'wait'} for you to check "
                             f"{'its match' if n == 1 else 'their matches'}.")
        if failed:
            parts.append(failed[0][1] if len(failed) == 1 else f"{len(failed)} didn't copy.")
        if ready:
            parts.append(f"{ready} {'is' if ready == 1 else 'are'} matched and ready to copy.")
        if job.stage == "failed" and not failed:
            parts.append(job.status or "The rip failed.")
        return " ".join(parts) or job.status or "It waits for you."

    def _check_row(self, job: Job, ti: int, m: Match | None, episodes: list[Episode] | None) -> CheckRow:
        note = m.note if m is not None and m.note else (
            "It isn't matched to anything yet." if m is None else "Its match needs checking.")
        read = self._card_text(job, ti)
        if not read and m is not None:
            hit = re.search(r"title card reads “([^”]+)”", m.note or "")
            read = hit.group(1) if hit else ""
        current = m.episode if m is not None and m.target == "episode" else None
        options: list[tuple[Episode, str]] = []
        seen: set[tuple] = set()
        eps = episodes or []

        def add(e: Episode, why: str) -> None:
            key = (e.season, e.number)
            if key not in seen and len(options) < 4:
                seen.add(key)
                options.append((e, why))

        if current is not None:
            add(current, _current_reason(m))
        season = current.season if current is not None else job.season
        text = m.note if m is not None else ""
        # Episodes the note names: "S03E08 “Wizard Battle”", "episode 5, “Half Loop”", or just “Half Loop”.
        for s, n, name in re.findall(r"S(\d{1,2})E(\d{1,3}) “([^”]+)”", text):
            e = next((e for e in eps if e.season == int(s) and e.number == int(n)), None)
            add(e or Episode(int(s), int(n), name), "Its note says it could be this one.")
        for n, name in re.findall(r"episodes? (\d{1,3}), “([^”]+)”", text):
            e = next((e for e in eps if e.number == int(n) and e.season in (None, season)), None)
            add(e or Episode(season, int(n), name), "Its note says it could be this one.")
        for name in re.findall(r"“([^”]+)”", text):
            if read and name.casefold() == read.casefold():
                continue
            e = next((e for e in eps if e.title.casefold() == name.casefold()), None)
            if e is not None:
                add(e, "Its note says it could be this one.")
        # Episodes whose name shares a word with what the card read.
        if read and eps:
            words = {w for w in re.findall(r"[a-z0-9']+", read.casefold()) if len(w) > 2 and w not in _STOPWORDS}
            scored = []
            for e in eps:
                mine = [w for w in re.findall(r"[A-Za-z0-9']+", e.title) if w.casefold() in words]
                if mine:
                    scored.append((-len(mine), e.season if e.season is not None else season or 0, e.number, e, mine[0]))
            scored.sort(key=lambda x: x[:3])
            for *_, e, word in scored:
                add(e, f"Its name has “{word}”, which the title card read.")
        return CheckRow(title_index=ti, read=read, note=note, current=current, options=options,
                        held=m.held if m is not None else "")

    def _season_views(self, jobs: list[Job], running: set[str],
                      episodes: dict[int, list[Episode] | None]) -> list[SeasonView]:
        """TV seasons with some but not all episodes in Plex. Hold job_lock."""
        groups: dict[tuple[str, int], list[Job]] = {}
        for job in jobs:
            if self._layout(job) == "movie" or not job.show.strip() or job.season is None:
                continue
            if not job.files and job.id not in running:
                continue        # scanned but never ripped
            groups.setdefault((job.show.strip().casefold(), job.season), []).append(job)
        rank = {"todo": 0, "ripping": 1, "ripped": 2, "plex": 3}
        out = []
        for (_, season), js in sorted(groups.items()):
            js.sort(key=lambda j: j.created, reverse=True)
            show_id = next((j.show_id for j in js if j.show_id is not None), None)
            listed = episodes.get(show_id) if show_id is not None else None
            state: dict[int, tuple[str, int | None]] = {}
            names: dict[int, str] = {}
            discs = [j.disc_number for j in js if j.disc_number]
            for j in js:
                files = {f.title_index: f for f in j.files}
                tps = {tp.title_index: tp for tp in j.titles_progress}
                for m in j.matches:
                    ti = m.title_index
                    if m.target != "episode" or m.episode is None or m.episode.season not in (None, season):
                        continue
                    if ti not in files and ti not in tps:
                        continue        # a title this rip left out
                    f, tp = files.get(ti), tps.get(ti)
                    if f is not None and f.state == "verified":
                        st = "plex"
                    elif j.id in running and tp is not None and tp.state == "ripping":
                        st = "ripping"
                    elif f is not None and not f.local_deleted:
                        st = "ripped"
                    else:
                        st = "todo"
                    for n in range(m.episode.number, (m.episode_end or m.episode.number) + 1):
                        if n == m.episode.number and m.episode.title:
                            names.setdefault(n, m.episode.title)
                        if n not in state or rank[st] > rank[state[n][0]]:
                            state[n] = (st, j.disc_number)
            if listed is not None:
                for e in listed:
                    if e.season in (None, season):
                        names[e.number] = e.title or names.get(e.number, "")
                numbers = sorted({e.number for e in listed if e.season in (None, season)} | set(state))
            else:
                numbers = sorted(state)
            eps = [SeasonEp(n, names.get(n, ""), *state.get(n, ("todo", None))) for n in numbers]
            in_plex = sum(1 for e in eps if e.state == "plex")
            if not 0 < in_plex < len(eps):
                continue
            left = [n for n in numbers if n not in state]
            next_disc = max(discs) + 1 if left and discs else None
            if left and next_disc:
                text = f"Next: disc {next_disc}. {_ranges(left)} {_isnt(len(left))} ripped yet."
            elif left:
                text = f"{_ranges(left)} {_isnt(len(left))} ripped yet."
            else:
                k = len(eps) - in_plex
                text = (f"Every episode is on a disc already ripped. {k} {_isnt(k)} "
                        f"in Plex yet.")
            out.append(SeasonView(show=js[0].show, show_id=show_id, season=season, episodes=eps,
                                  in_plex=in_plex, next_disc=next_disc, next_text=text))
        return out

    def _next_disc_prefill(self, job: Job, label: str) -> None:
        """Fill in a TV disc from the season in progress it continues: same show name, or the same
        label before _S/_D. Only when exactly one season fits; what the label says wins."""
        if self._layout(job) == "movie":
            return
        try:
            jobs = self.jobs()
            seasons = self.queue(jobs, set(getattr(self, "_runs", {}))).seasons
        except Exception:  # noqa: BLE001 -- a prefill is a convenience; the user fills it in otherwise
            return
        name, stem = job.show.strip().casefold(), _label_stem(label)
        fits = []
        for sv in seasons:
            if job.season is not None and sv.season != job.season:
                continue
            group = [j for j in jobs if j.show.strip().casefold() == sv.show.strip().casefold()
                     and j.season == sv.season and j.id != job.id]
            if (name and name == sv.show.strip().casefold()) or (stem and any(_label_stem(j.disc_label) == stem
                                                                             for j in group)):
                fits.append((sv, group))
        if len(fits) != 1 or not fits[0][1]:
            return
        sv, group = fits[0]
        last = max(group, key=lambda j: j.created)
        job.show, job.show_id, job.year, job.season = last.show, last.show_id, last.year, sv.season
        job.media_kind, job.destination, job.library = last.media_kind, last.destination, last.library
        if job.disc_number is None and sv.next_disc:
            job.disc_number = sv.next_disc


class _EncodeCtrl:
    """The encode running now: which one, and the events that stop or suspend it."""

    def __init__(self, job_id: str, title_index: int):
        self.job_id, self.title_index = job_id, title_index
        self.cancel = threading.Event()     # stop_encode
        self.pause = threading.Event()      # set: ffmpeg is suspended (paused by you or by the setting)


def card_workers() -> int:
    """Titles read at once. Each reading is an ffmpeg and an OCR process using 2 to 3 cores,
    and 4 at once was quickest on a 32-thread machine (8 titles in 73 s against 118 s one
    at a time); more only competed for the CPU."""
    return max(1, min(4, (os.cpu_count() or 2) // 8))


class Controller(QueueViews, season.SeasonChecks, nasfix.NasFixes):
    def __init__(self, cfg: Config, store: JobStore | None = None, tvmaze: TvMaze | None = None,
                 discdb_client: discdb.TheDiscDb | None = None):
        self.cfg = cfg
        self.cfg.dovi_tool = _resolve_tool(cfg.dovi_tool)
        self.store = store or JobStore()
        self.tvmaze = tvmaze or TvMaze()
        self.discdb = discdb_client or discdb.TheDiscDb()
        self._discdb_hits: dict[str, discdb.DiscHit | None] = {}    # job id → TheDiscDB's record of the disc
        self._cards: dict[str, dict[int, titlecard.TitleReading]] = {}   # job id → title cards read so far
        self._card_state: dict[str, dict[int, CardStatus]] = {}          # job id → each title's card status
        self._scans: dict[int, tuple[dict, Disc]] = {}
        self._scan_watch: dict[int, scanwatch.ScanWatch] = {}
        self._scan_cancel: dict[int, threading.Event] = {}
        self.scan_times = scanwatch.ScanTimes(os.path.join(config.state_dir(), "scan-times.json"))
        self._pin_codes: dict[int, str] = {}
        self._covers: dict[tuple, tuple[str, str]] = {}
        self.job_lock = threading.RLock()
        self._runs: dict[str, _Run] = {}                       # job id → the rip running for it
        self._frozen: dict[str, dict[int, Match]] = {}         # job id → matches titles were copied under
        self.sample_interval = 1.0                             # seconds between speed samples
        # MakeMKV can't share a drive between runs: a drive listing or scan started while
        # another run holds the drive makes one of them fail with "Failed to open disc".
        # Every makemkvcon run goes through this lock.
        self._mkv = threading.Lock()
        self._last_drives: list[Drive] = []
        self._busy: set[str] = set()                           # job ids with a copy or a re-check running
        self._show_eps: dict[int, tuple[float, list | None]] = {}   # show id → (when, every episode or None)
        self._show_dirs: dict[tuple, tuple[float, str]] = {}   # (destination, show, year) -> (when, folder)
        self.plex_fetch = None                                 # plex.Fetch for tests; None uses the network
        # The AV1 encoder: one thread for the whole app, one encode at a time, in the order queued.
        self._enc_jobs: dict[str, Job] = {}                    # job id → the live Job of each disc with encodes to do
        self._enc_thread: threading.Thread | None = None
        self._enc_thread_lock = threading.Lock()               # taken after job_lock, never before it
        self._enc_wake = threading.Event()
        self._enc_ctrl: _EncodeCtrl | None = None              # the encode running now
        self.clock = _dt.datetime.now                          # local time, for av1_when "night"; tests replace it
        self.encode_poll = 2.0                                 # seconds between pause checks while encoding
        self.encode_wait = 30.0                                # seconds between checks while the setting holds encodes back
        self.encode_save_interval = 5.0                        # seconds between job saves from encode progress
        self.av1_stats: "av1.Stats | None" = None              # made on first use; tests set it

    # --- drives and discs -------------------------------------------------

    def drives(self) -> list[Drive]:
        """The drives MakeMKV sees. While MakeMKV is busy (a rip or scan), the last list is
        returned instead of starting a second makemkvcon, which would break the busy one."""
        if not self._mkv.acquire(blocking=False):
            return _copy.deepcopy(self._last_drives)
        try:
            self._last_drives = makemkv.list_drives(self.cfg)
            return _copy.deepcopy(self._last_drives)
        finally:
            self._mkv.release()

    def _scan(self, drive_index: int) -> tuple[dict, Disc]:
        if not self._mkv.acquire(timeout=5):
            raise RuntimeError("The drive is busy with a rip or another scan. Scan again when it finishes.")
        watch = scanwatch.ScanWatch()
        cancel = threading.Event()
        self._scan_watch[drive_index], self._scan_cancel[drive_index] = watch, cancel
        try:
            live = dict(watch=watch, cancel=cancel) if _rip_accepts("watch", makemkv.scan_disc_parsed) else {}
            parsed, disc = makemkv.scan_disc_parsed(self.cfg, drive_index, min_seconds=SCAN_MIN_SECONDS, **live)
        finally:
            self._mkv.release()
            self._scan_cancel.pop(drive_index, None)
        self.scan_times.record(scanwatch.disc_kind(disc.uhd, disc.disc_type), watch.status().elapsed)
        classify.classify_titles(disc.titles, self.cfg.min_episode_minutes * 60)
        self._scans[drive_index] = (parsed, disc)
        return parsed, disc

    def scan(self, drive_index: int) -> Disc:
        return _copy.deepcopy(self._scan(drive_index)[1])

    def scan_status(self, drive_index: int) -> scanwatch.ScanStatus | None:
        """What the scan of this drive is doing, for the window to show while it runs."""
        w = self._scan_watch.get(drive_index)
        return w.status() if w is not None else None

    def stop_scan(self, drive_index: int) -> None:
        ev = self._scan_cancel.get(drive_index)
        if ev is not None:
            ev.set()

    def scan_usual(self) -> str:
        return self.scan_times.usual()

    def disc_info(self, drive_index: int) -> discinfo.DiscReport:
        cached = self._scans.get(drive_index)
        # Reuse the last scan while a disc is still in the drive. Asked through the drive's
        # status, not MakeMKV, so Disc info never competes with a rip for the drive.
        device = cached[1].drive.device if cached else ""
        if not cached or (device and watch.disc_present(device) is False):
            cached = self._scan(drive_index)
        parsed, disc = cached
        device = disc.drive.device
        # DVDs have no BDMV/META artwork, and mounting one can hang the drive (see cover()).
        meta = (discinfo.fetch_meta(device, os.path.join(config.state_dir(), "covers"), disc.label)
                if device and "DVD" not in disc.disc_type else None)
        report = discinfo.build_report(parsed, disc, meta)
        if not report.cover_path:
            report.cover_path, report.cover_source = self.cover(drive_index)
        return report

    @staticmethod
    def show_query(disc_name: str, label: str = "") -> str:
        """'ADVENTURE TIME - IT CAME FROM THE NIGHTOSPHERE' / 'SEVERANCE_S1_D1' -> a TVmaze search."""
        name = disc_name or label
        name = re.split(r"\s*:\s+|\s+-\s+", name)[0]                 # drop a volume's subtitle ("Show: Season 1", "Show - Title")
        name = re.sub(r"[_.]+", " ", name)
        name = re.sub(r"\b(VOL(UME)?|SEASON|SERIES|DISC|DISK|PART)\s*\d+\b.*$", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\bS\d{1,2}\s*D\d{1,2}\b.*$|\bS\d{1,2}\b.*$|\bD\d{1,2}\b.*$", "", name, flags=re.IGNORECASE)
        return re.sub(r"\s+", " ", name).strip(" -:,")

    @classmethod
    def show_name(cls, disc_name: str, label: str = "") -> str:
        """The show or film name to start from: show_query's guess, with an all-caps disc
        name ("ADVENTURE TIME") turned into "Adventure Time" for Plex and the search box."""
        name = cls.show_query(disc_name, label)
        return name.title() if name.isupper() else name

    def cover(self, drive_index: int, season: int | None = None) -> tuple[str, str]:
        """(image path, source) for the disc in the drive: the disc's own artwork (Blu-ray
        BDMV/META), else a TVmaze poster for a TV disc (DVDs carry no artwork). ("", "") when
        there is none. Cached per disc label, so the disc is mounted and TVmaze asked once."""
        cached = self._scans.get(drive_index)
        if not cached:
            return "", ""
        disc = cached[1]
        key = (disc.label, season)
        if key in self._covers:
            return self._covers[key]
        covers = os.path.join(config.state_dir(), "covers")
        result = ("", "")
        # Only Blu-rays carry artwork (BDMV/META). Mounting a DVD to look finds nothing, and on
        # 2026-10-26 the file-system read on a DVD hung the USB drive until it was replugged.
        if disc.drive.device and "DVD" not in disc.disc_type:
            meta = discinfo.fetch_meta(disc.drive.device, covers, disc.label)
            if meta.cover_path:
                result = (meta.cover_path, "disc")
        # Only TV discs: a film searched on TVmaze can match an unrelated show of the same name.
        if not result[0] and any(t.kind == "episode" for t in disc.titles):
            query = self.show_query(disc.name, disc.label)
            try:
                hits = self.tvmaze.search(query) if query else []
                url = self.tvmaze.poster_url(hits[0].id, season) if hits else None
                if url:
                    os.makedirs(covers, exist_ok=True)
                    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", disc.label or query)
                    path = os.path.join(covers, f"{stem}.tvmaze{os.path.splitext(url)[1] or '.jpg'}")
                    with open(path, "wb") as fh:
                        fh.write(self.tvmaze.download(url))
                    result = (path, "TVmaze")
            except (TvMazeError, OSError):
                pass    # no cover is fine; the placeholder shows
        self._covers[key] = result
        return result

    def disc_present(self, device: str) -> bool | None:
        """Cheap enough to poll: no MakeMKV, no spin-up. None while the drive can't tell yet."""
        return watch.disc_present(device)

    def eject(self, drive_index: int) -> None:
        drive = next((d for d in self.drives() if d.index == drive_index), None)
        if drive and drive.device:
            makemkv.eject(drive.device)

    # --- jobs ---------------------------------------------------------------

    def new_job(self, disc: Disc) -> Job:
        titles = _copy.deepcopy(disc.titles)
        kind = "movie" if any(t.kind == "feature" and t.selected for t in titles) else "tv"
        dest = self.cfg.destination(kind) or (self.cfg.destinations[0] if self.cfg.destinations else None)
        label = disc.label or disc.name
        job = Job(id=self.store.new_id(), disc_label=label, drive_index=disc.drive.index,
                  min_seconds=SCAN_MIN_SECONDS, media_kind=kind,
                  show=self.show_name(disc.name, disc.label),
                  # Labels use underscores (SEVERANCE_S1_D1), which regex \b treats as word characters.
                  season=_guess(label, r"(?:(?<![A-Z0-9])S|SEASON[ _]?)(\d{1,2})(?!\d)") if kind == "tv" else None,
                  disc_number=_guess(label, r"(?:(?<![A-Z0-9])D|DISC[ _]?)(\d{1,2})(?!\d)"),
                  destination=dest.path if dest else "", library=dest.library if dest else "",
                  version=("2160p" if disc.uhd else "1080p") if self.cfg.keep_1080p_version else "",
                  audio_langs=list(self.cfg.audio_langs), subtitle_langs=list(self.cfg.subtitle_langs),
                  titles=titles, hdr_policy=dataclasses.replace(self.cfg.hdr), status="Ready to rip.")
        job.rip_dir = os.path.join(self.cfg.rip_path, job.id)
        if kind == "tv":
            self._next_disc_prefill(job, label)
        return job

    def save_job(self, job: Job) -> None:
        with self.job_lock:
            # The encoder keeps its own Job for a disc with encodes to do; a copy of it saved from
            # elsewhere must not put back older encode states, so the two share one list.
            live = self._enc_jobs.get(job.id)
            if live is not None and live is not job:
                job.encodes = live.encodes
            self.store.save(job)

    _save = save_job

    def jobs(self) -> list[Job]:
        """Every job, newest first. A disc whose AV1 version is being made is the encoder's own
        Job object, so its encode progress is current."""
        with self.job_lock:
            live = dict(self._enc_jobs)
        return [live.get(j.id, j) for j in self.store.all()]

    def _layout(self, job: Job) -> str:
        dest = next((d for d in self.cfg.destinations if d.path == job.destination), None) \
            or self.cfg.destination(job.media_kind)
        return dest.layout if dest else "tv"

    # --- rip ----------------------------------------------------------------

    def stream_possible(self, job: Job) -> tuple[bool, str]:
        """Whether this job's film would be copied to the NAS while it rips, and if not, why in one sentence."""
        tail = "so the film is copied after the rip."
        with self.job_lock:
            picked = [t for t in job.titles if t.selected]
            matches = list(job.matches)
        if self._layout(job) != "movie":
            return False, "Only a film is copied while it rips, and this disc goes to a TV library."
        if not self.cfg.stream_films:
            return False, f"Copying films while they rip is off in Settings, {tail}"
        if len(picked) != 1:
            if not picked:
                return False, "No title is selected."
            return False, f"{len(picked)} titles are selected, {tail}"
        if not job.hdr_policy.is_raw:
            return False, f"The HDR setting isn't RAW, {tail}"
        supported = getattr(makemkv, "selection_supported", None)
        if not (supported and hasattr(makemkv, "selection_for") and supported(self.cfg)):
            return False, f"This MakeMKV can't choose the languages during the rip, {tail}"
        if follow is None or not _rip_accepts("on_title_start"):
            return False, f"This version of Disc Shelf can't copy a file while MakeMKV writes it, {tail}"
        m = next((m for m in matches if m.title_index == picked[0].index), None) \
            or next((m for m in match.feature_match(picked) if m.title_index == picked[0].index), None)
        if not m or not m.ready:
            return False, f"The film's match needs checking first, {tail}"
        if not job.show.strip():
            return False, f"The film has no name yet, {tail}"
        if not os.path.isdir(job.destination):
            return False, f"{job.destination} isn't there (check that the NAS share is mounted), {tail}"
        return True, ""

    def rip(self, job: Job, progress: Progress, cancel: threading.Event,
            samples: Callable[[Sample], None] | None = None) -> None:
        picked = [t for t in job.titles if t.selected]
        if not picked:
            raise ValueError("No titles are selected. Tick at least one title to rip.")
        with self.job_lock:
            if job.id in self._runs:
                raise RuntimeError("This disc is already ripping.")
        # The window drops unticked streams from the selected titles. MakeMKV's own track
        # selection means stream N on the disc isn't track N in the MKV, so carry the choice
        # over as languages, which mkvmerge can match reliably.
        kept = [s for t in picked for s in t.streams]
        if any(s.kind == "audio" for s in kept):
            job.audio_langs = sorted({s.lang for s in kept if s.kind == "audio" and s.lang})
        if kept:
            job.subtitle_langs = sorted({s.lang for s in kept if s.kind == "subtitle" and s.lang})
        os.makedirs(job.rip_dir, exist_ok=True)

        streamable, why = self.stream_possible(job)
        if streamable:
            mode, note = "stream", ""
        else:
            mode = "per_title" if self.cfg.copy_as_ripped else "after"
            # Only a film disc with the setting on expected to stream, so only it is told why not.
            note = why if self._layout(job) == "movie" and self.cfg.stream_films else ""

        run = _Run(job, mode, cancel, self.sample_interval, samples, progress)
        with self.job_lock:
            job.copy_mode, job.copy_mode_note = mode, note
            job.titles_progress = [TitleProgress(t.index, total_bytes=t.size_bytes) for t in picked]
            job.files = []
            self._frozen.pop(job.id, None)
            if self._layout(job) == "movie" and not job.matches:
                # A film needs no lookup to match, so it can be copied as soon as it's ripped.
                job.matches = match.feature_match(job.titles)
            job.stage, job.status, job.progress = "rip", "Ripping…", 0.0
            job.dismissed = False
            self._runs[job.id] = run
            self._save(job)

        order = [t.index for t in picked]
        # One MakeMKV run for the whole disc saves opening it again for every title, when the
        # titles that weren't picked are small enough to rip and throw away.
        together = mode != "stream" and self.cfg.rip_in_one_run and _rip_accepts("together") \
            and makemkv.together_worth_it(picked, job.titles, job.min_seconds)
        if together:
            order = sorted(order)           # MakeMKV writes them in title order
        n = len(order)
        total_size = sum(t.size_bytes for t in picked)
        workdir = os.path.join(job.rip_dir, ".work")
        os.makedirs(workdir, exist_ok=True)

        sizes = {t.index: t.size_bytes for t in picked}

        def rip_text(f: float) -> str:
            k = max(1, min(n, run.started or int(f * n) + 1))
            text = f"Ripping title {k} of {n}"
            spans = run.timeline()
            opening = any(s.kind == "open" and s.end is None for s in spans) and \
                not any(s.kind == "read" and s.end is None for s in spans)
            rate = run.rip_meter.rate
            if opening:
                text += " · opening the disc"
            elif rate > 0:
                text += f" · {rate / 1e6:.1f} MB/s · {rate / BLURAY_1X:.1f}×"
            with self.job_lock:
                ripped = {p.title_index: p.ripped_bytes for p in job.titles_progress}
            est = ripstats.estimate(order, sizes, spans, time.monotonic() - run.t0, ripped) if spans else None
            if est is not None:
                left = est.seconds
            else:   # MakeMKV without the per-title callbacks: the whole disc at the latest speed
                left = max(0, total_size - run.rip_meter.total) / rate if total_size and rate > 0 else 0
            if left > 0:
                text += f" · {math.ceil(left / 60)} min left"
            return text

        def ripping(f: float, _text: str) -> None:
            if not run.bytes_seen and total_size:
                run.rip_meter.update(int(max(0.0, min(1.0, f)) * total_size))   # MakeMKV without on_bytes
            run.emit(RIP_SHARE * f, rip_text(f))

        def on_title_start(ti: int, path: str) -> None:
            run.span("open", ti, end=True)
            run.span("read", ti)
            run.rip_meter.restart()
            with self.job_lock:
                run.started += 1
                tp = _tp(job, ti)
                if tp:
                    tp.state = "copying" if mode == "stream" else "ripping"
                self._save(job)
            if mode == "stream":
                self._stream_start(run, ti, path)

        def on_bytes(ti: int, _path: str, written: int) -> None:
            run.bytes_seen = True
            with self.job_lock:
                tp = _tp(job, ti)
                if tp:
                    tp.ripped_bytes = written
                total = sum(p.ripped_bytes for p in job.titles_progress)
            run.rip_meter.update(total)
            with self.job_lock:
                if tp:
                    tp.rip_rate = run.rip_meter.rate

        def on_title_done(ti: int, path: str) -> None:
            size = os.path.getsize(path)
            run.span("read", ti, end=True, size=size)
            nxt = order[order.index(ti) + 1] if ti in order and order.index(ti) + 1 < n else None
            if nxt is not None and not run.cancel.is_set():
                run.span("open", nxt)
            with self.job_lock:
                run.seen.add(ti)
                tp = _tp(job, ti)
                if tp:
                    tp.ripped_bytes = tp.total_bytes = size
                    tp.rip_rate = 0.0
                    if tp.state != "copying":
                        tp.state = "ripped"
                f = _file(job, ti)
                if f is None:
                    f = RippedFile(title_index=ti, path=path)
                    job.files.append(f)
                    job.files.sort(key=lambda x: order.index(x.title_index) if x.title_index in order else n)
                f.path, f.size_bytes = path, size
                self._save(job)
            if mode == "stream":
                run.stream_done.set()
            else:
                self._card_queue(run, ti, path)
                run.q.put(("post", ti))

        kwargs: dict = {"min_seconds": job.min_seconds}
        if together:
            kwargs["together"] = True
        if _rip_accepts("on_title_done"):
            kwargs.update(on_title_start=on_title_start, on_title_done=on_title_done, on_bytes=on_bytes)
        if mode == "stream":
            kwargs["selection"] = makemkv.selection_for(job.audio_langs, job.subtitle_langs)
        else:
            run.thread = threading.Thread(target=self._work, args=(run, workdir), daemon=True,
                                          name=f"discshelf-copier-{job.id}")
            run.thread.start()

        try:
            if not self._mkv.acquire(timeout=30):
                raise RuntimeError("The drive is busy with a scan. Choose Rip again in a moment.")
            try:
                if "on_title_start" in kwargs and order:
                    run.span("open", order[0])
                paths = makemkv.rip_titles(self.cfg, job.drive_index, order, job.rip_dir, ripping, cancel, **kwargs)
            finally:
                self._mkv.release()
            # A MakeMKV wrapper without the callbacks only reports the files at the end.
            if len(paths) == n:
                for ti, p in zip(order, paths):
                    if ti not in run.seen:
                        on_title_done(ti, p)
            if not cancel.is_set():
                # MakeMKV is done with the disc, so the next one can go in while the last titles copy.
                self._eject_after_rip(job)
        except makemkv.Cancelled:
            self._end_run(run, stop_stream=True)
            with self.job_lock:
                job.status = "Rip stopped. Start it again to rip from the beginning."
                self._save(job)
            raise
        except Exception as e:
            self._end_run(run, stop_stream=True)
            with self.job_lock:
                job.stage, job.status = "failed", f"Rip failed: {e}"
                self._save(job)
            raise

        self._rip_over(run)
        run.emit(RIP_SHARE, "Ripped. Finishing the last title…")
        if mode == "stream":
            self._stream_finish(run)
        self._end_run(run)
        shutil.rmtree(workdir, ignore_errors=True)
        if run.post_errors:
            with self.job_lock:
                job.stage, job.status = "failed", f"Rip failed: {run.post_errors[0]}"
                self._save(job)
            raise run.post_errors[0]
        self._settle(run)

    def _eject_after_rip(self, job: Job) -> bool:
        """Open the tray when Settings says so. The device comes from the scan or the last drive
        listing, so no makemkvcon is started for it. Returns whether the tray opened."""
        if not self.cfg.eject_after_rip:
            return False
        cached = self._scans.get(job.drive_index)
        device = cached[1].drive.device if cached else ""
        device = device or next((d.device for d in self._last_drives if d.index == job.drive_index), "")
        if not device:
            return False
        try:
            return bool(makemkv.eject(device))
        except Exception:  # noqa: BLE001 -- a tray that won't open never fails the rip
            return False

    def _rip_over(self, run: "_Run") -> None:
        with self.job_lock:
            if not run.rip_over:
                run.rip_over = True
                run.tail_total = run.q.qsize() + 1

    def _end_run(self, run: "_Run", stop_stream: bool = False) -> None:
        """Wait for the copier (or the film's copy) to finish, then forget the run."""
        self._rip_over(run)
        if stop_stream:
            run.follow_cancel.set()
            run.stream_done.set()
        if run.follow_thread:
            run.follow_thread.join()
        if run.cards_thread:
            # Before the copier stops: a card read late can still release its title's copy.
            if stop_stream:
                with self.job_lock:
                    run.card_pending.clear()
            run.cards_q.put(None)
            run.cards_thread.join()
        if run.thread:
            run.q.put(None)
            run.thread.join()
        with self.job_lock:
            if self._runs.get(run.job.id) is run:
                del self._runs[run.job.id]

    def _settle(self, run: "_Run") -> None:
        """The stage after a rip: done when every title is on the NAS (or skipped), else match."""
        job = run.job
        with self.job_lock:
            by = {m.title_index: m for m in job.matches}
            verified = [f for f in job.files if f.state == "verified"]
            settled = all(tp.state == "verified" or (by.get(tp.title_index) and by[tp.title_index].target == "skip")
                          for tp in job.titles_progress)
            n = len(job.files)
            waiting = sum(1 for tp in job.titles_progress if tp.state == "waiting_match")
            done = run.mode != "after" and bool(verified) and settled
            if done:
                job.stage, job.copied_at, job.status = "plex", now_iso(), "Copied and checked."
            else:
                job.stage, job.progress = "match", 1.0
                job.status = self._match_status(job, run, n, len(verified), waiting)
            self._save(job)
        if done:
            text = self._scan_plex(job, verified)
            with self.job_lock:
                job.status, job.stage, job.progress = text, "done", 1.0
                self._save(job)
        run.emit(1.0, job.status)

    def _match_status(self, job: Job, run: "_Run", n: int, on_nas: int, waiting: int) -> str:
        titles = f"{n} title{'s' if n != 1 else ''}"
        if run.mode == "stream" and run.copy_errors:
            return f"The film is ripped, but copying it while it ripped failed: {run.copy_errors[0][1]} Copy it again."
        if run.mode == "after" or (not on_nas and not run.copy_errors):
            return f"Ripped {titles}. Match them to episodes next."
        text = f"Ripped {titles}, and {on_nas} {'is' if on_nas == 1 else 'are'} on the NAS."
        if waiting:
            text += f" {waiting} {'waits' if waiting == 1 else 'wait'} for a match to be confirmed."
        if run.copy_errors:
            ti, e = run.copy_errors[0]
            text += f" Title {ti} couldn't be copied ({e}); copy it again."
        return text

    # The worker: post-processes each ripped title, then copies it, in title order, so
    # MakeMKV's own thread only ever queues work and the drive keeps reading.

    def _work(self, run: "_Run", workdir: str) -> None:
        job = run.job
        while True:
            item = run.q.get()
            if item is None:
                with self.job_lock:
                    if run.q.empty():
                        return
                run.q.put(None)          # copies were queued after the rip ended; do them first
                continue
            kind, ti = item
            if run.cancel.is_set():
                with self.job_lock:
                    run.queued.discard(ti)
                continue
            if kind == "post":
                try:
                    self._post_one(run, ti, workdir)
                except Exception as e:
                    with self.job_lock:
                        run.post_errors.append(e)
                        tp = _tp(job, ti)
                        if tp:
                            tp.state, tp.note = "failed", f"It couldn't be prepared: {e}"
                        self._save(job)
                    self._tail_step(run)
                    continue
            if run.mode == "per_title":
                self._copy_if_ready(run, ti)
            self._tail_step(run)

    def _tail_step(self, run: "_Run") -> None:
        if run.rip_over:
            run.tail_done += 1

    def _say(self, run: "_Run", frac: float, text: str) -> None:
        """Overall progress for work left after the rip; during the rip the rip's own text wins."""
        if run.rip_over:
            run.emit(RIP_SHARE + (1 - RIP_SHARE) * min(1.0, (run.tail_done + frac) / max(1, run.tail_total)), text)

    def _post_one(self, run: "_Run", ti: int, workdir: str) -> None:
        job = run.job
        with self.job_lock:
            f = _file(job, ti)
            path = f.path
        self._say(run, 0.0, f"Keeping the chosen languages in title {ti}")
        run.span("languages", ti)
        try:
            hdr.remux_tracks(path, path, job.audio_langs, job.subtitle_langs, self.cfg)
        finally:
            run.span("languages", ti, end=True)
        self._say(run, 0.3, f"Checking HDR formats in title {ti}")
        info = hdr.probe(path, self.cfg)
        if info.dolby_vision and info.dv_profile == 7:
            info.dv_layer_kind = hdr.dv_layer_kind(path, self.cfg)
        converted = False
        if not job.hdr_policy.is_raw and info.dolby_vision:
            self._say(run, 0.5, f"Applying the HDR setting to title {ti}")
            hdr.apply_policy(path, job.hdr_policy, info, workdir, self.cfg)
            info = hdr.probe(path, self.cfg)
            converted = True
        size = os.path.getsize(path)
        with self.job_lock:
            f.hdr, f.size_bytes = info, size
            if converted:
                f.state = "converted"
            tp = _tp(job, ti)
            if tp:
                tp.total_bytes = tp.ripped_bytes = size
            run.posted.add(ti)
            self._save(job)

    def _copy_if_ready(self, run: "_Run", ti: int) -> None:
        job = run.job
        with self.job_lock:
            run.queued.discard(ti)
            tp, f = _tp(job, ti), _file(job, ti)
            if not tp or not f or ti not in run.posted or tp.state not in WAITING_STATES:
                return
            m = next((m for m in job.matches if m.title_index == ti), None)
            if not self._ready(run, ti, m):
                _mark_waiting(tp, m, ti in run.card_pending)
                self._save(job)
                return

        def step(phase: str, frac: float, text: str) -> None:
            self._say(run, frac, f"{'Copying' if phase == 'copy' else 'Checking'} title {ti} · {text}")

        run.span("copy", ti)
        try:
            self._copy_one(job, f, run.cancel, run.copy_meter, step)
        except transfer.Cancelled:
            pass
        except Exception as e:
            with self.job_lock:
                run.copy_errors.append((ti, e))
        finally:
            run.span("copy", ti, end=True)

    def _queue_ready(self, job: Job) -> None:
        """Queue every ripped title whose match is now ready; refresh why the others wait. Hold job_lock."""
        run = self._runs.get(job.id)
        if not run or run.mode != "per_title":
            return
        by = {m.title_index: m for m in job.matches}
        changed = False
        for tp in job.titles_progress:
            ti = tp.title_index
            if ti not in run.posted or tp.state not in WAITING_STATES or ti in run.queued:
                continue
            m = by.get(ti)
            if self._ready(run, ti, m):
                run.queued.add(ti)
                if run.rip_over:
                    run.tail_total += 1
                run.q.put(("copy", ti))
            else:
                changed = _mark_waiting(tp, m, ti in run.card_pending) or changed
        if changed:
            self._save(job)

    @staticmethod
    def _ready(run: "_Run", ti: int, m: Match | None) -> bool:
        """A guess from running times waits for the title's card, which may correct it."""
        if not (m and m.ready):
            return False
        return not (ti in run.card_pending and m.source in ("", "runtime"))

    # --- title cards ---------------------------------------------------------

    def _cards_wanted(self, job: Job) -> bool:
        return self.cfg.read_title_cards and self._layout(job) != "movie" and bool(ocr.engine())

    def _card_queue(self, run: "_Run", ti: int, path: str) -> None:
        if not self._cards_wanted(run.job):
            return
        with self.job_lock:
            run.card_pending.add(ti)
            self._card_state.setdefault(run.job.id, {})[ti] = CardStatus()
            if run.cards_thread is None:
                run.cards_thread = threading.Thread(target=self._card_work, args=(run,), daemon=True,
                                                    name=f"discshelf-cards-{run.job.id}")
                run.cards_thread.start()
        run.cards_q.put((ti, path))

    def _card_work(self, run: "_Run") -> None:
        job = run.job
        while True:
            item = run.cards_q.get()
            if item is None:
                return
            ti, path = item
            try:
                if not run.cancel.is_set():
                    self._read_one(job, ti, path)
                    self.apply_cards(job)
            except Exception:  # noqa: BLE001 -- a card that can't be read leaves the running-time guess
                pass
            finally:
                with self.job_lock:
                    run.card_pending.discard(ti)
                    self._queue_ready(job)

    def _themes(self, job: Job) -> theme.ThemeBook | None:
        key = job.show_id if job.show_id is not None else job.show
        return theme.ThemeBook(str(key)) if key not in (None, "") else None

    def _card_episodes(self, job: Job) -> list[Episode]:
        """The show's episodes, for the card reader to stop at; [] reads each title to the limit."""
        if job.show_id is None:
            return []
        try:
            return self.tvmaze.episodes(job.show_id, None)
        except Exception:  # noqa: BLE001 -- TVmaze being down only costs the early stop
            return []

    def _read_one(self, job: Job, ti: int, path: str, again: bool = True) -> titlecard.TitleReading:
        """Read one title's card, keeping its status up to date. Raises what scan_title raises."""
        with self.job_lock:
            st = self._card_state.setdefault(job.id, {}).setdefault(ti, CardStatus())
            st.state, st.started, st.note = "reading", time.monotonic(), ""
        themes = self._themes(job)
        try:
            reading = titlecard.scan_title(path, self._card_episodes(job),
                                           limit_s=max(1, self.cfg.card_search_minutes) * 60, themes=themes,
                                           ffmpeg=self.cfg.ffmpeg, fpcalc=self.cfg.fpcalc)
        except Exception as e:
            with self.job_lock:
                st.state, st.note, st.seconds = "failed", str(e) or type(e).__name__, time.monotonic() - st.started
            raise
        with self.job_lock:
            self._cards.setdefault(job.id, {})[ti] = reading
            st.state, st.seconds = "read", time.monotonic() - st.started
            earlier = [(i, r) for i, r in self._cards[job.id].items() if i != ti]
        # A title read before its show's theme was learned (the first one) was read from the
        # start. If it stopped during what turned out to be the theme, it likely stopped at
        # the show's own caption, so it is read again from the theme's end.
        if themes is not None and again:
            with self.job_lock:
                redo = [(i, r) for i, r in earlier if titlecard.place_theme(r, themes)]
            for i, r in redo:
                try:
                    self._read_one(job, i, r.path, again=False)
                except Exception:  # noqa: BLE001 -- the first reading stays
                    with self.job_lock:
                        self._card_state[job.id][i].state = "read"
        return reading

    def card_status(self, job: Job) -> dict[int, CardStatus]:
        """Each title's card status, for the Match page; empty when no cards were read this session."""
        with self.job_lock:
            return {ti: _copy.copy(st) for ti, st in self._card_state.get(job.id, {}).items()}

    def card_reads(self, job: Job, titles: list[int]) -> dict[int, tuple[str, float]]:
        """What each of these titles' cards reads on its own, as (text, seconds in), for comparing
        two titles that hold one episode. Read jointly, text both show is taken for the show's
        own and dropped, so each title is judged alone here."""
        with self.job_lock:
            readings = {ti: r for ti, r in self._cards.get(job.id, {}).items() if ti in titles}
        if not readings or job.show_id is None:
            return {}
        episodes = self._show_episodes(job.show_id) or []
        out = {}
        for ti, r in readings.items():
            cm = titlecard.pick_episodes({ti: r}, episodes).get(ti)
            if cm is not None and cm.episode is not None and cm.text:
                out[ti] = (cm.text, cm.t)
        return out

    # --- watching a title ----------------------------------------------------

    def playable(self, job: Job, ti: int) -> str:
        """A file that plays this title: the local rip, else its checked copy on the NAS; "" before it's ripped."""
        return play.playable(job, ti)

    def play(self, path: str, start_s: float = 0.0) -> str:
        """Open the file in a video player. Returns "" or what went wrong, in a sentence."""
        return play.launch(path, start_s)

    def player(self) -> "play.Player | None":
        return play.find_player()

    def _library_rels(self, job: Job) -> list[str]:
        if not job.destination or job.season is None:
            return []
        return season.library_files(job.destination, job.show, job.year, job.season)

    def read_cards(self, job: Job, progress: Progress | None = None, cancel: threading.Event | None = None,
                   only: set[int] | None = None) -> int:
        """Read the title cards of a disc that's already ripped, several titles at once, and
        match from them as each one finishes. Returns how many titles got a match from their
        card. Setting cancel stops before the titles not started yet. only limits it to those
        titles (two that hold the same episode), keeping what the others' cards read."""
        files = [(f.title_index, f.path) for f in job.files if f.path and os.path.isfile(f.path)
                 and (only is None or f.title_index in only)]
        if not files:
            raise ValueError("No ripped titles are left on this computer to read.")
        if not ocr.engine():
            raise RuntimeError("No OCR is installed. Run make install-disc-shelf to add it.")
        with self.job_lock:
            if only is None:
                self._cards.pop(job.id, None)
                self._card_state[job.id] = {ti: CardStatus() for ti, _ in files}
            else:
                for ti, _ in files:
                    self._cards.get(job.id, {}).pop(ti, None)
                    self._card_state.setdefault(job.id, {})[ti] = CardStatus()
        n, done = len(files), 0
        if progress:
            progress(0.0, f"Reading title cards · 0 of {n}")

        def one(ti: int, path: str):
            if cancel is not None and cancel.is_set():
                return
            self._read_one(job, ti, path)

        pool = ThreadPoolExecutor(max_workers=card_workers(), thread_name_prefix="discshelf-card")
        try:
            futures = [pool.submit(one, ti, path) for ti, path in files]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:  # noqa: BLE001 -- its status says why; the other titles carry on
                    pass
                done += 1
                self.apply_cards(job)
                if progress:
                    progress(done / n, f"Reading title cards · {done} of {n}")
                if cancel is not None and cancel.is_set():
                    pool.shutdown(wait=True, cancel_futures=True)
                    break
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            with self.job_lock:
                for st in self._card_state.get(job.id, {}).values():
                    if st.state in ("waiting", "reading"):
                        st.state = "stopped"
        return self.apply_cards(job)

    def apply_cards(self, job: Job) -> int:
        """Match from every title card read so far. A card replaces only a running-time guess or
        an earlier card reading, never a match from TheDiscDB, the case, the user, or one already
        confirmed or copied. Returns how many titles now have a card match."""
        with self.job_lock:
            readings = dict(self._cards.get(job.id, {}))
            show_id = job.show_id
        if not readings or show_id is None or self._layout(job) == "movie":
            return 0
        episodes = self.tvmaze.episodes(show_id, None)
        picks = titlecard.pick_episodes(readings, episodes)
        with self.job_lock:
            for ti, cm in picks.items():
                st = self._card_state.setdefault(job.id, {}).setdefault(ti, CardStatus(state="read"))
                if st.state == "read" or st.state == "none":
                    st.text, st.sure = cm.text, cm.sure
                    st.t = cm.t if cm.episode is not None else None
                    st.state = "read" if cm.episode is not None else "none"
            new = {m.title_index: m for m in titlecard.card_matches(picks, job.titles, episodes)
                   if m.target == "episode" and m.title_index in readings}
            locked = self._locked(job)
            out, n = [], 0
            for cur in job.matches:
                m = new.get(cur.title_index)
                if (m is None or cur.title_index in locked or cur.confirmed
                        or cur.source not in ("", "runtime", "card")):
                    out.append(cur)
                    n += cur.source == "card"
                    continue
                m.source = "card"
                out.append(m)
                n += 1
            have = {m.title_index for m in out}
            for ti, m in new.items():
                if ti not in have:
                    m.source = "card"
                    out.append(m)
                    n += 1
            out.sort(key=lambda m: m.title_index)
        self.update_matches(job, out)
        return n

    # --- the film copied while it rips -------------------------------------

    def _stream_start(self, run: "_Run", ti: int, path: str) -> None:
        job = run.job
        with self.job_lock:
            rel = self._dest_map(job, strict=True).get(ti) or ""
            f = _file(job, ti)
            if f is None:
                f = RippedFile(title_index=ti, path=path)
                job.files.append(f)
            f.path, f.dest_rel, f.state = path, rel, "copying"
            self._freeze(job, ti)
            tp = _tp(job, ti)
            self._save(job)
        dest = os.path.join(job.destination, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)

        def copied(c: int, checked: int, rate: float) -> None:
            with self.job_lock:
                if tp:
                    tp.copied_bytes, tp.checked_bytes, tp.copy_rate = c, checked, rate
                size = f.size_bytes
            run.copy_meter.update(c)
            if run.rip_over and size:
                run.emit(RIP_SHARE + (1 - RIP_SHARE) * min(1.0, checked / size),
                         f"Checking the last parts of the film · {_of(checked, size)}")

        def go() -> None:
            try:
                run.follow_result = follow.stream_copy(path, dest, run.stream_done, copied, run.follow_cancel)
            except BaseException as e:      # handed to the rip thread, which reports it
                run.follow_error = e

        run.follow_thread = threading.Thread(target=go, daemon=True, name=f"discshelf-follow-{job.id}")
        run.follow_thread.start()

    def _stream_finish(self, run: "_Run") -> None:
        job = run.job
        with self.job_lock:
            f = job.files[0] if job.files else None
        if f is None:
            raise RuntimeError("MakeMKV finished without writing the film.")
        if run.follow_thread is None:
            # MakeMKV never said the file was created, so copy it the ordinary way.
            try:
                self._copy_one(job, f, run.cancel, run.copy_meter, None)
            except Exception as e:
                run.copy_errors.append((f.title_index, e))
            return
        run.follow_thread.join()
        res, err = run.follow_result, run.follow_error
        dest = os.path.join(job.destination, f.dest_rel)
        if err is None and res is not None:
            try:
                ok = res.bytes == f.size_bytes and os.path.getsize(dest) == f.size_bytes
            except OSError:
                ok = False
            if not ok:
                err = RuntimeError(f"The NAS copy of {os.path.basename(dest)} is a different size from the rip.")
        try:
            info = hdr.probe(f.path, self.cfg)
        except Exception:
            info = None         # only for the details screen; the film is fine without it
        with self.job_lock:
            f.hdr = info or f.hdr
            tp = _tp(job, f.title_index)
            if err is not None:
                run.copy_errors.append((f.title_index, err))
                f.state = "ripped"          # the local rip is complete, so copy() can send it again
                if tp:
                    tp.state, tp.note = "failed", f"Copying it while it ripped failed: {err}"
            else:
                f.sha256, f.state = res.sha256, "verified"
                if tp:
                    tp.state, tp.note = "verified", ""
                    tp.copied_bytes = tp.checked_bytes = f.size_bytes
                self._queue_encode(job, f)
            self._save(job)

    # --- match --------------------------------------------------------------

    def search_show(self, query: str) -> list:
        return self.tvmaze.search(query)

    def episodes(self, show_id: int, season: int | None) -> list:
        """One season's episodes, or every season's when season is None."""
        return self.tvmaze.episodes(show_id, season)

    def _episodes_done(self, job: Job) -> set[int]:
        done: set[int] = set()
        for other in self.jobs():
            if other.id == job.id or other.show.lower() != job.show.lower() or other.season != job.season:
                continue
            for m in other.matches:
                if m.target == "episode" and m.episode:
                    done.update(range(m.episode.number, (m.episode_end or m.episode.number) + 1))
        return done

    def suggest_matches(self, job: Job, show_id: int | None) -> list[Match]:
        """Works before, during and after the rip; titles already on the NAS keep their match."""
        with self.job_lock:
            # During a rip only some files exist yet, so the rip's own list of titles decides.
            chosen = {tp.title_index for tp in job.titles_progress} or {f.title_index for f in job.files}
            titles = [t for t in job.titles if not chosen or t.index in chosen]
        if self._layout(job) == "movie":
            matches = self._discdb_matches(job, titles, []) or match.feature_match(titles)
        else:
            if show_id is None:
                raise ValueError("Pick the show from the search results first.")
            job.show_id = show_id
            self.season_others(job, refresh=True)
            known = self._discdb_matches(job, titles, self.tvmaze.episodes(show_id, None))
            if known:
                return self.update_matches(job, known)
            episodes = self.tvmaze.episodes(show_id, job.season or 1)
            start = match.next_episode_number(self._episodes_done(job))
            matches = match.match_titles(titles, episodes, start_number=start)
            for m in matches:
                m.source = "runtime"
            if not job.season:
                # A volume DVD can mix seasons in any order, so a guess from season 1 needs checking.
                for m in matches:
                    if m.target == "episode":
                        m.confidence = "check"
                        m.note = ("The disc doesn't say which season it holds, so this is a guess from season 1. "
                                  "Enter the episode list from the case if it's wrong.")
            self.update_matches(job, matches)
            # Title cards read so far (some may have been read before the show was chosen) win over the guess.
            self.apply_cards(job)
            return job.matches
        return self.update_matches(job, matches)

    def _discdb_matches(self, job: Job, titles: list[Title], episodes: list) -> list[Match] | None:
        """Matches from TheDiscDB when it has this exact disc, else None. Looked up once per job;
        a failed lookup (offline, timeout) is tried again next time."""
        if job.id not in self._discdb_hits:
            disc = Disc(drive=Drive(job.drive_index), label=job.disc_label, name=job.disc_label, titles=job.titles)
            try:
                self._discdb_hits[job.id] = discdb.find_disc(disc, job.show, db=self.discdb, min_seconds=job.min_seconds)
            except discdb.DiscDbError:
                return None
        hit = self._discdb_hits[job.id]
        return discdb.to_matches(hit, titles, episodes) if hit else None

    def match_from_list(self, job: Job, show_id: int, text: str) -> list[Match]:
        """Match the titles, in disc order, to episode names typed or pasted from the case.
        The names are looked up across every season of the show."""
        names = match.parse_episode_list(text)
        if not names:
            raise ValueError("Type the episode names from the case, one per line.")
        with self.job_lock:
            chosen = {tp.title_index for tp in job.titles_progress} or {f.title_index for f in job.files}
            titles = [t for t in job.titles if not chosen or t.index in chosen]
        job.show_id = show_id
        self.season_others(job, refresh=True)
        episodes = self.tvmaze.episodes(show_id, None)
        matches = match.match_list(titles, episodes, names)
        for m in matches:
            m.source = "list"
        return self.update_matches(job, matches)

    def episode_list_from_photo(self, path: str, show_id: int) -> "caseocr.CaseList":
        """The episode names printed on a photo of the case, in disc order, for the user to check."""
        return caseocr.episode_list_from_photo(path, self.tvmaze.episodes(show_id, None))

    def update_matches(self, job: Job, matches: list[Match]) -> list[Match]:
        """Replace the job's matches. A title being copied or already on the NAS keeps the match it was copied under."""
        with self.job_lock:
            locked = self._locked(job)
            frozen = self._frozen.get(job.id, {})
            old = {m.title_index: m for m in job.matches}
            out, seen = [], set()
            for m in matches:
                ti = m.title_index
                if ti in locked:
                    m = _copy.deepcopy(frozen[ti]) if ti in frozen else old.get(ti, m)
                out.append(m)
                seen.add(ti)
            for ti in sorted(locked - seen):
                if ti in frozen or ti in old:
                    out.append(_copy.deepcopy(frozen[ti]) if ti in frozen else old[ti])
            job.matches = out
            self.apply_season(job)
            self._save(job)
            self._queue_ready(job)
            return job.matches

    def confirm_matches(self, job: Job) -> None:
        """The user accepted every match: mark them confirmed, and copy the ripped ones if a rip is still going."""
        with self.job_lock:
            for m in job.matches:
                if m.target != "skip" and m.title_index not in self._locked(job):
                    m.confirmed = True
            self._save(job)
            self._queue_ready(job)

    def _locked(self, job: Job) -> set[int]:
        out = {tp.title_index for tp in job.titles_progress if tp.state in LOCKED_STATES}
        return out | {f.title_index for f in job.files if f.state in ("copying", "verified")}

    def _freeze(self, job: Job, ti: int) -> None:
        """Keep a copy of the match a title is copied under, so edits made in place can be undone."""
        m = next((m for m in job.matches if m.title_index == ti), None)
        if m is not None:
            self._frozen.setdefault(job.id, {})[ti] = _copy.deepcopy(m)

    def _dest_map(self, job: Job, strict: bool) -> dict[int, str | None]:
        """title_index → path under the destination. Paths already used on the NAS are kept and
        never handed to another title. strict=False gives None instead of raising when a name can't be made."""
        layout = self._layout(job)
        fixed = {f.title_index: f.dest_rel for f in job.files
                 if f.dest_rel and f.title_index in self._locked(job)}
        order = sorted({m.title_index for m in job.matches} - set(fixed))
        by = {m.title_index: m for m in job.matches}
        scheme = naming.Scheme.from_config(self.cfg)
        show_dir = self._show_dir(job, scheme) if layout != "movie" else ""
        rels: list[str | None] = []
        for ti in order:
            try:
                rels.append(naming.relpath_for(job, by[ti], layout, scheme, show_dir=show_dir))
            except ValueError:
                if strict:
                    raise
                rels.append(None)
        names = naming.dedupe(list(fixed.values()) + rels)[len(fixed):]
        out: dict[int, str | None] = dict(fixed)
        out.update(zip(order, names))
        return out

    def _show_dir(self, job: Job, scheme: "naming.Scheme") -> str:
        """The show's folder when the destination already has one ("Bluey" or "bluey (2018)"),
        so a new rip joins it rather than making a second folder for the same show; "" uses
        the scheme's. The listing is remembered for a minute, as this runs on every preview."""
        if not job.show.strip() or not job.destination:
            return ""
        key = (job.destination, job.show, job.year)
        hit = self._show_dirs.get(key)
        if hit is None or time.monotonic() - hit[0] > 60:
            try:
                names = [e.name for e in os.scandir(job.destination) if e.is_dir()]
            except OSError:
                names = []
            hit = (time.monotonic(), naming.existing_show_folder(names, job.show, job.year, scheme) or "")
            self._show_dirs[key] = hit
        return hit[1]

    def plan_paths(self, job: Job) -> list[RippedFile]:
        with self.job_lock:
            paths = self._dest_map(job, strict=True)
            for f in job.files:
                f.dest_rel = paths.get(f.title_index) or ""
            return [f for f in job.files if f.dest_rel]

    def preview_paths(self, job: Job) -> list[tuple[int, str]]:
        """(title_index, path under the destination) for each matched title, before or during the rip.
        A title whose name can't be made yet (no show name, no episode chosen) is left out."""
        with self.job_lock:
            paths = self._dest_map(job, strict=False)
            return [(m.title_index, paths[m.title_index]) for m in job.matches if paths.get(m.title_index)]

    # --- copy and Plex ------------------------------------------------------

    def _copy_one(self, job: Job, f: RippedFile, cancel: threading.Event | None, meter: "_Meter | None",
                  step: Callable[[str, float, str], None] | None) -> None:
        """Copy one file to the NAS and check it, keeping its TitleProgress and state up to date.
        step(phase "copy" | "check", fraction of this file, text) reports progress. Raises on failure."""
        with self.job_lock:
            if not os.path.isdir(job.destination):
                raise FileNotFoundError(f"{job.destination} isn't there. Check that the NAS share is mounted.")
            rel = self._dest_map(job, strict=True).get(f.title_index)
            if not rel:
                raise ValueError(f"Title {f.title_index} isn't matched to anything, so it has no name on the NAS.")
            tp = _tp(job, f.title_index)
            if tp is None:
                tp = TitleProgress(f.title_index, total_bytes=f.size_bytes, ripped_bytes=f.size_bytes)
                job.titles_progress.append(tp)
            before = f.state if f.state in ("ripped", "converted") else "ripped"
            f.dest_rel, f.state = rel, "copying"
            tp.state, tp.note, tp.copied_bytes, tp.checked_bytes = "copying", "", 0, 0
            self._freeze(job, f.title_index)
            size = f.size_bytes or os.path.getsize(f.path)
            self._save(job)
        dest = os.path.join(job.destination, rel)

        def copying(fr: float, _text: str) -> None:
            with self.job_lock:
                tp.copied_bytes = int(size * max(0.0, min(1.0, fr)))
                total = sum(p.copied_bytes for p in job.titles_progress)
            if meter:
                meter.update(total)
                tp.copy_rate = meter.rate
            if step:
                step("copy", fr, _copy_text(tp.copied_bytes, size, tp.copy_rate))

        def checking(fr: float, _text: str) -> None:
            with self.job_lock:
                tp.checked_bytes = int(size * max(0.0, min(1.0, fr)))
            if step:
                step("check", fr, _of(tp.checked_bytes, size))

        try:
            transfer.copy(f.path, dest, copying, cancel)
            with self.job_lock:
                tp.state, tp.copied_bytes, tp.copy_rate = "checking", size, 0.0
                self._save(job)
            if not f.sha256 and self.cfg.verify == "readback":
                f.sha256 = transfer.sha256(f.path)
            ok = transfer.verify(f.path, dest, self.cfg.verify, f.sha256 or None, checking)
        except transfer.Cancelled:
            with self.job_lock:
                f.state, tp.state = before, "ripped"
                self._save(job)
            raise
        except Exception as e:
            with self.job_lock:
                f.state, tp.state, tp.note = "failed", "failed", f"Copy failed: {e}"
                self._save(job)
            raise
        with self.job_lock:
            if not ok:
                f.state, tp.state = "failed", "failed"
                tp.note = f"The NAS copy of {os.path.basename(dest)} doesn't match. Copy it again."
                self._save(job)
                raise RuntimeError(tp.note)
            f.state, tp.state, tp.checked_bytes = "verified", "verified", size
            self._queue_encode(job, f)
            self._save(job)

    def copy(self, job: Job, progress: Progress, cancel: threading.Event,
             samples: Callable[[Sample], None] | None = None) -> None:
        with self.job_lock:
            if job.id in self._runs:
                raise RuntimeError("This disc is still ripping; its titles are copied as each one finishes.")
            files = self.plan_paths(job)
            todo = [f for f in files if f.state != "verified"]
            held = [m for m in job.matches if m.held and any(f.title_index == m.title_index for f in todo)]
        if held:
            raise ValueError(f"{held[0].held} Settle it at the top of the Match page first: compare the "
                             "titles, then keep one, keep both as versions, or save one as an extra.")
        if not files:
            raise ValueError("Nothing to copy. Match at least one title to an episode, the film or an extra.")
        if todo and not os.path.isdir(job.destination):
            raise FileNotFoundError(f"{job.destination} isn't there. Check that the NAS share is mounted.")
        self._copy_files(job, todo, progress, cancel, samples)
        self._finish(job, files, progress)

    def copy_ready(self, job: Job, progress: Progress, cancel: threading.Event,
                   samples: Callable[[Sample], None] | None = None) -> int:
        """After the rip: copy the titles whose match is ready and that aren't on the NAS yet, and
        failed ones again. Titles waiting for a check are left alone, and the job stays in "match"
        until they're settled. Returns how many files were copied."""
        with self.job_lock:
            if job.id in self._runs:
                raise RuntimeError("This disc is still ripping; its titles are copied as each one finishes.")
            paths = self._dest_map(job, strict=False)
            by = {m.title_index: m for m in job.matches}
            todo = []
            for f in job.files:
                m = by.get(f.title_index)
                if f.state == "verified" or m is None or m.target == "skip" or not paths.get(f.title_index):
                    continue
                if not (m.ready or f.state == "failed"):
                    continue
                if self._local_size(f) <= 0:
                    tp = _tp(job, f.title_index)
                    if tp is not None:
                        tp.note = "The local copy is gone, so it can't be copied again. Rip the disc again."
                    continue
                f.dest_rel = paths[f.title_index]
                todo.append(f)
            self._save(job)
        if todo and not os.path.isdir(job.destination):
            raise FileNotFoundError(f"{job.destination} isn't there. Check that the NAS share is mounted.")
        if todo:
            self._copy_files(job, todo, progress, cancel, samples)
        with self.job_lock:
            by = {m.title_index: m for m in job.matches}
            left = [f for f in job.files if f.state != "verified"
                    and not (by.get(f.title_index) and by[f.title_index].target == "skip")]
            verified = [f for f in job.files if f.state == "verified"]
            if left or not verified:
                checks = sum(1 for f in left if by.get(f.title_index) and not by[f.title_index].ready)
                text = f"Copied {_plural(len(todo), 'title')}."
                if checks:
                    text += (f" {checks} {'waits' if checks == 1 else 'wait'} for you to check "
                             f"{'its match' if checks == 1 else 'their matches'}.")
                elif left:
                    text += f" {_plural(len(left), 'title')} still {_isnt(len(left))} on the NAS."
                job.stage, job.progress, job.status = "match", 1.0, text
                self._save(job)
        if left or not verified:
            progress(1.0, job.status)
            return len(todo)
        self._finish(job, verified, progress)
        return len(todo)

    def _copy_files(self, job: Job, todo: list[RippedFile], progress: Progress, cancel: threading.Event,
                    samples: Callable[[Sample], None] | None) -> None:
        """Copy these files in turn under one progress bar. Raises what _copy_one raises."""
        with self.job_lock:
            job.stage, job.status, job.dismissed = "copy", "Copying to the NAS…", False
            for f in todo:
                tp = _tp(job, f.title_index)
                if tp:
                    tp.copied_bytes = tp.checked_bytes = 0
            self._busy.add(job.id)
            self._save(job)
        try:
            meter = _Meter("copy", samples, self.sample_interval, time.monotonic())
            total = sum(f.size_bytes for f in todo) or 1
            done = 0
            for k, f in enumerate(todo):
                # Each file: 80% copying, 20% reading it back to check it.
                lo, mid, hi = done / total, (done + 0.8 * f.size_bytes) / total, (done + f.size_bytes) / total
                base = done

                def step(phase: str, fr: float, text: str, lo=lo, mid=mid, hi=hi, base=base, k=k, f=f) -> None:
                    if phase == "copy":
                        job.progress = lo + (mid - lo) * fr
                        job.status = _copy_text(base + int(f.size_bytes * fr), total, meter.rate)
                    else:
                        job.progress = mid + (hi - mid) * fr
                        job.status = f"Checking title {k + 1} of {len(todo)} · {text}"
                    progress(job.progress, job.status)

                try:
                    self._copy_one(job, f, cancel, meter, step)
                except transfer.Cancelled:
                    with self.job_lock:
                        job.status = "Copy paused. Start it again to carry on where it stopped."
                        self._save(job)
                    raise
                except Exception as e:
                    with self.job_lock:
                        job.status = f"Copy failed: {e}"
                        self._save(job)
                    raise
                done += f.size_bytes
        finally:
            with self.job_lock:
                self._busy.discard(job.id)

    def _finish(self, job: Job, files: list[RippedFile], progress: Progress) -> None:
        """Every title is on the NAS: ask Plex to scan, and the job is done."""
        with self.job_lock:
            job.stage, job.copied_at = "plex", now_iso()
            job.status = "Copied and checked."
            self._save(job)
        text = self._scan_plex(job, files)
        with self.job_lock:
            job.status, job.stage, job.progress = text, "done", 1.0
            self._save(job)
        progress(1.0, job.status)

    def _scan_plex(self, job: Job, files: list[RippedFile]) -> str:
        if not job.library:
            return "Copied and checked."
        if not self.cfg.plex_url:
            return "Copied and checked. Add your Plex server in Settings so new rips are scanned automatically."
        token = config.load_secret("plex_token")
        if not token:
            return "Copied and checked. Sign in to Plex in Settings so new rips are scanned automatically."
        try:
            plex = Plex(self.cfg.plex_url, token, fetch=self.plex_fetch)
            section = plex.section(job.library)
            if not section:
                return f"Copied and checked. Plex has no library called “{job.library}”, so nothing was scanned."
            top = files[0].dest_rel.split("/")[0]
            plex.refresh(section["key"], plex.plex_path(section, top))
            return f"Copied and checked. Plex is scanning {top} into {job.library}."
        except PlexError as e:
            return f"Copied and checked, but Plex couldn't be asked to scan: {e}"

    def recover_titles(self) -> list[tuple[Job, list[int]]]:
        """Put back ripped titles a job lost track of: a title whose own "<disc>_tNN.mkv" is in the
        job's rip folder but isn't among its files. Before the rip watcher was fixed, every
        second title of a disc could be lost this way while the previous title's file was being
        rewritten. Each one gets the language step it missed. Returns the jobs changed."""
        out = []
        for job in self.jobs():
            if job.id in self._runs or job.stage not in ("match", "failed") or not os.path.isdir(job.rip_dir):
                continue
            have = {f.title_index for f in job.files}
            names = os.listdir(job.rip_dir)
            found = []
            for tp in job.titles_progress:
                ti = tp.title_index
                own = next((n for n in names if n.lower().endswith(f"_t{ti:02d}.mkv")), None)
                if ti in have or own is None or tp.state in LOCKED_STATES:
                    continue
                path = os.path.join(job.rip_dir, own)
                try:
                    hdr.remux_tracks(path, path, job.audio_langs, job.subtitle_langs, self.cfg)
                    info = hdr.probe(path, self.cfg)
                except Exception:  # noqa: BLE001 -- a file that can't be read stays lost, as it was
                    continue
                size = os.path.getsize(path)
                job.files.append(RippedFile(title_index=ti, path=path, size_bytes=size, hdr=info))
                tp.state, tp.note, tp.total_bytes, tp.ripped_bytes, tp.rip_rate = "ripped", "", size, size, 0.0
                found.append(ti)
            if found:
                job.files.sort(key=lambda f: f.title_index)
                n = len(job.files)
                job.status = f"Ripped {n} title{'s' if n != 1 else ''}. Match them to episodes next."
                if job.stage == "failed" and len(job.files) == len(job.titles_progress):
                    job.stage = "match"
                self._save(job)
                out.append((job, found))
        return out

    def cleanup(self, apply: bool = True) -> list[str]:
        """Delete the local rips the clean-up setting says are due. Uses free_local's rule: only a
        file whose NAS copy was verified and is still there at the same size."""
        removed = []
        for job in self.jobs():
            if job.stage != "done" or not job.copied_at or job.id in self._runs or job.id in self._busy:
                continue
            if not transfer.due_for_cleanup(job.copied_at, self.cfg.cleanup):
                continue
            with self.job_lock:
                job = self._enc_jobs.get(job.id, job)
                for f in job.files:
                    if self._safe_local(job, f, check_nas=True):
                        removed.append(f.path)
                        if apply:
                            self._delete_local(f)
                for e in job.encodes:
                    if self._safe_av1(job, e, check_nas=True):
                        removed.append(e.path)
                        if apply:
                            self._delete_av1(e)
            if apply:
                self._remove_empty_dir(job.rip_dir)
                self._save(job)
        return removed

    # --- the Queue page -----------------------------------------------------

    def _card_text(self, job: Job, ti: int) -> str:
        st = self._card_state.get(job.id, {}).get(ti)
        return st.text if st is not None and st.text else ""

    def _show_episodes(self, show_id: int) -> list | None:
        """Every episode of a show, asked of TVmaze once per show. None when it can't be had
        (offline); that is asked again after a few minutes, not on every refresh."""
        hit = self._show_eps.get(show_id)
        if hit is not None and (hit[1] is not None or time.monotonic() - hit[0] < EPISODES_RETRY):
            return hit[1]
        try:
            eps = list(self.tvmaze.episodes(show_id, None))
        except Exception:  # noqa: BLE001 -- offline or a bad reply: the jobs' own matches are used instead
            eps = None
        self._show_eps[show_id] = (time.monotonic(), eps)
        return eps

    @staticmethod
    def _local_size(f: RippedFile) -> int:
        if f.local_deleted or not f.path:
            return 0
        try:
            return os.path.getsize(f.path)
        except OSError:
            return 0

    def _rip_folder_bytes(self) -> int:
        total = 0
        for root, _dirs, names in os.walk(self.cfg.rip_path):
            for n in names:
                try:
                    total += os.lstat(os.path.join(root, n)).st_size
                except OSError:
                    pass
        return total

    @staticmethod
    def _delete_local(f: RippedFile) -> int:
        try:
            size = os.path.getsize(f.path)
            os.remove(f.path)
        except FileNotFoundError:
            size = 0
        f.local_deleted = True
        return size

    @staticmethod
    def _delete_av1(e: EncodeJob) -> int:
        try:
            size = os.path.getsize(e.path)
            os.remove(e.path)
        except FileNotFoundError:
            size = 0
        e.local_deleted = True
        return size

    @staticmethod
    def _remove_empty_dir(path: str) -> None:
        """Remove a job's rip folder once nothing but an empty work folder is left in it."""
        if not path or not os.path.isdir(path):
            return
        work = os.path.join(path, ".work")
        if os.path.isdir(work) and not os.listdir(work):
            os.rmdir(work)
        if not os.listdir(path):
            os.rmdir(path)

    def confirm_title(self, job: Job, title_index: int, episode: Episode) -> None:
        """Match one title to this episode and confirm it. During a per-title rip it's copied as soon
        as it's ripped; a title already on the NAS keeps its match."""
        with self.job_lock:
            matches = _copy.deepcopy(job.matches)
            m = next((m for m in matches if m.title_index == title_index), None)
            if m is None:
                m = Match(title_index, "episode", confidence="manual", source="manual")
                matches.append(m)
                matches.sort(key=lambda x: x.title_index)
            old = m.episode
            changed = m.target != "episode" or old is None or (old.season, old.number) != (episode.season, episode.number)
            m.target, m.episode, m.confirmed = "episode", _copy.deepcopy(episode), True
            if changed:
                m.source, m.confidence, m.episode_end, m.note = "manual", "manual", None, ""
            self.update_matches(job, matches)

    def dismiss(self, job: Job) -> None:
        """Take a job out of the queue; History still lists it. A failed AV1 version is given up
        (skipped), so its remux may be deleted; one still being made carries on."""
        with self.job_lock:
            live = self._enc_jobs.get(job.id)
            if live is not None and live is not job:
                live.dismissed = True
            job.encodes = live.encodes if live is not None else job.encodes
            for e in job.encodes:
                if e.state == "failed":
                    _remove_av1_files(e)
                    e.state, e.encoded = "skipped", False
                    e.note = (e.note + " It was given up, so the remux is kept as it is.").strip()
            job.dismissed = True
            self._save(job)

    def free_local(self, job: Job | None = None) -> int:
        """Delete local rips already verified on the NAS (and still there at the same size), for one
        job or every job. A file that isn't verified is never deleted. Returns the bytes freed."""
        freed = 0
        for j in [job] if job is not None else self.jobs():
            with self.job_lock:
                j = self._enc_jobs.get(j.id, j)
                if j.id in self._runs or j.id in self._busy:
                    continue
                gone = [f for f in j.files if self._safe_local(j, f, check_nas=True)]
                for f in gone:
                    freed += self._delete_local(f)
                av = [e for e in j.encodes if self._safe_av1(j, e, check_nas=True)]
                for e in av:
                    freed += self._delete_av1(e)
                gone += av
                if gone:
                    self._remove_empty_dir(j.rip_dir)
                    self._save(j)
        return freed

    def nas_folder(self, job: Job) -> str:
        """The folder on the NAS mount the job's files went to: the season folder, or the film's."""
        with self.job_lock:
            rels = [f.dest_rel for f in job.files if f.dest_rel]
        if not rels:
            rels = [p for _, p in self.preview_paths(job)]
        dirs = [posixpath.dirname(r) for r in rels]
        # Extras sit a folder deeper (Show/Featurettes); the episodes' or the film's folder is the one to open.
        main = [d for d in dirs if posixpath.basename(d) not in naming.EXTRAS_FOLDERS] or \
            [posixpath.dirname(d) for d in dirs]
        if main:
            folder = collections.Counter(main).most_common(1)[0][0]
        elif job.show.strip():
            scheme = naming.Scheme.from_config(self.cfg)
            folder = naming.show_folder(job.show, job.year, scheme)
            if self._layout(job) != "movie" and job.season is not None:
                folder += "/" + naming.season_folder(job.season, scheme)
        else:
            folder = ""
        return os.path.join(job.destination, folder) if folder else job.destination

    def reverify(self, job: Job, progress: Progress) -> list[str]:
        """Read every NAS copy back and compare it with the checksum taken when it was copied (or
        the local rip when there is none). Returns one sentence per copy that is missing or
        different, [] when all match. Those are marked failed, so the queue offers to copy them again."""
        if not os.path.isdir(job.destination):
            raise FileNotFoundError(f"{job.destination} isn't there. Check that the NAS share is mounted.")
        with self.job_lock:
            # A failed one with a checksum is read too: if its NAS copy matches after all, it's good again.
            files = [f for f in job.files if f.dest_rel and (f.state in ("verified", "copied")
                                                            or (f.state == "failed" and f.sha256))]
            by = {m.title_index: m for m in job.matches}
            self._busy.add(job.id)
        total = sum(f.size_bytes for f in files) or 1
        done, problems, bad, good = 0, [], [], []
        try:
            for f in files:
                label = _title_label(f.title_index, by.get(f.title_index))
                dest = os.path.join(job.destination, f.dest_rel)
                base = done

                def part(fr: float, _t: str, base=base, f=f, label=label) -> None:
                    progress((base + fr * f.size_bytes) / total, f"Checking {label} · {_of(fr * f.size_bytes, f.size_bytes)}")

                progress(base / total, f"Checking {label}")
                try:
                    size = os.path.getsize(dest)
                except OSError:
                    problems.append(f"The NAS copy of {label} is missing.")
                    bad.append((f, f"{REVERIFY_NOTE}, and its NAS copy is missing."))
                    done += f.size_bytes
                    continue
                want = f.sha256
                if not want and self._local_size(f) > 0:
                    want = transfer.sha256(f.path)
                if f.size_bytes and size != f.size_bytes:
                    ok = False
                elif want:
                    ok = transfer.sha256(dest, part) == want
                else:
                    ok = True       # no checksum and no local copy: the size is all there is to compare
                if not ok:
                    problems.append(f"The NAS copy of {label} doesn't match the rip.")
                    bad.append((f, f"{REVERIFY_NOTE}, and its NAS copy doesn't match the rip."))
                elif f.state == "failed":
                    good.append(f)
                done += f.size_bytes
        finally:
            with self.job_lock:
                self._busy.discard(job.id)
        with self.job_lock:
            for f, note in bad:
                f.state = "failed"
                tp = _tp(job, f.title_index)
                if tp is None:
                    tp = TitleProgress(f.title_index, total_bytes=f.size_bytes, ripped_bytes=f.size_bytes)
                    job.titles_progress.append(tp)
                tp.state, tp.note = "failed", note
            for f in good:
                f.state = "verified"
                tp = _tp(job, f.title_index)
                if tp is not None:
                    tp.state, tp.note = "verified", ""
            if bad:
                job.dismissed = False
                job.status = f"{_plural(len(bad), 'NAS copy', 'NAS copies')} of {len(files)} need copying again."
            else:
                job.status = f"Checked again: all {_plural(len(files), 'NAS copy', 'NAS copies')} match the rip."
            self._save(job)
        progress(1.0, job.status)
        return problems

    def plex_link(self, job: Job) -> str:
        """Plex Web's page for this film, show or season when Plex can find it, else Plex Web's
        home page. Never raises: offline, signed out or not found all give the home page."""
        if not self.cfg.plex_url:
            return "https://app.plex.tv/desktop"      # Plex's own web app finds the user's servers
        root = f"{self.cfg.plex_url.rstrip('/')}/web/index.html"
        try:
            token = config.load_secret("plex_token")
            if not token or not job.library or not job.show.strip():
                return root
            plex = Plex(self.cfg.plex_url, token, fetch=self.plex_fetch)
            section = plex.section(job.library)
            if not section:
                return root
            item = plex.find(section["key"], job.show, job.year)
            if not item or not item.get("ratingKey"):
                return root
            key = str(item["ratingKey"])
            if self._layout(job) != "movie" and job.season is not None:
                season = plex.season(key, job.season)
                if season and season.get("ratingKey"):
                    key = str(season["ratingKey"])
            return plex.web_url(plex.identity(), key)
        except Exception:  # noqa: BLE001 -- a link is never worth an error dialog
            return root

    # --- the AV1 second version ---------------------------------------------
    # After a 4K title's remux is verified on the NAS, an EncodeJob is queued for it. One encoder
    # thread for the whole app takes them in the order queued: encode (av1.encode, at low
    # priority), check the quality (av1.check_quality), copy next to the remux and check the copy,
    # ask Plex to scan, and record the time and size for the estimates. It never holds job_lock
    # while a tool runs, and never touches the drive.

    def _av1_wanted(self, job: Job) -> bool:
        return job.av1 != "off" and (job.av1 in ("cpu", "gpu") or bool(self.cfg.av1_enabled))

    def _encode_settings(self, job: Job) -> "av1.EncodeSettings":
        encoder = job.av1 if job.av1 in ("cpu", "gpu") else (self.cfg.av1_encoder or "cpu")
        return av1.EncodeSettings(encoder=encoder, quality=int(self.cfg.av1_quality), grain=bool(self.cfg.av1_grain))

    def _av1_label(self, job: Job, ti: int) -> str:
        """"The AV1 version" for a film, "The AV1 version of S01E02 “Half Loop”" for an episode."""
        if self._layout(job) == "movie" and len(job.encodes) <= 1:
            return "The AV1 version"
        m = next((m for m in job.matches if m.title_index == ti), None)
        return f"The AV1 version of {_title_label(ti, m)}"

    def _queue_encode(self, job: Job, f: RippedFile) -> bool:
        """Queue the AV1 version of a title whose remux was just verified on the NAS, when it's a 4K
        title and this disc wants one. Hold job_lock. Returns whether one was queued."""
        if f.state != "verified" or not f.dest_rel or not f.path or not self._av1_wanted(job):
            return False
        width = _video_width(job, f.title_index)
        if not av1.is_4k(width):
            return False
        live = self._enc_jobs.get(job.id)
        if live is not None and live is not job:
            job.encodes = live.encodes
        if _enc(job, f.title_index) is not None:
            return False
        s = self._encode_settings(job)
        rel = av1.av1_name(f.dest_rel)
        job.encodes.append(EncodeJob(
            f.title_index, encoder=s.encoder, quality=s.quality, grain=s.uses_grain, src=f.path,
            path=os.path.join(os.path.dirname(f.path), posixpath.basename(rel)), dest_rel=rel,
            queued=now_iso(), width=width))
        self._enc_jobs.setdefault(job.id, job)
        self._kick_encoder()
        return True

    def _kick_encoder(self) -> None:
        """Start the encoder thread if it isn't running, and wake it."""
        with self._enc_thread_lock:
            if self._enc_thread is None or not self._enc_thread.is_alive():
                self._enc_thread = threading.Thread(target=self._encode_loop, daemon=True, name="discshelf-encoder")
                self._enc_thread.start()
        self._enc_wake.set()

    def _encode_gate(self) -> tuple[bool, str, str]:
        """Whether the "when to encode" setting lets an encode run now: (open, why a waiting encode
        waits, why a running one is paused)."""
        when = self.cfg.av1_when
        if when == "idle" and self._runs:
            return (False, "It waits until nothing is ripping, as Settings says.",
                    "Paused while a disc rips. It carries on when the rip finishes.")
        if when == "night":
            lo, hi = NIGHT_HOURS
            if not lo <= self.clock().hour < hi:
                return (False, "It waits until 1 am. Settings says encodes run from 1 am to 7 am.",
                        "Paused at 7 am. It carries on at 1 am, as Settings says.")
        return True, "", ""

    def _next_encode(self) -> tuple[Job, EncodeJob] | None:
        """The waiting encode queued first, every disc. Hold job_lock."""
        best = None
        for job in self._enc_jobs.values():
            for e in job.encodes:
                if e.state == "waiting":
                    key = (e.queued, job.created, e.title_index)
                    if best is None or key < best[0]:
                        best = (key, job, e)
        return (best[1], best[2]) if best else None

    def _forget_idle(self) -> None:
        """Stop tracking discs with nothing left to encode. Hold job_lock."""
        for jid in [jid for jid, j in self._enc_jobs.items() if not any(e.state in ENCODE_ACTIVE for e in j.encodes)]:
            del self._enc_jobs[jid]

    def _encode_loop(self) -> None:
        while True:
            self._enc_wake.clear()
            with self.job_lock:
                pick = self._next_encode()
                if pick is None:
                    with self._enc_thread_lock:
                        if self._enc_thread is threading.current_thread():
                            self._enc_thread = None
                    self._forget_idle()
                    return
                ok, why, _ = self._encode_gate()
                for job in self._enc_jobs.values():
                    changed = False
                    for e in job.encodes:
                        if e.state == "waiting" and e.note != why:
                            e.note, changed = why, True
                    if changed:
                        self._save(job)
            if not ok:
                self._enc_wake.wait(self.encode_wait)
                continue
            self._run_encode(*pick)

    def _run_encode(self, job: Job, e: EncodeJob) -> None:
        """Encode, check, copy and scan one title's AV1 version. Never raises."""
        ctrl = _EncodeCtrl(job.id, e.title_index)
        with self.job_lock:
            self._enc_ctrl = ctrl
            s = av1.EncodeSettings(encoder=e.encoder, quality=e.quality, grain=e.grain)
            remux = _file(job, e.title_index)
            source_bytes = remux.size_bytes if remux else 0
            title = next((t for t in job.titles if t.index == e.title_index), None)
            duration = title.duration_s if title else 0
            label = self._av1_label(job, e.title_index)
        try:
            if not (e.encoded and os.path.isfile(e.path)):
                if not os.path.isfile(e.src):
                    raise av1.EncodeError("The local remux is gone, so there's nothing to encode it from.")
                with self.job_lock:
                    e.state, e.note, e.started, e.finished = "encoding", "", now_iso(), ""
                    e.fraction, e.fps, e.size_bytes, e.projected_bytes, e.eta_seconds = 0.0, 0.0, 0, 0, None
                    e.encoded, e.vmaf, e.sha256 = False, None, ""
                    self._save(job)
                res = self._encode_step(job, e, s, ctrl)
                with self.job_lock:
                    e.encoded, e.size_bytes, e.seconds, e.fps = True, res.size_bytes, res.seconds, res.fps
                    e.fraction, e.eta_seconds, e.projected_bytes = 1.0, 0.0, res.size_bytes
                    self._save(job)
            if ctrl.cancel.is_set():
                raise av1.Cancelled()
            if self.cfg.av1_check and e.vmaf is None:
                with self.job_lock:
                    e.state, e.note = "checking", ""
                    self._save(job)
                q = av1.check_quality(e.src, e.path, self.cfg)
                if ctrl.cancel.is_set():
                    raise av1.Cancelled()
                with self.job_lock:
                    e.vmaf = q.score
                    if not q.passed:
                        _remove_av1_files(e)
                        e.state, e.encoded, e.finished = "failed", False, now_iso()
                        e.note = (f"{label} scored {_score(q.score, q.threshold)} on the quality check "
                                  f"(needs {q.threshold:.0f}), so it wasn't copied.")
                        job.dismissed = False
                        self._save(job)
                        return
            with self.job_lock:
                e.state, e.note = "copying", ""
                self._save(job)
            self._copy_av1(job, e, ctrl.cancel)
            with self.job_lock:
                e.state, e.note, e.finished = "verified", "", now_iso()
                self._save(job)
            try:
                self._scan_plex(job, [e])
            except Exception:  # noqa: BLE001 -- the copy is verified; Plex finds it on its next scan anyway
                pass
            if e.seconds > 0:
                try:
                    self._stats().record(s, e.width, duration, e.seconds, e.size_bytes, source_bytes)
                except OSError:
                    pass        # the estimates stay as they were
        except (av1.Cancelled, transfer.Cancelled):
            with self.job_lock:
                _remove_av1_files(e)
                e.state, e.encoded, e.held, e.finished = "skipped", False, False, now_iso()
                e.fps, e.eta_seconds = 0.0, None
                e.note = "Stopped. The remux stays in Plex as it is."
                self._save(job)
        except Exception as x:  # noqa: BLE001 -- shown on the queue card; the encoder carries on with the next
            with self.job_lock:
                if e.state in ("encoding", "paused"):
                    _remove_av1_files(e)     # a finished file whose check or copy failed is kept for a retry
                    e.encoded = False
                why = str(x).strip() or type(x).__name__
                if e.state == "copying":
                    e.note = f"{label} didn't copy: {why}"
                elif why.startswith("The ") or why.startswith("Couldn't"):
                    e.note = f"{label} wasn't made. {why}"
                else:
                    e.note = f"{label} wasn't made: {why}"
                if not e.note.endswith("."):
                    e.note += "."
                e.state, e.finished, e.fps, e.eta_seconds = "failed", now_iso(), 0.0, None
                job.dismissed = False
                self._save(job)
        finally:
            with self.job_lock:
                if self._enc_ctrl is ctrl:
                    self._enc_ctrl = None
                self._forget_idle()

    def _encode_step(self, job: Job, e: EncodeJob, s: "av1.EncodeSettings", ctrl: "_EncodeCtrl") -> "av1.EncodeResult":
        """av1.encode on a thread of its own, so this one can pause and resume it as the setting
        and the Pause button say."""
        box: dict = {}
        last = [time.monotonic()]

        def on_progress(p) -> None:
            with self.job_lock:
                e.fraction, e.fps, e.size_bytes = p.fraction, p.fps, p.size_bytes
                e.projected_bytes, e.eta_seconds = p.projected_bytes, p.eta_seconds
                now = time.monotonic()
                if now - last[0] >= self.encode_save_interval:
                    last[0] = now
                    self._save(job)

        def go() -> None:
            try:
                box["result"] = av1.encode(e.src, e.path, s, self.cfg, progress=on_progress,
                                           cancel=ctrl.cancel, pause=ctrl.pause)
            except BaseException as x:      # handed to the encoder thread
                box["error"] = x

        self._apply_pause(job, e, ctrl)
        th = threading.Thread(target=go, daemon=True, name="discshelf-av1")
        th.start()
        while th.is_alive():
            th.join(self.encode_poll)
            if th.is_alive():
                self._apply_pause(job, e, ctrl)
        if "error" in box:
            raise box["error"]
        return box["result"]

    def _apply_pause(self, job: Job, e: EncodeJob, ctrl: "_EncodeCtrl") -> None:
        """Suspend the running encode while you paused it or the setting holds it back, and resume it after."""
        ok, _, why = self._encode_gate()
        with self.job_lock:
            hold = e.held or not ok
            if hold != ctrl.pause.is_set():
                if hold:
                    ctrl.pause.set()
                else:
                    ctrl.pause.clear()
            if e.state not in ("encoding", "paused"):
                return
            state = "paused" if hold else "encoding"
            note = ("Paused. Resume it to carry on where it stopped." if e.held else why) if hold else ""
            if (e.state, e.note) != (state, note):
                e.state, e.note = state, note
                if hold:
                    e.fps, e.eta_seconds = 0.0, None
                self._save(job)

    def _copy_av1(self, job: Job, e: EncodeJob, cancel: threading.Event) -> None:
        """Copy the AV1 file next to the remux on the NAS and check it the way remuxes are checked."""
        if not os.path.isdir(job.destination):
            raise FileNotFoundError(f"{job.destination} isn't there. Check that the NAS share is mounted.")
        dest = os.path.join(job.destination, e.dest_rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        transfer.copy(e.path, dest, lambda _f, _t: None, cancel)
        if not e.sha256 and self.cfg.verify == "readback":
            digest = transfer.sha256(e.path)
            with self.job_lock:
                e.sha256 = digest
        if not transfer.verify(e.path, dest, self.cfg.verify, e.sha256 or None, None):
            raise RuntimeError(f"the NAS copy of {os.path.basename(dest)} doesn't match. Choose Encode AV1 again "
                               "to copy it again.")

    def _stats(self) -> "av1.Stats":
        if self.av1_stats is None:
            self.av1_stats = av1.Stats()
        return self.av1_stats

    def av1_estimate(self, settings: "av1.EncodeSettings", width: int = 3840, duration_s: float = 9960,
                     source_bytes: float = 62e9) -> tuple[int, float, bool]:
        """(size in bytes, encoding seconds, measured) for one encode with these settings; by default
        a 2 h 46 min 4K film from a 62 GB remux. measured is False until real encodes were recorded."""
        return self._stats().estimate(settings, width, duration_s, int(source_bytes))

    def _live(self, job: Job) -> Job:
        """The encoder's own Job for this disc when it has one. Hold job_lock."""
        live = self._enc_jobs.get(job.id)
        if live is not None and live is not job:
            job.encodes = live.encodes
            return live
        return job

    def _is_current(self, job: Job, e: EncodeJob) -> bool:
        c = self._enc_ctrl
        return c is not None and c.job_id == job.id and c.title_index == e.title_index

    def pause_encode(self, job: Job) -> bool:
        """Pause this disc's running encode (it's suspended where it is), or hold its waiting ones
        back. A quality check or a copy can't be paused. Returns whether anything changed."""
        with self.job_lock:
            job = self._live(job)
            ctrl = self._enc_ctrl if self._enc_ctrl is not None and self._enc_ctrl.job_id == job.id else None
            current = next((e for e in job.encodes if self._is_current(job, e)), None)
            changed = False
            if current is not None:
                if current.state in ("encoding", "paused") and not current.held:
                    current.held = changed = True
            else:
                for e in job.encodes:
                    if e.state == "waiting":
                        e.state, e.held, e.note = "paused", True, "Paused before it started. Resume it to encode it."
                        changed = True
            if changed:
                self._save(job)
        if current is not None and ctrl is not None:
            self._apply_pause(job, current, ctrl)
        return changed

    def resume_encode(self, job: Job) -> bool:
        """Undo pause_encode. Returns whether anything changed."""
        with self.job_lock:
            job = self._live(job)
            ctrl = self._enc_ctrl if self._enc_ctrl is not None and self._enc_ctrl.job_id == job.id else None
            current = None
            changed = False
            for e in job.encodes:
                if not e.held:
                    continue
                e.held, changed = False, True
                if self._is_current(job, e):
                    current = e
                elif e.state == "paused":
                    e.state, e.note = "waiting", ""
            if changed:
                self._enc_jobs.setdefault(job.id, job)
                self._save(job)
        if current is not None and ctrl is not None:
            self._apply_pause(job, current, ctrl)
        if changed:
            self._kick_encoder()
        return changed

    def stop_encode(self, job: Job) -> bool:
        """Stop this disc's running encode and skip the rest of its AV1 versions, failed ones
        included. The remuxes are untouched. Returns whether anything changed."""
        with self.job_lock:
            job = self._live(job)
            changed = False
            for e in job.encodes:
                if self._is_current(job, e):
                    self._enc_ctrl.cancel.set()     # the encoder thread marks it skipped
                    changed = True
                elif e.state in ("waiting", "paused", "failed"):
                    _remove_av1_files(e)
                    e.state, e.held, e.encoded, e.finished = "skipped", False, False, now_iso()
                    e.note = "Skipped. The remux stays in Plex as it is."
                    changed = True
            if changed:
                self._save(job)
                self._forget_idle()
        self._enc_wake.set()
        return changed

    def retry_encode(self, job: Job) -> int:
        """Queue this disc's failed AV1 versions again, at the back of the queue. One whose file
        was made and checked but didn't copy is only copied again; the others are encoded again
        with the current Settings. Returns how many were queued."""
        with self.job_lock:
            job = self._live(job)
            s = self._encode_settings(job)
            n = 0
            for e in job.encodes:
                if e.state != "failed":
                    continue
                if not (e.encoded and os.path.isfile(e.path)):
                    e.encoded, e.vmaf, e.sha256 = False, None, ""
                    e.encoder, e.quality, e.grain = s.encoder, s.quality, s.uses_grain
                    e.fraction, e.fps, e.size_bytes, e.projected_bytes, e.eta_seconds = 0.0, 0.0, 0, 0, None
                e.state, e.note, e.finished, e.held, e.queued = "waiting", "", "", False, now_iso()
                n += 1
            if n:
                self._enc_jobs.setdefault(job.id, job)
                self._save(job)
        if n:
            self._kick_encoder()
        return n

    def encode_status(self) -> EncodeStatus:
        """What the encoder is doing, across every disc."""
        ok, why, paused_why = self._encode_gate()
        with self.job_lock:
            ctrl = self._enc_ctrl
            out = EncodeStatus(gate_open=ok, note="" if ok else why)
            for job in self._enc_jobs.values():
                for e in job.encodes:
                    if e.state == "waiting":
                        out.waiting += 1
                    if ctrl is not None and ctrl.job_id == job.id and ctrl.title_index == e.title_index:
                        out.job_id, out.title_index, out.state = job.id, e.title_index, e.state
            if not ok and out.state == "paused":
                out.note = paused_why
        return out

    def recover_encodes(self) -> list[tuple[Job, list[int]]]:
        """On app start: encodes the last run left encoding, checking or copying go back to waiting
        (a half-made AV1 file is deleted; a finished one is kept and only checked and copied), and
        the encoder carries on with the queue. Returns the jobs changed and their titles."""
        out = []
        with self.job_lock:
            for job in self.store.all():
                if job.id in self._enc_jobs or not job.encodes:
                    continue
                back, changed = [], False
                for e in job.encodes:
                    if e.state not in ENCODE_ACTIVE or e.state == "waiting":
                        continue
                    changed = True
                    finished = e.encoded and e.path and os.path.isfile(e.path)
                    if not finished:
                        _remove_av1_files(e)
                        e.encoded, e.vmaf, e.sha256 = False, None, ""
                        e.fraction, e.fps, e.size_bytes, e.projected_bytes = 0.0, 0.0, 0, 0
                    e.eta_seconds = None
                    if e.state in ENCODE_RUNNING or (e.state == "paused" and not e.held):
                        back.append(e.title_index)
                        e.state, e.note = "waiting", ""
                    elif e.state == "paused":
                        e.note = ("Paused. Resume it to carry on." if finished
                                  else "Paused. Resume it to encode it from the start.")
                self._enc_jobs[job.id] = job
                if changed:
                    self._save(job)
                if back:
                    out.append((job, back))
            self._forget_idle()
            waiting = self._next_encode() is not None
        if waiting:
            self._kick_encoder()
        return out

    # --- Plex sign-in -------------------------------------------------------

    def plex_sign_in_start(self) -> tuple[int, str, str]:
        pin_id, code, url = Plex(self.cfg.plex_url, "").pin_start()
        self._pin_codes[pin_id] = code
        return pin_id, code, url

    def plex_sign_in_poll(self, pin_id: int) -> bool:
        token = Plex(self.cfg.plex_url, "").pin_poll(pin_id, self._pin_codes.get(pin_id))
        if token:
            config.save_secret("plex_token", token)
            return True
        return False

    def plex_signed_in(self) -> bool:
        return bool(config.load_secret("plex_token"))

    def plex_scan_folder(self, library: str, rel: str) -> str:
        """Ask Plex to scan one folder of a library; rel is under the destination folder. Returns
        a sentence for the Library page. Raises PlexError when Plex can't be asked."""
        token = config.load_secret("plex_token")
        if not library or not self.cfg.plex_url or not token:
            return "Plex wasn't asked to scan: sign in to Plex and choose its library for this destination in Settings."
        plex = Plex(self.cfg.plex_url, token, fetch=self.plex_fetch)
        section = plex.section(library)
        if not section:
            raise PlexError(f"Plex has no library called “{library}”. Choose the right one in Settings.")
        plex.refresh(section["key"], plex.plex_path(section, rel))
        return f"Plex is scanning {rel}."

    def plex_libraries(self) -> list[str]:
        token = config.load_secret("plex_token")
        if not token:
            return []
        try:
            return [s["title"] for s in Plex(self.cfg.plex_url, token).sections()]
        except PlexError:
            return []
