"""The optional AV1 second version of a 4K disc: encode, set HDR10 metadata, check quality, estimate.

The remux from the disc is always kept unchanged. The AV1 file sits next to it in the
same Plex folder, so Plex lists the two as versions of one film or episode. Many TV
boxes (the Nvidia Shield, Apple TVs before 2026) can't decode AV1 and keep playing the remux;
the AV1 file is for phones, browsers and remote streams. For that reason it carries
HDR10 only: no Dolby Vision, which AV1 tools don't carry over reliably.

Every external tool goes through an injectable `run` (subprocess.run) or `popen`
(subprocess.Popen), so the tests never start ffmpeg, ffprobe or mkvmerge.
"""
from __future__ import annotations

import json
import os
import posixpath
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass
from fractions import Fraction
from typing import Callable

from . import hdr
from .config import Config, state_dir
from .models import HdrInfo

QUALITY = ["Small", "Balanced", "High", "Very high", "Near the disc"]   # index 0..4, as the design's slider

# Encoder settings for each quality step.
#
# SVT-AV1 (CPU): presets 4-6 are where SVT-AV1 gives most of its compression for the
# time spent; below 4 a 4K film takes most of a day on the 7950X, above 6 files grow
# quickly. CRF does the real work and steps down about 4 per quality step, which is
# roughly a 30-40% size step. tune=0 is SVT-AV1's visual-quality tuning, which keeps
# texture that the default PSNR tuning smooths away.
SVT = [  # (preset, crf)
    (6, 34),   # Small
    (6, 30),   # Balanced
    (5, 27),   # High
    (5, 23),   # Very high
    (4, 19),   # Near the disc
]
# NVENC AV1 (GPU): p6 and p7 are the slowest, best presets and the RTX 4080 still
# encodes 4K faster than real time with them. VBR with a constant-quality target (-cq,
# and -b:v 0 so no bitrate cap) behaves like CRF. Lookahead plus spatial and temporal
# AQ put bits where the eye notices, and a quarter-resolution first pass helps scene
# changes. NVENC needs about a third more bits than SVT-AV1 for the same look.
NVENC = [  # (preset, cq)
    ("p6", 38),
    ("p6", 34),
    ("p6", 30),
    ("p7", 26),
    ("p7", 22),
]
GOP_FRAMES = 240              # a keyframe at least every 10 s, so seeking in a browser stays quick
FILM_GRAIN = 8                # SVT-AV1's film-grain level; 8 suits most film-sourced discs

# Default estimates for a 2 h 46 min (166 min) 4K HDR film, the design's example: AV1
# size in GB and 4K encoding speed in frames a second, per quality step. They are
# replaced by measured runs (Stats) once the first encodes finish.
#
# Measured on 2026-10-26 on this machine (Ryzen 9 7950X, RTX 4080, ffmpeg 9.0.2,
# SVT-AV1 4.2.0), at nice 10. VMAF is the mean over the clip, video only in MB/min.
#
#   clip                                   quality    encoder       fps   MB/min  VMAF
#   Pitch Perfect, 1080p H.264, 60 s       Balanced   cpu           101    24.1   97.5
#                                          Balanced   gpu           308    24.0   97.0
#                                          High       cpu            86    26.0   97.8
#                                          High       cpu + grain    50    24.6   97.4
#                                          High       gpu           308    36.4   98.0
#                                          Very high  cpu            87    34.4   98.3
#                                          Very high  gpu           276    57.1   98.6
#   synthetic 4K 10-bit HDR10, 10 s        High       cpu            16
#                                          High       cpu + grain     8
#   synthetic 4K 10-bit HDR10, 40 s        High       gpu            78
#                                          Near the disc gpu          69
#
# There was no 4K disc to measure. The CPU's 4K speed on the synthetic clip matched
# the design's figure for "High" (15 fps), so the CPU row is the design's. The GPU
# managed about 78 fps at 4K rather than the design's 125, so the GPU row is the
# measured one. Grain cost 1.6x on the real film clip and 2x on the noisy synthetic
# one, not the design's 8%. Sizes are the design's figures; the 1080p clip agrees
# with their ratio of GPU to CPU size (about 1.4x at "High").
DEFAULT_MINUTES = 166
DEFAULT_SIZE_GB = {"cpu": [8, 11, 15, 20, 27], "gpu": [11, 15, 20, 27, 36]}
DEFAULT_FPS_4K = {"cpu": [24, 19, 15, 11.5, 8], "gpu": [82, 80, 78, 72, 69]}
GRAIN_SLOWDOWN = 1.6          # film-grain analysis and denoising, measured on the 1080p film clip
FILM_FPS = 24000 / 1001
UHD_PIXELS = 3840 * 2160

# VMAF quality check. VMAF is trained on SDR video and isn't calibrated for PQ HDR,
# so the score is used as a relative check (did the encode lose a lot against the
# disc?), not as an absolute rating. Both sides are scaled to 1080p 10-bit the same
# way, which keeps the check quick and matches VMAF's default 1080p model.
VMAF_SIZE = (1920, 1080)
VMAF_MODEL = "version=vmaf_v0.6.1"


class EncodeError(RuntimeError):
    """An encode or quality check failed; the message is one plain sentence for the user."""


class Cancelled(Exception):
    """The user cancelled the encode; nothing is left behind."""


@dataclass
class EncodeSettings:
    encoder: str = "cpu"      # "cpu" (libsvtav1) | "gpu" (av1_nvenc)
    quality: int = 2          # 0..4, an index into QUALITY
    grain: bool = True        # film-grain synthesis (cpu only; ignored for gpu)

    @property
    def uses_grain(self) -> bool:
        return self.grain and self.encoder == "cpu"


@dataclass
class EncodeProgress:
    fraction: float
    fps: float
    out_seconds: float
    total_seconds: float
    size_bytes: int
    projected_bytes: int
    eta_seconds: float | None


@dataclass
class EncodeResult:
    path: str
    size_bytes: int
    seconds: float
    fps: float


@dataclass
class QualityResult:
    score: float
    clip_scores: list[float]
    passed: bool
    threshold: float


def _q(s: EncodeSettings) -> int:
    return max(0, min(len(QUALITY) - 1, int(s.quality)))


def _nice() -> list[str]:
    # Encodes share the machine with rips, copies and whoever is using it; they can wait.
    nice = shutil.which("nice")
    return [nice, "-n", "10"] if nice else []


def is_4k(width: int) -> bool:
    return (width or 0) >= 3000


_VERSION_SUFFIX = re.compile(r" - (?:2160p|1080p)$", re.IGNORECASE)


def av1_name(remux_rel: str) -> str:
    """The AV1 file's path next to the remux, e.g. "... /Dune Part Two (2024) - 2160p AV1.mkv"."""
    folder, name = posixpath.split(remux_rel)
    stem = _VERSION_SUFFIX.sub("", posixpath.splitext(name)[0])
    return posixpath.join(folder, f"{stem} - 2160p AV1.mkv")


# ---------------------------------------------------------------- encoding

def ffmpeg_cmd(src: str, dst: str, s: EncodeSettings, cfg: Config, info: HdrInfo | None = None) -> list[str]:
    """The ffmpeg command that writes dst: AV1 video, every audio and subtitle track copied as-is."""
    q = _q(s)
    cmd = [cfg.ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1"]
    if s.encoder == "gpu":
        # Decode on the GPU too and keep the frames there, so the CPU stays free.
        cmd += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    cmd += ["-i", src, "-map", "0:v:0", "-map", "0:a?", "-map", "0:s?",
            "-map_chapters", "0", "-map_metadata", "0"]
    if s.encoder == "gpu":
        preset, cq = NVENC[q]
        # NVENC has no Dolby Vision switch, so the RPUs are dropped from the frames
        # before they reach it and nothing can pass them on.
        cmd += ["-vf", "sidedata=mode=delete:type=DOVI_METADATA,sidedata=mode=delete:type=DOVI_RPU_BUFFER",
                "-c:v", "av1_nvenc", "-preset", preset, "-tune", "hq", "-multipass", "qres",
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0", "-rc-lookahead", "32",
                "-spatial-aq", "1", "-temporal-aq", "1", "-highbitdepth", "1"]
    else:
        preset, crf = SVT[q]
        params = "tune=0"
        if s.grain:
            params += f":film-grain={FILM_GRAIN}:film-grain-denoise=1"
        cmd += ["-c:v", "libsvtav1", "-pix_fmt", "yuv420p10le", "-preset", str(preset), "-crf", str(crf),
                "-svtav1-params", params, "-dolbyvision", "0"]
    cmd += ["-g", str(GOP_FRAMES)]
    if info is not None and info.hdr10:
        # Tag the bitstream itself; mkvmerge then adds the same to the container.
        cmd += ["-color_primaries", "bt2020", "-color_trc", "smpte2084", "-colorspace", "bt2020nc",
                "-color_range", "tv"]
    cmd += ["-c:a", "copy", "-c:s", "copy", "-f", "matroska", dst]
    return cmd


def _parse_progress_block(lines: list[str]) -> dict[str, str]:
    out = {}
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep:
            out[key] = value.strip()
    return out


def parse_progress(text: str) -> list[dict[str, str]]:
    """Split ffmpeg's -progress output into one dict per report (each ends with progress=...)."""
    blocks, cur = [], []
    for line in text.splitlines():
        cur.append(line)
        if line.startswith("progress="):
            blocks.append(_parse_progress_block(cur))
            cur = []
    return blocks


def _out_seconds(block: dict[str, str]) -> float | None:
    # out_time_us is microseconds; out_time_ms is microseconds too (an old ffmpeg bug kept for compatibility).
    for key in ("out_time_us", "out_time_ms"):
        v = block.get(key, "")
        if v.lstrip("-").isdigit():
            return max(0.0, int(v) / 1e6)
    m = re.match(r"(\d+):(\d+):(\d+(?:\.\d+)?)", block.get("out_time", ""))
    if m:
        return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    return None


class _Meter:
    """Turns -progress reports into EncodeProgress, counting only time spent not paused.

    Progress is counted in video frames when the frame rate is known. ffmpeg's out_time
    follows whichever stream is furthest ahead, and with audio copied it starts out at
    the end of the first audio packets (4 s in on a real run) before the video catches up.
    """

    def __init__(self, total_seconds: float, clock, frame_rate: float = 0.0):
        self.total, self.clock, self.rate = total_seconds, clock, frame_rate
        self.active = 0.0             # seconds spent encoding, pauses excluded
        self.last_tick = clock()
        self.paused = False
        self.frames = 0
        self.size = 0
        self.out = 0.0

    def tick(self) -> None:
        now = self.clock()
        if not self.paused:
            self.active += now - self.last_tick
        self.last_tick = now

    def pause(self, paused: bool) -> None:
        self.tick()
        self.paused = paused

    def update(self, block: dict[str, str]) -> EncodeProgress:
        self.tick()
        out = _out_seconds(block)
        if out is not None and self.rate <= 0:
            self.out = out
        if block.get("frame", "").isdigit():
            self.frames = int(block["frame"])
            if self.rate > 0:
                self.out = min(self.total, self.frames / self.rate)
        if block.get("total_size", "").isdigit():
            self.size = int(block["total_size"])
        frac = min(1.0, self.out / self.total) if self.total > 0 else 0.0
        if block.get("progress") == "end":
            frac = 1.0
        fps = self.frames / self.active if self.active > 0 else 0.0
        projected = int(self.size / frac) if frac > 0.001 else 0
        rate = self.out / self.active if self.active > 0 else 0.0     # source seconds per wall second
        eta = (self.total - self.out) / rate if rate > 0 and frac < 1.0 else (0.0 if frac >= 1.0 else None)
        return EncodeProgress(frac, fps, self.out, self.total, self.size, projected, eta)


def _ratio(v) -> float:
    try:
        return float(Fraction(str(v)))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _source_details(src: str, cfg: Config, run) -> tuple[dict, list[dict], float]:
    """(first video stream, first few frames, duration in seconds) from ffprobe."""
    data = hdr._json([cfg.ffprobe, "-v", "error", "-print_format", "json", "-show_streams",
                      "-show_format", "-select_streams", "v:0", src], run)
    streams = data.get("streams") or []
    duration = _ratio((data.get("format") or {}).get("duration") or 0)
    frames = hdr._first_frames(src, cfg, run)
    return (streams[0] if streams else {}), frames, duration


def _side_data(stream: dict, frames: list[dict], kind: str) -> dict:
    for sd in (stream.get("side_data_list") or []) + [x for f in frames for x in f.get("side_data_list") or []]:
        if sd.get("side_data_type") == kind:
            return sd
    return {}


def _hdr10_info(stream: dict, frames: list[dict]) -> HdrInfo:
    transfer = stream.get("color_transfer") or next((f.get("color_transfer") for f in frames if f.get("color_transfer")), "")
    primaries = stream.get("color_primaries") or next((f.get("color_primaries") for f in frames if f.get("color_primaries")), "")
    info = HdrInfo(codec=stream.get("codec_name", ""), hdr10=transfer == "smpte2084" and primaries == "bt2020")
    if stream.get("width") and stream.get("height"):
        info.resolution = f"{stream['width']}x{stream['height']}"
    return info


def _num(v) -> str:
    """A ffprobe ratio such as "34000/50000" as mkvmerge wants it, e.g. "0.68"."""
    return f"{_ratio(v):.6g}"


def hdr10_mkvmerge_cmd(src_partial: str, out: str, stream: dict, frames: list[dict], cfg: Config) -> list[str]:
    """mkvmerge writing out with HDR10 colour metadata on the video track (track 0, as ffmpeg maps it)."""
    t = "0:"
    cmd = [cfg.mkvmerge, "-o", out,
           "--colour-matrix-coefficients", t + "9",          # BT.2020 non-constant luminance
           "--colour-bits-per-channel", t + "10",
           "--colour-range", t + "1",                        # limited (TV) range
           "--colour-transfer-characteristics", t + "16",    # SMPTE ST 2084 (PQ)
           "--colour-primaries", t + "9"]                    # BT.2020
    md = _side_data(stream, frames, "Mastering display metadata")
    keys = ("red_x", "red_y", "green_x", "green_y", "blue_x", "blue_y")
    if all(k in md for k in keys):
        cmd += ["--chromaticity-coordinates", t + ",".join(_num(md[k]) for k in keys)]
    if "white_point_x" in md and "white_point_y" in md:
        cmd += ["--white-colour-coordinates", t + f"{_num(md['white_point_x'])},{_num(md['white_point_y'])}"]
    if "max_luminance" in md:
        cmd += ["--max-luminance", t + _num(md["max_luminance"])]
    if "min_luminance" in md:
        cmd += ["--min-luminance", t + _num(md["min_luminance"])]
    cll = _side_data(stream, frames, "Content light level metadata")
    if cll.get("max_content"):
        cmd += ["--max-content-light", t + str(int(cll["max_content"]))]
    if cll.get("max_average"):
        cmd += ["--max-frame-light", t + str(int(cll["max_average"]))]
    return cmd + [src_partial]


def _remove(*paths: str) -> None:
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def _reader(stream, q: queue.Queue, tag: str) -> None:
    try:
        for line in stream:
            q.put((tag, line.decode(errors="replace") if isinstance(line, bytes) else line))
    except (ValueError, OSError):
        pass
    q.put((tag, None))


def _plain_error(lines) -> str:
    last = next((l.strip() for l in reversed(list(lines)) if l.strip()), "")
    return last or "ffmpeg stopped without saying why"


def encode(src: str, dst: str, s: EncodeSettings, cfg: Config,
           progress: Callable[[EncodeProgress], None] | None = None,
           cancel: threading.Event | None = None, pause: threading.Event | None = None,
           popen=subprocess.Popen, run=subprocess.run, clock=time.monotonic,
           poll_s: float = 0.25) -> EncodeResult:
    """Encode src to dst as AV1 at low priority, then set HDR10 metadata on it.

    ffmpeg writes dst + ".encode.partial", mkvmerge writes dst + ".partial" from that,
    and dst appears only when both have finished. pause.set() suspends ffmpeg and
    pause.clear() resumes it; cancel.set() stops it and removes the partial files.
    """
    cancel, pause = cancel or threading.Event(), pause or threading.Event()
    try:
        stream, frames, duration = _source_details(src, cfg, run)
    except hdr.ToolError as e:
        raise EncodeError(f"Couldn't read the remux before encoding it: {str(e).splitlines()[0]}") from e
    info = _hdr10_info(stream, frames)
    stage, partial = dst + ".encode.partial", dst + ".partial"
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)

    cmd = _nice() + ffmpeg_cmd(src, stage, s, cfg, info)
    rate = _ratio(stream.get("avg_frame_rate") or 0) or _ratio(stream.get("r_frame_rate") or 0)
    meter = _Meter(duration, clock, rate)
    started = clock()
    try:
        proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except OSError as e:
        raise EncodeError(f"Couldn't start ffmpeg: {e}.") from e
    lines: queue.Queue = queue.Queue()
    errors: deque[str] = deque(maxlen=20)
    for pipe, tag in ((proc.stdout, "out"), (proc.stderr, "err")):
        threading.Thread(target=_reader, args=(pipe, lines, tag), daemon=True).start()

    stopped, open_pipes, block = False, 2, []
    try:
        while open_pipes:
            if cancel.is_set():
                if stopped:
                    proc.send_signal(signal.SIGCONT)
                proc.kill()
                proc.wait()
                raise Cancelled()
            if pause.is_set() != stopped:
                stopped = pause.is_set()
                meter.pause(stopped)         # before the signal, so no paused time is counted
                proc.send_signal(signal.SIGSTOP if stopped else signal.SIGCONT)
            try:
                tag, line = lines.get(timeout=poll_s)
            except queue.Empty:
                continue
            if line is None:
                open_pipes -= 1
            elif tag == "err":
                errors.append(line)
            else:
                block.append(line)
                if line.startswith("progress="):
                    p = meter.update(_parse_progress_block(block))
                    block = []
                    if progress:
                        progress(p)
        rc = proc.wait()
        if cancel.is_set():
            raise Cancelled()
        if rc != 0:
            raise EncodeError(f"The AV1 encode failed: {_plain_error(errors)}")
        meter.tick()

        if info.hdr10:
            mkv = hdr10_mkvmerge_cmd(stage, partial, stream, frames, cfg)
            proc2 = run(_nice() + mkv, capture_output=True, text=True)
            if proc2.returncode not in (0, 1):            # 1 is warnings only; the file is written
                raise EncodeError("Couldn't add the HDR10 details to the AV1 file: "
                                  f"{_plain_error((proc2.stdout or '').splitlines())}")
            _remove(stage)
        else:
            os.replace(stage, partial)
        if cancel.is_set():
            raise Cancelled()
        os.replace(partial, dst)
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        _remove(stage, partial)
        raise
    seconds = meter.active if meter.active > 0 else clock() - started
    size = os.path.getsize(dst)
    return EncodeResult(dst, size, seconds, meter.frames / seconds if seconds > 0 else 0.0)


# ---------------------------------------------------------------- quality check

def _clip_starts(duration: float, clips: int, clip_s: float) -> list[float]:
    """Evenly spaced clip starts, skipping the first and last 5% (logos and credits)."""
    lo, hi = duration * 0.05, duration * 0.95 - clip_s
    if clips <= 0 or hi <= lo:
        return [max(0.0, min(lo, duration - clip_s))] if duration > 0 else []
    if clips == 1:
        return [round((lo + hi) / 2, 3)]
    step = (hi - lo) / (clips - 1)
    return [round(lo + i * step, 3) for i in range(clips)]


def vmaf_cmd(src: str, dst: str, start: float, clip_s: float, log_path: str, cfg: Config) -> list[str]:
    w, h = VMAF_SIZE
    scale = f"scale={w}:{h}:flags=bicubic,format=yuv420p10le,setpts=PTS-STARTPTS"
    graph = (f"[0:v:0]{scale}[dist];[1:v:0]{scale}[ref];"
             f"[dist][ref]libvmaf=model={VMAF_MODEL}:log_fmt=json:log_path={log_path}:n_threads=16"
             # Matroska rounds timestamps to the millisecond, so a frame of one file can land
             # 1 ms before its twin in the other. The default sync then pairs it with the
             # previous frame, which scored about 2 frames in every 24 near zero on a real film.
             ":ts_sync_mode=nearest")
    seek = ["-ss", f"{start:.3f}", "-t", f"{clip_s:.3f}"]
    return [cfg.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
            *seek, "-i", dst, *seek, "-i", src, "-lavfi", graph, "-f", "null", "-"]


def parse_vmaf(text: str) -> float:
    """The mean VMAF score from libvmaf's JSON log."""
    data = json.loads(text)
    pooled = (data.get("pooled_metrics") or {}).get("vmaf") or {}
    if "mean" in pooled:
        return float(pooled["mean"])
    scores = [f["metrics"]["vmaf"] for f in data.get("frames") or [] if "vmaf" in (f.get("metrics") or {})]
    if not scores:
        raise ValueError("no VMAF scores in the log")
    return sum(scores) / len(scores)


def check_quality(src: str, dst: str, cfg: Config, clips: int = 6, clip_s: float = 10.0,
                  threshold: float = 93.0, progress: Callable[[float, str], None] | None = None,
                  run=subprocess.run) -> QualityResult:
    """Score the AV1 file against the remux with VMAF on a few short clips; passed when the mean ≥ threshold."""
    try:
        _, _, duration = _source_details(src, cfg, run)
    except hdr.ToolError as e:
        raise EncodeError(f"Couldn't read the remux for the quality check: {str(e).splitlines()[0]}") from e
    starts = _clip_starts(duration, clips, clip_s)
    if not starts:
        raise EncodeError("Couldn't check the quality: the remux reports no length.")
    scores = []
    with tempfile.TemporaryDirectory(prefix="discshelf-vmaf-") as tmp:
        for i, start in enumerate(starts):
            if progress:
                progress(i / len(starts), f"Checking clip {i + 1} of {len(starts)}")
            log = os.path.join(tmp, f"clip{i}.json")
            cmd = _nice() + vmaf_cmd(src, dst, start, clip_s, log, cfg)
            proc = run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise EncodeError(f"The quality check failed: {_plain_error((proc.stderr or '').splitlines())}")
            try:
                with open(log) as fh:
                    scores.append(round(parse_vmaf(fh.read()), 2))
            except (OSError, ValueError, KeyError) as e:
                raise EncodeError(f"The quality check gave no score for clip {i + 1}.") from e
    if progress:
        progress(1.0, "Quality checked")
    score = round(sum(scores) / len(scores), 2)
    return QualityResult(score, scores, score >= threshold, threshold)


# ---------------------------------------------------------------- estimates

def _stats_path() -> str:
    return os.path.join(state_dir(), "av1_stats.json")


def _pixels(width: int) -> float:
    # Only the width is known; films are close enough to 16:9 for an estimate.
    return float(width) * width * 9 / 16 if width else UHD_PIXELS


class Stats:
    """Measured encodes, kept so the estimates come from this machine and these discs."""

    def __init__(self, path: str | None = None):
        self.path = path or _stats_path()
        self.runs: list[dict] = []
        try:
            with open(self.path) as fh:
                self.runs = [r for r in json.load(fh).get("runs", []) if isinstance(r, dict)]
        except (FileNotFoundError, json.JSONDecodeError, AttributeError):
            self.runs = []

    def record(self, s: EncodeSettings, width: int, duration_s: float, seconds: float,
               size_bytes: int, source_bytes: int) -> None:
        if duration_s <= 0 or seconds <= 0 or size_bytes <= 0:
            return
        self.runs.append({"encoder": s.encoder, "quality": _q(s), "grain": s.uses_grain, "width": width,
                          "duration_s": duration_s, "seconds": seconds, "size_bytes": size_bytes,
                          "source_bytes": source_bytes, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
        # The numbers only improve estimates: a file that can't be written (a read-only state
        # folder, os.devnull in the sample controller) must never fail an encode that worked.
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"runs": self.runs[-200:]}, fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def estimate(self, s: EncodeSettings, width: int, duration_s: float, source_bytes: int) -> tuple[int, float, bool]:
        """(size in bytes, encoding time in seconds, measured) for one encode with these settings."""
        q, px = _q(s), _pixels(width)
        same = [r for r in self.runs if r.get("encoder") == s.encoder and r.get("quality") == q]
        if same:
            # Bytes and encoding seconds per second of film per pixel, averaged over the runs.
            size_rate = sum(r["size_bytes"] / (r["duration_s"] * _pixels(r["width"])) for r in same) / len(same)
            time_rate = []
            for r in same:
                t = r["seconds"] / (r["duration_s"] * _pixels(r["width"]))
                if s.encoder == "cpu" and r.get("grain") != s.uses_grain:
                    t = t * GRAIN_SLOWDOWN if s.uses_grain else t / GRAIN_SLOWDOWN
                time_rate.append(t)
            size = size_rate * duration_s * px
            seconds = sum(time_rate) / len(time_rate) * duration_s * px
            measured = True
        else:
            scale = (duration_s / (DEFAULT_MINUTES * 60)) * (px / UHD_PIXELS)
            size = DEFAULT_SIZE_GB[s.encoder][q] * 1e9 * scale
            fps = DEFAULT_FPS_4K[s.encoder][q] / (GRAIN_SLOWDOWN if s.uses_grain else 1)
            seconds = duration_s * FILM_FPS / fps * (px / UHD_PIXELS)
            measured = False
        if source_bytes > 0:
            size = min(size, source_bytes)     # a re-encode bigger than the disc would not be kept anyway
        return int(size), float(seconds), measured
