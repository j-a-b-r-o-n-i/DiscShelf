#!/usr/bin/env python3
"""
Tests for ripstats.py, on the timings of a real Blu-ray (Bluey Season Two, The First Half):
25 titles of about 440 s, MakeMKV estimating 1.9 GB each and writing about 0.93 GB, about 42 s
to open the disc before each title, and two titles that read at about 5.8 MB/s against 8 to 13.

What matters: the time left counts opening the disc for every title still to rip and uses the
sizes MakeMKV actually writes on this disc, not its estimate; the speed comes from reading
time only; the slow titles are flagged; there's no estimate before any speed is known; and the
current title, opening or reading, is placed first on the planned timeline.

Run:  python3 discshelf/test_ripstats.py    (also works under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import ripstats  # noqa: E402
from discshelf.models import Span  # noqa: E402

OPEN = [42, 41, 42, 42, 42, 43, 42, 44, 46, 42, 42, 42, 44, 42]
READ = [118, 132, 120, 109, 103, 95, 92, 78, 82, 72, 73, 111, 165, 154]
SIZE = [930, 909, 925, 920, 931, 981, 900, 921, 935, 934, 936, 945, 943, 912]
EST = 1925_000_000


def disc(done: int = 14):
    spans, t = [], 0.0
    for i in range(done):
        spans.append(Span("open", i + 1, t, t + OPEN[i]))
        spans.append(Span("read", i + 1, t + OPEN[i], t + OPEN[i] + READ[i], SIZE[i] * 1_000_000))
        t += OPEN[i] + READ[i]
    return spans, t


def run() -> int:
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    order = list(range(1, 26))
    est = {ti: EST for ti in order}

    # Title 15 has opened in 49 s and read 650 MB in 80 s.
    spans, t = disc()
    spans += [Span("open", 15, t, t + 49), Span("read", 15, t + 49, None)]
    now = t + 49 + 80
    e = ripstats.estimate(order, est, spans, now, {15: 650_000_000})
    check(e is not None, "no estimate with 14 titles done")
    rate = sum(SIZE[9:14]) * 1e6 / sum(READ[9:14])
    check(abs(e.rate - rate) < 1, f"speed over the last 5 titles' reading time: {e.rate} != {rate}")
    check(abs(e.ratio - sum(SIZE) / 1925 / 14) < 0.001, f"size ratio: {e.ratio}")
    check(abs(e.open_s - sum(OPEN) / 14) < 0.01, f"opening time: {e.open_s}")
    check(e.titles_left == 11, f"titles left: {e.titles_left}")
    per = EST * e.ratio
    want = (per - 650e6) / rate + 10 * (e.open_s + per / rate)
    check(abs(e.seconds - want) < 1, f"time left: {e.seconds} != {want}")
    check(20 * 60 < e.seconds < 32 * 60, f"about 27 min left on this disc: {e.seconds / 60:.1f} min")
    check(e.low < e.seconds < e.high, f"range: {e.low} {e.seconds} {e.high}")
    check([p.title_index for p in e.planned] == list(range(15, 26)), f"planned: {[p.title_index for p in e.planned]}")
    check(e.planned[0].read_start == t + 49 and abs(e.planned[-1].end - now - e.seconds) < 0.01,
          "the planned timeline doesn't start at the current title or end at the estimate")
    check(e.planned[1].open_start == e.planned[0].end, "the next title opens when the current one ends")

    # MakeMKV's own estimate of 1.9 GB per title would have given twice the reading time.
    naive = (11 * EST - 650e6) / rate
    check(naive > 1.6 * e.seconds - 10 * e.open_s, "the size ratio isn't applied")

    check(ripstats.slow_titles(ripstats.timings(spans)) == {13, 14}, f"slow: {ripstats.slow_titles(ripstats.timings(spans))}")

    # Opening the disc for the next title: the opening time left is counted, then the whole title.
    spans, t = disc()
    spans.append(Span("open", 15, t, None))
    e = ripstats.estimate(order, est, spans, t + 10, {})
    check(e.planned[0].read_start == t + e.open_s, f"opening title: read starts at {e.planned[0].read_start - t}")
    check(abs(e.seconds - 11 * (e.open_s + EST * e.ratio / e.rate) + 10) < 1, f"while opening: {e.seconds}")

    # Nothing done yet: no estimate until the first title has read for 20 s, then its own speed.
    first = [Span("open", 1, 0, 42), Span("read", 1, 42, None)]
    check(ripstats.estimate(order, est, first[:1], 10, {}) is None, "an estimate while opening the first title")
    check(ripstats.estimate(order, est, first, 50, {1: 50_000_000}) is None, "an estimate after 8 s of reading")
    e = ripstats.estimate(order, est, first, 72, {1: 240_000_000})
    check(e is not None and abs(e.rate - 8e6) < 1 and e.recent == 0 and e.ratio == 1.0 and e.open_s == 42,
          f"first title: {e}")

    for f in failures:
        print("FAIL:", f)
    print(f"ripstats: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_ripstats():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
