"""Everything Disc Shelf can learn about a disc's formats, gathered into one report for the Disc info screen.

Sources: MakeMKV's scan (titles, streams, codecs, channel layouts, frame rates) and,
when the disc's file system can be mounted, BDMV/META (the disc's own title and cover
thumbnails). Nothing here writes to the disc.
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class VideoInfo:
    codec: str = ""              # e.g. "HEVC (H.265)", "AVC (H.264)"
    resolution: str = ""         # e.g. "3840x2160"
    frame_rate: str = ""         # e.g. "23.976"
    aspect: str = ""             # e.g. "16:9"
    hdr: list[str] = field(default_factory=list)   # what MakeMKV reports before ripping, e.g. ["HDR10", "Dolby Vision"]
    label: str = ""              # one line, e.g. "4K · HEVC · 23.976 fps · HDR10"


@dataclass
class AudioInfo:
    lang: str = ""               # e.g. "English"
    codec: str = ""              # e.g. "Dolby TrueHD Atmos", "DTS-HD Master Audio"
    channels: str = ""           # e.g. "7.1", "5.1", "2.0"
    sample_rate: str = ""        # e.g. "48 kHz"
    bitrate: str = ""
    note: str = ""               # "Commentary", "Audio description", ...


@dataclass
class SubtitleInfo:
    lang: str = ""
    codec: str = ""              # "PGS" on Blu-ray
    forced: bool = False


@dataclass
class TitleInfo:
    index: int
    source: str = ""
    duration: str = ""           # "0:57:04"
    size: str = ""               # "17.6 GB"
    chapters: int = 0
    segments: int = 0
    kind: str = ""               # from classify: episode / play_all / extra / feature / short / duplicate
    video: VideoInfo = field(default_factory=VideoInfo)
    audio: list[AudioInfo] = field(default_factory=list)
    subtitles: list[SubtitleInfo] = field(default_factory=list)


@dataclass
class DiscReport:
    title: str = ""              # the disc's own title (BDMV/META), else MakeMKV's disc name
    label: str = ""              # volume label
    disc_type: str = ""          # "4K UHD Blu-ray", "Blu-ray", "DVD"
    capacity: str = ""           # "81.7 GB of data"
    layers: str = ""             # "3 layers (100 GB disc)", inferred from the data size
    drive: str = ""              # drive model and firmware
    libredrive: bool = False
    cover_path: str = ""         # largest thumbnail from BDMV/META/DL, "" when none
    cover_source: str = ""       # "disc" (BDMV/META) | "TVmaze" (a DVD or a disc without artwork) | ""
    highlights: list[str] = field(default_factory=list)   # chips: "4K UHD", "HDR10", "Dolby Atmos", "DTS:X", "7 audio languages"
    audio_languages: list[str] = field(default_factory=list)
    subtitle_languages: list[str] = field(default_factory=list)
    titles: list[TitleInfo] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)          # plain sentences, e.g. "HDR formats are confirmed after ripping."


@dataclass
class DiscMeta:
    title: str = ""
    set_number: int | None = None    # disc N of a set
    num_sets: int | None = None
    cover_path: str = ""             # copied out of the disc, so it survives the unmount


def mount_point(device: str, mounts: str = "/proc/mounts") -> str:
    try:
        with open(mounts) as fh:
            for line in fh:
                parts = line.split()
                if parts and parts[0] == device:
                    # /proc/mounts escapes spaces as \040
                    return re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1), 8)), parts[1])
    except OSError:
        pass
    return ""


def read_meta(root: str) -> DiscMeta:
    """Parse BDMV/META/DL/bdmt_*.xml under a mounted disc (English first)."""
    files = sorted(glob.glob(os.path.join(root, "BDMV", "META", "DL", "bdmt_*.xml")),
                   key=lambda p: (not p.endswith("bdmt_eng.xml"), p))
    if not files:
        return DiscMeta()
    try:
        tree = ET.parse(files[0])
    except ET.ParseError:
        return DiscMeta()
    ns = {"di": "urn:BDA:bdmv;discinfo"}
    meta = DiscMeta()
    meta.title = (tree.findtext(".//di:title/di:name", default="", namespaces=ns) or "").strip()
    for attr, tag in (("set_number", "setNumber"), ("num_sets", "numSets")):
        text = tree.findtext(f".//di:title/di:{tag}", default="", namespaces=ns)
        if text.strip().isdigit():
            setattr(meta, attr, int(text))
    thumbs = [os.path.join(os.path.dirname(files[0]), t.get("href", ""))
              for t in tree.iterfind(".//di:thumbnail", ns) if t.get("href")]
    thumbs = [t for t in thumbs if os.path.isfile(t)]
    if thumbs:
        meta.cover_path = max(thumbs, key=os.path.getsize)    # the larger file is the larger picture
    return meta


def fetch_meta(device: str, cache_dir: str, label: str = "", run=subprocess.run) -> DiscMeta:
    """Read the disc's own title and cover. Mounts read-only with udisksctl if needed and unmounts again."""
    root = mount_point(device)
    mounted_here = False
    if not root:
        r = run(["udisksctl", "mount", "--no-user-interaction", "-o", "ro", "-b", device],
                capture_output=True, text=True)
        if r.returncode != 0:
            return DiscMeta()
        root = mount_point(device)
        mounted_here = True
    try:
        meta = read_meta(root) if root else DiscMeta()
        if meta.cover_path:
            os.makedirs(cache_dir, exist_ok=True)
            stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", label or os.path.basename(root) or "disc")
            dest = os.path.join(cache_dir, stem + os.path.splitext(meta.cover_path)[1].lower())
            shutil.copyfile(meta.cover_path, dest)
            meta.cover_path = dest
        return meta
    finally:
        if mounted_here:
            run(["udisksctl", "unmount", "--no-user-interaction", "-b", device], capture_output=True, text=True)


def layers_for(size_bytes: int) -> str:
    """The disc's layer count, inferred from how much data it holds (BD layers hold 25 GB, UHD BD-66/100 use 33 GB layers)."""
    gb = size_bytes / 1e9
    if gb <= 0:
        return ""
    if gb > 66:
        return "3 layers (100 GB disc)"
    if gb > 50:
        return "2 layers (66 GB disc)"
    if gb > 25:
        return "2 layers (50 GB disc)"
    if gb > 9.4:
        return "1 layer (25 GB disc)"
    return "DVD-sized"


# MakeMKV attribute ids (apdefs.h) read here beyond what models.Stream keeps
_A_CODEC_LONG, _A_BITRATE, _A_SAMPLE_RATE, _A_ASPECT, _A_FRAME_RATE, _A_TREE = 7, 13, 17, 20, 21, 30
_CODEC_NAMES = {"Mpeg4": "AVC (H.264)", "MpegH": "HEVC (H.265)", "Mpeg2": "MPEG-2", "VC-1": "VC-1"}
_HDR_TOKENS = (("Dolby Vision", "Dolby Vision"), ("HDR10+", "HDR10+"), ("HDR10", "HDR10"),
               ("SMPTE ST 2084", "HDR10"), ("BT.2020", "HDR10"))


def _size(n: int) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _duration(s: int) -> str:
    h, rem = divmod(s, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}"


def _channels(layout: str, count: str) -> str:
    """'5.1(side)' → '5.1'; fall back to a channel count: 6 → '5.1', 8 → '7.1'."""
    m = re.match(r"(\d\.\d)", layout or "")
    if m:
        return m.group(1)
    return {"1": "1.0", "2": "2.0", "6": "5.1", "8": "7.1"}.get(count or "", count or "")


def _video(streams_raw: dict, title) -> VideoInfo:
    vids = [s for s in title.streams if s.kind == "video"]
    if not vids:
        return VideoInfo()
    v = vids[0]
    raw = streams_raw.get(v.index, {})
    rate = (raw.get(_A_FRAME_RATE, "") or "").split(" ")[0]
    hdr: list[str] = []
    text = " ".join(" ".join(streams_raw.get(s.index, {}).values()) for s in vids)
    for token, name in _HDR_TOKENS:
        if token in text and name not in hdr:
            hdr.append(name)
    codec = _CODEC_NAMES.get(v.codec, v.codec_long or v.codec)
    res = v.resolution
    short = "4K" if res.startswith("3840") else "1080p" if res.endswith("1080") else "720p" if res.endswith("720") else res
    label = " · ".join(x for x in (short, codec.split(" ")[0], f"{rate} fps" if rate else "", *hdr) if x)
    return VideoInfo(codec=codec, resolution=res, frame_rate=rate, aspect=raw.get(_A_ASPECT, ""), hdr=hdr, label=label)


def _audio(streams_raw: dict, title) -> list[AudioInfo]:
    out = []
    for s in title.streams:
        if s.kind != "audio" or "core" in s.flags.split(","):
            continue    # the DTS / Dolby core MakeMKV lists beside its HD track is the same sound
        raw = streams_raw.get(s.index, {})
        rate = raw.get(_A_SAMPLE_RATE, "")
        note = ("Commentary" if "commentary" in s.flags else
                "Audio description" if "descriptive" in s.flags else "")
        out.append(AudioInfo(lang=s.lang_name or s.lang, codec=raw.get(_A_CODEC_LONG, "") or s.codec_long or s.codec,
                             channels=_channels(raw.get(40, ""), raw.get(14, "")),
                             sample_rate=f"{int(rate) // 1000} kHz" if rate.isdigit() else "",
                             bitrate=raw.get(_A_BITRATE, ""), note=note))
    return out


def _subtitles(title) -> list[SubtitleInfo]:
    return [SubtitleInfo(lang=s.lang_name or s.lang, codec=s.codec, forced="forced" in s.flags)
            for s in title.streams if s.kind == "subtitle"]


def build_report(parsed: dict, disc, meta: DiscMeta | None = None) -> DiscReport:
    """parsed: makemkv.parse_robot output of the scan; disc: the same scan as a Disc, already classified."""
    meta = meta or DiscMeta()
    rep = DiscReport(title=meta.title or disc.name or disc.label, label=disc.label,
                     drive=disc.drive.model, libredrive=disc.libredrive, cover_path=meta.cover_path,
                     cover_source="disc" if meta.cover_path else "")
    if meta.num_sets and meta.num_sets > 1 and meta.set_number:
        rep.title += f" · Disc {meta.set_number} of {meta.num_sets}"
    sizes = [t.size_bytes for t in disc.titles if t.kind not in ("play_all", "duplicate")]
    data = max(sum(sizes), max((t.size_bytes for t in disc.titles), default=0))
    rep.capacity = f"{_size(data)} of video" if data else ""
    rep.layers = layers_for(data)

    for t in disc.titles:
        raw = parsed.get("sinfo", {}).get(t.index, {})
        rep.titles.append(TitleInfo(index=t.index, source=t.source, duration=_duration(t.duration_s),
                                    size=_size(t.size_bytes), chapters=t.chapters, segments=len(t.segments),
                                    kind=t.kind, video=_video(raw, t), audio=_audio(raw, t),
                                    subtitles=_subtitles(t)))

    picked = [ti for ti, t in zip(rep.titles, disc.titles) if t.selected and t.kind in ("feature", "episode")]
    main = picked[0] if picked else max(rep.titles, key=lambda ti: ti.size, default=None)
    if disc.uhd:
        rep.disc_type = "4K UHD Blu-ray"
    elif "Blu-ray" in disc.disc_type:
        rep.disc_type = "Blu-ray"
    else:
        rep.disc_type = disc.disc_type.replace(" disc", "") or "Disc"

    if main:
        rep.audio_languages = list(dict.fromkeys(a.lang for a in main.audio if a.lang))
        rep.subtitle_languages = list(dict.fromkeys(s.lang for s in main.subtitles if s.lang and not s.forced))
        hl = []
        res = main.video.resolution
        hl.append("4K UHD" if res.startswith("3840") else "Full HD 1080p" if res.endswith("1080") else res)
        hl += main.video.hdr
        codecs = " ".join(a.codec for a in main.audio)
        for token, name in (("Atmos", "Dolby Atmos"), ("DTS:X", "DTS:X"), ("DTS-HD Master", "DTS-HD Master Audio"),
                            ("TrueHD", "Dolby TrueHD"), ("LPCM", "LPCM")):
            if token in codecs and not any(name in h or h in name for h in hl):
                hl.append(name)
        if rep.audio_languages:
            hl.append(f"{len(rep.audio_languages)} audio language{'s' if len(rep.audio_languages) > 1 else ''}")
        if rep.subtitle_languages:
            hl.append(f"{len(rep.subtitle_languages)} subtitle language{'s' if len(rep.subtitle_languages) > 1 else ''}")
        rep.highlights = [h for h in hl if h]
        if res.startswith("3840") and not main.video.hdr:
            rep.notes.append("MakeMKV doesn't report HDR formats before ripping. They are checked on the ripped file.")
    if any(t.kind == "play_all" for t in disc.titles):
        rep.notes.append("A play-all title repeats episodes that are also listed on their own, so it is skipped.")
    if not disc.libredrive:
        rep.notes.append("LibreDrive is off, so 4K UHD discs can't be read and Blu-rays may rip slowly.")
    return rep
