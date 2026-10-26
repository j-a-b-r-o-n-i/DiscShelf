#!/usr/bin/env python3
"""
Tests for av1.py with fake ffprobe, ffmpeg and mkvmerge; nothing is encoded. What
matters: the AV1 file never carries Dolby Vision, audio and subtitles are copied
unchanged, the video is 10-bit, progress is read from ffmpeg's -progress output,
pause and cancel reach ffmpeg and cancel leaves nothing behind, the HDR10 details
are written into the container, and the quality check and estimates add up.

Run:  python3 discshelf/test_av1.py    (also works under pytest)
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import av1  # noqa: E402
from discshelf.config import Config  # noqa: E402
from discshelf.models import HdrInfo  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
# A real -progress stream: ffmpeg 9.0.2 encoding 4 s of a 4K clip with SVT-AV1 and
# copying its audio. Its first report says out_time 4.027 s at frame 0.
PROGRESS = open(os.path.join(FIXTURES, "av1_progress.txt")).read()
# A real libvmaf JSON log, trimmed to three frames.
VMAF_JSON = open(os.path.join(FIXTURES, "av1_vmaf.json")).read()

# ffprobe for a UHD HDR10 remux: as for MakeMKV output, the container carries the colour tags.
STREAM = {"index": 0, "codec_name": "hevc", "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
          "color_transfer": "smpte2084", "color_primaries": "bt2020", "avg_frame_rate": "24000/1001",
          "r_frame_rate": "24000/1001"}
FRAME = {"color_transfer": "smpte2084", "color_primaries": "bt2020", "side_data_list": [
    {"side_data_type": "Mastering display metadata", "red_x": "34000/50000", "red_y": "16000/50000",
     "green_x": "13250/50000", "green_y": "34500/50000", "blue_x": "7500/50000", "blue_y": "3000/50000",
     "white_point_x": "15635/50000", "white_point_y": "16450/50000",
     "min_luminance": "1/10000", "max_luminance": "10000000/10000"},
    {"side_data_type": "Content light level metadata", "max_content": 1000, "max_average": 400},
]}
SDR_STREAM = {"index": 0, "codec_name": "h264", "width": 1920, "height": 1080, "avg_frame_rate": "24000/1001"}

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)


def cp(cmd, out="", rc=0, err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


def tool_args(cmd):
    """The command from the tool onwards, without the `nice -n 10` in front."""
    for i, part in enumerate(cmd):
        if os.path.basename(part) in ("ffmpeg", "ffprobe", "mkvmerge"):
            return cmd[i:]
    raise AssertionError(f"no tool in {cmd}")


def value(cmd, flag):
    return cmd[cmd.index(flag) + 1]


class FakeRun:
    def __init__(self, stream=STREAM, frames=(FRAME,), duration=4.004, mkvmerge_rc=0):
        self.stream, self.frames, self.duration, self.mkvmerge_rc = stream, list(frames), duration, mkvmerge_rc
        self.calls = []

    def __call__(self, cmd, **kw):
        cmd = tool_args(cmd)
        self.calls.append(cmd)
        tool = os.path.basename(cmd[0])
        if tool == "ffprobe":
            if "-show_frames" in cmd:
                return cp(cmd, json.dumps({"frames": self.frames}))
            return cp(cmd, json.dumps({"streams": [self.stream], "format": {"duration": str(self.duration)}}))
        if tool == "mkvmerge":
            if self.mkvmerge_rc in (0, 1):
                with open(value(cmd, "-o"), "w") as fh:
                    fh.write("av1 with hdr10")
            return cp(cmd, "Error: no space left" if self.mkvmerge_rc > 1 else "", rc=self.mkvmerge_rc)
        if tool == "ffmpeg":           # a VMAF clip: write the log libvmaf would write
            graph = value(cmd, "-lavfi")
            log = graph.split("log_path=")[1].split(":")[0]
            with open(log, "w") as fh:
                fh.write(VMAF_JSON)
            return cp(cmd)
        raise AssertionError(f"unexpected command {cmd}")


class FakeFfmpeg:
    """Stands in for subprocess.Popen running ffmpeg: replays a -progress stream.

    `on_block` is called with the block number before each block is sent, and can
    act like the user (set pause or cancel) and wait for what ffmpeg would see.
    """

    def __init__(self, text=PROGRESS, rc=0, stderr="", on_block=None, clock=None):
        self.text, self.rc, self.err, self.on_block, self.clock = text, rc, stderr, on_block, clock
        self.calls, self.signals = [], []
        self.killed = threading.Event()

    def __call__(self, cmd, **kw):
        cmd = tool_args(cmd)
        self.calls.append(cmd)
        with open(cmd[-1], "w") as fh:       # ffmpeg creates its output at once
            fh.write("partial")
        fake = self

        class Proc:
            returncode = None

            def __init__(self):
                self.stdout, self.stderr = fake._stdout(), iter(fake.err.splitlines(keepends=True))

            def send_signal(self, sig):
                fake.signals.append(sig)

            def kill(self):
                fake.signals.append(signal.SIGKILL)
                self.returncode = -9
                fake.killed.set()

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if self.returncode is None:
                    self.returncode = fake.rc
                return self.returncode
        self.proc = Proc()
        return self.proc

    def _stdout(self):
        n = 0
        for line in self.text.splitlines(keepends=True):
            if n == 0 or line.startswith("frame="):
                if self.on_block:
                    self.on_block(n)
                if self.killed.is_set():
                    return
                if self.clock:
                    self.clock[0] += 1.0
                n += 1
            yield line


def wait_for(cond, timeout=5.0):
    ev = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if cond():
            return True
        ev.wait(0.01)
    return False


def part_cmd(cfg):
    hdr10 = HdrInfo(hdr10=True)
    crfs, cqs = [], []
    for q in range(len(av1.QUALITY)):
        for grain in (False, True):
            c = av1.ffmpeg_cmd("/rips/in.mkv", "/out/x.mkv", av1.EncodeSettings("cpu", q, grain), cfg, hdr10)
            g = av1.ffmpeg_cmd("/rips/in.mkv", "/out/x.mkv", av1.EncodeSettings("gpu", q, grain), cfg, hdr10)
            for cmd, name in ((c, "cpu"), (g, "gpu")):
                check(cmd[0] == "ffmpeg" and cmd[-1] == "/out/x.mkv" and value(cmd, "-f") == "matroska",
                      f"{name} q{q}: output: {cmd}")
                check(value(cmd, "-c:a") == "copy" and value(cmd, "-c:s") == "copy", f"{name} q{q}: audio/subs not copied")
                maps = [cmd[i + 1] for i, x in enumerate(cmd) if x == "-map"]
                check(maps == ["0:v:0", "0:a?", "0:s?"], f"{name} q{q}: maps {maps}")
                check(value(cmd, "-progress") == "pipe:1", f"{name} q{q}: no -progress pipe:1")
                check(value(cmd, "-color_trc") == "smpte2084" and value(cmd, "-color_primaries") == "bt2020",
                      f"{name} q{q}: HDR10 tags missing")
            check(value(c, "-c:v") == "libsvtav1" and value(c, "-pix_fmt") == "yuv420p10le", f"cpu q{q}: not 10-bit SVT")
            check(value(c, "-dolbyvision") == "0", f"cpu q{q}: Dolby Vision not off")
            params = value(c, "-svtav1-params")
            check(("film-grain=8" in params and "film-grain-denoise=1" in params) == grain,
                  f"cpu q{q} grain={grain}: {params}")
            check(value(g, "-c:v") == "av1_nvenc" and value(g, "-highbitdepth") == "1", f"gpu q{q}: not 10-bit NVENC")
            check("DOVI_METADATA" in value(g, "-vf") and "DOVI_RPU_BUFFER" in value(g, "-vf"),
                  f"gpu q{q}: Dolby Vision side data not dropped")
            check(value(g, "-rc") == "vbr" and value(g, "-b:v") == "0" and value(g, "-tune") == "hq",
                  f"gpu q{q}: rate control {g}")
            check(value(g, "-preset") in ("p6", "p7") and value(g, "-spatial-aq") == "1", f"gpu q{q}: preset/AQ")
            check(not any("film-grain" in x for x in g), f"gpu q{q}: grain passed to NVENC")
            check(value(g, "-hwaccel") == "cuda", f"gpu q{q}: not decoding on the GPU")
            check(4 <= int(value(c, "-preset")) <= 6, f"cpu q{q}: preset {value(c, '-preset')}")
        crfs.append(int(value(c, "-crf")))
        cqs.append(int(value(g, "-cq")))
    check(crfs == sorted(crfs, reverse=True) and len(set(crfs)) == 5, f"CRF does not step down: {crfs}")
    check(cqs == sorted(cqs, reverse=True) and len(set(cqs)) == 5, f"CQ does not step down: {cqs}")
    sdr = av1.ffmpeg_cmd("/rips/in.mkv", "/out/x.mkv", av1.EncodeSettings("cpu", 2, True), cfg, HdrInfo())
    check("-color_trc" not in sdr and "-color_trc" not in av1.ffmpeg_cmd("a", "b", av1.EncodeSettings(), cfg),
          "SDR source tagged as HDR10")


def part_progress():
    blocks = av1.parse_progress(PROGRESS)
    check(len(blocks) == 6 and blocks[-1]["progress"] == "end", f"blocks: {len(blocks)}")
    check(av1._out_seconds(blocks[-1]) == 4.027 and av1._out_seconds({"out_time": "00:01:02.500000"}) == 62.5,
          "out_time not read")
    now = [0.0]
    m = av1._Meter(4.004, lambda: now[0], 24000 / 1001)
    reports = []
    for b in blocks:
        now[0] += 1.0
        reports.append(m.update(b))
    first, last = reports[0], reports[-1]
    # out_time is 4.027 s in the first report (the copied audio ran ahead); frames say 0.
    check(first.fraction == 0.0 and first.eta_seconds is None, f"first report: {first}")
    check(last.fraction == 1.0 and last.size_bytes == 8199431 and last.projected_bytes == 8199431
          and last.eta_seconds == 0.0, f"last report: {last}")
    check(abs(last.fps - 96 / 6) < 0.01, f"fps: {last.fps}")
    mid = reports[3]                     # frame 45 of 96 after 4 s
    check(abs(mid.fraction - 45 / 96) < 0.01 and mid.projected_bytes == int(1030 / mid.fraction), f"middle: {mid}")
    check(abs(mid.eta_seconds - (4.004 - mid.out_seconds) / (mid.out_seconds / 4)) < 0.01, f"eta: {mid.eta_seconds}")
    # Without a frame rate, out_time is used.
    m2 = av1._Meter(8.054, lambda: 1.0)
    check(m2.update(blocks[0]).fraction == 0.5, "out_time fallback")


def part_encode(cfg, tmp):
    src, dst = os.path.join(tmp, "Film - 2160p.mkv"), os.path.join(tmp, "out", "Film - 2160p AV1.mkv")
    with open(src, "w") as fh:
        fh.write("remux")

    # A whole run: progress reported, HDR10 written by mkvmerge, only dst left.
    run, now = FakeRun(), [0.0]
    pop = FakeFfmpeg(clock=now)
    seen = []
    r = av1.encode(src, dst, av1.EncodeSettings("cpu", 2, True), cfg, progress=seen.append,
                   popen=pop, run=run, clock=lambda: now[0], poll_s=0.01)
    check(len(seen) == 6 and seen[-1].fraction == 1.0, f"progress reports: {[p.fraction for p in seen]}")
    check(r.path == dst and os.path.exists(dst) and open(dst).read() == "av1 with hdr10", "dst not the mkvmerge output")
    check(sorted(os.listdir(os.path.dirname(dst))) == ["Film - 2160p AV1.mkv"], f"left over: {os.listdir(os.path.dirname(dst))}")
    check(r.fps > 0 and r.seconds > 0 and r.size_bytes == len("av1 with hdr10"), f"result: {r}")
    ff = pop.calls[0]
    check(ff[-1] == dst + ".encode.partial" and "-color_trc" in ff, f"ffmpeg cmd: {ff}")
    mkv = run.calls[-1]
    check(mkv[:3] == ["mkvmerge", "-o", dst + ".partial"] and mkv[-1] == dst + ".encode.partial", f"mkvmerge: {mkv}")
    want = {"--colour-transfer-characteristics": "0:16", "--colour-primaries": "0:9",
            "--colour-matrix-coefficients": "0:9", "--colour-range": "0:1", "--colour-bits-per-channel": "0:10",
            "--max-content-light": "0:1000", "--max-frame-light": "0:400",
            "--chromaticity-coordinates": "0:0.68,0.32,0.265,0.69,0.15,0.06",
            "--white-colour-coordinates": "0:0.3127,0.329", "--max-luminance": "0:1000",
            "--min-luminance": "0:0.0001"}
    for flag, v in want.items():
        check(flag in mkv and value(mkv, flag) == v, f"mkvmerge {flag}: {mkv}")

    # An SDR source: no mkvmerge step, the encode is renamed into place.
    os.remove(dst)
    run = FakeRun(stream=SDR_STREAM, frames=[{}])
    av1.encode(src, dst, av1.EncodeSettings("gpu", 1, False), cfg, popen=FakeFfmpeg(), run=run, poll_s=0.01)
    check(open(dst).read() == "partial" and not any(c[0] == "mkvmerge" for c in run.calls), "SDR: mkvmerge ran")
    os.remove(dst)

    # Pause: SIGSTOP then SIGCONT, and time spent paused is not counted.
    pause, now = threading.Event(), [0.0]

    def pausing(n):
        if n == 3:
            pause.set()
            check(wait_for(lambda: signal.SIGSTOP in pop.signals), "pause did not stop ffmpeg")
            now[0] += 100.0              # a long pause
            pause.clear()
            check(wait_for(lambda: signal.SIGCONT in pop.signals), "clearing pause did not resume ffmpeg")
    pop = FakeFfmpeg(on_block=pausing, clock=now)
    r = av1.encode(src, dst, av1.EncodeSettings("cpu", 2, False), cfg, pause=pause, popen=pop, run=FakeRun(),
                   clock=lambda: now[0], poll_s=0.01)
    check(pop.signals == [signal.SIGSTOP, signal.SIGCONT], f"signals: {pop.signals}")
    check(r.seconds < 10 and os.path.exists(dst), f"paused time counted: {r.seconds}")
    os.remove(dst)

    # Cancel: ffmpeg killed, partial files removed, Cancelled raised.
    cancel = threading.Event()

    def cancelling(n):
        if n == 2:
            cancel.set()
            pop.killed.wait(5)
    pop = FakeFfmpeg(on_block=cancelling)
    try:
        av1.encode(src, dst, av1.EncodeSettings("gpu", 2, False), cfg, cancel=cancel, popen=pop, run=FakeRun(),
                   poll_s=0.01)
        check(False, "cancel did not raise Cancelled")
    except av1.Cancelled:
        pass
    check(signal.SIGKILL in pop.signals, "cancel did not kill ffmpeg")
    check(os.listdir(os.path.dirname(dst)) == [], f"cancel left files: {os.listdir(os.path.dirname(dst))}")

    # ffmpeg fails: EncodeError with its last line, nothing left behind.
    pop = FakeFfmpeg(text="frame=1\nprogress=continue\n", rc=1,
                     stderr="[av1_nvenc @ 0x5] OpenEncodeSessionEx failed\nError while opening encoder: No capable devices found\n")
    try:
        av1.encode(src, dst, av1.EncodeSettings("gpu", 2, False), cfg, popen=pop, run=FakeRun(), poll_s=0.01)
        check(False, "ffmpeg failure did not raise")
    except av1.EncodeError as e:
        check(str(e).endswith("No capable devices found"), f"error text: {e}")
    check(os.listdir(os.path.dirname(dst)) == [], "ffmpeg failure left files")

    # mkvmerge fails: EncodeError, nothing left behind.
    try:
        av1.encode(src, dst, av1.EncodeSettings("cpu", 2, False), cfg, popen=FakeFfmpeg(), run=FakeRun(mkvmerge_rc=2),
                   poll_s=0.01)
        check(False, "mkvmerge failure did not raise")
    except av1.EncodeError as e:
        check("HDR10" in str(e) and "no space left" in str(e), f"mkvmerge error text: {e}")
    check(os.listdir(os.path.dirname(dst)) == [], "mkvmerge failure left files")


def part_quality(cfg):
    run = FakeRun(duration=1000.0)
    seen = []
    q = av1.check_quality("/rips/remux.mkv", "/nas/av1.mkv", cfg, progress=lambda f, m: seen.append((f, m)), run=run)
    clips = [c for c in run.calls if c[0] == "ffmpeg"]
    check(len(clips) == 6, f"clips: {len(clips)}")
    starts = [float(value(c, "-ss")) for c in clips]
    check(starts[0] == 50.0 and starts[-1] == 940.0 and len({round(b - a, 3) for a, b in zip(starts, starts[1:])}) == 1,
          f"clip starts: {starts}")
    for c in clips:
        i = c.index("-i")
        check(c[i + 1] == "/nas/av1.mkv" and c[c.index("-i", i + 1) + 1] == "/rips/remux.mkv", f"inputs: {c}")
        check(c.count("-ss") == 2 and c.count("10.000") == 2, f"both inputs not seeked the same: {c}")
        g = value(c, "-lavfi")
        check(g.count("scale=1920:1080") == 2 and g.count("yuv420p10le") == 2 and "ts_sync_mode=nearest" in g,
              f"vmaf graph: {g}")
    check(q.score == 98.3 and q.clip_scores == [98.3] * 6 and q.passed and q.threshold == 93.0, f"result: {q}")
    check(seen[0][0] == 0.0 and seen[-1][0] == 1.0 and "1 of 6" in seen[0][1], f"progress: {seen}")
    check(not av1.check_quality("a", "b", cfg, threshold=99.0, run=FakeRun(duration=1000.0)).passed, "threshold ignored")
    check(av1.parse_vmaf(json.dumps({"frames": [{"metrics": {"vmaf": 90}}, {"metrics": {"vmaf": 96}}]})) == 93.0,
          "per-frame fallback")

    def failing(cmd, **kw):
        cmd = tool_args(cmd)
        if cmd[0] == "ffmpeg":
            return cp(cmd, rc=1, err="Error initializing filter 'libvmaf'\n")
        return FakeRun(duration=1000.0)(cmd)
    try:
        av1.check_quality("a", "b", cfg, run=failing)
        check(False, "a VMAF failure did not raise")
    except av1.EncodeError as e:
        check("libvmaf" in str(e), f"VMAF error text: {e}")


def part_names():
    check(av1.av1_name("Movies/Dune Part Two (2024)/Dune Part Two (2024).mkv")
          == "Movies/Dune Part Two (2024)/Dune Part Two (2024) - 2160p AV1.mkv", "plain name")
    check(av1.av1_name("Movies/Dune Part Two (2024)/Dune Part Two (2024) - 2160p.mkv")
          == "Movies/Dune Part Two (2024)/Dune Part Two (2024) - 2160p AV1.mkv", "2160p suffix not stripped")
    check(av1.av1_name("Shows/Severance/Season 01/Severance - S01E01 - Good News About Hell - 1080p.mkv")
          == "Shows/Severance/Season 01/Severance - S01E01 - Good News About Hell - 2160p AV1.mkv", "episode name")
    check(av1.av1_name("Film.mkv") == "Film - 2160p AV1.mkv", "no folder")
    check(av1.is_4k(3840) and av1.is_4k(3000) and not av1.is_4k(1920) and not av1.is_4k(0), "is_4k")
    check(av1.QUALITY == ["Small", "Balanced", "High", "Very high", "Near the disc"], "QUALITY")


def part_stats(tmp):
    path = os.path.join(tmp, "state", "av1_stats.json")
    st = av1.Stats(path)
    film = av1.DEFAULT_MINUTES * 60
    hi = av1.EncodeSettings("cpu", 2, False)
    size, secs, measured = st.estimate(hi, 3840, film, 62_000_000_000)
    check(not measured and size == 15_000_000_000, f"default size: {size}")
    check(abs(secs - film * av1.FILM_FPS / 15) < 1, f"default time: {secs}")
    _, grain_secs, _ = st.estimate(av1.EncodeSettings("cpu", 2, True), 3840, film, 0)
    check(abs(grain_secs / secs - av1.GRAIN_SLOWDOWN) < 1e-6, "grain not slower")
    _, gpu_grain, _ = st.estimate(av1.EncodeSettings("gpu", 2, True), 3840, film, 0)
    _, gpu_plain, _ = st.estimate(av1.EncodeSettings("gpu", 2, False), 3840, film, 0)
    check(gpu_grain == gpu_plain and gpu_plain < secs, "gpu estimate")
    half, _, _ = st.estimate(hi, 3840, film / 2, 0)
    hd, _, _ = st.estimate(hi, 1920, film, 0)
    check(half == size // 2 and hd == size // 4, f"scaling: {half} {hd}")
    capped, _, _ = st.estimate(hi, 3840, film, 5_000_000_000)
    check(capped == 5_000_000_000, "not capped at the source size")

    # One measured run of 1 h: 10 GB in 4 h. A 2 h film then estimates 20 GB in 8 h.
    st.record(hi, 3840, 3600, 4 * 3600, 10_000_000_000, 40_000_000_000)
    st.record(av1.EncodeSettings("cpu", 4, False), 3840, 3600, 9 * 3600, 30_000_000_000, 40_000_000_000)
    st.record(hi, 3840, 0, 10, 10, 10)          # nonsense, ignored
    st2 = av1.Stats(path)
    check(len(st2.runs) == 2, f"runs saved: {len(st2.runs)}")
    size, secs, measured = st2.estimate(hi, 3840, 7200, 0)
    check(measured and abs(size - 20e9) < 1e3 and abs(secs - 8 * 3600) < 1e-6, f"measured: {size} {secs}")
    _, gsecs, _ = st2.estimate(av1.EncodeSettings("cpu", 2, True), 3840, 7200, 0)
    check(abs(gsecs - 8 * 3600 * av1.GRAIN_SLOWDOWN) < 1e-6, "grain adjustment on a plain run")
    check(not st2.estimate(av1.EncodeSettings("gpu", 2, False), 3840, 7200, 0)[2], "gpu used cpu runs")
    check(not st2.estimate(av1.EncodeSettings("cpu", 1, False), 3840, 7200, 0)[2], "other quality used")

    old = os.environ.get("XDG_STATE_HOME")
    os.environ["XDG_STATE_HOME"] = os.path.join(tmp, "xdg")
    try:
        check(av1.Stats().path == os.path.join(tmp, "xdg", "disc-shelf", "av1_stats.json"), "XDG_STATE_HOME ignored")
    finally:
        if old is None:
            del os.environ["XDG_STATE_HOME"]
        else:
            os.environ["XDG_STATE_HOME"] = old
    with open(path, "w") as fh:
        fh.write("not json")
    check(av1.Stats(path).runs == [], "a damaged stats file is not ignored")


def run():
    failures.clear()
    cfg = Config()
    with tempfile.TemporaryDirectory() as tmp:
        part_cmd(cfg)
        part_progress()
        part_encode(cfg, tmp)
        part_quality(cfg)
        part_names()
        part_stats(tmp)
    for f in failures:
        print("FAIL:", f)
    print("av1: ok" if not failures else f"av1: {len(failures)} failure(s)")
    return 1 if failures else 0


def test_av1():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
