#!/usr/bin/env python3
"""
Tests for Controller.cover(): the disc's own artwork wins; a TV disc without any (a DVD)
gets a TVmaze poster, saved once and cached; a film without artwork gets none, because a
TVmaze search for a film can match an unrelated show; a TVmaze failure leaves no cover.

Run:  python3 discshelf/test_cover.py    (also works under pytest)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import discinfo, pipeline  # noqa: E402
from discshelf.config import Config  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import Disc, Drive, Title  # noqa: E402
from discshelf.tvmaze import ShowHit, TvMazeError  # noqa: E402


class FakeTvMaze:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def search(self, q):
        self.calls.append(("search", q))
        if self.fail:
            raise TvMazeError("Could not reach TVmaze (offline).")
        return [ShowHit(290, "Adventure Time", 2010)]

    def poster_url(self, show_id, season=None):
        self.calls.append(("poster", show_id, season))
        return "https://static.tvmaze.com/uploads/images/original_untouched/1/4898.jpg"

    def download(self, url):
        self.calls.append(("download", url))
        return b"\xff\xd8poster"


def disc(kind, disc_type="DVD disc"):
    titles = [Title(index=i, duration_s=700, kind=kind) for i in range(3)]
    return Disc(drive=Drive(0, "/dev/sr9", "DRIVE", "ADVENTURE TIME VOL 2", True), label="ADVENTURE TIME VOL 2",
                name="ADVENTURE TIME - IT CAME FROM THE NIGHTOSPHERE", disc_type=disc_type, titles=titles)


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    saved = (discinfo.fetch_meta, os.environ.get("XDG_STATE_HOME"))
    with tempfile.TemporaryDirectory() as root:
        os.environ["XDG_STATE_HOME"] = root
        try:
            # A DVD with episodes and no artwork on the disc: TVmaze poster, fetched once.
            discinfo.fetch_meta = lambda device, cache, label="", run=None: discinfo.DiscMeta()
            tv = FakeTvMaze()
            ctl = pipeline.Controller(Config(), JobStore(os.path.join(root, "jobs")), tvmaze=tv)
            ctl._scans[0] = ({}, disc("episode"))
            path, source = ctl.cover(0)
            check(source == "TVmaze" and os.path.isfile(path) and open(path, "rb").read() == b"\xff\xd8poster",
                  f"TV DVD cover: {path!r} {source!r}")
            check(("search", "ADVENTURE TIME") in tv.calls, f"search query: {tv.calls}")
            n = len(tv.calls)
            check(ctl.cover(0) == (path, source) and len(tv.calls) == n, "a second call asked TVmaze again")

            # The disc's own artwork wins over TVmaze.
            art = os.path.join(root, "own.jpg")
            open(art, "wb").write(b"disc art")
            discinfo.fetch_meta = lambda device, cache, label="", run=None: discinfo.DiscMeta(cover_path=art)
            tv2 = FakeTvMaze()
            ctl2 = pipeline.Controller(Config(), JobStore(os.path.join(root, "jobs2")), tvmaze=tv2)
            ctl2._scans[0] = ({}, disc("episode", "Blu-ray disc"))
            check(ctl2.cover(0) == (art, "disc") and not tv2.calls, "disc artwork didn't win")

            # A DVD is never mounted to look for artwork: it has none, and the mount can hang the drive.
            mounted = []
            discinfo.fetch_meta = lambda device, cache, label="", run=None: mounted.append(device) or discinfo.DiscMeta()
            ctl5 = pipeline.Controller(Config(), JobStore(os.path.join(root, "jobs5")), tvmaze=FakeTvMaze())
            ctl5._scans[0] = ({}, disc("episode"))
            ctl5.cover(0)
            check(not mounted, "a DVD was mounted to look for artwork")

            # A film without artwork: no TVmaze lookup at all.
            discinfo.fetch_meta = lambda device, cache, label="", run=None: discinfo.DiscMeta()
            tv3 = FakeTvMaze()
            ctl3 = pipeline.Controller(Config(), JobStore(os.path.join(root, "jobs3")), tvmaze=tv3)
            ctl3._scans[0] = ({}, disc("feature"))
            check(ctl3.cover(0) == ("", "") and not tv3.calls, f"film looked up on TVmaze: {tv3.calls}")

            # TVmaze down: no cover, no exception.
            ctl4 = pipeline.Controller(Config(), JobStore(os.path.join(root, "jobs4")), tvmaze=FakeTvMaze(fail=True))
            ctl4._scans[0] = ({}, disc("episode"))
            check(ctl4.cover(0) == ("", ""), "a TVmaze failure wasn't absorbed")

            check(pipeline.Controller.show_query("Game of Thrones: Season 1 Disc 2") == "Game of Thrones", "show_query colon")
            check(pipeline.Controller.show_query("", "SEVERANCE_S1_D1") == "SEVERANCE", "show_query label")
        finally:
            discinfo.fetch_meta = saved[0]
            if saved[1] is None:
                os.environ.pop("XDG_STATE_HOME", None)
            else:
                os.environ["XDG_STATE_HOME"] = saved[1]

    for f in failures:
        print("FAIL:", f)
    print(f"cover: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_cover():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
