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
from discshelf.config import Config  # noqa: E402
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
