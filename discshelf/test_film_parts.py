#!/usr/bin/env python3
"""
Tests for a film that comes on two discs ("… - Part 1" and "… - Part 2"): each disc keeps its
part in the film's name, each film's year is looked up on TheDiscDB, and a disc whose name is
already taken in storage by another disc's copy fails to copy instead of replacing it. MakeMKV is faked; the copy is a real rsync into a
temporary "NAS" folder.

Run:  python3 discshelf/test_film_parts.py    (also works under pytest)
"""
import json
import os
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from discshelf import config, discdb, hdr, makemkv, pipeline  # noqa: E402
from discshelf.config import Config, Destination  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import Disc, Drive, HdrInfo, Stream, Title  # noqa: E402

# TheDiscDB knows both films (but not these discs), so each film's year comes from its title.
DISCDB_FILMS = json.dumps({"data": {"mediaItems": {"nodes": [
    {"title": "Harry Potter and the Deathly Hallows: Part 1", "year": 2010, "type": "Movie", "releases": []},
    {"title": "Harry Potter and the Deathly Hallows: Part 2", "year": 2011, "type": "Movie", "releases": []},
]}}}).encode()


class FakeTvMaze:
    def search(self, q):
        return []

    def episodes(self, show_id, season):
        return []


def film_disc(label, name):
    t = Title(index=0, source="00800.mpls", duration_s=140 * 60, size_bytes=5000, chapters=30,
              segments=[1], streams=[Stream(0, "video", resolution="3840x2160")])
    return Disc(drive=Drive(0, "/dev/sr9", "TEST DRIVE", label, True), label=label,
                name=name, disc_type="Blu-ray disc", titles=[t])


def test_film_names():
    film = pipeline.Controller.film_name
    assert film("Harry Potter and the Deathly Hallows - Part 1") == "Harry Potter and the Deathly Hallows - Part 1"
    assert film("Dune: Part Two") == "Dune: Part Two"
    assert film("ALIEN_ROMULUS") == "Alien Romulus"
    assert film("", "SOME_FILM") == "Some Film"
    # A TV volume still loses its subtitle.
    assert pipeline.Controller.show_name("Game of Thrones: Season 1 Disc 2") == "Game of Thrones"


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as root:
        nas = os.path.join(root, "nas", "Movies")
        os.makedirs(nas)
        cfg = Config(destinations=[Destination("movie", "Movies", nas, "", "movie")],
                     rip_dir=os.path.join(root, "rips"), read_title_cards=False, eject_after_rip=False,
                     stream_films=False)
        ctl = pipeline.Controller(cfg, JobStore(os.path.join(root, "jobs")), tvmaze=FakeTvMaze(),
                                  discdb_client=discdb.TheDiscDb(fetch=lambda _u, _b: DISCDB_FILMS))
        holder = {}

        def fake_scan(cfg_, idx, run=None, min_seconds=0):
            return {"sinfo": {}}, holder["disc"]

        def fake_rip(cfg_, idx, titles, out_dir, progress, cancel=None, popen=None, min_seconds=0, **_kw):
            paths = []
            for ti in titles:
                p = os.path.join(out_dir, f"Film_t{ti:02d}.mkv")
                with open(p, "wb") as fh:
                    fh.write(os.urandom(4096 + len(holder["disc"].label)))
                paths.append(p)
            progress(1.0, "done")
            return paths

        saved = (makemkv.scan_disc_parsed, makemkv.rip_titles, hdr.remux_tracks, hdr.probe, config.load_secret)
        makemkv.scan_disc_parsed, makemkv.rip_titles = fake_scan, fake_rip
        hdr.remux_tracks = lambda src, dst, a, s, cfg_, run=None: dst
        hdr.probe = lambda path, cfg_, run=None: HdrInfo(resolution="3840x2160", codec="hevc")
        config.load_secret = lambda name, path=None: ""
        try:
            def rip_and_copy(label, name, show=None):
                holder["disc"] = film_disc(label, name)
                job = ctl.new_job(ctl.scan(0))
                if show is not None:
                    job.show = show
                ctl.rip(job, lambda f, t: None, threading.Event())
                ctl.suggest_matches(job, show_id=None)
                error = None
                try:
                    ctl.copy(job, lambda f, t: None, threading.Event())
                except Exception as e:  # noqa: BLE001
                    error = e
                return job, error

            p1, err = rip_and_copy("HP_DH_P1", "Harry Potter and the Deathly Hallows - Part 1")
            check(p1.media_kind == "movie", f"kind: {p1.media_kind}")
            check(p1.show == "Harry Potter and the Deathly Hallows - Part 1", f"part 1 name: {p1.show}")
            check(err is None and p1.files and p1.files[0].state == "verified", f"part 1 copy: {err}")
            want = "Harry Potter and the Deathly Hallows - Part 1 (2010)"
            check(p1.year == 2010 and p1.files[0].dest_rel == f"{want}/{want}.mkv", f"part 1 path: {p1.files[0].dest_rel}")
            p2, err = rip_and_copy("HP_DH_P2", "Harry Potter and the Deathly Hallows - Part 2")
            check(err is None and p2.year == 2011 and p2.files[0].dest_rel != p1.files[0].dest_rel,
                  f"part 2 path: {p2.files[0].dest_rel if p2.files else None} {err}")

            # Given the same name as part 1 (as the old guess did), the second disc must not replace it.
            first = os.path.join(nas, p1.files[0].dest_rel)
            before = open(first, "rb").read()
            p3, err = rip_and_copy("HP_DH_P2B", "x", show=p1.show)
            check(isinstance(err, FileExistsError), f"same name copied anyway: {err!r}")
            check(open(first, "rb").read() == before, "the first disc's copy was replaced")
            check(p3.files and p3.files[0].state == "failed", f"clash state: {p3.files[0].state if p3.files else None}")
        finally:
            (makemkv.scan_disc_parsed, makemkv.rip_titles, hdr.remux_tracks, hdr.probe, config.load_secret) = saved

    for f in failures:
        print("FAIL:", f)
    print(f"film parts: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_film_parts():
    assert run() == 0


if __name__ == "__main__":
    test_film_names()
    sys.exit(run())
