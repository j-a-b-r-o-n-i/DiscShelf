#!/usr/bin/env python3
"""
Tests for pipeline.py: one TV disc taken through new_job → rip → suggest_matches →
plan_paths → copy → cleanup, with MakeMKV and the HDR tools faked and a real rsync
copy into a temporary "NAS" folder. A second disc in the same season continues the
episode numbering.

The overlap tests rip with a fake MakeMKV that writes files over time and calls the
per-title callbacks from its own thread, so copying while ripping, matching during
the rip, locked matches, a film copied as it rips, and the speed samples are all
exercised against the real rsync copy.

Run:  python3 discshelf/test_pipeline.py    (also works under pytest)
"""
import copy
import hashlib
import os
import sys
import tempfile
import threading
import time
import types

# TheDiscDB knows none of the test discs, and the tests never touch the network.
NO_DISCDB = '{"data": {"mediaItems": {"nodes": []}}}'.encode()

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from discshelf import config, discdb, hdr, makemkv, pipeline  # noqa: E402
from discshelf.config import Config, Destination  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import Disc, Drive, Episode, HdrInfo, HdrPolicy, Job, Stream, Title  # noqa: E402


class FakeTvMaze:
    def search(self, q):
        return []

    def episodes(self, show_id, season):
        return [Episode(season, n, f"Episode {n}", 22) for n in range(1, 11)]


def tv_disc(label):
    def t(i, mins, segs):
        return Title(index=i, source=f"{i:05d}.mpls", duration_s=mins * 60, size_bytes=1000 + i,
                     chapters=5, segments=segs, streams=[Stream(0, "video", resolution="1920x1080")])
    titles = [t(0, 22, [1]), t(1, 21, [2]), t(2, 22, [3]), t(3, 65, [1, 2, 3])]
    return Disc(drive=Drive(0, "/dev/sr9", "TEST DRIVE", label, True), label=label,
                name="Test Show", disc_type="Blu-ray disc", titles=titles)


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as root:
        nas = os.path.join(root, "nas", "Shows")
        os.makedirs(nas)
        cfg = Config(destinations=[Destination("tv", "TV shows", nas, "", "tv")], rip_dir=os.path.join(root, "rips"),
                     read_title_cards=False, eject_after_rip=False)
        ctl = pipeline.Controller(cfg, JobStore(os.path.join(root, "jobs")), tvmaze=FakeTvMaze(),
                                  discdb_client=discdb.TheDiscDb(fetch=lambda _u, _b: NO_DISCDB))

        disc_holder = {}

        def fake_scan(cfg_, idx, run=None, min_seconds=0):
            return {"sinfo": {}}, disc_holder["disc"]

        def fake_rip(cfg_, idx, titles, out_dir, progress, cancel=None, popen=None, min_seconds=0):
            check(min_seconds == pipeline.SCAN_MIN_SECONDS, f"rip used minlength {min_seconds}")
            paths = []
            for n, ti in enumerate(titles):
                p = os.path.join(out_dir, f"Test_t{ti:02d}.mkv")
                with open(p, "wb") as fh:
                    fh.write(os.urandom(4096 + ti))
                progress((n + 1) / len(titles), f"Title {n + 1}")
                paths.append(p)
            return paths

        saved = (makemkv.scan_disc_parsed, makemkv.rip_titles, hdr.remux_tracks, hdr.probe, config.load_secret)
        makemkv.scan_disc_parsed, makemkv.rip_titles = fake_scan, fake_rip
        hdr.remux_tracks = lambda src, dst, a, s, cfg_, run=None: dst
        hdr.probe = lambda path, cfg_, run=None: HdrInfo(resolution="1920x1080", codec="h264")
        config.load_secret = lambda name, path=None: ""
        try:
            disc_holder["disc"] = tv_disc("TEST_SHOW_S1_D1")
            disc = ctl.scan(0)
            job = ctl.new_job(disc)
            check(job.media_kind == "tv" and job.season == 1 and job.disc_number == 1,
                  f"guesses: {job.media_kind} S{job.season} D{job.disc_number}")
            check([t.kind for t in job.titles] == ["episode", "episode", "episode", "play_all"],
                  f"classified: {[t.kind for t in job.titles]}")
            job.show = "Test Show"
            seen = []
            ctl.rip(job, lambda f, t: seen.append(f), threading.Event())
            check(job.stage == "match" and len(job.files) == 3, f"after rip: {job.stage} {len(job.files)}")
            check(seen == sorted(seen) and seen[-1] == 1.0, "rip progress went backwards or stopped short")

            ms = ctl.suggest_matches(job, show_id=1)
            eps = [m.episode.number for m in ms if m.target == "episode"]
            check(eps == [1, 2, 3], f"disc 1 episodes: {eps}")
            files = ctl.plan_paths(job)
            check(files[0].dest_rel == "Test Show/Season 01/Test Show - S01E01.mkv", f"path: {files[0].dest_rel}")

            ctl.copy(job, lambda f, t: None, threading.Event())
            check(job.stage == "done" and all(f.state == "verified" for f in job.files), f"after copy: {job.stage}")
            dest = os.path.join(nas, files[2].dest_rel)
            check(os.path.isfile(dest) and os.path.getsize(dest) == os.path.getsize(job.files[2].path),
                  "the NAS copy is missing or a different size")

            # Disc 2 of the same season carries on from episode 4.
            disc_holder["disc"] = tv_disc("TEST_SHOW_S1_D2")
            job2 = ctl.new_job(ctl.scan(0))
            job2.show = "Test Show"
            ctl.rip(job2, lambda f, t: None, threading.Event())
            eps2 = [m.episode.number for m in ctl.suggest_matches(job2, show_id=1) if m.target == "episode"]
            check(eps2 == [4, 5, 6], f"disc 2 episodes: {eps2}")

            # A disc TheDiscDB knows is matched from its record, not by running time.
            job3 = ctl.new_job(ctl.scan(0))
            job3.show = "Test Show"
            hit = discdb.DiscHit(media_title="Test Show", release_title="Season 1", disc_name="Disc 3")
            ctl._discdb_hits[job3.id] = hit
            real_to_matches = discdb.to_matches
            discdb.to_matches = lambda h, titles, eps: [
                pipeline.Match(t.index, "episode", eps[-1], confidence="close", source="discdb",
                               note="From TheDiscDB: Season 1, Disc 3.") for t in titles if t.selected]
            try:
                ms3 = ctl.suggest_matches(job3, show_id=1)
            finally:
                discdb.to_matches = real_to_matches
            check(ms3 and all(m.source == "discdb" for m in ms3) and job3.show_id == 1, f"TheDiscDB hit: {ms3}")

            # Clean-up: "keep" never deletes; "delete" removes the verified local rips of finished jobs.
            ctl.cfg.cleanup = "keep"
            check(ctl.cleanup(apply=True) == [], "keep policy deleted something")
            ctl.cfg.cleanup = "delete"
            due = ctl.cleanup(apply=False)
            check(len(due) == 3 and all(os.path.exists(p) for p in due), f"dry-run clean-up: {due}")
            ctl.cleanup(apply=True)
            check(not any(os.path.exists(p) for p in due), "clean-up left files behind")
            check(os.path.exists(job2.files[0].path), "clean-up touched a job that was never copied")

            reloaded = ctl.store.load(job.id)
            check(reloaded and reloaded.files[0].hdr and reloaded.files[0].hdr.codec == "h264",
                  "the job did not survive a save and reload")
        finally:
            (makemkv.scan_disc_parsed, makemkv.rip_titles, hdr.remux_tracks, hdr.probe, config.load_secret) = saved

    for f in failures:
        print("FAIL:", f)
    print(f"pipeline: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


CHUNK, CHUNKS = 32 * 1024, 6


def make_fake_rip(log, hold=None):
    """A rip_titles that writes each title in chunks and calls the callbacks from its own thread."""
    def fake_rip(cfg_, idx, titles, out_dir, progress, cancel=None, popen=None, min_seconds=0, *,
                 on_title_start=None, on_title_done=None, on_bytes=None, selection=None):
        log["selection"] = selection
        paths = []

        def work():
            for n, ti in enumerate(titles):
                p = os.path.join(out_dir, f"Test_t{ti:02d}.mkv")
                with open(p, "wb") as fh:
                    on_title_start(ti, p)
                    log.setdefault("started", threading.Event()).set()
                    for c in range(CHUNKS):
                        fh.write(os.urandom(CHUNK))
                        fh.flush()
                        on_bytes(ti, p, (c + 1) * CHUNK)
                        progress((n + (c + 1) / CHUNKS) / len(titles), f"Title {n + 1} of {len(titles)}")
                        time.sleep(0.02)
                if hold:
                    hold(n, ti)
                log.setdefault("done", {})[ti] = time.monotonic()
                on_title_done(ti, p)
                paths.append(p)

        th = threading.Thread(target=work)
        th.start()
        th.join()
        return paths
    return fake_rip


def fake_stream_copy(src, dst, done, progress, cancel=None):
    """Copies a growing file the way follow.stream_copy does, without the chunk checks."""
    t0, part, sent = time.monotonic(), dst + ".partial", 0
    with open(src, "rb") as fin, open(part, "wb") as fout:
        while True:
            if cancel is not None and cancel.is_set():
                raise pipeline.transfer.Cancelled()
            block = fin.read(CHUNK)
            if block:
                fout.write(block)
                sent += len(block)
                progress(sent, sent, sent / max(1e-6, time.monotonic() - t0))
            elif done.is_set():
                break
            else:
                time.sleep(0.01)
    os.replace(part, dst)
    with open(dst, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return types.SimpleNamespace(bytes=sent, chunks_resent=0, seconds=time.monotonic() - t0, sha256=digest)


def run_overlap():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    missing = object()
    names = ("scan_disc_parsed", "rip_titles", "selection_for", "selection_supported")
    saved_mk = {n: getattr(makemkv, n, missing) for n in names}
    saved = (hdr.remux_tracks, hdr.probe, config.load_secret, pipeline.follow)
    remuxed = []
    with tempfile.TemporaryDirectory() as root:
        shows, movies = os.path.join(root, "nas", "Shows"), os.path.join(root, "nas", "Movies")
        os.makedirs(shows)
        os.makedirs(movies)
        cfg = Config(destinations=[Destination("tv", "TV shows", shows, "", "tv"),
                                   Destination("movie", "Movies", movies, "", "movie")],
                     rip_dir=os.path.join(root, "rips"),
                     read_title_cards=False, eject_after_rip=False)
        ctl = pipeline.Controller(cfg, JobStore(os.path.join(root, "jobs")), tvmaze=FakeTvMaze(),
                                  discdb_client=discdb.TheDiscDb(fetch=lambda _u, _b: NO_DISCDB))
        ctl.sample_interval = 0.02
        disc_holder = {}
        makemkv.scan_disc_parsed = lambda cfg_, idx, run=None, min_seconds=0: ({"sinfo": {}}, disc_holder["disc"])
        hdr.remux_tracks = lambda src, dst, a, s, cfg_, run=None: remuxed.append(src) or dst
        hdr.probe = lambda path, cfg_, run=None: HdrInfo(resolution="1920x1080", codec="h264")
        config.load_secret = lambda name, path=None: ""
        makemkv.selection_supported = lambda cfg_: True
        makemkv.selection_for = lambda audio, subs: f"+sel:all,-sel:!({','.join(audio)})"
        pipeline.follow = types.SimpleNamespace(stream_copy=fake_stream_copy)
        try:
            # 1. Three episodes, matched and confirmed while the disc rips: title 0 is on
            #    the NAS before the last title finishes, and the rip ends with the disc done.
            log = {}
            holder = {}

            def hold(n, ti):
                if ti != 2:
                    return
                tp0 = holder["job"].titles_progress[0]
                deadline = time.monotonic() + 10
                while tp0.state != "verified" and time.monotonic() < deadline:
                    time.sleep(0.01)
                log["t0_before_t2_done"] = tp0.state == "verified"

            makemkv.rip_titles = make_fake_rip(log, hold)
            disc_holder["disc"] = tv_disc("TEST_SHOW_S1_D1")
            job = ctl.new_job(ctl.scan(0))
            job.show = "Test Show"
            holder["job"] = job
            check(ctl.stream_possible(job)[0] is False, "a TV disc said it could stream")
            check(not ctl.preview_paths(job), "preview paths before any match")
            samples, errors = [], []

            def ripper():
                try:
                    ctl.rip(job, lambda f, t: None, threading.Event(), samples=samples.append)
                except Exception as e:      # reported below
                    errors.append(e)

            th = threading.Thread(target=ripper)
            th.start()
            log.setdefault("started", threading.Event()).wait(5)
            ctl.suggest_matches(job, show_id=1)
            ctl.confirm_matches(job)
            preview = ctl.preview_paths(job)
            check([p for _, p in preview][:1] == ["Test Show/Season 01/Test Show - S01E01.mkv"], f"preview: {preview}")
            th.join(30)
            check(not errors, f"rip raised: {errors}")
            check(job.copy_mode == "per_title" and not job.copy_mode_note, f"mode: {job.copy_mode} {job.copy_mode_note!r}")
            check(log.get("t0_before_t2_done") is True, "title 0 wasn't on the NAS before title 2 finished ripping")
            check(job.stage == "done", f"stage after an overlapped rip: {job.stage} ({job.status})")
            check([tp.state for tp in job.titles_progress] == ["verified"] * 3,
                  f"states: {[tp.state for tp in job.titles_progress]}")
            check(all(os.path.isfile(os.path.join(shows, f.dest_rel)) for f in job.files), "an episode is missing on the NAS")
            check(len(remuxed) == 3, f"remux ran {len(remuxed)} times, expected 3")
            for kind in ("rip", "copy"):
                tot = [s.total for s in samples if s.kind == kind]
                check(len(tot) >= 2 and tot == sorted(tot) and tot[-1] > tot[0], f"{kind} sample totals: {tot}")
            rip_total = max(s.total for s in samples if s.kind == "rip")
            check(rip_total == 3 * CHUNKS * CHUNK, f"rip total {rip_total}")

            # 2. A change to a title already on the NAS is ignored, even if made in place.
            ms = copy.deepcopy(job.matches)
            ms[0].episode = Episode(1, 9, "Episode 9", 22)
            ctl.update_matches(job, ms)
            check(job.matches[0].episode.number == 1, "a verified title's match changed")
            job.matches[1].episode.number = 8
            ctl.update_matches(job, job.matches)
            check(job.matches[1].episode.number == 2, "an in-place edit of a verified title's match stuck")

            # 3. A match that needs checking and isn't confirmed waits; confirming and copying finishes it.
            disc_holder["disc"] = tv_disc("TEST_SHOW_S1_D2")
            makemkv.rip_titles = make_fake_rip({})
            job2 = ctl.new_job(ctl.scan(0))
            job2.show = "Test Show"
            ms = ctl.suggest_matches(job2, show_id=1)
            ms = copy.deepcopy(ms)
            ms[2].confidence, ms[2].note = "check", "Title is 22 min but the episode is listed as 30 min."
            ctl.update_matches(job2, ms)
            ctl.rip(job2, lambda f, t: None, threading.Event())
            states = [tp.state for tp in job2.titles_progress]
            check(states == ["verified", "verified", "waiting_match"], f"states with an unconfirmed match: {states}")
            check(job2.stage == "match", f"stage with a title waiting: {job2.stage}")
            check(job2.titles_progress[2].note, "the waiting title has no note")
            ctl.confirm_matches(job2)
            check(job2.titles_progress[2].state == "waiting_match", "confirm_matches copied without a rip running")
            copy_samples, texts = [], []
            ctl.copy(job2, lambda f, t: texts.append(t), threading.Event(), samples=copy_samples.append)
            check(job2.stage == "done" and all(f.state == "verified" for f in job2.files), f"after copy: {job2.stage}")
            check(job2.titles_progress[2].state == "verified", "copy() didn't update titles_progress")
            check(any(s.kind == "copy" for s in copy_samples), "copy() sent no copy samples")
            check(any(" of " in t and "KB" in t for t in texts), f"copy texts: {texts[:3]}")

            # 4. A film with the RAW HDR setting is copied while it rips, with no remux.
            def film_job(policy, name):
                t = Title(index=0, source="00800.mpls", duration_s=6000, size_bytes=CHUNKS * CHUNK, kind="feature",
                          selected=True, streams=[Stream(0, "video", resolution="3840x2160")])
                j = Job(id=ctl.store.new_id() + str(len(ctl.jobs())), media_kind="movie", show=name,
                        year=2012, destination=movies, titles=[t], hdr_policy=policy,
                        audio_langs=["eng"], subtitle_langs=["eng"])
                j.rip_dir = os.path.join(cfg.rip_path, j.id)
                return j

            remuxed.clear()
            log = {}
            makemkv.rip_titles = make_fake_rip(log)
            film = film_job(HdrPolicy(), "Pitch Perfect")
            check(ctl.stream_possible(film) == (True, ""), f"stream_possible: {ctl.stream_possible(film)}")
            samples = []
            ctl.rip(film, lambda f, t: None, threading.Event(), samples=samples.append)
            f0 = film.files[0] if film.files else None
            check(film.copy_mode == "stream", f"film mode: {film.copy_mode}")
            check(log.get("selection", "").startswith("+sel"), f"selection passed: {log.get('selection')}")
            check(not remuxed, "a streamed film was remuxed")
            check(f0 and f0.state == "verified" and film.stage == "done", f"film: {f0 and f0.state} {film.stage}")
            dest = os.path.join(movies, "Pitch Perfect (2012)/Pitch Perfect (2012).mkv")
            with open(f0.path, "rb") as fh:
                local = hashlib.sha256(fh.read()).hexdigest()
            check(os.path.isfile(dest) and f0.sha256 == local, "the streamed film's NAS copy or hash is wrong")
            check({s.kind for s in samples} == {"rip", "copy"}, f"film sample kinds: {({s.kind for s in samples})}")

            # 5. The same film with a non-RAW HDR setting is copied after its rip instead, and says why.
            remuxed.clear()
            makemkv.rip_titles = make_fake_rip({})
            film2 = film_job(HdrPolicy(keep="hdr10"), "Arrival")
            ok, why = ctl.stream_possible(film2)
            check(not ok and "RAW" in why, f"non-RAW stream_possible: {ok} {why!r}")
            ctl.rip(film2, lambda f, t: None, threading.Event())
            check(film2.copy_mode == "per_title" and "RAW" in film2.copy_mode_note,
                  f"non-RAW film: {film2.copy_mode} {film2.copy_mode_note!r}")
            check(len(remuxed) == 1 and film2.stage == "done", f"non-RAW film: remux {len(remuxed)}, {film2.stage}")
        finally:
            for n, v in saved_mk.items():
                if v is missing:
                    if hasattr(makemkv, n):
                        delattr(makemkv, n)
                else:
                    setattr(makemkv, n, v)
            hdr.remux_tracks, hdr.probe, config.load_secret, pipeline.follow = saved

    for f in failures:
        print("FAIL:", f)
    print(f"pipeline overlap: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_pipeline():
    assert run() == 0


def test_pipeline_overlap():
    assert run_overlap() == 0


if __name__ == "__main__":
    sys.exit(run() | run_overlap())
