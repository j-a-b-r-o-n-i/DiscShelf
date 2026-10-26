"""HDR detection and the HDR policy: probe a rip, optionally drop or rewrite Dolby Vision, trim tracks.

Every external tool goes through an injectable `run` (subprocess.run) or `popen`
(subprocess.Popen) so the tests never start ffprobe, ffmpeg, mkvmerge or dovi_tool.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile

from .config import Config
from .models import HdrInfo, HdrPolicy

HDR10PLUS_SIDE_DATA = "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)"
DOVI_SIDE_DATA = "DOVI configuration record"

# Frames read when guessing FEL vs MEL. The enhancement-layer type is the same for
# the whole film in practice, and 200 frames is a few MB, so this stays quick.
LAYER_SAMPLE_FRAMES = 200


class ToolError(RuntimeError):
    """An external tool failed; the message carries the command and what it printed."""


def _check(cmd: list[str], proc, ok=(0,)) -> None:
    if proc.returncode not in ok:
        # mkvmerge prints its errors on stdout, ffmpeg and dovi_tool on stderr.
        msg = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
        raise ToolError(f"{cmd[0]} failed (exit {proc.returncode}): {msg[-2000:]}")


def _json(cmd: list[str], run) -> dict:
    proc = run(cmd, capture_output=True, text=True)
    # mkvmerge -J exits 1 for warnings but still prints valid JSON.
    _check(cmd, proc, ok=(0, 1))
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as e:
        raise ToolError(f"{cmd[0]} printed something that is not JSON: {e}") from e


def video_stream(path: str, cfg: Config, run=subprocess.run) -> dict:
    """ffprobe's description of the first video stream, {} when there is none."""
    data = _json([cfg.ffprobe, "-v", "error", "-print_format", "json", "-show_streams",
                  "-select_streams", "v:0", path], run)
    streams = data.get("streams") or []
    return streams[0] if streams else {}


def _first_frames(path: str, cfg: Config, run) -> list[dict]:
    data = _json([cfg.ffprobe, "-v", "error", "-print_format", "json", "-show_frames",
                  "-read_intervals", "%+#3", "-select_streams", "v:0", path], run)
    return data.get("frames") or []


def probe(path: str, cfg: Config, run=subprocess.run) -> HdrInfo:
    st = video_stream(path, cfg, run)
    info = HdrInfo(codec=st.get("codec_name", ""))
    if st.get("width") and st.get("height"):
        info.resolution = f"{st['width']}x{st['height']}"
    for sd in st.get("side_data_list") or []:
        if sd.get("side_data_type") == DOVI_SIDE_DATA:
            info.dolby_vision = True
            info.dv_profile = sd.get("dv_profile")
            if info.dv_profile == 7:
                info.dv_enhancement_layer = bool(sd.get("el_present_flag"))
    frames = _first_frames(path, cfg, run)
    # The container does not always carry the colour tags (ffmpeg's own MKV muxer
    # leaves them out), but every decoded frame has them, so fall back to the frames.
    transfer = st.get("color_transfer") or next((f.get("color_transfer") for f in frames if f.get("color_transfer")), "")
    primaries = st.get("color_primaries") or next((f.get("color_primaries") for f in frames if f.get("color_primaries")), "")
    info.hdr10 = transfer == "smpte2084" and primaries == "bt2020"
    info.hdr10plus = any(sd.get("side_data_type") == HDR10PLUS_SIDE_DATA
                         for f in frames for sd in f.get("side_data_list") or [])
    return info


def _annexb_cmd(src: str, cfg: Config, frames: int | None = None) -> list[str]:
    """ffmpeg copying the first video stream as raw Annex-B HEVC, the input dovi_tool reads."""
    cmd = [cfg.ffmpeg, "-v", "error", "-nostdin", "-i", src, "-map", "0:v:0", "-c:v", "copy",
           "-bsf:v", "hevc_mp4toannexb"]
    if frames:
        cmd += ["-frames:v", str(frames)]
    return cmd + ["-f", "hevc"]


def dv_layer_kind(path: str, cfg: Config, run=subprocess.run) -> str | None:
    """"FEL" or "MEL" for a profile 7 file, None when it cannot be told.

    `dovi_tool info --summary` prints a line such as "Profile: 7 (FEL)"; the word in
    brackets is the enhancement-layer type dovi_tool works out from the RPUs.
    """
    with tempfile.TemporaryDirectory(prefix="discshelf-dv-") as tmp:
        sample, rpu = os.path.join(tmp, "sample.hevc"), os.path.join(tmp, "RPU.bin")
        steps = [
            _annexb_cmd(path, cfg, LAYER_SAMPLE_FRAMES) + ["-y", sample],
            [cfg.dovi_tool, "extract-rpu", "-i", sample, "-o", rpu],
            [cfg.dovi_tool, "info", "-i", rpu, "--summary"],
        ]
        out = ""
        for cmd in steps:
            try:
                proc = run(cmd, capture_output=True, text=True)
            except OSError:
                return None          # the tool is not installed
            if proc.returncode != 0:
                return None
            out = proc.stdout or ""
    m = re.search(r"Profiles?:[^\n]*?\b7 \(([^)]*)\)", out)
    if not m:
        return None
    kinds = set(re.findall(r"\b(FEL|MEL)\b", m.group(1)))
    # A mix would mean some frames carry a full enhancement layer; converting would lose those.
    return "FEL" if "FEL" in kinds else ("MEL" if kinds else None)


def _identify(path: str, cfg: Config, run) -> dict:
    return _json([cfg.mkvmerge, "-J", path], run)


def _pick(tracks: list[dict], kind: str, langs: list[str]) -> tuple[list[int], int]:
    """(ids of tracks of this kind whose language is wanted, how many tracks of that kind exist)."""
    wanted = {l.lower() for l in langs}
    of_kind = [t for t in tracks if t.get("type") == kind]
    keep = [t["id"] for t in of_kind
            if (t.get("properties") or {}).get("language", "und").lower() in wanted]
    return keep, len(of_kind)


def _mkvmerge(cmd: list[str], out: str, run) -> None:
    proc = run(cmd, capture_output=True, text=True)
    try:
        _check(cmd, proc, ok=(0, 1))     # 1 is warnings only; the file is written
    except ToolError:
        _remove(out)
        raise


def _remove(*paths: str) -> None:
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _temp_beside(path: str) -> str:
    # Same folder as the final file so the last step is an atomic rename.
    d, name = os.path.split(os.path.abspath(path))
    return os.path.join(d, f".{name}.discshelf-tmp.mkv")


def remux_tracks(src: str, dst: str, audio_langs: list[str], subtitle_langs: list[str],
                 cfg: Config, run=subprocess.run) -> str:
    """Write dst with all video, audio and subtitles in the given ISO 639-2 languages, and chapters.

    If no audio track is in a listed language, every audio track is kept: a file with
    no sound is worse than one with extra languages. dst may equal src.
    """
    tracks = _identify(src, cfg, run).get("tracks") or []
    audio, n_audio = _pick(tracks, "audio", audio_langs)
    subs, _ = _pick(tracks, "subtitles", subtitle_langs)
    tmp = _temp_beside(dst)
    cmd = [cfg.mkvmerge, "-o", tmp]
    if audio and len(audio) < n_audio:   # no -a at all keeps every audio track
        cmd += ["-a", ",".join(map(str, audio))]
    cmd += ["-s", ",".join(map(str, subs))] if subs else ["-S"]
    cmd.append(src)
    _mkvmerge(cmd, tmp, run)
    os.replace(tmp, dst)
    return dst


def _pipe(producer: list[str], consumer: list[str], popen) -> None:
    """producer | consumer, raising with whichever side failed."""
    p1 = popen(producer, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        p2 = popen(consumer, stdin=p1.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except BaseException:
        p1.kill()
        raise
    p1.stdout.close()                    # so the producer sees a broken pipe if the consumer dies
    out2, err2 = p2.communicate()
    err1 = p1.stderr.read() if p1.stderr else b""
    p1.wait()
    for cmd, proc, err in ((consumer, p2, err2), (producer, p1, err1)):
        if proc.returncode != 0:
            text = err.decode(errors="replace") if isinstance(err, bytes) else (err or "")
            raise ToolError(f"{cmd[0]} failed (exit {proc.returncode}): {text.strip()[-2000:]}")


def _fps(stream: dict) -> str:
    """The frame rate as mkvmerge wants it for --default-duration, e.g. "24000/1001p"."""
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = stream.get(key) or ""
        num, _, den = rate.partition("/")
        if num.isdigit() and int(num) > 0 and (not den or (den.isdigit() and int(den) > 0)):
            return f"{rate}p"
    return "24000/1001p"                 # every UHD film disc; better than mkvmerge guessing 25


def apply_policy(src: str, policy: HdrPolicy, info: HdrInfo, workdir: str, cfg: Config,
                 run=subprocess.run, popen=subprocess.Popen) -> str:
    """Apply the HDR policy to one rip and return the path of the file to keep.

    The new file replaces src (same path) only once it is complete. Files without
    Dolby Vision, and RAW policies, leave src untouched.
    """
    if not info.dolby_vision or policy.is_raw:
        return src
    if policy.keep == "hdr10":
        tool = [cfg.dovi_tool, "remove"]                       # drops the RPUs and any enhancement layer
    elif policy.convert_p81 and info.dv_profile == 7:
        tool = [cfg.dovi_tool, "-m", "2", "convert", "--discard"]   # profile 7 → 8.1, EL discarded
    else:
        return src

    os.makedirs(workdir, exist_ok=True)
    stem = os.path.splitext(os.path.basename(src))[0]
    hevc = os.path.join(workdir, f"{stem}.video.hevc")
    tmp = _temp_beside(src)
    try:
        fps = _fps(video_stream(src, cfg, run))
        _pipe(_annexb_cmd(src, cfg) + ["-"], tool + ["-i", "-", "-o", hevc], popen)
        # New video first, then everything except video from the original: audio, subtitles,
        # chapters, attachments and tags come across as they were.
        cmd = [cfg.mkvmerge, "-o", tmp, "--default-duration", f"0:{fps}", hevc, "-D", src]
        _mkvmerge(cmd, tmp, run)
        os.replace(tmp, src)
    finally:
        _remove(hevc, tmp)
    return src
