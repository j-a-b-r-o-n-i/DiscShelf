"""Check and rename the files already in a TV library, so their names follow a naming.Scheme.

Nothing here changes a file until apply() is called. Scanning only lists folders (no stat
calls except for files that share an episode), because a library is usually a network share
with thousands of files. A show is checked in three steps:

    scan = scan_show(root, "Bluey (2018)")          # what is on disk, with each file's SxxEyy
    look = lookup(search, episodes, scan.folder)    # the show and its episode list on TVmaze
    plan = plan_show(scan, look, scheme, fixes, readings)   # problems, and old name → new name

apply() renames with os.rename (so only within one filesystem), never overwrites a file,
and writes an undo log; undo() reverses it.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Callable

from . import naming
from .models import Episode, Job

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".mov", ".wmv", ".mpg", ".mpeg", ".webm"}
SET_ASIDE = "Set aside"
# Plex skips every file in a folder holding this, so a set-aside duplicate isn't listed again.
PLEXIGNORE = (".plexignore", "*\n")
FIXES = ("version", "extra", "aside")

# S01E02, s01e02, S01 E02, S01E02-E03, S01E02E03, S01E02-03, S1959E20; 1x02, 1x02-1x03, 1x02-03.
_SXE = re.compile(r"(?<![A-Za-z0-9])[Ss](\d{1,4})[ ._-]?[Ee](\d{1,3})"
                  r"(?:(?:[ ._]?-[ ._]?|[ ._]?)[Ee](\d{1,3})|-(\d{1,3})(?![\dp]))?(?![\d])")
_NXN = re.compile(r"(?<![A-Za-z0-9])(\d{1,2})[xX](\d{2,3})(?:-(?:\d{1,2}[xX])?(\d{2,3}))?(?![\dp])")
_BARE_E = re.compile(r"(?<![A-Za-z0-9])[Ee](\d{1,3})(?![\d])")
_COPY = re.compile(r"\s\((\d)\)$")          # the " (2)" dedupe used to add
_SEASON_DIR = re.compile(r"^(?:season|series|s)[ ._-]?(\d{1,4})\b", re.IGNORECASE)
_QUALITY = re.compile(r"(?<![A-Za-z0-9])(2160p|1080p|720p|576p|480p|4k|uhd)(?![A-Za-z0-9])", re.IGNORECASE)
_SIDECAR_REST = re.compile(r"^(?:\.[^/]+|-(?:thumb|fanart|poster|banner)\.[A-Za-z0-9]+)$", re.IGNORECASE)
_YEAR = re.compile(r"\((\d{4})\)\s*$")


def _fold(s: str) -> str:
    return s.casefold()


def _key(s: str) -> str:
    """A name compared without case, punctuation or spacing."""
    return re.sub(r"[^0-9a-z]+", "", s.casefold())


# --------------------------------------------------------------------------- parsing

@dataclass
class Parsed:
    season: int | None = None
    number: int | None = None
    end: int | None = None        # last episode of a double episode
    copy: int = 0                 # 2 for "… (2).mkv"
    code_end: int = 0             # where the SxxEyy code ends in the stem


def parse_name(name: str) -> Parsed:
    """The episode code in one file name (not a path). Unparsed names give season None."""
    stem = os.path.splitext(name)[0]
    out = Parsed()
    m = _COPY.search(stem)
    if m:
        out.copy = int(m.group(1))
        stem = stem[:m.start()]
    sm = _SXE.search(stem)
    if sm:
        out.season, out.number = int(sm.group(1)), int(sm.group(2))
        end = sm.group(3) or sm.group(4)
        out.code_end = sm.end()
    else:
        nm = _NXN.search(stem)
        if not nm:
            return Parsed(copy=out.copy)
        out.season, out.number = int(nm.group(1)), int(nm.group(2))
        end = nm.group(3)
        out.code_end = nm.end()
    if end is not None and int(end) > out.number:
        out.end = int(end)
    return out


def season_of_folder(name: str) -> int | None:
    """"Season 01", "Season 1 (1991)", "S02", "Series 3" → the number; "Specials" → 0."""
    if name.strip().casefold() in ("specials", "special"):
        return 0
    m = _SEASON_DIR.match(name.strip())
    return int(m.group(1)) if m else None


def quality_in(name: str) -> str:
    m = _QUALITY.search(name)
    if not m:
        return ""
    q = m.group(1).lower()
    return "2160p" if q in ("4k", "uhd") else q


def show_query(folder: str) -> tuple[str, int | None, bool]:
    """(show name, year or None, is a release folder) from a show folder's name.

    "Bluey (2018)" → ("Bluey", 2018, False). A release folder such as
    "Star.Trek.Discovery.S01-S03.1080p.Bluray" gives ("Star Trek Discovery", None, True):
    its name is cut at the first season, year or quality, and TVmaze names the show."""
    name = folder.strip()
    m = _YEAR.search(name)
    if m and not re.search(r"[._]|\bS\d\d|\d{3,4}p", name[:m.start()]):
        return name[:m.start()].strip(), int(m.group(1)), False
    release = bool(re.search(r"\w\.\w.*\.\w|\bS\d{2}\b|\bS\d{2}-S\d{2}|\d{3,4}p|\bSeasons?\b|\bComplete\b", name,
                             re.IGNORECASE))
    if not release:
        return name, None, False
    s = re.sub(r"[._]+", " ", name)
    year = None
    ym = re.search(r"\((\d{4})(?:[-–]\d{2,4})?\)|\b(19\d\d|20\d\d)\b", s)
    cut = re.search(r"\(|\[|\bS\d{1,2}\b|\bS\d{2}(?:E|-)|\bSeasons?\b|\bComplete\b|\d{3,4}p|"
                    r"\b(?:19|20)\d\d\b| - ", s, re.IGNORECASE)
    if ym:
        year = int(ym.group(1) or ym.group(2))
    base = s[:cut.start()] if cut and cut.start() > 0 else s
    return re.sub(r"\s+", " ", base).strip(" -"), year, True


# --------------------------------------------------------------------------- scanning

@dataclass
class LibFile:
    rel: str                      # path under the library folder, "/" between parts
    season: int | None = None
    number: int | None = None
    end: int | None = None
    copy: int = 0
    code_end: int = 0
    folder_season: int | None = None   # from the season folder it sits in, None when not in one
    quality: str = ""             # a quality written in its name, e.g. "1080p"
    sidecars: list[str] = field(default_factory=list)   # subtitles, .nfo, -thumb.jpg with the same stem
    size: int = 0                 # only read for files that share an episode

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]

    @property
    def code(self) -> str:
        if self.season is None:
            return ""
        c = f"S{self.season:02d}E{self.number:02d}"
        return c + (f"-E{self.end:02d}" if self.end else "")


@dataclass
class ShowScan:
    folder: str                   # the show folder's name under the library folder
    files: list[LibFile] = field(default_factory=list)       # episodes, or files that should be
    extras: list[str] = field(default_factory=list)          # rels of videos in extras folders, left alone
    aside: list[str] = field(default_factory=list)           # rels already in the Set aside folder


def list_shows(root: str) -> list[str]:
    """Show folders in a TV library folder, by name. One directory listing, nothing more."""
    with os.scandir(root) as it:
        names = [e.name for e in it if not e.name.startswith(".") and e.is_dir()]
    return sorted(names, key=_fold)


def _is_extras(name: str) -> bool:
    return _fold(name) in {_fold(f) for f in naming.EXTRAS_FOLDERS} or _fold(name) in (
        "extras", "featurette", "bonus", "behind the scenes", "deleted and extended scenes")


def scan_show(root: str, folder: str, cancel: Callable[[], bool] | None = None) -> ShowScan:
    """Every video under root/folder, with its episode code. Read-only.

    Files in an extras folder (Featurettes, Deleted Scenes, …) or the Set aside folder are
    listed but never renamed. os.scandir gives file types without a stat on most systems,
    which matters on a network share."""
    out = ShowScan(folder)
    top = os.path.join(root, folder)
    stack = [("", None, False)]           # (rel dir under the show folder, its season, inside extras)
    while stack:
        if cancel and cancel():
            break
        rdir, season, extras = stack.pop()
        here = os.path.join(top, rdir) if rdir else top
        try:
            with os.scandir(here) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        videos, others = [], []
        for e in entries:
            if e.name.startswith("."):
                continue
            rel = f"{rdir}/{e.name}" if rdir else e.name
            try:
                is_dir = e.is_dir()
            except OSError:
                continue
            if is_dir:
                sub_season = season_of_folder(e.name)
                aside = not rdir and _fold(e.name) == _fold(SET_ASIDE)
                stack.append((rel, sub_season if sub_season is not None else season,
                              extras or aside or _is_extras(e.name)))
                if aside:
                    out.aside.append(rel)
            elif os.path.splitext(e.name)[1].lower() in VIDEO_EXTS:
                videos.append(e.name)
            else:
                others.append(e.name)
        for v in videos:
            rel = f"{folder}/{rdir}/{v}" if rdir else f"{folder}/{v}"
            if extras:
                out.extras.append(rel)
                continue
            p = parse_name(v)
            if p.season is None and season is not None:
                # "E05.mkv" or "Show.E05.1080p.mkv" in a season folder: the folder gives the season.
                bare = _BARE_E.search(os.path.splitext(v)[0])
                if bare:
                    p.season, p.number, p.code_end = season, int(bare.group(1)), bare.end()
            f = LibFile(rel, p.season, p.number, p.end, p.copy, p.code_end, season, quality_in(v))
            out.files.append(f)
        # Sidecars go with the video whose stem is the longest prefix of their name.
        stems = sorted(((os.path.splitext(v)[0], v) for v in videos), key=lambda s: -len(s[0]))
        by_name = {f.name: f for f in out.files if f.rel.rsplit("/", 1)[0] == (f"{folder}/{rdir}" if rdir else folder)}
        for o in others:
            for stem, v in stems:
                if o.startswith(stem) and _SIDECAR_REST.match(o[len(stem):]) and v in by_name:
                    by_name[v].sidecars.append(f"{folder}/{rdir}/{o}" if rdir else f"{folder}/{o}")
                    break
    out.files.sort(key=lambda f: (f.season if f.season is not None else 9999, f.number or 0, f.rel))
    return out


# --------------------------------------------------------------------------- TVmaze

@dataclass
class Lookup:
    name: str                     # the show's name for file names
    year: int | None
    show_id: int | None = None
    hits: list = field(default_factory=list)       # every TVmaze hit, best first
    episodes: list[Episode] = field(default_factory=list)
    note: str = ""                # a sentence when the show couldn't be found or is ambiguous
    ambiguous: bool = False


def pick_show(hits: list, name: str, year: int | None):
    """(the hit, how sure) for a folder name: "exact" (the name, and the year when there is
    one), "several" (more than one show has exactly this name), or "loose" (no exact name;
    the first show whose name contains it, else TVmaze's best match)."""
    want = _key(name)
    same = [h for h in hits if _key(h.name) == want or _key(re.sub(r"\s*\([^)]*\)$", "", h.name)) == want]
    if year:
        dated = [h for h in same if h.year == year] or [h for h in hits if h.year == year and want in _key(h.name)]
        if dated:
            return dated[0], "exact"
    if same:
        return same[0], "several" if len(same) > 1 and not year else "exact"
    near = [h for h in hits if want and want in _key(h.name)]
    return ((near or hits)[0] if hits else None), "loose"


def lookup(search: Callable[[str], list], episodes: Callable[[int, int | None], list], folder: str,
           show_id: int | None = None) -> Lookup:
    """Find a show folder's show on TVmaze and its episodes. show_id picks a hit by hand."""
    name, year, release = show_query(folder)
    hits = list(search(name) or [])
    if not hits:
        # "Power Rangers (1994-2022)", "One Piece 2023", "Show (1993) --Group--": search for the bare name.
        m = re.match(r"(.+?)\s*(?:[(\[]|\s-{1,2}\s*|\s--|$)", name)
        bare = m.group(1) if m else name
        ym = re.search(r"(?:\(|\s)((?:19|20)\d\d)\b", name)
        bare = re.sub(r"\s+(?:19|20)\d\d$", "", bare).strip()
        if bare and bare != name:
            name, year, release = bare, year or (int(ym.group(1)) if ym else None), True
            hits = list(search(name) or [])
    hit, sure = pick_show(hits, name, year)
    if show_id is not None:
        hit = next((h for h in hits if h.id == show_id), hit)
        sure = "exact"
    if hit is None:
        return Lookup(name, year, hits=hits, note=f"TVmaze has no show called “{name}”. Rename the folder to the "
                                                  "show's name and year, then check it again.")
    # A folder Disc Shelf or Plex named keeps its own name; a release folder takes TVmaze's.
    picked = show_id is not None and _key(hit.name) != _key(name)
    out = Lookup(hit.name if release or sure == "loose" or picked else name,
                 year if year and not picked else hit.year, hit.id, hits)
    if sure == "several":
        out.ambiguous = True
        out.note = (f"{sum(_key(h.name) == _key(name) for h in hits)} shows on TVmaze are called “{name}”. "
                    f"This uses the one from {hit.year or 'an unknown year'}; pick another if it's wrong.")
    elif sure == "loose" and not release:
        out.ambiguous = True
        out.note = (f"TVmaze has no show called exactly “{name}”, so this uses “{hit.name}”. "
                    "Pick another if it's wrong.")
    out.episodes = list(episodes(hit.id, None) or [])
    return out


class Throttle:
    """At most one call every `gap` seconds: TVmaze allows about 20 calls per 10 seconds."""

    def __init__(self, gap: float = 0.6):
        self.gap, self.last = gap, 0.0

    def __call__(self, fn):
        def wrapped(*a):
            wait = self.last + self.gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            return fn(*a)
        return wrapped


# --------------------------------------------------------------------------- title-card readings

@dataclass
class Reading:
    """What Disc Shelf's own records say about a file it copied."""
    source: str                   # "card" | "discdb"
    text: str = ""                # what the title card read, when it was read
    version: str = ""             # the match's version, e.g. "Short cut"
    seconds: int = 0              # the title's running time
    disc: str = ""                # e.g. "Disc 1, title 26"


def _norm_path(p: str) -> str:
    return _fold(os.path.normpath(p))


def readings_from_jobs(jobs: list[Job]) -> dict[str, Reading]:
    """Normalised NAS path → Reading, for every copied file whose match came from a title card or TheDiscDB."""
    out: dict[str, Reading] = {}
    for job in jobs:
        by = {m.title_index: m for m in job.matches}
        titles = {t.index: t for t in job.titles}
        for f in job.files:
            m = by.get(f.title_index)
            if not f.dest_rel or not job.destination or m is None or m.source not in ("card", "discdb"):
                continue
            t = titles.get(f.title_index)
            said = re.search(r"reads “([^”]+)”", m.note or "")
            disc = f"Disc {job.disc_number}, title {f.title_index}" if job.disc_number else f"Title {f.title_index}"
            out[_norm_path(os.path.join(job.destination, f.dest_rel))] = Reading(
                m.source, said.group(1) if said else "", m.version, t.duration_s if t else 0, disc)
    return out


# --------------------------------------------------------------------------- planning

@dataclass
class Rename:
    src: str                      # rel under the library folder
    dst: str
    why: str                      # one short sentence for the table
    kind: str = "scheme"          # "scheme" | "season" | "byname" | "version" | "extra" | "aside"
    sidecars: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Problem:
    kind: str                     # "duplicate" | "no_number" | "unknown_episode" | "wrong_season" | "clash" | "show"
    text: str
    files: list[str] = field(default_factory=list)
    fixable: bool = False         # a duplicate: offers keep as version / extra / set aside
    title: str = ""               # the episode's name, for a duplicate


@dataclass
class Cell:
    season: int
    number: int
    state: str                    # "ok" (card or TheDiscDB agrees) | "unread" | "dup" | "missing" | "unknown"
    tip: str
    files: list[str] = field(default_factory=list)


@dataclass
class ShowPlan:
    folder: str
    lookup: Lookup | None
    files: int = 0
    named: int = 0                # files whose name already matches the scheme
    renames: list[Rename] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    cells: list[Cell] = field(default_factory=list)
    extras: int = 0
    extra_files: list[tuple[str, str]] = field(default_factory=list)   # (rel, content) to create, e.g. .plexignore

    @property
    def target_folder(self) -> str:
        return self.renames[0].dst.split("/", 1)[0] if self.renames else self.folder


_RES_VERSION = re.compile(r"(?:2160p|1080p|720p|576p|480p)(?: AV1)?", re.IGNORECASE)


def _version_of(f: LibFile, title: str) -> str:
    """A version already written in a file's name, so a rename keeps it: " - 2160p" or
    " - 2160p AV1" in any name, and "… - Name - Short cut" in a name Disc Shelf or Plex made.
    Release names ("Show.S01E01.1080p.BluRay.x264") have none."""
    stem = _COPY.sub("", os.path.splitext(f.name)[0]) if f.copy else os.path.splitext(f.name)[0]
    rest = stem[f.code_end:].strip(" -._")
    named = False
    for cand in (title, naming.safe_name(title)):
        if cand and _fold(rest).startswith(_fold(cand)):
            rest, named = rest[len(cand):].strip(" -"), True
            break
    rest = re.sub(r"\s*\[[^\]]*\]$", "", rest).strip(" -")
    # Only a Plex-style name ("Show (Year) - S01E01 - …") carries a version; in a release
    # name ("Show.1x05.720p") the same word is just the quality.
    own = bool(re.fullmatch(r".+ - [Ss]\d+[Ee]\d+(?:-?[Ee]\d+)?", stem[:f.code_end]))
    if own and _RES_VERSION.fullmatch(rest):
        return rest
    return rest if own and named else ""


def _by_name(name: str, by_title: dict[str, list[Episode]]) -> tuple[Episode | None, int | None]:
    """(episode, last episode of a double) whose name is the file's name, without a leading
    number ("124. The Inner Light") or trailing tags. (None, None) when no episode, or more
    than one, has that name."""
    stem = os.path.splitext(name)[0]
    stem = re.sub(r"^\s*\d{1,4}\s*[.)_-]\s*", "", stem)
    stem = re.sub(r"\s*[\[(][^\])]*[\])]\s*$", "", stem)
    k = _key(stem)
    if not k:
        return None, None
    hits = by_title.get(k, [])
    if len(hits) == 1:
        return hits[0], None
    parts = sorted(hits or by_title.get("double:" + k, []), key=lambda e: (e.season, e.number))
    if len(parts) == 2 and parts[0].season == parts[1].season and parts[1].number == parts[0].number + 1:
        return parts[0], parts[1].number
    return None, None


def _len(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def plan_show(scan: ShowScan, look: Lookup | None, scheme: naming.Scheme | None = None,
              fixes: dict[str, str] | None = None, readings: dict[str, Reading] | None = None,
              records_root: str = "", root: str = "") -> ShowPlan:
    """What would change in one show folder, and what needs a decision.

    fixes maps a duplicate's rel to "version" (the default), "extra" or "aside". readings
    come from readings_from_jobs, keyed under records_root (the destination's path as the
    jobs saved it). root is where the library is on this machine, used only to read the
    size of files that share an episode."""
    scheme = scheme or naming.Scheme()
    fixes = fixes or {}
    readings = readings or {}
    files = [replace(f, sidecars=list(f.sidecars)) for f in scan.files]   # planning fills in guesses
    originals = {f.rel: f for f in scan.files}
    plan = ShowPlan(scan.folder, look, files=len(files), extras=len(scan.extras))
    if look is None or look.show_id is None:
        plan.problems.append(Problem("show", look.note if look else "This show hasn't been looked up on TVmaze yet."))
        return plan
    if look.note:
        plan.problems.append(Problem("show", look.note))
    eps = {(e.season, e.number): e for e in look.episodes}
    folder = naming.show_folder(look.name, look.year, scheme)

    def reading(f: LibFile) -> Reading | None:
        return readings.get(_norm_path(os.path.join(records_root, f.rel))) if records_root else None

    def title_of(f: LibFile) -> str:
        e = eps.get((f.season, f.number))
        return e.title if e else ""

    # Group files that hold the same episode with the same version: those are duplicates.
    groups: dict[tuple, list[LibFile]] = {}
    versions: dict[str, str] = {}
    by_title: dict[str, list[Episode]] = {}
    for e in look.episodes:
        by_title.setdefault(_key(e.title), []).append(e)
    for e in look.episodes:      # "Encounter at Farpoint (1)" and "(2)" are one file called "Encounter at Farpoint"
        base = _key(naming._PART.sub("", e.title))
        if base and base != _key(e.title):
            by_title.setdefault("double:" + base, []).append(e)
    matched_by_name: set[str] = set()
    for f in files:
        if f.season is None:
            e, end = _by_name(f.name, by_title)
            if e is not None:
                f.season, f.number, f.end, f.code_end = e.season, e.number, end, 0
                matched_by_name.add(f.rel)
        if f.season is None:
            plan.problems.append(Problem("no_number", f"“{f.name}” has no season and episode number, so it's left "
                                                      "as it is. Rename it by hand or move it to an extras folder.",
                                         [f.rel]))
            continue
        if (f.season, f.number) not in eps:
            what = ("Specials aren't numbered on TVmaze" if f.season == 0
                    else f"TVmaze has no {f.code} for {look.name}")
            plan.problems.append(Problem("unknown_episode", f"{what}, so “{f.name}” is left as it is.", [f.rel]))
            continue
        versions[f.rel] = _version_of(f, title_of(f))
        groups.setdefault((f.season, f.number, _fold(versions[f.rel])), []).append(f)
        if f.folder_season is not None and f.folder_season != f.season:
            plan.problems.append(Problem("wrong_season", f"“{f.name}” is {f.code} but sits in the Season "
                                                         f"{f.folder_season} folder. The rename moves it.", [f.rel]))

    targets: list[tuple[LibFile, str, str, str]] = []      # (file, dst, why, kind)
    for (season, number, _v), fs in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        e = eps[(season, number)]
        if len(fs) > 1 and root:
            for f in fs:
                if not f.size:
                    try:
                        f.size = os.stat(os.path.join(root, f.rel)).st_size
                    except OSError:
                        f.size = 0
                    originals[f.rel].size = f.size     # kept, so a new scheme doesn't read it again
        # The file to keep as the episode: no " (2)", then the biggest.
        fs = sorted(fs, key=lambda f: (f.copy, -f.size, f.rel))
        for k, f in enumerate(fs):
            ext = os.path.splitext(f.name)[1].lstrip(".").lower() or "mkv"
            end_t = eps[(season, f.end)].title if f.end and (season, f.end) in eps else ""
            version = versions[f.rel]
            why, kind = "Follows the scheme", "scheme"
            if f.folder_season is not None and f.folder_season != season:
                why, kind = f"Moved to the Season {season} folder", "season"
            if f.rel in matched_by_name:
                why, kind = f"No episode number; its name is {f.code} on TVmaze", "byname"
            r = reading(f)
            if kind == "scheme" and r and r.text:
                why = f"Follows the scheme · card reads “{r.text}”"
            if k == 0:
                dst = naming.episode_relpath(look.name, look.year, season, number, f.end, version, ext, e.title,
                                             end_t, f.quality, scheme)
                targets.append((f, dst, why, kind))
                continue
            label = (r.version if r and r.version else "") or f"Version {k + 1}"
            fix = fixes.get(f.rel, "version")
            heard = ""
            if r and r.text:
                heard = f" Its title card reads “{r.text}”, so it is {e.title}."
            elif r and r.source == "discdb":
                heard = f" TheDiscDB lists it as {e.title}."
            long = f" It's {_len(r.seconds)} long." if r and r.seconds else ""
            plan.problems.append(Problem(
                "duplicate", f"Two files are {f.code} {e.title}: “{fs[0].name}” and “{f.name}”.{heard}{long} "
                             "Most likely one is a second cut of the episode.", [fs[0].rel, f.rel], fixable=True,
                title=e.title))
            if fix == "extra":
                dst = naming.extra_relpath(folder, f"{e.title} ({label})", "Featurettes", ext, scheme)
                targets.append((f, dst, f"A second {e.title}, kept as an extra", "extra"))
            elif fix == "aside":
                dst = f"{folder}/{SET_ASIDE}/{f.name}"
                targets.append((f, dst, "Set aside, not deleted. Plex skips this folder", "aside"))
                plan.extra_files.append((f"{folder}/{SET_ASIDE}/{PLEXIGNORE[0]}", PLEXIGNORE[1]))
            else:
                dst = naming.episode_relpath(look.name, look.year, season, number, f.end, label, ext, e.title,
                                             end_t, f.quality, scheme)
                targets.append((f, dst, f"A second cut of {e.title}, kept as a version", "version"))

    # Two files may not end up with one name, and a file that stays may not be overwritten.
    moving = {_fold(f.rel) for f, dst, _w, _k in targets if dst != f.rel}
    staying = {_fold(f.rel) for f in files} - moving
    seen: dict[str, str] = {}
    for f, dst, why, kind in targets:
        d = _fold(dst)
        if dst == f.rel:
            plan.named += 1
            seen[d] = f.rel
            continue
        if d in seen or (d in staying and d != _fold(f.rel)):
            plan.problems.append(Problem("clash", f"“{f.name}” would be named “{dst.rsplit('/', 1)[-1]}”, which another "
                                                  "file already has, so it's left as it is.", [f.rel]))
            continue
        seen[d] = f.rel
        new_stem = os.path.splitext(dst)[0]
        old_stem = os.path.splitext(f.rel)[0]
        side = [(s, new_stem + s[len(old_stem):]) for s in f.sidecars]
        plan.renames.append(Rename(f.rel, dst, why, kind, side))
    plan.extra_files = list(dict.fromkeys(plan.extra_files))

    # One cell per episode of every season that has a file.
    have: dict[tuple[int, int], list[LibFile]] = {}
    for fs in groups.values():
        for f in fs:
            for n in range(f.number, (f.end or f.number) + 1):
                have.setdefault((f.season, n), []).append(f)
    dup_eps = {(fs[0].season, fs[0].number) for fs in groups.values() if len(fs) > 1}
    seasons = sorted({s for s, _n in have if s != 0})
    for e in sorted(look.episodes, key=lambda e: (e.season, e.number)):
        if e.season not in seasons:
            continue
        fs = have.get((e.season, e.number), [])
        code = f"S{e.season:02d}E{e.number:02d}"
        if not fs:
            plan.cells.append(Cell(e.season, e.number, "missing", f"{code} {e.title}: no file"))
            continue
        if (e.season, e.number) in dup_eps:
            plan.cells.append(Cell(e.season, e.number, "dup", f"{code} {e.title}: {len(fs)} files",
                                   [f.rel for f in fs]))
            continue
        r = next((reading(f) for f in fs if reading(f)), None)
        if r:
            tip = f"{code} {e.title}: " + (f"title card reads “{r.text}”" if r.text else
                                          "TheDiscDB lists it" if r.source == "discdb" else "title card read")
            plan.cells.append(Cell(e.season, e.number, "ok", tip, [f.rel for f in fs]))
        else:
            plan.cells.append(Cell(e.season, e.number, "unread", f"{code} {e.title}: title card not read",
                                   [f.rel for f in fs]))
    for f in files:
        if f.season in seasons and (f.season, f.number) not in eps:
            plan.cells.append(Cell(f.season, f.number, "unknown", f"{f.code}: not on TVmaze", [f.rel]))
    return plan


def problem_count(plan: ShowPlan) -> int:
    """Problems that need a look: everything except a wrong season folder, which the rename fixes."""
    return sum(1 for p in plan.problems if p.kind != "wrong_season")


# --------------------------------------------------------------------------- renaming

class RenameError(Exception):
    """A rename couldn't be done. The message says what happened and what to do next."""


def moves_of(renames: list[Rename]) -> list[tuple[str, str]]:
    out = []
    for r in renames:
        out.append((r.src, r.dst))
        out.extend(r.sidecars)
    return out


def _exists(p: str) -> bool:
    return os.path.lexists(p)


def _same(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _write_log(path: str, log: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(log, fh, indent=1)
    os.replace(tmp, path)


def _makedirs(path: str, made: list[str], stop: str) -> None:
    missing = []
    p = path
    while p and p != stop and not os.path.isdir(p):
        missing.append(p)
        p = os.path.dirname(p)
    for d in reversed(missing):
        os.mkdir(d)
        made.append(d)


def apply(root: str, moves: list[tuple[str, str]], undo_dir: str, label: str = "",
          extra_files: list[tuple[str, str]] | None = None, progress: Callable[[float, str], None] | None = None,
          scan_folders: list[str] | None = None) -> str:
    """Rename each (src, dst) under root and return the undo log's path.

    Nothing is renamed if a destination already holds a file that isn't one of the files
    being renamed. Folders are made as needed, and folders left empty are removed. A
    rename that fails part way stops there; the log covers what was done, so undo()
    still puts it back."""
    progress = progress or (lambda _f, _t="": None)
    root = os.path.abspath(root)
    srcs = {_fold(s) for s, _d in moves}
    dsts: set[str] = set()
    for s, d in moves:
        if not _exists(os.path.join(root, s)):
            raise RenameError(f"“{s}” isn't there any more. Check the show again, then rename.")
        fd = _fold(d)
        if fd in dsts:
            raise RenameError(f"Two files would be named “{d}”. Check the show again, then rename.")
        dsts.add(fd)
        full = os.path.join(root, d)
        if _exists(full) and fd not in srcs and not _same(full, os.path.join(root, s)):
            raise RenameError(f"“{d}” already exists, so nothing was renamed. Move or rename that file, "
                              "then check the show again.")
    os.makedirs(undo_dir, exist_ok=True)
    stamp = dt.datetime.now().astimezone()
    path = os.path.join(undo_dir, stamp.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6] + ".json")
    log = {"created": stamp.isoformat(timespec="seconds"), "root": root, "label": label, "ops": [],
           "dirs_made": [], "files_made": [], "dirs_removed": [], "scan_folders": scan_folders or [],
           "undone": False}
    _write_log(path, log)

    def mv(a: str, b: str) -> None:
        fa, fb = os.path.join(root, a), os.path.join(root, b)
        if _exists(fb) and not _same(fa, fb):
            raise RenameError(f"“{b}” appeared while renaming, so renaming stopped. {len(log['ops'])} renames were "
                              "done; choose Undo last rename to put them back.")
        _makedirs(os.path.dirname(fb), log["dirs_made"], root)
        os.rename(fa, fb)
        log["ops"].append([a, b])
        _write_log(path, log)

    # A file whose new name is another file's old name (or the same name in other letter
    # case) goes through a temporary name, so nothing is ever in the way.
    via = [(s, d) for s, d in moves if _fold(d) in srcs]
    direct = [(s, d) for s, d in moves if _fold(d) not in srcs]
    temps = []
    total = len(moves) + len(via)
    done = 0
    try:
        for s, d in via:
            head, tail = s.rsplit("/", 1) if "/" in s else ("", s)
            t = (f"{head}/" if head else "") + f".discshelf-{uuid.uuid4().hex[:8]}-{tail}"
            mv(s, t)
            temps.append((t, d))
            done += 1
            progress(done / total, f"Renaming {done} of {total}")
        for s, d in direct + temps:
            mv(s, d)
            done += 1
            progress(done / total, f"Renaming {done} of {total}")
        for rel, content in extra_files or []:
            full = os.path.join(root, rel)
            if not _exists(full) and os.path.isdir(os.path.dirname(full)):
                with open(full, "w") as fh:
                    fh.write(content)
                log["files_made"].append(rel)
        # Folders the renames emptied, deepest first, never root itself.
        dirs = sorted({os.path.dirname(os.path.join(root, s)) for s, _d in moves}, key=len, reverse=True)
        for d in dirs:
            while d.startswith(root + os.sep) and os.path.isdir(d):
                try:
                    os.rmdir(d)            # only succeeds on an empty folder
                except OSError:
                    break
                log["dirs_removed"].append(os.path.relpath(d, root))
                d = os.path.dirname(d)
    finally:
        _write_log(path, log)
    return path


def last_log(undo_dir: str) -> str | None:
    """The newest undo log that hasn't been undone."""
    try:
        names = sorted((n for n in os.listdir(undo_dir) if n.endswith(".json")), reverse=True)
    except OSError:
        return None
    for n in names:
        try:
            with open(os.path.join(undo_dir, n)) as fh:
                if not json.load(fh).get("undone"):
                    return os.path.join(undo_dir, n)
        except (OSError, ValueError):
            continue
    return None


def read_log(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def undo(path: str, progress: Callable[[float, str], None] | None = None) -> tuple[int, list[str]]:
    """Put back every rename in one undo log. Returns (renames undone, one sentence per one that couldn't be)."""
    progress = progress or (lambda _f, _t="": None)
    log = read_log(path)
    root = log["root"]
    ops = list(reversed(log.get("ops", [])))
    skipped: list[str] = []
    n = 0
    for k, (a, b) in enumerate(ops):
        fa, fb = os.path.join(root, a), os.path.join(root, b)
        if not _exists(fb):
            skipped.append(f"“{b}” isn't there any more, so it couldn't be named “{a}” again.")
            continue
        if _exists(fa) and not _same(fa, fb):
            skipped.append(f"“{a}” exists again, so “{b}” was left as it is.")
            continue
        os.makedirs(os.path.dirname(fa), exist_ok=True)
        os.rename(fb, fa)
        n += 1
        progress((k + 1) / max(1, len(ops)), f"Putting back {k + 1} of {len(ops)}")
    for rel in log.get("files_made", []):
        try:
            os.remove(os.path.join(root, rel))
        except OSError:
            pass
    for d in sorted(log.get("dirs_made", []), key=len, reverse=True):
        try:
            os.rmdir(d)
        except OSError:
            pass
    log["undone"] = True
    _write_log(path, log)
    return n, skipped

