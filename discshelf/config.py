"""Settings, kept in ~/.config/disc-shelf/config.json. The Plex token sits apart in secrets.json (0600)."""
from __future__ import annotations

import dataclasses
import json
import os
import shutil
from dataclasses import dataclass, field

from .models import HdrPolicy, _build


def config_dir() -> str:
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "disc-shelf")


def state_dir() -> str:
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "disc-shelf")


def adopt_host_data() -> list[str]:
    """Inside the Flatpak the XDG folders are ~/.var/app/<id>/…, so settings, the Plex sign-in and
    jobs from a non-Flatpak install (~/.config/disc-shelf, ~/.local/state/disc-shelf) would be
    invisible. The first time the Flatpak starts with none of its own, copy them over. Returns
    the folders created."""
    if not os.environ.get("FLATPAK_ID"):
        return []
    copied = []
    for src, dst in ((os.path.expanduser("~/.config/disc-shelf"), config_dir()),
                     (os.path.expanduser("~/.local/state/disc-shelf"), state_dir())):
        if os.path.realpath(src) == os.path.realpath(dst) or not os.path.isdir(src) or os.path.exists(dst):
            continue
        shutil.copytree(src, dst)      # copy2 keeps secrets.json readable only by the user
        copied.append(dst)
    return copied


@dataclass
class Destination:
    kind: str                    # "tv" | "movie" | "anime" | anything the user adds
    label: str                   # shown in the UI, e.g. "TV shows"
    path: str                    # local path of the NAS folder (a mounted share)
    library: str = ""            # Plex library to scan afterwards, "" for none
    layout: str = "tv"           # "tv": Show/Season NN/... | "movie": Title (Year)/...


def default_destinations() -> list[Destination]:
    """None: the first-run setup asks where TV shows and films go."""
    return []


def sample_destinations(root: str) -> list[Destination]:
    """Folders under root, for sample mode and tests."""
    return [
        Destination("tv", "TV shows", os.path.join(root, "Shows"), "TV Shows", "tv"),
        Destination("movie", "Movies", os.path.join(root, "Movies"), "Movies", "movie"),
        Destination("anime", "Anime", os.path.join(root, "Anime"), "Anime", "tv"),
    ]


@dataclass
class Config:
    destinations: list[Destination] = field(default_factory=default_destinations)
    rip_dir: str = "~/rips"
    makemkvcon: list[str] = field(default_factory=lambda: ["makemkvcon"])
    min_episode_minutes: int = 15
    audio_langs: list[str] = field(default_factory=lambda: ["eng"])
    subtitle_langs: list[str] = field(default_factory=lambda: ["eng"])
    hdr: HdrPolicy = field(default_factory=HdrPolicy)    # defaults to RAW: keep everything, convert nothing
    keep_1080p_version: bool = False
    copy_as_ripped: bool = True  # copy each title to the NAS as soon as it's ripped and its match is ready
    stream_films: bool = True    # copy a film while it rips (needs RAW HDR and MakeMKV choosing the languages)
    read_title_cards: bool = True    # read each episode's on-screen title card to match it (needs OCR)
    card_search_minutes: int = 5     # how far into a title to look for its card: 2 | 5 | 10 | 20
    rip_in_one_run: bool = True      # one MakeMKV run for the whole disc when that's quicker than one per title
    # File names (naming.Scheme). The defaults are Plex's own examples.
    name_episode: str = "name"   # "name": S01E01 - Episode name | "code": S01E01 | "name_quality": … [1080p]
    name_show_year: bool = True  # "Bluey (2018)" rather than "Bluey"
    name_season_pad: bool = True # "Season 01" rather than "Season 1"
    name_film: str = "year"      # "year": Aliens (1986) | "edition": … {edition-…} | "edition_quality": … [2160p]
    name_safe_chars: bool = True # replace characters a Windows share can't store (":" becomes " -")
    eject_after_rip: bool = True     # open the tray once a rip finishes without an error, ready for the next disc
    # The AV1 second version of a 4K disc (av1.py), made after the remux is on the NAS:
    av1_enabled: bool = True     # make one for every 4K disc; a disc can override it (Job.av1)
    av1_encoder: str = "cpu"     # "cpu" (SVT-AV1) | "gpu" (NVENC)
    av1_quality: int = 2         # 0..4, an index into av1.QUALITY
    av1_grain: bool = True       # rebuild film grain on playback (CPU encoder only)
    av1_check: bool = True       # score it with VMAF before copying; below the threshold it isn't copied
    av1_when: str = "after"      # "after": right after the copy, at low priority | "idle": only while nothing
                                 # rips | "night": only between 01:00 and 07:00 local time
    cleanup: str = "7days"       # "delete" (once Plex lists every file) | "7days" | "keep"
    verify: str = "readback"     # "readback": hash the NAS copy through the mount | "size"
    plex_url: str = ""           # e.g. http://192.168.1.20:32400; "" until the setup or Settings fills it in
    dovi_tool: str = "dovi_tool"
    mkvmerge: str = "mkvmerge"
    ffprobe: str = "ffprobe"
    ffmpeg: str = "ffmpeg"
    fpcalc: str = "fpcalc"       # Chromaprint, to skip a show's theme song when reading title cards

    def destination(self, kind: str) -> Destination | None:
        return next((d for d in self.destinations if d.kind == kind), None)

    @property
    def rip_path(self) -> str:
        return os.path.expanduser(self.rip_dir)


def load(path: str | None = None) -> Config:
    path = path or os.path.join(config_dir(), "config.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return Config()
    dests = [_build(Destination, d) for d in data.pop("destinations", [])] or default_destinations()
    hdr = _build(HdrPolicy, data.pop("hdr", None)) or HdrPolicy()
    cfg = _build(Config, data)
    cfg.destinations, cfg.hdr = dests, hdr
    return cfg


def save(cfg: Config, path: str | None = None) -> None:
    path = path or os.path.join(config_dir(), "config.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(dataclasses.asdict(cfg), fh, indent=2)
    os.replace(tmp, path)


def load_secret(name: str, path: str | None = None) -> str:
    path = path or os.path.join(config_dir(), "secrets.json")
    try:
        with open(path) as fh:
            return json.load(fh).get(name, "")
    except FileNotFoundError:
        return ""


def save_secret(name: str, value: str, path: str | None = None) -> None:
    path = path or os.path.join(config_dir(), "secrets.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        data = {}
    data[name] = value
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh)
    os.replace(path + ".tmp", path)
