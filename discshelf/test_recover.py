#!/usr/bin/env python3
"""
Tests for Controller.recover_titles(): a title whose own "<disc>_tNN.mkv" is in a job's rip
folder but missing from the job (the rip watcher used to lose every second title) is put
back after the language step; titles the job has, files of other titles, jobs still ripping
and titles already on the NAS are left alone.

Run:  python3 discshelf/test_recover.py    (also works under pytest)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import hdr, pipeline  # noqa: E402
from discshelf.config import Config, Destination  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import HdrInfo, Job, RippedFile, TitleProgress  # noqa: E402


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    saved = (hdr.remux_tracks, hdr.probe)
    remuxed = []
    hdr.remux_tracks = lambda src, dst, a, s, cfg: remuxed.append(os.path.basename(src)) or dst
    hdr.probe = lambda path, cfg: HdrInfo()
    try:
        with tempfile.TemporaryDirectory() as root:
            rips = os.path.join(root, "rips")
            os.makedirs(rips)
            for ti in range(1, 6):
                with open(os.path.join(rips, f"SHOW-C{ti}_t{ti:02d}.mkv"), "w") as fh:
                    fh.write("x" * (100 + ti))
            with open(os.path.join(rips, "OTHER_t09.mkv"), "w") as fh:
                fh.write("not a title of this job")
            store = JobStore(os.path.join(root, "jobs"))
            ctl = pipeline.Controller(Config(), store)
            path = lambda ti: os.path.join(rips, f"SHOW-C{ti}_t{ti:02d}.mkv")  # noqa: E731
            job = Job(id="lost", disc_label="SHOW", rip_dir=rips, stage="match",
                      files=[RippedFile(1, path(1), 101), RippedFile(3, path(3), 103), RippedFile(5, path(5), 105)],
                      titles_progress=[TitleProgress(1, "waiting_match"), TitleProgress(2, "ripping"),
                                       TitleProgress(3, "waiting_match"), TitleProgress(4, "verified"),
                                       TitleProgress(5, "waiting_match")])
            ripping = Job(id="busy", disc_label="SHOW", rip_dir=rips, stage="match",
                          titles_progress=[TitleProgress(2, "ripping")])
            store.save(job)
            store.save(ripping)
            ctl._runs["busy"] = object()

            got = ctl.recover_titles()
            check([(j.id, ti) for j, ti in got] == [("lost", [2])], f"recovered: {[(j.id, ti) for j, ti in got]}")
            check(remuxed == ["SHOW-C2_t02.mkv"], f"language step ran on: {remuxed}")
            back = next(j for j in ctl.jobs() if j.id == "lost")
            check([f.title_index for f in back.files] == [1, 2, 3, 5], f"files: {[f.title_index for f in back.files]}")
            f2 = next(f for f in back.files if f.title_index == 2)
            tp2 = next(p for p in back.titles_progress if p.title_index == 2)
            check(f2.path == path(2) and f2.size_bytes == 102 and tp2.state == "ripped", f"title 2: {f2} {tp2}")
            check("Ripped 4 titles" in back.status, f"status: {back.status}")
            check(ctl.recover_titles() == [], "a second run found something again")

            # The app closed just after MakeMKV finished a film that was copied while it ripped:
            # the film is kept and waits to be copied, and the unfinished copy in storage goes.
            remuxed.clear()
            nas = os.path.join(root, "nas")
            os.makedirs(os.path.join(nas, "Film"))
            stale = os.path.join(nas, "Film", "Film.mkv.partial")
            with open(stale, "w") as fh:
                fh.write("most of it")
            frip = os.path.join(root, "rips-film")
            os.makedirs(os.path.join(frip, ".work"))
            fpath = os.path.join(frip, "Film_t00.mkv")
            with open(fpath, "w") as fh:
                fh.write("f" * 500)
            film = Job(id="film", disc_label="FILM", media_kind="movie", rip_dir=frip, destination=nas,
                       stage="rip", copy_mode="stream", status="Ripping title 1 of 1 · 16 min left",
                       files=[RippedFile(0, fpath, 500, dest_rel="Film/Film.mkv", state="copying")],
                       titles_progress=[TitleProgress(0, "copying", total_bytes=500, ripped_bytes=500,
                                                      copied_bytes=400)])
            # A TV disc ripped one title at a time: title 1 is in storage, title 2 finished ripping,
            # and the app closed while MakeMKV was writing title 3.
            trip = os.path.join(root, "rips-tv")
            os.makedirs(trip)
            tpath = lambda ti: os.path.join(trip, f"TV_t{ti:02d}.mkv")  # noqa: E731
            for ti, size in ((1, 100), (2, 200), (3, 50)):
                with open(tpath(ti), "w") as fh:
                    fh.write("t" * size)
            tv = Job(id="tv", disc_label="TV", media_kind="tv", rip_dir=trip, stage="rip", copy_mode="per_title",
                     status="Ripping title 3 of 3",
                     files=[RippedFile(1, tpath(1), 100, state="verified", hdr=HdrInfo()), RippedFile(2, tpath(2), 200)],
                     titles_progress=[TitleProgress(1, "verified"), TitleProgress(2, "ripped"),
                                      TitleProgress(3, "ripping", ripped_bytes=50)])
            # Left alone: a rip you stopped, and a disc scanned but not ripped yet.
            stopped = Job(id="stopped", disc_label="STOP", rip_dir=trip, stage="rip",
                          status=f"{pipeline.RIP_STOPPED} Start it again to rip from the beginning.",
                          titles_progress=[TitleProgress(3, "ripping")])
            fresh = Job(id="fresh", disc_label="NEW", rip_dir=os.path.join(root, "rips-new"), stage="rip")
            for j in (film, tv, stopped, fresh):
                store.save(j)
            ctl.cfg.destinations = [Destination("movie", "Movies", nas, "", "movie")]

            got = {j.id: ti for j, ti in ctl.recover_titles()}
            check(got == {"film": [0], "tv": [2]}, f"recovered after a closed rip: {got}")
            film = store.load("film")
            check(film.stage == "match" and film.status == "Ripped 1 title. Check the match next.",
                  f"film: {film.stage} {film.status!r}")
            check(film.files[0].state == "ripped" and film.files[0].hdr is not None, f"film file: {film.files[0]}")
            check(film.titles_progress[0].state == "ripped" and film.titles_progress[0].copied_bytes == 0,
                  f"film title: {film.titles_progress[0]}")
            check(os.path.isfile(fpath) and not os.path.exists(stale), "the film or the stale copy in storage")
            check(not os.path.exists(os.path.join(frip, ".work")), "the work folder was left")
            check(remuxed == ["TV_t02.mkv"], f"language step ran on: {remuxed}")
            tv = store.load("tv")
            states = {tp.title_index: tp.state for tp in tv.titles_progress}
            check(states == {1: "verified", 2: "ripped", 3: "failed"}, f"tv titles: {states}")
            check([f.title_index for f in tv.files] == [1, 2] and tv.files[1].hdr is not None, f"tv files: {tv.files}")
            check(not os.path.exists(tpath(3)) and os.path.isfile(tpath(1)) and os.path.isfile(tpath(2)),
                  "the half-written title was kept, or a finished one deleted")
            check(tv.stage == "failed" and "1 title didn't finish" in tv.status, f"tv: {tv.stage} {tv.status!r}")
            check(store.load("stopped").stage == "rip" and store.load("fresh").stage == "rip", "a stopped or new job changed")
            check(ctl.recover_titles() == [], "a second run after a closed rip found something again")
    finally:
        hdr.remux_tracks, hdr.probe = saved

    for f in failures:
        print("FAIL:", f)
    print(f"recover: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_recover():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
