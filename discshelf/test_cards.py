#!/usr/bin/env python3
"""
Tests for how the controller uses title cards, on the readings of the 16 titles of the
Adventure Time "It Came from the Nightosphere" DVD (fixtures/adventure_time_cards.json).

What matters: a card replaces a running-time guess or an earlier card, never a match from
TheDiscDB, the case list, the user, or one already confirmed; a ripped title whose match is
only a guess waits for its card before it's copied; and the card reader thread reads each
title it's given and then lets the title go.

Run:  python3 discshelf/test_cards.py    (also works under pytest)
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import discdb, ocr, pipeline, titlecard  # noqa: E402
from discshelf.config import Config  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import Episode, Job, Match, Title  # noqa: E402
from discshelf.test_titlecard import load  # noqa: E402

NO_DISCDB = b'{"data": {"mediaItems": {"nodes": []}}}'


class CartoonTvMaze:
    def __init__(self, episodes):
        self.eps = episodes

    def search(self, q):
        return []

    def episodes(self, show_id, season):
        return [e for e in self.eps if season in (None, e.season)]


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    episodes, readings, truth = load()
    saved = (titlecard.read_title, ocr.engine)
    with tempfile.TemporaryDirectory() as root:
        try:
            ctl = pipeline.Controller(Config(), JobStore(os.path.join(root, "jobs")), tvmaze=CartoonTvMaze(episodes),
                                      discdb_client=discdb.TheDiscDb(fetch=lambda _u, _b: NO_DISCDB))
            titles = [Title(i, title_id=f"{i + 1:02d}", duration_s=704, size_bytes=5 * 10 ** 8, kind="episode",
                            selected=True) for i in sorted(readings)]
            job = Job(id="cards", disc_label="ADVENTURE TIME VOL 2", titles=titles, show="Adventure Time",
                      show_id=290, media_kind="tv", stage="match")
            guess = lambda i: Match(i, "episode", Episode(1, i, f"Guess {i}", 15), confidence="check",  # noqa: E731
                                    source="runtime")
            job.matches = [guess(t.index) for t in titles]
            job.matches[2] = Match(3, "episode", Episode(1, 11, "Finn the Wizard", 15), source="manual")
            job.matches[4] = Match(5, "episode", Episode(1, 5, "Guess 5", 15), confidence="check", confirmed=True,
                                   source="runtime")
            job.matches[6] = Match(7, "episode", Episode(2, 15, "The Real You", 15), confidence="close", source="list")

            ctl._cards[job.id] = readings
            n = ctl.apply_cards(job)
            by = {m.title_index: m for m in job.matches}
            se = lambda m: m.episode and (m.episode.season, m.episode.number)  # noqa: E731
            check(by[3].source == "manual" and se(by[3]) == (1, 11), f"a hand-picked match was replaced: {by[3]}")
            check(by[5].source == "runtime" and by[5].confirmed, f"a confirmed match was replaced: {by[5]}")
            check(by[7].source == "list", f"a match from the case list was replaced: {by[7]}")
            from_cards = [m for m in job.matches if m.source == "card"]
            check(n == len(from_cards) >= 10, f"card matches: {n}, {len(from_cards)}")
            wrong = [(m.title_index, m.episode.title) for m in from_cards
                     if m.confidence == "close" and se(m) != truth[m.title_index]]
            check(not wrong, f"a card match is close but wrong: {wrong}")
            check(all(se(m) == truth[m.title_index] for m in from_cards if m.confidence == "close"), "close card")

            # A guess waits for the card; a card, list or hand-picked match doesn't.
            r = pipeline._Run(job, "per_title", threading.Event(), 1.0, None, lambda f, t: None)
            r.card_pending = {1, 2}
            close_guess = Match(1, "episode", Episode(1, 1, "x", 15), confidence="close", source="runtime")
            check(not ctl._ready(r, 1, close_guess), "a running-time guess was copied before its card was read")
            check(ctl._ready(r, 2, Match(2, "episode", Episode(1, 2, "x", 15), confidence="close", source="card")),
                  "a card match waited")
            check(ctl._ready(r, 3, close_guess), "a title with no card pending waited")

            # The reader thread: reads what it's given, then lets the title go.
            titlecard.read_title = lambda path, **kw: readings[int(path)]
            ocr.engine = lambda: "rapidocr"
            job2 = Job(id="cards2", disc_label="X", titles=titles, show="Adventure Time", show_id=290,
                       media_kind="tv", stage="rip", matches=[guess(t.index) for t in titles])
            r2 = pipeline._Run(job2, "per_title", threading.Event(), 1.0, None, lambda f, t: None)
            for ti in (1, 2, 3):
                ctl._card_queue(r2, ti, str(ti))
            r2.cards_q.put(None)
            r2.cards_thread.join(30)
            check(not r2.card_pending and sorted(ctl._cards[job2.id]) == [1, 2, 3],
                  f"reader: pending {r2.card_pending}, read {sorted(ctl._cards.get(job2.id, {}))}")

            # Title cards are off for films, and when the setting is off.
            ctl.cfg.read_title_cards = False
            r3 = pipeline._Run(job2, "per_title", threading.Event(), 1.0, None, lambda f, t: None)
            ctl._card_queue(r3, 4, "4")
            check(r3.cards_thread is None and not r3.card_pending, "cards were read with the setting off")
        finally:
            titlecard.read_title, ocr.engine = saved

    for f in failures:
        print("FAIL:", f)
    print(f"cards: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_cards():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
