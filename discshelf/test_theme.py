#!/usr/bin/env python3
"""
Tests for theme.py, on made-up fingerprints: random values for each title's own audio, and
one stretch of the same values (a few bits changed, as a different encode gives) for the
theme song, at a different point in each title. No fpcalc or audio.

What matters: the shared stretch is found where it is in each title; a show's theme is
learned from the first two titles that share it and found in later ones without learning
again; a second theme (a special, a season whose theme changes) is learned as another
version; two copies of the same episode don't make their whole opening a "theme"; and
fpcalc missing or failing gives no fingerprint rather than an error.

Run:  python3 discshelf/test_theme.py    (also works under pytest)
"""
import json
import os
import random
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import theme  # noqa: E402
from discshelf.theme import ITEM_S, ThemeBook, common_run, fingerprint  # noqa: E402

LISTEN = int(theme.LISTEN_S / ITEM_S)


def noise(rng: random.Random, n: int) -> list[int]:
    return [rng.getrandbits(32) for _ in range(n)]


def blur(rng: random.Random, values: list[int], bits: int = 3) -> list[int]:
    """The same sound from another encode: a few bits of each value differ."""
    out = []
    for v in values:
        for _ in range(rng.randint(0, bits)):
            v ^= 1 << rng.randrange(32)
        out.append(v)
    return out


def opening(rng: random.Random, song: list[int], at_s: float, n: int = LISTEN) -> list[int]:
    """A title's fingerprint with the song starting at_s seconds in."""
    k = int(at_s / ITEM_S)
    fp = noise(rng, k) + blur(rng, song)
    return (fp + noise(rng, n))[:n]


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    rng = random.Random(7)
    song = noise(rng, int(30 / ITEM_S))          # a 30 s theme
    other_song = noise(rng, int(25 / ITEM_S))    # the theme of a special
    near = lambda a, b: abs(a - b) <= 1.0  # noqa: E731

    # The shared stretch, found at its place in each title.
    a, b = opening(rng, song, 0.0), opening(rng, song, 95.0)
    found = common_run(a, b)
    check(found is not None and near(found.a_start * ITEM_S, 0) and near(found.a_end * ITEM_S, 30)
          and near(found.b_start * ITEM_S, 95) and near(found.b_end * ITEM_S, 125), f"common run: {found}")
    check(common_run(noise(rng, LISTEN), noise(rng, LISTEN)) is None, "unrelated audio shares nothing")
    # A 10 s sting is too short to be a theme.
    sting = noise(rng, int(10 / ITEM_S))
    check(common_run(opening(rng, sting, 20), opening(rng, sting, 40)) is None, "a sting is not a theme")

    with tempfile.TemporaryDirectory() as d:
        book = ThemeBook(290, d)
        # The first title has nothing to compare with; the second learns the theme from it.
        check(book.place_or_learn(opening(rng, song, 12.0)) is None, "first title: no theme yet")
        p = book.place_or_learn(opening(rng, song, 61.0))
        check(p is not None and p.learned and p.version == 0 and near(p.start_s, 61) and near(p.end_s, 91),
              f"second title learns the theme: {p}")
        # Later titles find it without learning, however long their cold open is.
        for at in (0.0, 47.5, 200.0):
            p = book.place_or_learn(opening(rng, song, at))
            check(p is not None and not p.learned and p.version == 0 and near(p.start_s, at)
                  and near(p.end_s, at + 30), f"theme at {at} s: {p}")
        # A special with its own theme: not found, then learned once a second title has it.
        check(book.place_or_learn(opening(rng, other_song, 30.0)) is None, "variant theme, first title")
        check(book.place(opening(rng, other_song, 30.0)) is None, "place() learns nothing")
        p = book.place_or_learn(opening(rng, other_song, 80.0))
        check(p is not None and p.learned and p.version == 1 and near(p.end_s, 105), f"variant learned: {p}")
        p = book.place_or_learn(opening(rng, other_song, 5.0))
        check(p is not None and p.version == 1 and not p.learned, f"variant found again: {p}")
        p = book.place_or_learn(opening(rng, song, 33.0))
        check(p is not None and p.version == 0, f"first theme still found: {p}")
        # Kept on disk, under the show.
        again = ThemeBook(290, d)
        vs = again.versions()
        check(len(vs) == 2 and [round(v["seconds"]) for v in vs] == [30, 25], f"versions: {[v['seconds'] for v in vs]}")
        check(os.path.basename(again.path) == "290.json", f"file: {again.path}")
        check(os.path.basename(ThemeBook("Bluey (2018)", d).path) == "Bluey-2018.json", "show name as a file name")

        # Two copies of one episode share their whole opening: too long to be a theme.
        dup_book = ThemeBook("dup", d)
        same = noise(rng, LISTEN)
        check(dup_book.place_or_learn(same) is None and dup_book.place_or_learn(blur(rng, same)) is None
              and dup_book.versions() == [], f"duplicate titles learned: {dup_book.versions()[:1]}")

        # A damaged file is treated as no themes learned.
        with open(os.path.join(d, "bad.json"), "w") as f:
            f.write("{not json")
        check(ThemeBook("bad", d).versions() == [], "damaged file")

    # fpcalc: the fingerprint, or None when it's missing or fails.
    calls = []

    def fake_fpcalc(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout=json.dumps({"duration": 600.0, "fingerprint": [1, -2, 3]}),
                               stderr="")
    fp = fingerprint("/rips/a.mkv", 120, "fpcalc", fake_fpcalc)
    check(fp == [1, 2 ** 32 - 2, 3], f"fingerprint: {fp}")
    check(calls[0] == ["fpcalc", "-raw", "-length", "120", "-json", "/rips/a.mkv"], f"fpcalc command: {calls[0]}")

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    check(fingerprint("/rips/a.mkv", 120, "fpcalc", missing) is None, "fpcalc missing")
    check(fingerprint("/rips/a.mkv", 120, "fpcalc",
                      lambda cmd, **kw: SimpleNamespace(returncode=2, stdout="", stderr="no audio")) is None,
          "fpcalc failing")
    check(fingerprint("/rips/a.mkv", 120, "fpcalc",
                      lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="oops", stderr="")) is None,
          "fpcalc printing something else")

    for f in failures:
        print("FAIL:", f)
    print(f"theme: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_theme():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
