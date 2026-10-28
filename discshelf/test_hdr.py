#!/usr/bin/env python3
"""
Tests for hdr.py with fake ffprobe, ffmpeg, mkvmerge and dovi_tool. What matters:
HDR10, HDR10+ and Dolby Vision are read from the places ffprobe actually reports
them, a RAW policy never touches the file, the original is only replaced once the
new file is complete, and trimming languages never leaves a film without sound.

Run:  python3 discshelf/test_hdr.py    (also works under pytest)
"""
import json
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import hdr  # noqa: E402
from discshelf.config import Config  # noqa: E402
from discshelf.models import HdrInfo, HdrPolicy  # noqa: E402

# Trimmed from real ffprobe 9.0 output for a 10-bit HDR10 HEVC MKV made with libx265.
# Note the container has no color_transfer/color_primaries; only the frames do.
STREAM_HDR10 = {
    "index": 0, "codec_name": "hevc", "profile": "Main 10", "codec_type": "video",
    "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le", "color_range": "tv",
    "color_space": "bt2020nc", "r_frame_rate": "24000/1001", "avg_frame_rate": "24000/1001",
    "time_base": "1/1000", "disposition": {"default": 1},
}
FRAME_HDR10 = {
    "media_type": "video", "stream_index": 0, "key_frame": 1, "pict_type": "I",
    "color_space": "bt2020nc", "color_primaries": "bt2020", "color_transfer": "smpte2084",
    "side_data_list": [
        {"side_data_type": "H.26[45] User Data Unregistered SEI message"},
        {"side_data_type": "Mastering display metadata", "red_x": "34000/50000", "red_y": "16000/50000",
         "min_luminance": "1/10000", "max_luminance": "10000000/10000"},
        {"side_data_type": "Content light level metadata", "max_content": 1000, "max_average": 400},
    ],
}
# Dolby Vision profile 7 as ffprobe reports it for a MakeMKV UHD rip (stream side data).
DOVI_P7 = {"side_data_type": "DOVI configuration record", "dv_version_major": 1, "dv_version_minor": 0,
           "dv_profile": 7, "dv_level": 6, "rpu_present_flag": 1, "el_present_flag": 1,
           "bl_present_flag": 1, "dv_bl_signal_compatibility_id": 6}
HDR10PLUS = {"side_data_type": "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)", "application version": 1}

# mkvmerge -J for a UHD rip: one video, English TrueHD + commentary, French, three subtitle tracks.
MKV_IDENTIFY = {
    "container": {"recognized": True, "supported": True, "type": "Matroska"},
    "chapters": [{"num_entries": 16}],
    "tracks": [
        {"id": 0, "type": "video", "codec": "HEVC/H.265/MPEG-H", "properties": {"language": "und", "number": 1}},
        {"id": 1, "type": "audio", "codec": "TrueHD Atmos", "properties": {"language": "eng", "language_ietf": "en"}},
        {"id": 2, "type": "audio", "codec": "AC-3", "properties": {"language": "fre", "language_ietf": "fr"}},
        {"id": 3, "type": "audio", "codec": "AC-3", "properties": {"language": "eng", "track_name": "Commentary"}},
        {"id": 4, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "eng"}},
        {"id": 5, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "fre"}},
        {"id": 6, "type": "subtitles", "codec": "HDMV PGS", "properties": {"language": "eng", "forced_track": True}},
    ],
    "identification_format_version": 20,
}


def cp(cmd, out="", rc=0, err=""):
    return subprocess.CompletedProcess(cmd, rc, out, err)


class FakeRun:
    def __init__(self, stream=None, frames=None, identify=None, summary="", fail=None):
        self.stream, self.frames = stream or STREAM_HDR10, frames if frames is not None else [FRAME_HDR10]
        self.identify, self.summary, self.fail = identify or MKV_IDENTIFY, summary, fail or set()
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        tool = os.path.basename(cmd[0])
        if tool in self.fail:
            return cp(cmd, rc=2, out="Error: boom")
        if tool == "ffprobe":
            if "-show_frames" in cmd:
                return cp(cmd, json.dumps({"frames": self.frames}))
            return cp(cmd, json.dumps({"streams": [self.stream]}))
        if tool == "mkvmerge" and cmd[1] == "-J":
            return cp(cmd, json.dumps(self.identify))
        if tool in ("mkvmerge", "ffmpeg") or cmd[1:2] == ["extract-rpu"]:
            out = cmd[cmd.index("-o") + 1] if "-o" in cmd else cmd[-1]
            with open(out, "w") as fh:
                fh.write("new")
            return cp(cmd, "Warning: something minor" if tool == "mkvmerge" else "", rc=1 if tool == "mkvmerge" else 0)
        if cmd[1:2] == ["info"]:
            return cp(cmd, self.summary)
        raise AssertionError(f"unexpected command {cmd}")


class FakePipe:
    """Stands in for both ends of ffmpeg | dovi_tool."""

    class _Stream:
        def close(self):
            pass

        def read(self):
            return b""

    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        p = type("P", (), {})()
        p.stdout, p.stderr, p.returncode = FakePipe._Stream(), FakePipe._Stream(), 0
        p.wait = lambda: 0
        p.kill = lambda: None
        if "-o" in cmd:
            def communicate():
                if self.fail:
                    p.returncode = 1
                    return b"", b"dovi_tool: no RPU found"
                with open(cmd[cmd.index("-o") + 1], "w") as fh:
                    fh.write("hevc")
                return b"", b""
            p.communicate = communicate
        return p


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # The badge: RAW, lossless for a 8.1 conversion that loses nothing (MEL), lossy otherwise.
    for pol, layer, want in ((HdrPolicy(), None, "raw"), (HdrPolicy(keep="hdr10"), "MEL", "lossy"),
                             (HdrPolicy(convert_p81=True), None, "lossy"),
                             (HdrPolicy(convert_p81=True), "FEL", "lossy"),
                             (HdrPolicy(convert_p81=True), "MEL", "lossless")):
        check(pol.compression(layer) == want, f"{pol} with {layer}: {pol.compression(layer)}, not {want}")
    check("nothing is lost" in HdrPolicy(convert_p81=True).raw_reasons("MEL")[0], "MEL reason")

    cfg = Config()

    # HDR10 from the frames when the container leaves the colour tags out.
    info = hdr.probe("x.mkv", cfg, run=FakeRun())
    check(info.hdr10 and not info.dolby_vision and info.hdr10plus is False, f"HDR10 file: {info}")
    check(info.resolution == "3840x2160" and info.codec == "hevc", f"resolution/codec: {info}")
    check(info.dv_layer_kind is None, "probe guessed FEL/MEL without dovi_tool")

    st = dict(STREAM_HDR10, side_data_list=[DOVI_P7], color_transfer="smpte2084", color_primaries="bt2020")
    fr = dict(FRAME_HDR10, side_data_list=FRAME_HDR10["side_data_list"] + [HDR10PLUS])
    info = hdr.probe("x.mkv", cfg, run=FakeRun(stream=st, frames=[fr]))
    check(info.dolby_vision and info.dv_profile == 7 and info.dv_enhancement_layer is True, f"DV P7: {info}")
    check(info.hdr10 and info.hdr10plus is True, f"HDR10+ not found: {info}")
    check(info.labels() == ["HDR10", "Dolby Vision · profile 7", "HDR10+"], f"labels: {info.labels()}")

    p8 = dict(DOVI_P7, dv_profile=8, el_present_flag=0, dv_bl_signal_compatibility_id=1)
    info = hdr.probe("x.mkv", cfg, run=FakeRun(stream=dict(STREAM_HDR10, side_data_list=[p8])))
    check(info.dv_profile == 8 and info.dv_enhancement_layer is None, f"DV P8: {info}")

    sdr = dict(FRAME_HDR10, color_primaries="bt709", color_transfer="bt709", side_data_list=[])
    info = hdr.probe("x.mkv", cfg, run=FakeRun(stream=dict(STREAM_HDR10, width=1920, height=1080), frames=[sdr]))
    check(not info.hdr10 and info.labels() == [], f"SDR read as HDR: {info}")

    # FEL/MEL from dovi_tool's summary line, None when it says nothing useful or fails.
    summary = "Parsing RPU file...\n\nSummary:\n  Frames: 200\n  Profile: 7 (FEL)\n  DM version: 1 (CM v2.9)\n"
    check(hdr.dv_layer_kind("x.mkv", cfg, run=FakeRun(summary=summary)) == "FEL", "FEL not read")
    check(hdr.dv_layer_kind("x.mkv", cfg, run=FakeRun(summary=summary.replace("FEL", "MEL"))) == "MEL", "MEL not read")
    check(hdr.dv_layer_kind("x.mkv", cfg, run=FakeRun(summary="Summary:\n  Profile: 8\n")) is None, "P8 gave a layer kind")
    check(hdr.dv_layer_kind("x.mkv", cfg, run=FakeRun(summary=summary, fail={"dovi_tool"})) is None, "failure not None")
    fr_run = FakeRun(summary=summary)
    hdr.dv_layer_kind("x.mkv", cfg, run=fr_run)
    check(["-frames:v", str(hdr.LAYER_SAMPLE_FRAMES)] == fr_run.calls[0][fr_run.calls[0].index("-frames:v"):][:2],
          f"sample not limited: {fr_run.calls[0]}")

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "Film.mkv")
        with open(src, "w") as fh:
            fh.write("original")

        # Track trimming picks ids from mkvmerge -J.
        r = FakeRun()
        dst = os.path.join(tmp, "out.mkv")
        hdr.remux_tracks(src, dst, ["eng"], ["eng"], cfg, run=r)
        mux = r.calls[-1]
        check(mux[mux.index("-a") + 1] == "1,3" and mux[mux.index("-s") + 1] == "4,6", f"wrong tracks: {mux}")
        check("--no-chapters" not in mux and mux[-1] == src, f"chapters dropped or wrong input: {mux}")
        check(open(dst).read() == "new" and not os.path.exists(hdr._temp_beside(dst)), "dst not finished")

        r = FakeRun()
        hdr.remux_tracks(src, dst, ["jpn"], ["jpn"], cfg, run=r)
        mux = r.calls[-1]
        check("-a" not in mux and "-S" in mux, f"no-match languages should keep all audio, no subs: {mux}")

        # RAW policy and non-DV files are left alone.
        r, p = FakeRun(), FakePipe()
        dv = HdrInfo(hdr10=True, dolby_vision=True, dv_profile=7, dv_enhancement_layer=True)
        check(hdr.apply_policy(src, HdrPolicy(), dv, tmp, cfg, run=r, popen=p) == src and not r.calls and not p.calls,
              "RAW policy ran a tool")
        check(hdr.apply_policy(src, HdrPolicy(keep="hdr10"), HdrInfo(hdr10=True), tmp, cfg, run=r, popen=p) == src
              and not r.calls, "non-DV file was processed")
        p8info = HdrInfo(hdr10=True, dolby_vision=True, dv_profile=8)
        check(hdr.apply_policy(src, HdrPolicy(convert_p81=True), p8info, tmp, cfg, run=r, popen=p) == src
              and not p.calls, "profile 8 was converted")

        # Keep HDR10 only: dovi_tool remove, then mux with the original's other tracks.
        work = os.path.join(tmp, "work")
        r, p = FakeRun(), FakePipe()
        out = hdr.apply_policy(src, HdrPolicy(keep="hdr10"), dv, work, cfg, run=r, popen=p)
        ff, dt = p.calls
        check(ff[:1] == ["ffmpeg"] and "hevc_mp4toannexb" in ff and ff[-1] == "-", f"ffmpeg: {ff}")
        check(dt[:2] == ["dovi_tool", "remove"] and dt[2:4] == ["-i", "-"], f"dovi_tool remove: {dt}")
        mux = r.calls[-1]
        check(mux[:2] == ["mkvmerge", "-o"] and mux[3:5] == ["--default-duration", "0:24000/1001p"], f"mux: {mux}")
        check(mux[-2:] == ["-D", src], f"original tracks not taken from src: {mux}")
        check(out == src and open(src).read() == "new", "src not replaced by the finished file")
        check(os.listdir(work) == [] and not os.path.exists(hdr._temp_beside(src)), f"intermediates left: {os.listdir(work)}")

        # Convert profile 7 to 8.1.
        r, p = FakeRun(stream=dict(STREAM_HDR10, avg_frame_rate="0/0", r_frame_rate="24/1")), FakePipe()
        hdr.apply_policy(src, HdrPolicy(convert_p81=True), dv, work, cfg, run=r, popen=p)
        check(p.calls[1][:5] == ["dovi_tool", "-m", "2", "convert", "--discard"], f"convert: {p.calls[1]}")
        check("0:24/1p" in r.calls[-1], f"frame rate fallback: {r.calls[-1]}")

        # A failure leaves the original in place and cleans up.
        with open(src, "w") as fh:
            fh.write("original")
        try:
            hdr.apply_policy(src, HdrPolicy(keep="hdr10"), dv, work, cfg, run=FakeRun(), popen=FakePipe(fail=True))
            check(False, "a dovi_tool failure did not raise")
        except hdr.ToolError as e:
            check("no RPU found" in str(e), f"error text lost: {e}")
        check(open(src).read() == "original" and os.listdir(work) == [], "failure damaged src or left files")
        try:
            hdr.apply_policy(src, HdrPolicy(keep="hdr10"), dv, work, cfg, run=FakeRun(fail={"mkvmerge"}), popen=FakePipe())
            check(False, "a mkvmerge failure did not raise")
        except hdr.ToolError:
            pass
        check(open(src).read() == "original" and os.listdir(work) == [] and not os.path.exists(hdr._temp_beside(src)),
              "mkvmerge failure damaged src or left files")

    for f in failures:
        print("FAIL:", f)
    print("ok" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


def test_hdr():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
