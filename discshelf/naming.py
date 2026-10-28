"""File and folder names media servers read, following Plex's guides for TV shows, movies and local extras.

TV:     Show (2019)/Season 01/Show (2019) - S01E01 - Episode name.mkv
        a double episode is S01E01-E02, a second cut of an episode adds " - Short cut"
Movie:  Title (2012)/Title (2012).mkv, another version adds " - 2160p" before the extension,
        an edition is "Title (2012) {edition-Director's Cut}.mkv"
Extras: <show or movie folder>/Featurettes/Name.mkv (one of the folder names Plex knows)

Plex reads only the show, the SxxEyy code and the year; it ignores everything after the
code, so the episode name, a version and a "[1080p]" tag are for people reading the folder.
Two files with the same code in one season folder become two versions of one episode.
Which of these parts a name has is a Scheme, set in Settings → File names.
"""
from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass

from .models import Job, Match

EXTRAS_FOLDERS = ("Behind The Scenes", "Deleted Scenes", "Featurettes", "Interviews",
                  "Scenes", "Shorts", "Trailers", "Other")

_ILLEGAL = re.compile(r'[<>"/\\|?*\x00-\x1f]')
_NEVER = re.compile(r"[/\x00]")       # can't be in a file name on any system


@dataclass(frozen=True)
class Scheme:
    """Which parts a file name has. The defaults are Plex's own examples, with the episode name."""
    episode: str = "name"        # "name": S01E01 - Episode name | "code": S01E01 | "name_quality": … [1080p]
    show_year: bool = True       # "Bluey (2018)" rather than "Bluey"
    season_pad: bool = True      # "Season 01" rather than "Season 1"
    film: str = "year"           # "year" | "edition": … {edition-…} | "edition_quality": … [2160p]
    safe_chars: bool = True      # replace characters a Windows share can't store

    @classmethod
    def from_config(cls, cfg) -> "Scheme":
        d = cls()
        return cls(episode=getattr(cfg, "name_episode", d.episode) or d.episode,
                   show_year=bool(getattr(cfg, "name_show_year", d.show_year)),
                   season_pad=bool(getattr(cfg, "name_season_pad", d.season_pad)),
                   film=film_choice(cfg),
                   safe_chars=bool(getattr(cfg, "name_safe_chars", d.safe_chars)))


DEFAULT = Scheme()


def film_choice(cfg) -> str:
    """The film name choice in effect. The {edition-…} tag is Plex's own: Jellyfin lists two files
    with different tags as two films. So with another media server (or none) the choice is name
    and year, which names an edition as a version (" - Special Edition"), and Jellyfin lists
    those under the one film."""
    film = getattr(cfg, "name_film", "year") or "year"
    if film.startswith("edition") and getattr(cfg, "media_server", "plex") != "plex":
        return "year"
    return film


def safe_name(s: str) -> str:
    """A name that is legal on an SMB or Windows share. ":" becomes " -" so "Alien: Romulus" reads naturally."""
    s = re.sub(r"\s*:\s*", " - ", s or "")
    s = _ILLEGAL.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Windows drops trailing dots and spaces, which would make the copy's name differ.
    return s.rstrip(". ").strip()


def clean(s: str, scheme: Scheme | None = None) -> str:
    """s as part of a file name under the scheme. Without safe characters, "/" and NUL still go."""
    if (scheme or DEFAULT).safe_chars:
        return safe_name(s)
    return re.sub(r"\s+", " ", _NEVER.sub("", s or "")).strip()


def show_folder(show: str, year: int | None = None, scheme: Scheme | None = None) -> str:
    scheme = scheme or DEFAULT
    name = clean(show, scheme)
    return f"{name} ({year})" if year and scheme.show_year else name


def season_folder(season: int, scheme: Scheme | None = None) -> str:
    return f"Season {season:02d}" if (scheme or DEFAULT).season_pad else f"Season {season}"


def quality_label(resolution: str) -> str:
    """"3840x2160" → "2160p", "1920x1080" → "1080p", "720x480" → "480p"; "" when unknown.
    The width decides too, so a letterboxed 1920x800 film is still 1080p."""
    m = re.match(r"\s*(\d+)\s*x\s*(\d+)", resolution or "")
    if not m:
        return ""
    w, h = int(m.group(1)), int(m.group(2))
    if w >= 3000 or h >= 1600:
        return "2160p"
    if w >= 1800 or h >= 1000:
        return "1080p"
    if w >= 1200 or h >= 700:
        return "720p"
    return f"{h}p"


def title_quality(job: Job, title_index: int) -> str:
    t = next((t for t in job.titles if t.index == title_index), None)
    v = next((s for s in (t.streams if t else []) if s.kind == "video"), None)
    return quality_label(v.resolution) if v else ""


_PART = re.compile(r"\s*(?:\(\s*(?:part\s*)?\w+\s*\)|[,:-]?\s*part\s+\w+)\s*$", re.IGNORECASE)


def double_title(first: str, second: str) -> str:
    """One name for a file that holds two episodes: "Encounter at Farpoint" for
    "Encounter at Farpoint (1)" and "(2)", else "Name 1 & Name 2"."""
    first, second = (first or "").strip(), (second or "").strip()
    if not second or second == first:
        return first
    if not first:
        return second
    a, b = _PART.sub("", first), _PART.sub("", second)
    if a and a.casefold() == b.casefold():
        return a
    return f"{first} & {second}"


def _ver(version: str, scheme: Scheme | None = None) -> str:
    v = clean(version, scheme)
    return f" - {v}" if v else ""


def existing_show_folder(names: list[str], show: str, year: int | None,
                         scheme: Scheme | None = None) -> str | None:
    """The folder among names (a destination's listing) that already holds this show, so a new
    rip joins it instead of starting a second folder. It matches the show's name without
    regard to case, bare or with the show's year; a show with no year known also matches
    "Name (any year)". None when there's no such folder."""
    bases = {clean(show, scheme).casefold(), safe_name(show).casefold()} - {""}
    for n in names:
        f = n.casefold().strip()
        if f in bases:
            return n
        m = re.fullmatch(r"(.+?) \((\d{4})\)", f)
        if m and m.group(1) in bases and (year is None or int(m.group(2)) == year):
            return n
    return None


def episode_relpath(show: str, year: int | None, season: int, number: int, episode_end: int | None = None,
                    version: str = "", ext: str = "mkv", title: str = "", end_title: str = "",
                    quality: str = "", scheme: Scheme | None = None, show_dir: str = "", prefix: str = "") -> str:
    """Show (Year)/Season 01/Show (Year) - S01E01 - Episode name - Version [1080p].ext

    The episode name is left out when it isn't known, and the quality when it isn't known
    or the scheme has no quality. A version equal to the quality tag isn't repeated.
    show_dir puts the file in a show folder that already exists instead of the scheme's,
    and its name starts the file name too unless prefix says otherwise."""
    scheme = scheme or DEFAULT
    folder = show_dir or show_folder(show, year, scheme)
    code = f"S{season:02d}E{number:02d}"
    double = episode_end is not None and episode_end != number
    if double:
        code += f"-E{episode_end:02d}"
    name = ""
    if scheme.episode in ("name", "name_quality"):
        name = clean(double_title(title, end_title) if double else title, scheme)
    tag = quality if scheme.episode == "name_quality" and quality else ""
    if tag and version.strip().casefold() == tag.casefold():
        version = ""
    stem = f"{prefix or folder} - {code}" + (f" - {name}" if name else "") + _ver(version, scheme) + (f" [{tag}]" if tag else "")
    return f"{folder}/{season_folder(season, scheme)}/{stem}.{ext}"


def movie_relpath(title: str, year: int | None, version: str = "", ext: str = "mkv", edition: str = "",
                  quality: str = "", scheme: Scheme | None = None) -> str:
    """Title (Year)/Title (Year) {edition-Director's Cut} - 2160p [2160p].ext, each part only when
    the scheme has it. The folder never carries the edition, so every edition and version of a
    film sits side by side in one folder. Without an edition in the scheme, an edition becomes
    a version (" - Director's Cut"), which Plex also lists under the one film."""
    scheme = scheme or DEFAULT
    base = f"{clean(title, scheme)} ({year})" if year else clean(title, scheme)
    stem = base
    ed = clean(edition, scheme).replace("}", "")
    if ed and scheme.film in ("edition", "edition_quality"):
        stem += f" {{edition-{ed}}}"
    elif ed:
        version = f"{ed} {version}".strip() if version and version.casefold() != ed.casefold() else ed
    tag = quality if scheme.film == "edition_quality" and quality else ""
    if tag and version.strip().casefold() == tag.casefold():
        version = ""
    stem += _ver(version, scheme) + (f" [{tag}]" if tag else "")
    return f"{base}/{stem}.{ext}"


def extra_relpath(show_or_movie_folder: str, name: str, folder: str = "Featurettes", ext: str = "mkv",
                  scheme: Scheme | None = None) -> str:
    """An extra in a Plex extras folder. An unknown folder name falls back to "Other", which Plex always reads."""
    if folder not in EXTRAS_FOLDERS:
        folder = next((f for f in EXTRAS_FOLDERS if f.lower() == (folder or "").strip().lower()), "Other")
    stem = clean(name, scheme) or "Extra"
    return f"{show_or_movie_folder}/{folder}/{stem}.{ext}"


def relpath_for(job: Job, match: Match, layout: str, scheme: Scheme | None = None, end_title: str = "",
                show_dir: str = "") -> str | None:
    """The path under the destination folder for one match, or None when the title is skipped.

    layout is a Destination.layout: "tv" or "movie". end_title is the second episode's name
    for a title that holds two; without it the file is named after the first. show_dir is
    the show's folder when the destination already has one (existing_show_folder); episodes
    and extras then go there. Raises ValueError when the job lacks what the name needs (no
    show name, or no season for an episode).
    """
    scheme = scheme or DEFAULT
    if match.target == "skip":
        return None
    if not job.show.strip():
        raise ValueError("This disc has no show or film name yet.")
    if match.target == "extra":
        folder = show_dir if show_dir and layout != "movie" else show_folder(job.show, job.year, scheme)
        return extra_relpath(folder, match.extra_name, match.extra_folder, scheme=scheme)
    quality = title_quality(job, match.title_index)
    if layout == "movie" or match.target == "feature":
        return movie_relpath(job.show, job.year, job.version, edition=match.version, quality=quality, scheme=scheme)
    if match.target == "episode":
        if match.episode is None:
            raise ValueError(f"Title {match.title_index} is set as an episode but has no episode chosen.")
        season = match.episode.season if match.episode.season is not None else job.season
        if season is None:
            raise ValueError("This disc has no season number.")
        return episode_relpath(job.show, job.year, season, match.episode.number, match.episode_end,
                               match.version or job.version, title=match.episode.title, end_title=end_title,
                               quality=quality, scheme=scheme, show_dir=show_dir)
    raise ValueError(f"Unknown match target {match.target!r}.")


def dedupe(paths: list[str | None]) -> list[str | None]:
    """Give repeated paths " (2)", " (3)" before the extension, keeping order. None passes through.

    Comparison ignores case, because SMB shares are case-insensitive. With the episode name and
    a version in the name this is rare: it takes two titles matched to one episode with no
    version set.
    """
    seen: set[str] = set()
    out: list[str | None] = []
    for p in paths:
        if p is None:
            out.append(None)
            continue
        root, ext = posixpath.splitext(p)
        cand, k = p, 1
        while cand.lower() in seen:
            k += 1
            cand = f"{root} ({k}){ext}"
        seen.add(cand.lower())
        out.append(cand)
    return out
