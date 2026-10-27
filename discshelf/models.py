"""Data shared by every part of Disc Shelf. Plain dataclasses, JSON round-trippable."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Stream:
    index: int
    kind: str                    # "video" | "audio" | "subtitle"
    codec: str = ""              # MakeMKV's short codec name, e.g. "MpegH", "TrueHD", "PGS"
    codec_long: str = ""
    lang: str = ""               # ISO 639-2, e.g. "eng"
    lang_name: str = ""
    name: str = ""               # MakeMKV's stream name, e.g. "Surround 5.1"
    channels: str = ""
    resolution: str = ""         # video only, e.g. "3840x2160"
    flags: str = ""              # anything else MakeMKV says about it (default, forced, commentary, ...)


@dataclass
class Title:
    index: int                   # MakeMKV's title number on this disc
    source: str = ""             # playlist or stream file, e.g. "00800.mpls"
    duration_s: int = 0
    size_bytes: int = 0
    chapters: int = 0
    segments: list[int] = field(default_factory=list)   # m2ts segment numbers, used to spot play-alls
    streams: list[Stream] = field(default_factory=list)
    kind: str = "unknown"        # "episode" | "play_all" | "extra" | "duplicate" | "short" | "feature" | "unknown"
    selected: bool = False
    title_id: str = ""           # a DVD's own title number, e.g. "02" (MakeMKV's original title id)


@dataclass
class Drive:
    index: int                   # MakeMKV's drive number (disc:N)
    device: str = ""             # e.g. "/dev/sr0"
    model: str = ""              # e.g. "BD-RE PIONEER BD-RW BDR-XD08U 1.01"
    disc_label: str = ""         # volume label of the inserted disc, "" when empty
    has_disc: bool = False


@dataclass
class Disc:
    drive: Drive
    label: str = ""              # volume label, e.g. "SEVERANCE_S1_D1"
    name: str = ""               # MakeMKV's disc name, e.g. "Severance"
    disc_type: str = ""          # "Blu-ray disc", "DVD disc", ...
    uhd: bool = False            # a 2160p video stream was found
    libredrive: bool = False     # MakeMKV said "Using LibreDrive mode"
    size_bytes: int = 0
    titles: list[Title] = field(default_factory=list)


@dataclass
class HdrInfo:
    resolution: str = ""         # e.g. "3840x2160"
    codec: str = ""              # e.g. "hevc"
    hdr10: bool = False
    hdr10plus: bool | None = None        # None: not checked
    dolby_vision: bool = False
    dv_profile: int | None = None
    dv_enhancement_layer: bool | None = None   # a profile 7 enhancement layer is present
    dv_layer_kind: str | None = None     # "FEL" | "MEL" | None when unknown

    def labels(self) -> list[str]:
        out = []
        if self.hdr10:
            out.append("HDR10")
        if self.dolby_vision:
            out.append(f"Dolby Vision · profile {self.dv_profile}" if self.dv_profile else "Dolby Vision")
        if self.hdr10plus:
            out.append("HDR10+")
        return out


@dataclass
class HdrPolicy:
    keep: str = "all"            # "all": every HDR format as on the disc | "hdr10": drop Dolby Vision
    convert_p81: bool = False    # rewrite Dolby Vision profile 7 as 8.1 (drops a full enhancement layer)

    def raw_reasons(self) -> list[str]:
        """Each setting that changes the HDR from what is on the disc. Empty means RAW."""
        out = []
        if self.keep != "all":
            out.append("“Keep HDR10 only” is selected, so the Dolby Vision layer is dropped.")
        if self.convert_p81 and self.keep == "all":
            out.append("“Convert Dolby Vision to profile 8.1” is on, so Dolby Vision is rewritten "
                       "and a full enhancement layer is lost.")
        return out

    @property
    def is_raw(self) -> bool:
        return not self.raw_reasons()


@dataclass
class Episode:
    season: int
    number: int
    title: str = ""
    runtime_min: int | None = None


@dataclass
class Match:
    title_index: int
    target: str = "skip"         # "episode" | "extra" | "feature" | "skip"
    episode: Episode | None = None
    episode_end: int | None = None       # last episode number when one title holds two episodes
    extra_name: str = ""         # file name stem for an extra, e.g. "Deleted Scenes"
    extra_folder: str = "Featurettes"    # a Plex extras folder name
    confidence: str = "manual"   # "close" | "check" | "manual"
    note: str = ""               # why it needs checking, in a sentence
    confirmed: bool = False      # the user confirmed it; "close" matches don't need this to be copied
    source: str = ""             # what made it: "runtime" | "card" | "discdb" | "list" | "manual"; "" = older jobs
    version: str = ""            # a second cut of an episode another title also holds, e.g. "Short cut";
                                 # both files keep the episode's name and Plex offers them under Play Version
    held: str = ""               # another title or file holds the same episode (season.py): this sentence
                                 # leads the note, and the title isn't copied until that's settled
    held_was: str = ""           # the confidence it had before it was held, given back afterwards

    @property
    def ready(self) -> bool:
        """May be copied as soon as its title is ripped."""
        return self.target != "skip" and not self.held and (self.confirmed or self.confidence == "close")


@dataclass
class TitleProgress:
    """Where one title is, while ripping and copying overlap."""
    title_index: int
    state: str = "waiting"       # "waiting" | "ripping" | "ripped" | "waiting_match" | "copying" | "checking"
                                 # | "verified" | "failed" | "skipped"
    total_bytes: int = 0         # size from the scan, refined to the real file size once ripped
    ripped_bytes: int = 0
    copied_bytes: int = 0
    checked_bytes: int = 0
    rip_rate: float = 0.0        # bytes per second, latest
    copy_rate: float = 0.0
    note: str = ""               # one sentence when it waits or fails


@dataclass
class Sample:
    """One point on the speed graphs. Kept in memory by the GUI, not saved with the job."""
    t: float                     # seconds since the job's rip (or copy) started
    kind: str                    # "rip" | "copy"
    rate: float                  # bytes per second over the last interval
    total: int                   # bytes ripped (or copied) so far, all titles


@dataclass
class Span:
    """One step of one title on the rip timeline, kept in memory like Sample. A span whose end
    is None is still going."""
    kind: str                    # "open" (MakeMKV opening the disc) | "read" | "languages" | "copy"
    title_index: int
    start: float                 # seconds since the rip started, the clock Sample.t uses
    end: float | None = None
    bytes: int = 0               # for "read": what MakeMKV wrote, before the languages step


@dataclass
class RippedFile:
    title_index: int
    path: str                    # the MKV in the local rip folder
    size_bytes: int = 0
    sha256: str = ""
    dest_rel: str = ""           # path under the destination folder, set once matched
    state: str = "ripped"        # "ripped" | "converted" | "copying" | "copied" | "verified" | "failed"
    hdr: HdrInfo | None = None
    local_deleted: bool = False  # the local rip was deleted after its NAS copy was verified


@dataclass
class EncodeJob:
    """The AV1 second version of one 4K title: encoded from the local remux, checked, then copied
    next to the remux on the NAS. The remux itself never changes."""
    title_index: int
    state: str = "waiting"       # "waiting" | "encoding" | "paused" | "checking" | "copying" | "verified"
                                 # | "failed" | "skipped"
    encoder: str = "cpu"         # "cpu" | "gpu"
    quality: int = 2             # an index into av1.QUALITY
    grain: bool = True
    src: str = ""                # the local remux it's encoded from
    path: str = ""               # the local AV1 file
    dest_rel: str = ""           # path under the job's destination, next to the remux
    fraction: float = 0.0        # of the encode
    fps: float = 0.0
    size_bytes: int = 0          # written so far; the final size once encoded
    projected_bytes: int = 0
    eta_seconds: float | None = None
    vmaf: float | None = None    # the quality check's score, None until checked
    note: str = ""               # one sentence when it waits, is paused, fails or is skipped
    started: str = ""            # ISO 8601
    finished: str = ""
    queued: str = ""             # when it was queued; encodes run in this order
    held: bool = False           # paused by you (not just held back by the "when to encode" setting)
    encoded: bool = False        # the encode finished and path is the complete AV1 file
    sha256: str = ""             # of the local AV1 file, taken for the NAS check
    width: int = 0               # of the source video
    seconds: float = 0.0         # time spent encoding, for the estimates
    local_deleted: bool = False  # the local AV1 file was deleted after its NAS copy was verified


@dataclass
class Job:
    id: str
    disc_label: str = ""
    drive_index: int = 0
    min_seconds: int = 120       # the --minlength the scan used; the rip must use the same or title numbers shift
    audio_langs: list[str] = field(default_factory=list)
    subtitle_langs: list[str] = field(default_factory=list)
    media_kind: str = "tv"       # a Destination.kind: "tv" | "movie" | "anime" | user-defined
    show: str = ""               # show or film name as Plex should see it
    show_id: int | None = None   # TVmaze show id once the show is chosen
    year: int | None = None
    season: int | None = None
    disc_number: int | None = None
    destination: str = ""        # local path of the NAS folder, e.g. /mnt/nas/Shows
    library: str = ""            # Plex library to scan afterwards, "" for none
    version: str = ""            # "" or a version tag such as "2160p" / "1080p"
    rip_dir: str = ""
    titles: list[Title] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)
    files: list[RippedFile] = field(default_factory=list)
    hdr_policy: HdrPolicy = field(default_factory=HdrPolicy)
    titles_progress: list[TitleProgress] = field(default_factory=list)
    copy_mode: str = ""          # "after" (copy once matched, after the rip) | "per_title" | "stream" (a film copied as it rips)
    copy_mode_note: str = ""     # why "stream" wasn't possible, when it fell back
    stage: str = "rip"           # "rip" | "match" | "copy" | "plex" | "done" | "failed"
    status: str = ""             # one sentence for the queue card
    progress: float = 0.0        # 0..1 within the current stage
    created: str = ""            # ISO 8601
    updated: str = ""
    copied_at: str = ""          # when the NAS copy was verified; drives the local clean-up
    dismissed: bool = False      # moved out of the queue by hand; it stays in History
    av1: str = ""                # "" follows Settings | "cpu" / "gpu": make one with that encoder | "off": none
    encodes: list[EncodeJob] = field(default_factory=list)


def to_dict(obj: Any) -> Any:
    return dataclasses.asdict(obj)


def _build(cls, data):
    if data is None:
        return None
    hints = {f.name: f for f in dataclasses.fields(cls)}
    kwargs = {}
    for k, v in data.items():
        if k not in hints:
            continue
        kwargs[k] = v
    return cls(**kwargs)


def job_from_dict(d: dict) -> Job:
    d = dict(d)
    titles = [title_from_dict(t) for t in d.pop("titles", [])]
    matches = []
    for m in d.pop("matches", []):
        m = dict(m)
        ep = m.pop("episode", None)
        matches.append(_build(Match, {**m, "episode": _build(Episode, ep)}))
    files = []
    for f in d.pop("files", []):
        f = dict(f)
        hdr = f.pop("hdr", None)
        files.append(_build(RippedFile, {**f, "hdr": _build(HdrInfo, hdr)}))
    policy = _build(HdrPolicy, d.pop("hdr_policy", None)) or HdrPolicy()
    progress = [_build(TitleProgress, p) for p in d.pop("titles_progress", [])]
    encodes = [_build(EncodeJob, e) for e in d.pop("encodes", None) or []]
    job = _build(Job, d)
    job.titles, job.matches, job.files, job.hdr_policy = titles, matches, files, policy
    job.titles_progress, job.encodes = progress, encodes
    return job


def title_from_dict(d: dict) -> Title:
    d = dict(d)
    streams = [_build(Stream, s) for s in d.pop("streams", [])]
    t = _build(Title, d)
    t.streams = streams
    return t
