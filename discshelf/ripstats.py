"""The rip timeline in numbers: how long each title took to open and to read, which read slowly,
and how long the rest of the disc will take.

Each MakeMKV run opens the disc before any data is written (about 40 s on a Blu-ray), and a
disc is ripped in one run or one run per title. So the time left is worked out per title,
from this disc's own titles: the time to open the disc, the size MakeMKV actually writes (its
own estimate is often about twice that), and the speed while reading. Everything here is plain arithmetic
over Span records; the pipeline and the window both call it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Span

RECENT = 5            # titles the reading speed is averaged over
SLOW = 0.75           # a title read below this share of that speed is flagged
DEFAULT_OPEN = 45.0   # seconds, until a title has been opened on this disc
MIN_READ = 20.0       # seconds of reading before the current title alone gives a speed


@dataclass
class Timing:
    title_index: int
    open_start: float | None = None
    read_start: float | None = None
    read_end: float | None = None
    bytes: int = 0

    @property
    def open_s(self) -> float | None:
        if self.open_start is None or self.read_start is None:
            return None
        return self.read_start - self.open_start

    @property
    def read_s(self) -> float | None:
        if self.read_start is None or self.read_end is None:
            return None
        return self.read_end - self.read_start

    @property
    def rate(self) -> float:
        s = self.read_s
        return self.bytes / s if s and s > 0 else 0.0


@dataclass
class Planned:
    """A title still to rip, placed on the timeline: when it should open, start reading and end."""
    title_index: int
    open_start: float
    read_start: float
    end: float
    bytes: float


@dataclass
class Estimate:
    seconds: float                  # until the last title is ripped
    low: float                      # at the fastest recent title's speed
    high: float                     # at the slowest's
    rate: float                     # bytes per second while reading, over the last few titles
    recent: int                     # how many titles that speed comes from (0: the current title alone)
    open_s: float                   # seconds to open the disc, per title
    ratio: float                    # what MakeMKV wrote over what it estimated, on this disc
    titles_left: int                # counting the current one
    per_title: float                # bytes expected from a typical title still to rip
    planned: list[Planned] = field(default_factory=list)


def timings(spans: list[Span]) -> dict[int, Timing]:
    out: dict[int, Timing] = {}
    for s in spans:
        t = out.setdefault(s.title_index, Timing(s.title_index))
        if s.kind == "open":
            t.open_start = s.start
        elif s.kind == "read":
            t.read_start, t.read_end = s.start, s.end
            t.bytes = s.bytes
    return out


def finished(tm: dict[int, Timing]) -> list[Timing]:
    """Titles read to the end, in the order they finished."""
    return sorted((t for t in tm.values() if t.read_end is not None and t.read_s), key=lambda t: t.read_end)


def slow_titles(tm: dict[int, Timing]) -> set[int]:
    """Titles read well below the speed of the titles around them."""
    done = finished(tm)
    out = set()
    for i, t in enumerate(done):
        around = done[max(0, i - RECENT):i] + done[i + 1:i + 1 + RECENT]
        secs = sum(o.read_s for o in around)
        if len(around) >= 2 and secs > 0 and t.rate < SLOW * sum(o.bytes for o in around) / secs:
            out.add(t.title_index)
    return out


def estimate(order: list[int], est: dict[int, int], spans: list[Span], now: float,
             ripped: dict[int, int]) -> Estimate | None:
    """The time left to rip the titles in order, or None until there is a reading speed.

    est is MakeMKV's size estimate for each title, ripped the bytes written so far for each
    (the current title's matters), now the time on the spans' clock."""
    tm = timings(spans)
    done = finished(tm)
    done_ids = {t.title_index for t in done}
    started = [ti for ti in order if ti in tm and ti not in done_ids]
    cur = tm[started[-1]] if started else None
    left = [ti for ti in order if ti not in done_ids and (cur is None or ti != cur.title_index)]

    recent = done[-RECENT:]
    secs = sum(t.read_s for t in recent)
    if recent and secs > 0:
        rate = sum(t.bytes for t in recent) / secs
    elif cur is not None and cur.read_start is not None and now - cur.read_start >= MIN_READ \
            and ripped.get(cur.title_index, 0) > 0:
        rate = ripped[cur.title_index] / (now - cur.read_start)
    else:
        return None
    if rate <= 0:
        return None
    rates = [t.rate for t in recent if t.rate > 0]
    fast, slow = (max(rates), min(rates)) if len(rates) >= 2 else (rate, rate)

    sized = [t for t in done if est.get(t.title_index)]
    ratio = (sum(t.bytes for t in sized) / sum(est[t.title_index] for t in sized)) if sized else 1.0
    typical = (sum(t.bytes for t in done) / len(done)) if done else 0.0

    def expect(ti: int) -> float:
        return est[ti] * ratio if est.get(ti) else typical

    opens = [t.open_s for t in done if t.open_s is not None and t.open_s > 0]
    if opens:
        open_s = sum(opens) / len(opens)
    elif cur is not None and cur.open_s:
        open_s = cur.open_s
    else:
        open_s = DEFAULT_OPEN

    def total(r: float) -> tuple[float, list[Planned]]:
        t, planned = now, []
        if cur is not None:
            size = expect(cur.title_index)
            if cur.read_start is None:      # still opening the disc
                start = (cur.open_start or now)
                read_at = max(now, start + open_s)
                end = read_at + size / r
                planned.append(Planned(cur.title_index, start, read_at, end, size))
            else:
                end = now + max(0.0, size - ripped.get(cur.title_index, 0)) / r
                planned.append(Planned(cur.title_index, cur.open_start if cur.open_start is not None else cur.read_start,
                                       cur.read_start, end, size))
            t = end
        for ti in left:
            size = expect(ti)
            planned.append(Planned(ti, t, t + open_s, t + open_s + size / r, size))
            t += open_s + size / r
        return t - now, planned

    seconds, planned = total(rate)
    sizes = [expect(ti) for ti in left] or ([expect(cur.title_index)] if cur else [])
    return Estimate(seconds=seconds, low=total(fast)[0], high=total(slow)[0], rate=rate,
                    recent=len(recent), open_s=open_s, ratio=ratio,
                    titles_left=len(left) + (1 if cur is not None else 0),
                    per_title=sum(sizes) / len(sizes) if sizes else 0.0, planned=planned)
