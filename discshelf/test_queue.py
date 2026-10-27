#!/usr/bin/env python3
"""
Tests for the controller side of the Queue page (pipeline.QueueViews and the Controller
methods it offers): which section each job goes in (needs you, working, done, and the
24-hour and dismissed rules), the check rows with their title-card readings and candidate
episodes, confirm_title during and after a rip, copy_ready leaving titles that need a
check, free_local never deleting a file that isn't verified on the NAS, the season view
and the next disc, reverify finding a changed or missing NAS copy, plex_link and its
fallbacks, ejecting after a rip, and the next disc's prefill in new_job.

MakeMKV, the HDR tools and TVmaze are faked (the fakes from test_pipeline.py), Plex is a
fake fetch, and copies go through the real rsync into a temporary "NAS" folder. Nothing
starts makemkvcon or touches a drive: makemkv.eject is replaced by a recorder.

Run:  python3 discshelf/test_queue.py    (also works under pytest)
"""
import copy
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from discshelf import config, discdb, hdr, makemkv, pipeline  # noqa: E402
from discshelf.config import Config, Destination  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import Episode, HdrInfo, Job, Match, RippedFile  # noqa: E402
from test_pipeline import NO_DISCDB, make_fake_rip, tv_disc  # noqa: E402

NAMES = ["Slumber Party Panic", "Finn the Wizard", "Ricardio the Heart Guy", "Business Time", "Wizard Battle",
         "My Two Favorite People", "Memories of Boom Boom Mountain", "The Creeps", "Freak City", "Donny"]


class CountingTvMaze:
    """Ten 22-minute episodes in season 1. Counts episode-list calls; fails while offline is set."""

    def __init__(self):
        self.calls = 0
        self.offline = False

    def search(self, q):
        return []

    def episodes(self, show_id, season):
        self.calls += 1
        if self.offline:
            raise pipeline.TvMazeError("Could not reach TVmaze (offline).")
        return [Episode(1, n, NAMES[n - 1], 22) for n in range(1, 11) if season in (None, 1)]


class FakeFetch:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers):
        self.calls.append(url)
        path = urllib.parse.urlsplit(url).path
        if path in self.routes:
            body = self.routes[path]
            if isinstance(body, Exception):
                raise body
            return 200, json.dumps(body).encode()
        return 404, b""


def ago(hours):
    return (dt.datetime.now().astimezone() - dt.timedelta(hours=hours)).isoformat(timespec="seconds")


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def item(view, job):
        return next((it for it in view.items if it.job.id == job.id), None)

    with tempfile.TemporaryDirectory() as root:
        nas = os.path.join(root, "nas", "Shows")
        movies = os.path.join(root, "nas", "Movies")
        os.makedirs(nas)
        os.makedirs(movies)
        cfg = Config(destinations=[Destination("tv", "TV shows", nas, "", "tv"),
                                   Destination("movie", "Movies", movies, "", "movie")],
                     rip_dir=os.path.join(root, "rips"), read_title_cards=False, copy_as_ripped=False,
                     eject_after_rip=True, plex_url="http://plex.test:32400")
        tv = CountingTvMaze()
        ctl = pipeline.Controller(cfg, JobStore(os.path.join(root, "jobs")), tvmaze=tv,
                                  discdb_client=discdb.TheDiscDb(fetch=lambda _u, _b: NO_DISCDB))
        ctl.plex_fetch = FakeFetch({})      # nothing here may reach a real Plex
        holder = {}
        ejected = []
        secrets = {}
        saved = (makemkv.scan_disc_parsed, makemkv.rip_titles, makemkv.eject, hdr.remux_tracks, hdr.probe,
                 config.load_secret, config.save_secret)
        makemkv.scan_disc_parsed = lambda cfg_, idx, run=None, min_seconds=0: ({"sinfo": {}}, holder["disc"])
        makemkv.rip_titles = make_fake_rip({})
        makemkv.eject = lambda device, run=None: ejected.append(device) or True
        hdr.remux_tracks = lambda src, dst, a, s, cfg_, run=None: dst
        hdr.probe = lambda path, cfg_, run=None: HdrInfo(resolution="1920x1080", codec="h264")
        config.load_secret = lambda name, path=None: secrets.get(name, "")
        config.save_secret = lambda name, value, path=None: secrets.__setitem__(name, value)
        try:
            # 1. Disc 1, copied after the rip. Ejected once MakeMKV finished, from the scan's device.
            holder["disc"] = tv_disc("TEST_SHOW_S1_D1")
            job = ctl.new_job(ctl.scan(0))
            job.show = "Test Show"
            ctl.rip(job, lambda f, t: None, threading.Event())
            check(ejected == ["/dev/sr9"], f"eject after the rip: {ejected}")
            check(job.stage == "match" and len(job.files) == 3, f"after rip: {job.stage} {len(job.files)}")

            # Title 2 needs a check: its note names another episode and its card read "WIZARD".
            ms = copy.deepcopy(ctl.suggest_matches(job, show_id=7))
            ms[2].confidence, ms[2].source = "check", "runtime"
            ms[2].note = ("Title is 22 min but the episode is listed as 30 min. Order unclear: the running time "
                          "also fits episode 5, “Wizard Battle”.")
            ctl.update_matches(job, ms)
            ctl._card_state[job.id] = {2: pipeline.CardStatus("read", text="WIZARD")}
            v = ctl.queue([job], set())
            it = item(v, job)
            check(it is not None and it.section == "needs", f"a disc with a check isn't under needs: {it}")
            check(it and [c.title_index for c in it.checks] == [2], f"check rows: {it and it.checks}")
            check(it and it.ready_to_copy == 2, f"ready to copy: {it and it.ready_to_copy}")
            check(it and "0 of 3 episodes in Plex." in it.reason and "title card" in it.reason, f"reason: {it and it.reason}")
            row = it.checks[0] if it and it.checks else None
            opts = [(e.number, why) for e, why in row.options] if row else []
            check(row and row.read == "WIZARD" and row.current and row.current.number == 3, f"row: {row}")
            check([n for n, _ in opts] == [3, 5, 2], f"options: {opts}")
            check(opts and opts[0][1] == "Its running time fits this episode.", f"current's reason: {opts[:1]}")
            check(len(opts) == 3 and "“Wizard”" in opts[2][1], f"card-word reason: {opts[2:]}")
            # With no card read this session, the reading comes from the note.
            ms = copy.deepcopy(job.matches)
            ms[2].note = "The title card reads “BUSINES TIME”, which could be “Business Time”."
            ctl.update_matches(job, ms)
            ctl._card_state.pop(job.id)
            row = item(ctl.queue([job], set()), job).checks[0]
            check(row.read == "BUSINES TIME" and [e.number for e, _ in row.options][:2] == [3, 4],
                  f"reading from the note: {row.read!r} {[e.number for e, _ in row.options]}")

            # copy_ready copies the two ready titles and leaves the check alone; the job stays in "match".
            n = ctl.copy_ready(job, lambda f, t: None, threading.Event())
            check(n == 2 and job.stage == "match", f"copy_ready: {n} {job.stage}")
            check([f.state for f in job.files] == ["verified", "verified", "ripped"], f"states: {[f.state for f in job.files]}")
            it = item(ctl.queue([job], set()), job)
            check(it and it.section == "needs" and it.ready_to_copy == 0 and len(it.checks) == 1, f"after copy_ready: {it}")
            check(it and it.safe_bytes == job.files[0].size_bytes + job.files[1].size_bytes, f"safe bytes: {it and it.safe_bytes}")

            # free_local deletes only verified files whose NAS copy is still there at the same size.
            nas1 = os.path.join(nas, job.files[1].dest_rel)
            with open(nas1, "ab") as fh:
                fh.write(b"x")          # the NAS copy is now a different size, so the local one stays
            freed = ctl.free_local(job)
            check(freed == job.files[0].size_bytes, f"freed {freed}")
            check(not os.path.exists(job.files[0].path) and job.files[0].local_deleted, "the verified file wasn't freed")
            check(os.path.exists(job.files[1].path) and not job.files[1].local_deleted, "a file with a bad NAS copy was freed")
            check(os.path.exists(job.files[2].path) and not job.files[2].local_deleted, "an unverified file was freed")
            with open(nas1, "rb+") as fh:
                fh.truncate(job.files[1].size_bytes)
            check(ctl.store.load(job.id).files[0].local_deleted, "local_deleted wasn't saved")

            # The season view: 2 of 10 episodes in Plex, the next disc is 2, and TVmaze is asked once.
            calls = tv.calls
            for _ in range(3):
                v = ctl.queue([job], set())
            check(tv.calls - calls <= 1, f"TVmaze asked {tv.calls - calls} times for one show")
            sv = v.seasons[0] if v.seasons else None
            check(sv and (sv.show, sv.season, sv.in_plex, len(sv.episodes)) == ("Test Show", 1, 2, 10),
                  f"season: {sv}")
            check(sv and [e.state for e in sv.episodes[:4]] == ["plex", "plex", "ripped", "todo"], f"states: {sv and sv.episodes[:4]}")
            check(sv and sv.episodes[0].disc == 1 and sv.episodes[3].disc is None, "disc numbers in the season")
            check(sv and sv.next_disc == 2 and sv.next_text == "Next: disc 2. E04–E10 aren't ripped yet.",
                  f"next: {sv and (sv.next_disc, sv.next_text)}")

            # confirm_title after the rip: a different episode becomes a manual, confirmed match.
            ctl.confirm_title(job, 2, Episode(1, 5, "Wizard Battle", 22))
            m2 = job.matches[2]
            check(m2.episode.number == 5 and m2.confirmed and m2.source == "manual" and m2.note == "",
                  f"confirmed: {m2}")
            ctl.confirm_title(job, 0, Episode(1, 9, "Freak City", 22))
            check(job.matches[0].episode.number == 1, "confirm_title changed a title already on the NAS")
            n = ctl.copy_ready(job, lambda f, t: None, threading.Event())
            check(n == 1 and job.stage == "done" and job.copied_at, f"second copy_ready: {n} {job.stage}")
            it = item(ctl.queue([job], set()), job)
            check(it and it.section == "done" and it.done_at == job.copied_at, f"done item: {it}")
            check(it and it.reason == "3 episodes copied to the NAS and checked.", f"done reason: {it and it.reason}")

            # The 24-hour rule, dismiss, and dismissed jobs leaving the queue.
            real_copied = job.copied_at
            job.copied_at = ago(25)
            check(item(ctl.queue([job], set()), job) is None, "a disc done 25 h ago is still in the queue")
            job.copied_at = real_copied
            ctl.dismiss(job)
            check(item(ctl.queue([job], set()), job) is None and ctl.store.load(job.id).dismissed, "dismiss")

            # reverify: one NAS copy changed (same size), one gone. Both are marked failed for Retry.
            job.dismissed = False
            p0, p2 = (os.path.join(nas, job.files[i].dest_rel) for i in (0, 2))
            with open(p0, "r+b") as fh:
                b = fh.read(1)
                fh.seek(0)
                fh.write(bytes([b[0] ^ 0xFF]))
            os.rename(p2, p2 + ".moved")
            problems = ctl.reverify(job, lambda f, t: None)
            check(len(problems) == 2 and "doesn't match" in problems[0] and "missing" in problems[1], f"reverify: {problems}")
            check([f.state for f in job.files] == ["failed", "verified", "failed"], f"after reverify: {[f.state for f in job.files]}")
            it = item(ctl.queue([job], set()), job)
            check(it and it.section == "needs" and [ti for ti, _ in it.failed] == [0, 2], f"failed: {it and it.failed}")
            check(it and "rip the disc again" in it.failed[0][1] and "rip the disc again" not in it.failed[1][1],
                  f"why: {it and it.failed}")
            # Put both back: a re-check finds them good again (their checksums were kept).
            os.rename(p2 + ".moved", p2)
            with open(p0, "r+b") as fh:
                fh.write(b)
            check(ctl.reverify(job, lambda f, t: None) == [], "a good copy failed reverify")
            check([f.state for f in job.files] == ["verified"] * 3, f"after the re-check: {[f.state for f in job.files]}")

            # free_local(None) frees the rest that's verified, and removes the emptied rip folder.
            freed = ctl.free_local(None)
            check(freed > 0 and not os.path.exists(job.rip_dir), f"free_local(None): {freed}, folder left: "
                                                                 f"{os.listdir(job.rip_dir) if os.path.isdir(job.rip_dir) else ''}")

            # Offline: the queue never raises, and a season falls back to the episodes the matches name.
            tv2 = CountingTvMaze()
            tv2.offline = True
            ctl2 = pipeline.Controller(cfg, ctl.store, tvmaze=tv2,
                                       discdb_client=discdb.TheDiscDb(fetch=lambda _u, _b: NO_DISCDB))
            j2 = copy.deepcopy(job)
            j2.files[2].state = "ripped"
            v2 = ctl2.queue([j2], set())
            check(tv2.calls == 1 and v2.seasons and len(v2.seasons[0].episodes) == 3, f"offline season: {v2.seasons}")
            check(v2.seasons and v2.seasons[0].next_disc is None, "an offline season named a next disc")
            ctl2.queue([j2], set())
            check(tv2.calls == 1, "an offline TVmaze was asked again straight away")

            # 2. Next-disc prefill: same show (by name or by label) and season in progress.
            j3 = copy.deepcopy(job)
            j3.id, j3.disc_number, j3.files[2].state, j3.created = "season-copy", 1, "ripped", job.created
            ctl.save_job(j3)
            ctl.store.delete(job.id)
            holder["disc"] = tv_disc("TEST_SHOW_S1")
            nj = ctl.new_job(ctl.scan(0))
            check((nj.show, nj.show_id, nj.season, nj.disc_number, nj.destination) == ("Test Show", 7, 1, 2, nas),
                  f"prefill: {(nj.show, nj.show_id, nj.season, nj.disc_number, nj.destination)}")
            holder["disc"] = tv_disc("TEST_SHOW_S1_D3")
            check(ctl.new_job(ctl.scan(0)).disc_number == 3, "the label's own disc number was replaced")
            disc = tv_disc("TEST_SHOW_SEASON1")
            disc.name = "TS Collection"
            holder["disc"] = disc
            nj = ctl.new_job(ctl.scan(0))
            check(nj.show_id == 7 and nj.disc_number == 2, f"prefill by label: {nj.show} {nj.show_id} {nj.disc_number}")
            holder["disc"] = tv_disc("TEST_SHOW_S2_D1")
            check(ctl.new_job(ctl.scan(0)).show_id is None, "a disc of another season was prefilled")
            other = tv_disc("OTHER_SHOW_S1")
            other.name = "Other Show"
            holder["disc"] = other
            nj = ctl.new_job(ctl.scan(0))
            check(nj.show == "Other Show" and nj.show_id is None and nj.disc_number is None, f"unrelated disc: {nj}")

            # 3. A per-title rip: working while nothing waits, needs while a title waits for a
            #    check, and confirm_title releases that title's copy before the rip ends.
            cfg.copy_as_ripped = True
            ejected.clear()
            log = {}
            seen = {}

            def hold(n, ti):
                if ti == 0:
                    seen["first"] = ctl.queue([holder["job"]], {holder["job"].id})
                if ti != 2:
                    return
                j = holder["job"]
                tp1 = j.titles_progress[1]
                deadline = time.monotonic() + 10
                while tp1.state != "waiting_match" and time.monotonic() < deadline:
                    time.sleep(0.01)
                seen["waiting"] = ctl.queue([j], {j.id})
                ctl.confirm_title(j, 1, Episode(1, 6, NAMES[5], 22))
                while tp1.state != "verified" and time.monotonic() < deadline:
                    time.sleep(0.01)
                seen["copied_before_end"] = tp1.state == "verified"

            makemkv.rip_titles = make_fake_rip(log, hold)
            holder["disc"] = tv_disc("TEST_SHOW_S1_D2")
            rj = ctl.new_job(ctl.scan(0))
            rj.show = "Test Show"
            holder["job"] = rj
            ms = copy.deepcopy(ctl.suggest_matches(rj, show_id=7))
            ms[1].confidence, ms[1].note = "check", "Title is 21 min but the episode is listed as 30 min."
            ctl.update_matches(rj, ms)
            ctl.rip(rj, lambda f, t: None, threading.Event())
            first = item(seen.get("first"), rj) if seen.get("first") else None
            check(first and first.section == "working", f"running with nothing waiting: {first and first.section}")
            waiting = item(seen.get("waiting"), rj) if seen.get("waiting") else None
            check(waiting and waiting.section == "needs" and [c.title_index for c in waiting.checks] == [1],
                  f"running with a title waiting: {waiting}")
            check(seen.get("copied_before_end") is True, "confirm_title didn't copy the title during the rip")
            # Title 2's running-time guess, S01E05, is already on disc 1 (the season check), so it
            # was held back; once it's given another episode the disc finishes.
            check(rj.stage == "match" and rj.matches[1].episode.number == 6, f"after the per-title rip: {rj.stage}")
            check(rj.matches[2].held == "S01E05 “Wizard Battle” is already on disc 1 title 2.",
                  f"title 2 not held: {rj.matches[2].held!r}")
            ctl.confirm_title(rj, 2, Episode(1, 7, NAMES[6], 22))
            check(not rj.matches[2].held and rj.matches[2].ready, "a new episode didn't release title 2")
            ctl.copy_ready(rj, lambda f, t: None, threading.Event())
            check(rj.stage == "done", f"after settling title 2: {rj.stage}")
            check(ejected == ["/dev/sr9"], f"eject after the second rip: {ejected}")

            # 4. No eject when the setting is off, or when the rip was stopped.
            makemkv.rip_titles = make_fake_rip({})
            cfg.copy_as_ripped = False
            for flag, stop in ((False, False), (True, True)):
                ejected.clear()
                cfg.eject_after_rip = flag
                ej = ctl.new_job(ctl.scan(0))
                ej.show = "Test Show"
                cancel = threading.Event()
                if stop:
                    cancel.set()
                ctl.rip(ej, lambda f, t: None, cancel)
                check(ejected == [], f"ejected with the setting {flag} and stop {stop}: {ejected}")

            # 5. Other sections: a failed rip needs you, older finished discs and unripped jobs don't show.
            failed = Job(id="failed-rip", show="Test Show", stage="failed", status="Rip failed: the disc can't be read.")
            old = Job(id="old", show="Film", media_kind="movie", stage="done", copied_at=ago(30))
            fresh = Job(id="fresh", show="Test Show", stage="rip")
            v = ctl.queue([failed, old, fresh], set())
            check([(i.job.id, i.section, i.reason) for i in v.items] ==
                  [("failed-rip", "needs", "Rip failed: the disc can't be read.")], f"other sections: {v.items}")
            check(v.local_bytes == sum(os.path.getsize(os.path.join(r, n)) for r, _, ns in os.walk(cfg.rip_path)
                                       for n in ns), "local bytes of the rip folder")

            # 6. Order: needs, then working, then done.
            a = Job(id="a", show="A", stage="match", created=ago(3), matches=[Match(0, "episode", Episode(1, 1))])
            b = Job(id="b", show="B", stage="rip", created=ago(1))
            c = Job(id="c", show="C", stage="done", copied_at=ago(2), created=ago(4))
            d = Job(id="d", show="D", stage="match", created=ago(0.5))
            v = ctl.queue([c, b, a, d], {"b"})
            check([(i.job.id, i.section) for i in v.items] == [("d", "needs"), ("a", "needs"), ("b", "working"),
                                                                 ("c", "done")], f"order: {[(i.job.id, i.section) for i in v.items]}")

            # 7. plex_link: the exact season when Plex has it, else Plex Web's home page.
            home = "http://plex.test:32400/web/index.html"
            pj = Job(id="p", show="Test Show", season=1, library="TV Shows", destination=nas)
            check(ctl.plex_link(pj) == home, "a link without a Plex sign-in")
            secrets.update(plex_token="tok", plex_client_id="cid")
            sections = {"MediaContainer": {"Directory": [{"key": "2", "title": "TV Shows", "type": "show",
                                                          "Location": [{"path": "/volume1/Media/Shows"}]}]}}
            routes = {"/identity": {"MediaContainer": {"machineIdentifier": "m1"}},
                      "/library/sections": sections,
                      "/library/sections/2/all": {"MediaContainer": {"Metadata": [{"ratingKey": "9001", "title": "Test Show"}]}},
                      "/library/metadata/9001/children": {"MediaContainer": {"Metadata": [
                          {"ratingKey": "9002", "type": "season", "index": 1}]}}}
            ctl.plex_fetch = FakeFetch(routes)
            check(ctl.plex_link(pj) == f"{home}#!/server/m1/details?key=%2Flibrary%2Fmetadata%2F9002",
                  f"season link: {ctl.plex_link(pj)}")
            pj.season = 4
            check(ctl.plex_link(pj).endswith("%2F9001"), "no such season didn't fall back to the show")
            pj.show = "Unknown"
            check(ctl.plex_link(pj) == home, "a show Plex lacks didn't give the home page")
            ctl.plex_fetch = FakeFetch({p: pipeline.PlexError("Could not reach plex.test") for p in routes})
            pj.show = "Test Show"
            check(ctl.plex_link(pj) == home, "offline Plex didn't give the home page")

            # 8. nas_folder: the season folder, not an extras folder; a film's own folder.
            nj = Job(id="n", show="Test Show", season=1, destination=nas, files=[
                RippedFile(0, "", dest_rel="Test Show/Featurettes/Extra.mkv"),
                RippedFile(1, "", dest_rel="Test Show/Season 01/Test Show - S01E01.mkv")])
            check(ctl.nas_folder(nj) == os.path.join(nas, "Test Show/Season 01"), f"nas folder: {ctl.nas_folder(nj)}")
            fj = Job(id="f", show="Arrival", year=2016, media_kind="movie", destination=movies)
            check(ctl.nas_folder(fj) == os.path.join(movies, "Arrival (2016)"), f"film folder: {ctl.nas_folder(fj)}")
        finally:
            (makemkv.scan_disc_parsed, makemkv.rip_titles, makemkv.eject, hdr.remux_tracks, hdr.probe,
             config.load_secret, config.save_secret) = saved

    # 9. The fake controller has the same methods, with sample data for every section.
    from discshelf.fake import FakeController
    fake = FakeController(Config(), delay=0)
    v = fake.queue(fake.jobs(), set())
    sections = [i.section for i in v.items]
    check(sections == sorted(sections, key=["needs", "working", "done"].index), f"fake order: {sections}")
    check({"needs", "working", "done"} <= set(sections), f"fake sections: {sections}")
    check(any(i.checks and all(c.options for c in i.checks) and any(c.read for c in i.checks) for i in v.items),
          "the fake has no check rows with options and readings")
    check(any(i.failed for i in v.items), "the fake has no failed copy")
    check(any(s.next_disc for s in v.seasons), "the fake has no season with a next disc")
    needs = next(i for i in v.items if i.checks)
    row = needs.checks[0]
    fake.confirm_title(needs.job, row.title_index, row.options[0][0])
    check(len(item(fake.queue(fake.jobs(), set()), needs.job).checks) == len(needs.checks) - 1, "fake confirm_title")
    check(fake.copy_ready(needs.job, lambda f, t: None, threading.Event()) > 0, "fake copy_ready copied nothing")
    done = next(i for i in v.items if i.section == "done")
    check(fake.free_local(done.job) > 0 and fake.free_local(done.job) == 0, "fake free_local")
    check(fake.plex_link(done.job).startswith(fake.cfg.plex_url) and fake.nas_folder(done.job), "fake links")
    check(fake.reverify(done.job, lambda f, t: None) == [], "fake reverify")
    fake.dismiss(done.job)
    check(item(fake.queue(fake.jobs(), set()), done.job) is None, "fake dismiss")
    sev = fake.new_job(fake.scan(0))
    check(sev.show_id == 44933 and sev.disc_number == 1, f"fake prefill: {sev.show_id} {sev.disc_number}")

    # A failed rip of a disc stops needing you once a newer job for the same disc finished;
    # a failed rip with nothing newer still needs you.
    with tempfile.TemporaryDirectory() as root:
        c2 = pipeline.Controller(Config(rip_dir=root), JobStore(os.path.join(root, "jobs")))
        old_fail = Job(id="a", disc_label="DISC_X", stage="failed", created="2026-10-26T09:00:00-04:00",
                       status="Rip failed: MakeMKV could not rip title 1: Failed to open disc")
        retried = Job(id="b", disc_label="DISC_X", stage="match", created="2026-10-26T09:06:00-04:00")
        lone = Job(id="c", disc_label="DISC_Y", stage="failed", created="2026-10-26T09:00:00-04:00",
                   status="Rip failed: the disc couldn't be read")
        v = c2.queue([old_fail, retried, lone], set())
        ids = {it.job.id for it in v.items}
        check("a" not in ids and "c" in ids, f"superseded failed rip still in the queue: {sorted(ids)}")

    for f in failures:
        print("FAIL:", f)
    print(f"queue: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_queue():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
