#!/usr/bin/env python3
"""
Tests for the AV1 second version in pipeline.Controller: an encode is queued once a 4K title's
remux is verified on the NAS (not for 1080p titles, not when it's off, and a disc's own choice
wins over Settings), one encoder thread runs them one at a time in the order queued, pause,
resume, stop and retry, a failed quality check, the copy next to the remux under av1.av1_name,
free_local never deleting a remux whose AV1 version isn't finished, picking up after a restart,
the "when to encode" setting (idle and night, with an injected clock), and the Queue sections.

av1.encode and av1.check_quality are replaced by fakes that write a small file, so nothing runs
ffmpeg. Copies go through the real rsync into a temporary "NAS" folder. Nothing touches a drive.

Run:  python3 discshelf/test_encode.py    (also works under pytest)
"""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from discshelf import av1, config, pipeline  # noqa: E402
from discshelf.config import Config, Destination  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import EncodeJob, Episode, Job, Match, RippedFile, Stream, Title, job_from_dict, to_dict  # noqa: E402


class FakeAv1:
    """Stands in for av1.encode and av1.check_quality. While hold is set an encode stands still
    (still obeying cancel and pause), so a test can look at it mid-way."""

    def __init__(self):
        self.hold = threading.Event()
        self.calls: list[str] = []          # src of each encode, in order
        self.checks: list[str] = []
        self.active = 0
        self.max_active = 0
        self.paused_seen = False
        self.scores: dict[str, float] = {}  # src -> VMAF score; 95 otherwise
        self._lock = threading.Lock()

    def encode(self, src, dst, s, cfg, progress=None, cancel=None, pause=None, **_kw):
        with self._lock:
            self.calls.append(src)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        partial = dst + ".encode.partial"
        try:
            with open(partial, "wb") as fh:
                fh.write(b"\0" * 1000)
            for k in range(1, 6):
                while True:
                    if cancel is not None and cancel.is_set():
                        os.remove(partial)
                        raise av1.Cancelled()
                    if pause is not None and pause.is_set():
                        self.paused_seen = True
                    elif not (self.hold.is_set() and k == 3):
                        break
                    time.sleep(0.01)
                if progress:
                    progress(av1.EncodeProgress(k / 5, 18.5, k * 60.0, 300.0, k * 1000, 5000, (5 - k) * 60.0))
                time.sleep(0.01)
            with open(partial, "wb") as fh:
                fh.write(os.urandom(5000))
            os.replace(partial, dst)
            return av1.EncodeResult(dst, 5000, 120.0, 18.5)
        finally:
            with self._lock:
                self.active -= 1

    def check_quality(self, src, dst, cfg, clips=6, clip_s=10.0, threshold=93.0, progress=None, **_kw):
        self.checks.append(src)
        score = self.scores.get(src, 95.0)
        return av1.QualityResult(score, [score] * clips, score >= threshold, threshold)


def wait(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    fake = FakeAv1()
    saved = (av1.encode, av1.check_quality, config.load_secret)
    av1.encode, av1.check_quality = fake.encode, fake.check_quality
    config.load_secret = lambda name, path=None: ""
    try:
        with tempfile.TemporaryDirectory() as root:
            movies = os.path.join(root, "nas", "Movies")
            shows = os.path.join(root, "nas", "Shows")
            os.makedirs(movies)
            os.makedirs(shows)
            jobs_dir = os.path.join(root, "jobs")
            cfg = Config(destinations=[Destination("tv", "TV shows", shows, "", "tv"),
                                       Destination("movie", "Movies", movies, "", "movie")],
                         rip_dir=os.path.join(root, "rips"), read_title_cards=False, eject_after_rip=False)

            def controller():
                c = pipeline.Controller(cfg, JobStore(jobs_dir))
                c.av1_stats = av1.Stats(os.path.join(root, "av1_stats.json"))
                c.encode_poll, c.encode_wait, c.encode_save_interval = 0.02, 0.02, 0.0
                return c

            ctl = controller()
            n = [0]

            def video(width):
                return [Stream(0, "video", resolution=f"{width}x{width * 9 // 16}")]

            def film(name, width=3840, av1_choice=""):
                n[0] += 1
                rip = os.path.join(root, "rips", f"{n[0]:02d}")
                os.makedirs(rip)
                path = os.path.join(rip, f"{name.replace(' ', '_')}_t00.mkv")
                with open(path, "wb") as fh:
                    fh.write(os.urandom(20000 + n[0]))
                job = Job(id=f"job{n[0]:02d}", disc_label=name.upper(), media_kind="movie", show=name, year=2024,
                          destination=movies, rip_dir=rip, stage="match", av1=av1_choice,
                          created=f"2026-10-26T10:{n[0]:02d}:00-04:00",
                          titles=[Title(0, duration_s=9960, size_bytes=os.path.getsize(path), streams=video(width),
                                        kind="feature", selected=True)],
                          files=[RippedFile(0, path, os.path.getsize(path))],
                          matches=[Match(0, "feature", confidence="close", source="")])
                ctl.save_job(job)
                return job

            def tv(name, episodes=2):
                n[0] += 1
                rip = os.path.join(root, "rips", f"{n[0]:02d}")
                os.makedirs(rip)
                job = Job(id=f"job{n[0]:02d}", disc_label=name.upper(), media_kind="tv", show=name, season=1,
                          destination=shows, rip_dir=rip, stage="match", created=f"2026-10-26T10:{n[0]:02d}:00-04:00")
                for i in range(episodes):
                    path = os.path.join(rip, f"{name}_t{i:02d}.mkv")
                    with open(path, "wb") as fh:
                        fh.write(os.urandom(10000 + i))
                    job.titles.append(Title(i, duration_s=3300, size_bytes=os.path.getsize(path),
                                            streams=video(3840), kind="episode", selected=True))
                    job.files.append(RippedFile(i, path, os.path.getsize(path)))
                    job.matches.append(Match(i, "episode", Episode(1, i + 1, f"Episode {i + 1}"), confidence="close"))
                ctl.save_job(job)
                return job

            def copy(job):
                ctl.copy(job, lambda f, t: None, threading.Event())

            def item(job):
                return next((it for it in ctl.queue(ctl.jobs(), set()).items if it.job.id == job.id), None)

            def state(job, ti=0):
                with ctl.job_lock:
                    e = pipeline._enc(job, ti)
                    return e.state if e else None

            # 1. A 4K film: queued after the copy, encoded, checked and copied next to the remux.
            fake.hold.set()
            dune = film("Dune Part Two")
            copy(dune)
            check(dune.stage == "done" and dune.files[0].state == "verified", f"remux copy: {dune.stage}")
            check(len(dune.encodes) == 1, f"encodes after the copy: {dune.encodes}")
            e = dune.encodes[0]
            check(e.dest_rel == av1.av1_name(dune.files[0].dest_rel) and e.dest_rel.endswith(" - 2160p AV1.mkv"),
                  f"AV1 name: {e.dest_rel} for {dune.files[0].dest_rel}")
            check(os.path.dirname(e.dest_rel) == os.path.dirname(dune.files[0].dest_rel), "the AV1 file isn't next to the remux")
            check((e.encoder, e.quality, e.grain, e.width) == ("cpu", 2, True, 3840), f"settings: {e}")
            check(wait(lambda: state(dune) == "encoding" and e.fraction > 0), f"never started encoding: {e.state}")
            it = item(dune)
            check(it is not None and it.section == "working" and it.encodes and it.encodes[0] is e,
                  f"an encoding disc isn't working: {it and (it.section, it.reason)}")
            check(it and it.reason.startswith("Encoding the AV1 version · "), f"working reason: {it and it.reason}")
            st = ctl.encode_status()
            check(st.job_id == dune.id and st.title_index == 0 and st.gate_open, f"encode status: {st}")
            # The remux is what it encodes from, so nothing deletes it now.
            check(ctl.free_local(dune) == 0 and os.path.isfile(dune.files[0].path), "free_local deleted a remux mid-encode")
            check(it and it.safe_bytes == 0, f"a remux with its encode pending counted as safe: {it and it.safe_bytes}")
            ctl.cfg.cleanup = "delete"
            check(ctl.cleanup(apply=False) == [], "cleanup offered a remux whose AV1 version isn't made")

            # Pause and resume: the encode is suspended where it is.
            check(ctl.pause_encode(dune) and state(dune) == "paused", f"pause: {state(dune)}")
            check(wait(lambda: fake.paused_seen), "the encode never saw the pause")
            check(ctl.encode_status().state == "paused", "status while paused")
            check("Paused" in item(dune).reason, f"paused reason: {item(dune).reason}")
            check(ctl.resume_encode(dune) and wait(lambda: state(dune) == "encoding"), f"resume: {state(dune)}")
            fake.hold.clear()
            check(wait(lambda: state(dune) == "verified"), f"never verified: {state(dune)} {e.note}")
            nas_av1 = os.path.join(movies, e.dest_rel)
            check(os.path.isfile(nas_av1) and os.path.getsize(nas_av1) == 5000, "the AV1 file isn't on the NAS")
            check(fake.checks == [dune.files[0].path], f"quality checks: {fake.checks}")
            check(e.vmaf == 95.0 and e.finished and e.sha256, f"after: {e}")
            check(len(ctl.av1_stats.runs) == 1 and ctl.av1_stats.runs[0]["seconds"] == 120.0, f"stats: {ctl.av1_stats.runs}")
            size, secs, measured = ctl.av1_estimate(av1.EncodeSettings("cpu", 2, True))
            check(measured and size > 0 and secs > 0, f"estimate after one encode: {size} {secs} {measured}")
            check(not ctl.av1_estimate(av1.EncodeSettings("gpu", 4, False))[2], "an unmeasured setting reads as measured")
            it = item(dune)
            check(it and it.section == "done" and "AV1 version" in it.reason, f"done: {it and (it.section, it.reason)}")
            check(it and it.safe_bytes == os.path.getsize(dune.files[0].path) + 5000, f"safe bytes: {it and it.safe_bytes}")
            remux = dune.files[0].path
            freed = ctl.free_local(dune)
            check(freed > 0 and not os.path.exists(remux) and not os.path.exists(e.path) and e.local_deleted,
                  f"free_local after both versions: {freed}")
            check(os.path.isfile(nas_av1) and os.path.isfile(os.path.join(movies, dune.files[0].dest_rel)),
                  "free_local touched the NAS")

            # 2. A 1080p film gets none; nor does a 4K film with AV1 off, unless the disc asks for one.
            hd = film("Arrival", width=1920)
            copy(hd)
            check(hd.encodes == [], f"1080p film queued an encode: {hd.encodes}")
            ctl.cfg.av1_enabled = False
            off = film("Tenet")
            copy(off)
            check(off.encodes == [], "queued with AV1 off in Settings")
            gpu = film("Oppenheimer", av1_choice="gpu")
            copy(gpu)
            check(len(gpu.encodes) == 1 and gpu.encodes[0].encoder == "gpu" and not gpu.encodes[0].grain,
                  f"per-disc gpu override: {gpu.encodes}")
            ctl.cfg.av1_enabled = True
            no = film("Interstellar", av1_choice="off")
            copy(no)
            check(no.encodes == [], "queued for a disc set to off")
            check(wait(lambda: state(gpu) == "verified"), f"gpu encode: {state(gpu)}")

            # 3. One at a time, in the order queued, across discs.
            fake.hold.set()
            fake.calls.clear()
            fake.max_active = 0
            a, b = film("Blade Runner 2049"), film("Arrival 4K")
            copy(a)
            copy(b)
            check(wait(lambda: state(a) == "encoding"), "first disc didn't start")
            time.sleep(0.1)
            check(state(b) == "waiting" and fake.max_active == 1, f"second ran alongside: {state(b)} {fake.max_active}")
            check("waits for the encode running now" in item(b).reason, f"waiting reason: {item(b).reason}")
            fake.hold.clear()
            check(wait(lambda: state(b) == "verified"), f"second disc: {state(b)}")
            check(fake.calls == [a.files[0].path, b.files[0].path] and fake.max_active == 1,
                  f"order {fake.calls}, at once {fake.max_active}")

            # 4. Stop: the running one and the rest of that disc are skipped; the remuxes stay.
            fake.hold.set()
            show = tv("Severance")
            copy(show)
            check(len(show.encodes) == 2, f"TV encodes: {show.encodes}")
            check(wait(lambda: state(show, 0) == "encoding"), "TV encode didn't start")
            check(ctl.stop_encode(show), "stop_encode")
            check(wait(lambda: state(show, 0) == "skipped") and state(show, 1) == "skipped",
                  f"after stop: {state(show, 0)} {state(show, 1)}")
            fake.hold.clear()
            e0 = show.encodes[0]
            check(not os.path.exists(e0.path) and not os.path.exists(e0.path + ".encode.partial"), "partial left after stop")
            check(all(os.path.isfile(f.path) for f in show.files), "stop deleted a remux")
            it = item(show)
            check(it and it.section == "done", f"stopped disc: {it and (it.section, it.reason)}")
            check(it and it.safe_bytes == sum(os.path.getsize(f.path) for f in show.files),
                  "a skipped AV1 version still holds the remux")

            # 5. A failed quality check: not copied, the disc needs you, and retry tries again.
            bad = film("Dunkirk")
            fake.scores[bad.files[0].path] = 89.4
            copy(bad)
            check(wait(lambda: state(bad) == "failed"), f"quality fail: {state(bad)}")
            eb = bad.encodes[0]
            want = "The AV1 version scored 89 on the quality check (needs 93), so it wasn't copied."
            check(eb.note == want and eb.vmaf == 89.4, f"note: {eb.note}")
            check(not os.path.exists(os.path.join(movies, eb.dest_rel)) and not os.path.exists(eb.path),
                  "a failed AV1 file was copied or kept")
            it = item(bad)
            check(it and it.section == "needs" and it.reason == want, f"failed: {it and (it.section, it.reason)}")
            check(ctl.free_local(bad) == 0 and os.path.isfile(bad.files[0].path), "free_local deleted the remux of a failed encode")
            check(pipeline._score(92.6, 93.0) == "92.6" and pipeline._score(89.4, 93.0) == "89", "score text")
            fake.scores[bad.files[0].path] = 96.0
            check(ctl.retry_encode(bad) == 1, "retry_encode queued nothing")
            check(wait(lambda: state(bad) == "verified"), f"retry: {state(bad)} {eb.note}")
            check(item(bad).section == "done", "retried disc isn't done")

            # A failed one given up (dismissed) lets the remux go.
            gone = film("Memento")
            fake.scores[gone.files[0].path] = 50.0
            copy(gone)
            check(wait(lambda: state(gone) == "failed"), "second quality fail")
            ctl.dismiss(gone)
            check(state(gone) == "skipped" and ctl.free_local(gone) > 0, "a dismissed failed encode still holds the remux")

            # 6. "When to encode": idle waits for the rip, night waits for 1 am and pauses at 7.
            ctl.cfg.av1_when = "idle"
            ctl._runs["ripping"] = object()
            idle = film("Heat")
            copy(idle)
            time.sleep(0.15)
            check(state(idle) == "waiting" and "nothing is ripping" in idle.encodes[0].note,
                  f"idle while ripping: {state(idle)} {idle.encodes[0].note}")
            check(not ctl.encode_status().gate_open, "status says the gate is open while ripping")
            del ctl._runs["ripping"]
            ctl._enc_wake.set()
            check(wait(lambda: state(idle) == "verified"), f"idle after the rip: {state(idle)}")

            clock = {"t": dt.datetime(2026, 10, 26, 14, 0)}
            ctl.clock = lambda: clock["t"]
            ctl.cfg.av1_when = "night"
            night = film("Collateral")
            fake.hold.set()
            copy(night)
            time.sleep(0.15)
            check(state(night) == "waiting" and "1 am" in night.encodes[0].note, f"night by day: {night.encodes[0].note}")
            check("1 am" in item(night).reason, f"night reason: {item(night).reason}")
            clock["t"] = dt.datetime(2026, 10, 27, 2, 0)
            ctl._enc_wake.set()
            check(wait(lambda: state(night) == "encoding"), f"night at 2 am: {state(night)}")
            fake.paused_seen = False
            clock["t"] = dt.datetime(2026, 10, 27, 7, 0)
            check(wait(lambda: state(night) == "paused" and fake.paused_seen), f"night at 7 am: {state(night)}")
            check("7 am" in night.encodes[0].note, f"paused at 7 note: {night.encodes[0].note}")
            check(not ctl.resume_encode(night) and state(night) == "paused", "resume overrode the night setting")
            clock["t"] = dt.datetime(2026, 10, 28, 1, 30)
            check(wait(lambda: state(night) == "encoding"), "didn't carry on at 1 am")
            fake.hold.clear()
            check(wait(lambda: state(night) == "verified"), f"night finish: {state(night)}")
            ctl.cfg.av1_when = "after"

            # 7. A restart: an encode left half-made starts again; a finished one is only copied.
            half = film("Alien")
            done_enc = film("Aliens")
            for j in (half, done_enc):
                ctl.cfg.av1_enabled = False
                copy(j)
                ctl.cfg.av1_enabled = True
                f = j.files[0]
                rel = av1.av1_name(f.dest_rel)
                e = EncodeJob(0, state="encoding" if j is half else "copying", src=f.path, dest_rel=rel,
                              path=os.path.join(j.rip_dir, os.path.basename(rel)), fraction=0.4, queued="2026-10-26T09:00:00",
                              width=3840, encoded=j is done_enc, vmaf=None if j is half else 95.0)
                with open(e.path + (".encode.partial" if j is half else ""), "wb") as fh:
                    fh.write(os.urandom(3000))
                j.encodes = [e]
                ctl.store.save(j)
            fake.calls.clear()
            fake.checks.clear()
            ctl2 = controller()
            check(ctl2.free_local(ctl2.store.load(half.id)) == 0, "free_local deleted a remux before recovery")
            back = ctl2.recover_encodes()
            check(sorted(j.id for j, _ in back) == sorted([half.id, done_enc.id]), f"recovered: {[j.id for j, _ in back]}")
            h2 = next(j for j in ctl2.jobs() if j.id == half.id)
            d2 = next(j for j in ctl2.jobs() if j.id == done_enc.id)
            check(wait(lambda: h2.encodes[0].state == "verified" and d2.encodes[0].state == "verified"),
                  f"after restart: {h2.encodes[0].state} {d2.encodes[0].state} {h2.encodes[0].note} {d2.encodes[0].note}")
            check(fake.calls == [h2.files[0].path], f"re-encoded after restart: {fake.calls}")
            check(fake.checks == [h2.files[0].path], f"re-checked after restart: {fake.checks}")
            check(os.path.getsize(os.path.join(movies, d2.encodes[0].dest_rel)) == 3000, "the finished file wasn't the one copied")
            check(os.path.getsize(os.path.join(movies, h2.encodes[0].dest_rel)) == 5000
                  and not os.path.exists(h2.encodes[0].path + ".encode.partial"), "the half-made file was used")
            check(ctl2.recover_encodes() == [], "recovering twice queued something")

            # 8. Old jobs without encodes still load, and an EncodeJob round-trips.
            old = to_dict(Job(id="old"))
            del old["encodes"], old["av1"]
            j = job_from_dict(json.loads(json.dumps(old)))
            check(j.encodes == [] and j.av1 == "", f"old job: {j.encodes} {j.av1!r}")
            j2 = job_from_dict(json.loads(json.dumps(to_dict(dune))))
            check(isinstance(j2.encodes[0], EncodeJob) and j2.encodes[0] == dune.encodes[0], "EncodeJob round trip")
            check(not ctl._enc_jobs and not ctl2._enc_jobs, f"encoder still tracks finished discs: {list(ctl._enc_jobs)}")
    finally:
        av1.encode, av1.check_quality, config.load_secret = saved

    # 9. The fake controller shows an encoding disc and a failed quality check.
    from discshelf.fake import FakeController
    fc = FakeController(Config(), delay=0)
    v = fc.queue(fc.jobs(), set())
    enc = next((i for i in v.items if any(e.state == "encoding" for e in i.encodes)), None)
    bad = next((i for i in v.items if any(e.state == "failed" for e in i.encodes)), None)
    check(enc is not None and enc.section == "working", f"fake encoding disc: {enc and enc.section}")
    check(bad is not None and bad.section == "needs" and "quality check" in bad.reason, f"fake failed check: {bad and bad.reason}")
    if enc is not None:
        check(fc.pause_encode(enc.job) and enc.job.encodes[0].state == "paused", "fake pause")
        check(fc.resume_encode(enc.job) and enc.job.encodes[0].state == "encoding", "fake resume")
    if bad is not None:
        check(fc.retry_encode(bad.job) == 1 and bad.job.encodes[0].state == "waiting", "fake retry")
    check(fc.av1_estimate(av1.EncodeSettings("cpu", 2, True))[0] > 0, "fake estimate")
    fast = FakeController(Config(), delay=0.001)
    fv = fast.queue(fast.jobs(), set())
    fe = next(i for i in fv.items if any(e.state == "encoding" for e in i.encodes)).job.encodes[0]
    check(wait(lambda: (fast.queue(fast.jobs(), set()), fe.state)[1] == "verified", 10), f"fake encode never finished: {fe.state}")

    for f in failures:
        print("FAIL:", f)
    print(f"encode: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_encode():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
