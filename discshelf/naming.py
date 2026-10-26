"""Plex file and folder names, following Plex's guides for TV shows, movies and local extras.

TV:     Show (2019)/Season 01/Show (2019) - S01E01.mkv, a double episode is S01E01-E02
Movie:  Title (2012)/Title (2012).mkv, another version adds " - 2160p" before the extension
Extras: <show or movie folder>/Featurettes/Name.mkv (one of the folder names Plex knows)
"""
from __future__ import annotations

import posixpath
import re

from .models import Job, Match

EXTRAS_FOLDERS = ("Behind The Scenes", "Deleted Scenes", "Featurettes", "Interviews",
                  "Scenes", "Shorts", "Trailers", "Other")

_ILLEGAL = re.compile(r'[<>"/\\|?*\x00-\x1f]')


def safe_name(s: str) -> str:
    """A name that is legal on an SMB or Windows share. ":" becomes " -" so "Alien: Romulus" reads naturally."""
    s = re.sub(r"\s*:\s*", " - ", s or "")
    s = _ILLEGAL.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Windows drops trailing dots and spaces, which would make the copy's name differ.
    return s.rstrip(". ").strip()


def show_folder(show: str, year: int | None = None) -> str:
    name = safe_name(show)
    return f"{name} ({year})" if year else name


def _ver(version: str) -> str:
    v = safe_name(version)
    return f" - {v}" if v else ""


def episode_relpath(show: str, year: int | None, season: int, number: int, episode_end: int | None = None,
                    version: str = "", ext: str = "mkv") -> str:
    folder = show_folder(show, year)
    code = f"S{season:02d}E{number:02d}"
    if episode_end is not None and episode_end != number:
        code += f"-E{episode_end:02d}"
    return f"{folder}/Season {season:02d}/{folder} - {code}{_ver(version)}.{ext}"


def movie_relpath(title: str, year: int | None, version: str = "", ext: str = "mkv") -> str:
    folder = show_folder(title, year)
    return f"{folder}/{folder}{_ver(version)}.{ext}"


def extra_relpath(show_or_movie_folder: str, name: str, folder: str = "Featurettes", ext: str = "mkv") -> str:
    """An extra in a Plex extras folder. An unknown folder name falls back to "Other", which Plex always reads."""
    if folder not in EXTRAS_FOLDERS:
        folder = next((f for f in EXTRAS_FOLDERS if f.lower() == (folder or "").strip().lower()), "Other")
    stem = safe_name(name) or "Extra"
    return f"{show_or_movie_folder}/{folder}/{stem}.{ext}"


def relpath_for(job: Job, match: Match, layout: str) -> str | None:
    """The path under the destination folder for one match, or None when the title is skipped.

    layout is a Destination.layout: "tv" or "movie". Raises ValueError when the job lacks
    what the name needs (no show name, or no season for an episode).
    """
    if match.target == "skip":
        return None
    if not job.show.strip():
        raise ValueError("The job has no show or film name yet.")
    if match.target == "extra":
        return extra_relpath(show_folder(job.show, job.year), match.extra_name, match.extra_folder)
    if layout == "movie" or match.target == "feature":
        return movie_relpath(job.show, job.year, job.version)
    if match.target == "episode":
        if match.episode is None:
            raise ValueError(f"Title {match.title_index} is set as an episode but has no episode chosen.")
        season = match.episode.season if match.episode.season is not None else job.season
        if season is None:
            raise ValueError("The job has no season number.")
        return episode_relpath(job.show, job.year, season, match.episode.number, match.episode_end, job.version)
    raise ValueError(f"Unknown match target {match.target!r}.")


def dedupe(paths: list[str | None]) -> list[str | None]:
    """Give repeated paths " (2)", " (3)" before the extension, keeping order. None passes through.

    Comparison ignores case, because SMB shares are case-insensitive.
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
