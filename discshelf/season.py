"""Which titles hold which episodes of a season: on this disc, on the season's other discs, and
as files already in the library. Two of them holding the same episode is a problem, and the
titles on this disc that cause one are held back from copying until it's settled.

Most of this is plain logic over jobs and file names. library_files() lists folders and never
writes; SeasonChecks is the part the controllers share, and it reads jobs through them.
"""
from __future__ import annotations

import collections
import copy as _copy
import difflib
import os
import posixpath
import re
import time
from dataclasses import dataclass, field

from .models import Episode, Job, Match, Title

VIDEO_EXT = (".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts", ".webm", ".mov")
SHORT_CUT_S = 5              # a title this much shorter than the other is called a short cut
OTHERS_TTL = 60.0            # seconds the other discs and library listing are reused before a fresh look
_FILE_CODE = re.compile(r"(?<![A-Za-z0-9])S(\d{1,3})E(\d{1,4})(?:-?E(\d{1,4}))?", re.IGNORECASE)


@dataclass
class Holder:
    """One title or file that holds one episode."""
    season: int
    number: int
    where: str                   # "here" (this disc) | "disc" (another disc of the season) | "library"
    title_index: int | None = None
    job_id: str = ""
    disc: str = ""               # how the other disc is named in a sentence: "disc 1", "“BLUEY_S1_D1”"
    name: str = ""               # a library file's name
    version: str = ""            # Match.version: a second version doesn't clash with the first
    on_nas: bool = False         # copied and checked, or a file in the library
    length_s: int = 0
    source: str = ""             # the title's playlist or stream file
    size_bytes: int = 0
    path: str = ""               # a file that plays it, "" when none is known
    dest_rel: str = ""           # its file under the destination, once copied (a library file's own path)
    audio: list[str] = field(default_factory=list)   # audio languages, ISO 639-2, in stream order
    main: str = ""               # its disc's main audio language
    source_of_match: str = ""    # Match.source: "card" | "discdb" | "list" | "runtime" | "manual"
    disc_no: int | None = None   # the other disc's number, given or by rip order

    def phrase(self) -> str:
        """Where it is, after "on" or "in": "title 6", "disc 1 title 7", "the library"."""
        if self.where == "here":
            return f"title {self.title_index}"
        if self.where == "disc":
            return f"{self.disc}, title {self.title_index}" if "," in self.disc else f"{self.disc} title {self.title_index}"
        return "the library"

    def at(self) -> str:
        """"on disc 1 title 7", "in the library"."""
        return ("in " if self.where == "library" else "on ") + self.phrase()

    def label(self) -> str:
        """For a list or a card: "Title 6, this disc", "Disc 1, title 7", "In the library"."""
        if self.where == "here":
            return f"Title {self.title_index}, this disc"
        if self.where == "disc":
            return f"{_cap(self.disc)}, title {self.title_index}"
        return "In the library"


@dataclass
class Problem:
    """Something on the Match page that needs a look.

    kind "duplicate": titles of this disc hold an episode another title or file also holds; they
    are held back until it's settled. kind "nas": every file holding the episode is already in
    storage (another disc's, or this disc's locked titles), so the fix renames files there.
    kind "check": one title's match is unsure (titles holds it, numbers its episode, if any)."""
    season: int
    numbers: list[int]           # the episodes it covers, more than one for a two-part title
    name: str                    # the first episode's name, "" when unknown
    titles: list[int]            # this disc's titles involved
    others: list[Holder]         # the season's other discs and library files that hold it too
    text: str                    # one sentence
    kind: str = "duplicate"
    sides: list[Holder] = field(default_factory=list)   # "nas": every file holding it, this disc's included

    @property
    def key(self) -> str:
        if self.kind == "check":
            return f"check:{self.titles[0]}"
        return f"{self.kind}:{self.season}:{self.numbers[0]}"

    @property
    def code(self) -> str:
        if not self.numbers:
            return ""
        return ep_code(self.season, self.numbers[0], self.numbers[-1] if len(self.numbers) > 1 else None)

    def episode_text(self) -> str:
        return f"{self.code} “{self.name}”" if self.name else self.code

    @property
    def flagged(self) -> str:
        """"error" for a duplicate, "warn" for a check."""
        return "warn" if self.kind == "check" else "error"


@dataclass
class Cell:
    """One episode of the season grid."""
    season: int
    number: int
    title: str
    state: str                   # "free" | "here" | "disc" | "library" | "duplicate" | "check"
    mark: str                    # the short text in the cell under the number: "T6", "D1", "Lib", "×2", "? T19", ""
    problem: str = ""            # the key of the problem it shows, "" when nothing's wrong
    tip: str = ""                # one sentence for the tooltip
    holders: list[Holder] = field(default_factory=list)


@dataclass
class SeasonStatus:
    season: int | None
    problems: list[Problem]
    cells: list[Cell]
    taken: dict[tuple[int, int], list[tuple[int | None, str]]]   # (season, number) → (this disc's title or None, where)
    holders: dict[tuple[int, int], list[Holder]]                  # every holder, by episode
    summary: str                 # one sentence over the grid
    discs: int = 1               # discs of the season counted, this one included
    library_files: int = 0       # files in the season folder no job of the season copied


def ep_code(season: int, number: int, end: int | None = None) -> str:
    code = f"S{season:02d}E{number:02d}"
    return f"{code}–E{end:02d}" if end and end != number else code


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _and(parts: list[str]) -> str:
    parts = [p for p in parts if p]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1] if parts else ""


def _titles_text(tis: list[int]) -> str:
    """"Title 6", "Titles 6 and 26"."""
    tis = sorted(tis)
    return f"Title {tis[0]}" if len(tis) == 1 else "Titles " + _and([str(t) for t in tis])


# ---------------------------------------------------------------------------
# Jobs of the same season
# ---------------------------------------------------------------------------

def same_season(a: Job, b: Job) -> bool:
    """Both jobs are the same season of the same show: by TVmaze id when both have one, else by
    name and year."""
    if a.season is None or a.season != b.season:
        return False
    if a.show_id is not None and b.show_id is not None:
        return a.show_id == b.show_id
    name = a.show.strip().casefold()
    return bool(name) and name == b.show.strip().casefold() and (a.year is None or b.year is None or a.year == b.year)


def same_disc(a: Job, b: Job) -> bool:
    """A re-rip of one disc: the same volume label, or with no label to go by, the same disc number."""
    if a.disc_label and b.disc_label:
        return a.disc_label == b.disc_label
    return a.disc_number is not None and a.disc_number == b.disc_number


def disc_name(job: Job, number: int | None = None) -> str:
    """How a disc is named in a sentence: "disc 1", "disc 1, First Half" when its label says more
    than the show and the disc's number, else by its label."""
    n = job.disc_number or number
    words = label_words(job)
    if n:
        return f"disc {n}, {words}" if words else f"disc {n}"
    return f"the disc “{job.disc_label}”" if job.disc_label else "another disc"


def label_words(job: Job) -> str:
    """What a disc's volume label says besides the show, season and disc number:
    "Bluey S1 First Half" → "First Half"; "BLUEY_S1_D1" → ""."""
    show = {w.casefold() for w in re.findall(r"[A-Za-z0-9']+", job.show or "")}
    words = " ".join(w for w in re.sub(r"[_.]+", " ", job.disc_label or "").split() if w.casefold() not in show)
    words = re.sub(r"\b(?:S(?:EASON)?\s?\d{1,2}|D(?:ISC)?\s?\d{1,2}|VOL(?:UME)?\s?\d{1,2})\b", " ", words,
                   flags=re.IGNORECASE)
    words = re.sub(r"\s+", " ", words).strip(" -")
    return words if re.search(r"[A-Za-z]{3}", words) else ""


def disc_numbers(jobs: list[Job]) -> dict[str, int]:
    """Each disc's number: its own when it has one, else its place in the order the season's discs
    were ripped, skipping numbers other discs already carry."""
    taken = {j.disc_number for j in jobs if j.disc_number}
    out = {j.id: j.disc_number for j in jobs if j.disc_number}
    n = 0
    for j in sorted((j for j in jobs if not j.disc_number), key=lambda j: j.created):
        n += 1
        while n in taken:
            n += 1
        out[j.id] = n
    return out


def kept_titles(job: Job) -> set[int]:
    """The titles this job rips or ripped: the rip's own list, else the ticked titles."""
    out = {tp.title_index for tp in job.titles_progress} | {f.title_index for f in job.files}
    if not out:
        out = {t.index for t in job.titles if t.selected}
    return out or {m.title_index for m in job.matches}


def match_episodes(m: Match, season: int | None) -> list[tuple[int, int]]:
    """(season, number) for each episode a match holds; [] for anything that isn't an episode."""
    if m.target != "episode" or m.episode is None:
        return []
    s = m.episode.season if m.episode.season is not None else season
    if s is None:
        return []
    end = m.episode_end if m.episode_end and m.episode_end >= m.episode.number else m.episode.number
    return [(s, n) for n in range(m.episode.number, end + 1)]


def title_holders(job: Job, where: str = "here", playable=None, number: int | None = None) -> list[Holder]:
    """A holder for each episode each kept title of the job is matched to. playable(job, ti),
    when given, names a file that plays the title; number is the disc's number by rip order."""
    keep = kept_titles(job)
    titles = {t.index: t for t in job.titles}
    files = {f.title_index: f for f in job.files}
    main = main_lang(job)
    out = []
    for m in job.matches:
        if m.title_index not in keep:
            continue
        t, f = titles.get(m.title_index), files.get(m.title_index)
        path = playable(job, m.title_index) if playable else ""
        verified = f is not None and f.state == "verified"
        for s, n in match_episodes(m, job.season):
            out.append(Holder(s, n, where, m.title_index, job.id, disc_name(job, number) if where == "disc" else "",
                              version=m.version, on_nas=verified,
                              length_s=t.duration_s if t else 0, source=t.source if t else "",
                              size_bytes=(f.size_bytes if f and f.size_bytes else (t.size_bytes if t else 0)),
                              path=path or "", dest_rel=f.dest_rel if verified else "", audio=audio_langs(t),
                              main=main, source_of_match=m.source, disc_no=job.disc_number or number))
    return out


def season_jobs(job: Job, jobs: list[Job]) -> list[Job]:
    """The season's other discs: same show and season, ripped (or ripping), newest first. A re-rip
    replaces the disc it repeats, this job included, so an older attempt at a disc never counts."""
    out: list[Job] = []
    for j in sorted(jobs, key=lambda j: j.created, reverse=True):
        if j.id == job.id or not same_season(job, j) or same_disc(job, j):
            continue
        if not (j.files or j.titles_progress):
            continue                # scanned but never ripped
        if any(same_disc(j, k) for k in out):
            continue                # an older rip of a disc already counted
        out.append(j)
    return out


# ---------------------------------------------------------------------------
# Files already in the library
# ---------------------------------------------------------------------------

def parse_episode_file(name: str) -> tuple[int, int, int | None] | None:
    """(season, number, last number of a double episode) from a file name such as
    "Bluey (2018) - S01E20.mkv" or "Show - s02e03-e04 - Name.mkv"; None when it names none."""
    m = _FILE_CODE.search(name or "")
    if not m:
        return None
    end = int(m.group(3)) if m.group(3) else None
    return int(m.group(1)), int(m.group(2)), end


def _bare(name: str) -> str:
    """A show folder's name without its year, folded, for comparing: "Bluey (2018)" -> "bluey"."""
    name = re.sub(r"\s*\((?:19|20)\d\d\)\s*$", "", name or "")
    name = re.sub(r"\s*[{\[].*?[}\]]", "", name)          # Plex's {tvdb-…} and the like
    name = re.sub(r"\s*:\s*", " - ", name)
    return re.sub(r"\s+", " ", name).strip(" .").casefold()


def library_files(destination: str, show: str, year: int | None, season: int,
                  listdir=os.listdir, isdir=os.path.isdir) -> list[str]:
    """Video files in the show's season folder, as paths under destination. Only lists folders.
    Folder names are matched loosely (with or without the year, "Season 1" or "Season 01"),
    since files may have been put there by hand or by another program."""
    try:
        shows = [d for d in listdir(destination) if _bare(d) == _bare(show)]
    except OSError:
        return []
    if year is not None and any(d.endswith(f"({year})") for d in shows):
        shows = [d for d in shows if d.endswith(f"({year})") or not re.search(r"\((?:19|20)\d\d\)$", d)]
    out = []
    want = {f"season {season}", f"season {season:02d}", f"season {season:03d}"} | ({"specials"} if season == 0 else set())
    for d in shows:
        top = os.path.join(destination, d)
        try:
            subs = [s for s in listdir(top) if s.strip().casefold() in want and isdir(os.path.join(top, s))]
        except OSError:
            continue
        for s in subs:
            try:
                names = listdir(os.path.join(top, s))
            except OSError:
                continue
            out += [posixpath.join(d, s, n) for n in sorted(names)
                    if n.lower().endswith(VIDEO_EXT) and not n.startswith(".")]
    return out


def library_holders(rels: list[str], known: set[str], season: int, destination: str = "") -> list[Holder]:
    """A holder for each episode in each library file that no job of the season copied there.
    known holds the jobs' own paths under the destination, compared without case (SMB shares
    ignore it). A file whose name has " - <something>" after the code is a second version."""
    known = {k.casefold() for k in known}
    out = []
    for rel in rels:
        if rel.casefold() in known:
            continue
        name = posixpath.basename(rel)
        hit = parse_episode_file(name)
        if hit is None or hit[0] != season:
            continue
        s, n, end = hit
        version = _file_version(name)
        for k in range(n, (end if end and end >= n else n) + 1):
            out.append(Holder(s, k, "library", name=name, version=version, on_nas=True,
                              path=os.path.join(destination, rel) if destination else "", dest_rel=rel))
    return out


def _file_version(name: str) -> str:
    """What follows the episode code as " - 2160p" or " - Short cut", or a " (2)" copy number."""
    m = _FILE_CODE.search(name)
    rest = os.path.splitext(name[m.end():])[0] if m else ""
    hit = re.match(r"\s+-\s+(.+)$", rest)
    if hit:
        return hit.group(1).strip()
    hit = re.match(r"\s*\((\d+)\)$", rest)
    return f"({hit.group(1)})" if hit else ""


# ---------------------------------------------------------------------------
# Problems, and holding back the titles that cause them
# ---------------------------------------------------------------------------

def find_problems(mine: list[Holder], others: list[Holder], names: dict[tuple[int, int], str] | None = None
                  ) -> list[Problem]:
    """Episodes held by two titles of this disc, or by a title here and another disc or library
    file. Two holders with different versions (a second version kept on purpose) don't clash.
    A two-part title that overlaps another title clashes on the episode they share."""
    names = names or {}
    here: dict[tuple[int, int], list[Holder]] = collections.defaultdict(list)
    for h in mine:
        here[(h.season, h.number)].append(h)
    there: dict[tuple[int, int], list[Holder]] = collections.defaultdict(list)
    for h in others:
        there[(h.season, h.number)].append(h)
    groups: dict[tuple, list[tuple[int, int]]] = {}
    found: dict[tuple, tuple[list[int], list[Holder]]] = {}
    for key in sorted(here):
        hs = here[key]
        count = collections.Counter(h.version for h in hs)
        tis = {h.title_index for h in hs if count[h.version] > 1}
        clash: list[Holder] = []
        for h in hs:
            for o in there.get(key, []):
                if o.version == h.version:
                    tis.add(h.title_index)
                    if o not in clash:
                        clash.append(o)
        if not tis:
            continue
        sig = (tuple(sorted(tis)), tuple((o.where, o.job_id, o.title_index, o.name) for o in clash))
        groups.setdefault(sig, []).append(key)
        found[sig] = (sorted(tis), clash)
    out = []
    for sig, keys in groups.items():
        tis, clash = found[sig]
        season = keys[0][0]
        numbers = [n for s, n in keys if s == season]
        name = names.get(keys[0], "")
        p = Problem(season, numbers, name, tis, clash, "")
        p.text = problem_text(p)
        out.append(p)
    out.sort(key=lambda p: (p.season, p.numbers[0]))
    return out


def find_nas_duplicates(mine: list[Holder], others: list[Holder], problems: list[Problem],
                        names: dict[tuple[int, int], str] | None = None) -> list[Problem]:
    """Episodes held by two files already in storage with the same version, where no title of this
    disc that could still change is involved: another disc's pair (copied before duplicates were
    checked), a pair of this disc's own copied titles, or a library file and a copied title.
    Such a pair is fixed by renaming files in storage. A duplicate problem whose titles here are
    all copied becomes one of these."""
    names = names or {}
    live = {(p.season, n) for p in problems for n in p.numbers if p.kind == "duplicate"
            and any(not any(h.title_index == t and h.on_nas for h in mine) for t in p.titles)}
    by: dict[tuple, list[Holder]] = collections.defaultdict(list)
    for h in mine + others:
        if h.on_nas and h.dest_rel:
            by[(h.season, h.number, h.version.casefold())].append(h)
    out = []
    for (season, number, _v), hs in sorted(by.items()):
        if len(hs) < 2 or (season, number) in live:
            continue
        name = names.get((season, number), "")
        p = Problem(season, [number], name, sorted(h.title_index for h in hs if h.where == "here"),
                    [h for h in hs if h.where != "here"], "", kind="nas", sides=hs)
        p.text = nas_text(p)
        out.append(p)
    return out


def nas_text(p: Problem) -> str:
    n = len(p.sides)
    return f"{'Two' if n == 2 else n} files in storage are {p.episode_text()}."


def find_checks(job: Job, locked: set[int] = frozenset()) -> list[Problem]:
    """One problem per title whose match needs a check and isn't held as a duplicate."""
    keep = kept_titles(job)
    out = []
    for m in sorted(job.matches, key=lambda m: m.title_index):
        if (m.confidence != "check" or m.confirmed or m.held or m.title_index in locked
                or m.title_index not in keep or m.target not in ("episode", "skip")):
            continue
        e = m.episode if m.target == "episode" else None
        season = (e.season if e and e.season is not None else job.season) or 0
        p = Problem(season, [e.number] if e else [], e.title if e else "", [m.title_index], [], "", kind="check")
        p.text = check_title(m)
        out.append(p)
    return out


def check_title(m: Match) -> str:
    """The panel's heading for a match that needs a check."""
    ti = m.title_index
    if m.target != "episode" or m.episode is None:
        return f"Title {ti} has no episode yet"
    return {"runtime": f"Title {ti} was matched by its running time only",
            "card": f"Title {ti}'s title card isn't clear",
            "list": f"Title {ti}'s place in the episode list needs a check",
            "discdb": f"TheDiscDB's match for title {ti} needs a check"}.get(m.source, f"Title {ti}'s match needs a check")


def card_agrees(text: str, title: str) -> bool:
    """A title card's text names this episode, allowing for OCR's odd letters."""
    ocr = str.maketrans("0158", "olsb")      # digits OCR reads for letters
    a, b = _fold(text or "").translate(ocr), _fold(title or "").translate(ocr)
    if not a or not b:
        return False
    if a == b or (len(b) >= 4 and b in a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def problem_text(p: Problem) -> str:
    ep = p.episode_text()
    where = _and([o.at() for o in p.others])
    both = "both" if len(p.titles) == 2 else "all"
    if len(p.titles) > 1 and not p.others:
        return f"{_titles_text(p.titles)} are {both} {ep}."
    if len(p.titles) > 1:
        return f"{_titles_text(p.titles)} are {both} {ep}, which is already {where}."
    return f"{_titles_text(p.titles)} is {ep}, which is already {where}."


def held_notes(problems: list[Problem]) -> dict[int, str]:
    """The sentence each title involved gets: "Title 26 is also S01E20 “Markets”." and
    "S01E44 “Mount Mumandad” is already on disc 1 title 12."."""
    out: dict[int, list[str]] = collections.defaultdict(list)
    for p in problems:
        ep = p.episode_text()
        for ti in p.titles:
            mates = [t for t in p.titles if t != ti]
            if mates:
                out[ti].append(f"{_titles_text(mates)} {'is' if len(mates) == 1 else 'are'} also {ep}.")
            if p.others:
                out[ti].append(f"{ep} is already {_and([o.at() for o in p.others])}.")
    return {ti: " ".join(dict.fromkeys(s)) for ti, s in out.items()}


def apply_holds(matches: list[Match], notes: dict[int, str], locked: set[int] = frozenset()) -> bool:
    """Mark each title in notes "check" with its sentence in front of its note, so it isn't
    copied until the problem is settled; give back the earlier confidence and note to titles
    no longer involved. A title already copied (locked) is never held. Returns whether anything
    changed."""
    changed = False
    for m in matches:
        if m.held and not (m.confidence == "check" and (m.note or "").startswith(m.held)):
            # Edited since it was held (a new pick): what it says now stands.
            m.held, m.held_was = "", ""
            changed = True
        want = "" if (m.title_index in locked or m.target != "episode") else notes.get(m.title_index, "")
        if m.held == want:
            continue
        if m.held:
            m.note = m.note[len(m.held):].strip()
            m.confidence = m.held_was or m.confidence
            m.held, m.held_was = "", ""
        if want:
            m.held_was, m.held = m.confidence, want
            m.confidence = "check"
            m.note = f"{want} {m.note or ''}".strip()
        changed = True
    return changed


def held_summary(matches: list[Match], problems: list[Problem]) -> str:
    """One sentence for the Queue: which titles wait because they share an episode."""
    held = {m.title_index for m in matches if m.held}
    mine = [p for p in problems if held & set(p.titles)]
    if not held:
        return ""
    if len(mine) == 1:
        n = len(held & set(mine[0].titles))
        return f"{mine[0].text[:-1]}, so {'it waits' if n == 1 else 'they wait'}. Compare them on the Match page."
    k = len(mine) or 1
    return (f"{k} episodes are matched to more than one title, so {_titles_text(sorted(held)).lower()} wait. "
            "Compare them on the Match page.") if k > 1 else \
        (f"{_titles_text(sorted(held))} share an episode with another title, so they wait. "
         "Compare them on the Match page.")


# ---------------------------------------------------------------------------
# The fixes offered for a problem
# ---------------------------------------------------------------------------

def _length(job: Job, ti: int) -> int:
    t = next((t for t in job.titles if t.index == ti), None)
    return t.duration_s if t else 0


LANG_NAMES = {"eng": "English", "fra": "French", "fre": "French", "spa": "Spanish", "deu": "German",
              "ger": "German", "ita": "Italian", "jpn": "Japanese", "por": "Portuguese", "nld": "Dutch",
              "dut": "Dutch", "kor": "Korean", "zho": "Chinese", "chi": "Chinese", "swe": "Swedish",
              "dan": "Danish", "nor": "Norwegian", "fin": "Finnish", "pol": "Polish", "rus": "Russian"}


def audio_langs(t: Title | None) -> list[str]:
    """The title's audio languages as ISO 639-2 codes, in stream order, each once."""
    out: list[str] = []
    for st in t.streams if t else []:
        if st.kind == "audio" and st.lang and st.lang not in out:
            out.append(st.lang)
    return out


def lang_name(code: str, t: Title | None = None) -> str:
    st = next((st for st in (t.streams if t else []) if st.kind == "audio" and st.lang == code and st.lang_name), None)
    return LANG_NAMES.get(code) or (st.lang_name if st else "") or code.upper()


def main_lang(job: Job) -> str:
    """The disc's own language: the first audio language you keep, else the one most titles play first."""
    if job.audio_langs:
        return job.audio_langs[0]
    count = collections.Counter(audio_langs(t)[0] for t in job.titles if audio_langs(t))
    return count.most_common(1)[0][0] if count else ""


def dub_of(mine: Title | None, others: list[Title | None], main: str) -> str:
    """The language this title is dubbed in, e.g. "French", when it has no audio in the disc's main
    language and another title holding the same episode does. "" otherwise."""
    langs = audio_langs(mine)
    if not main or not langs or main in langs or not any(main in audio_langs(o) for o in others):
        return ""
    return lang_name(langs[0], mine)


def dub_language(job: Job, ti: int, problem: Problem) -> str:
    titles = {t.index: t for t in job.titles}
    return dub_of(titles.get(ti), [titles.get(o) for o in problem.titles if o != ti], main_lang(job))


def is_short_cut(job: Job, ti: int, problem: Problem) -> bool:
    """This title is more than a few seconds shorter than every other title it clashes with."""
    mine = _length(job, ti)
    lengths = [_length(job, t) for t in problem.titles if t != ti] + \
        [o.length_s for o in problem.others if o.length_s]
    return bool(mine and lengths) and all(mine < x - SHORT_CUT_S for x in lengths)


def version_name(job: Job, ti: int, problem: Problem) -> str:
    """The dub's language ("French") for a title whose audio is all in another language, "Short cut"
    for a shorter title, else "Version 2" (or the next number not in use)."""
    dub = dub_language(job, ti, problem)
    if dub:
        return dub
    if is_short_cut(job, ti, problem):
        return "Short cut"
    used = {m.version for m in job.matches if m.version} | {o.version for o in problem.others if o.version}
    k = 2
    while f"Version {k}" in used:
        k += 1
    return f"Version {k}"


def _edit(matches: list[Match], ti: int) -> Match | None:
    return next((m for m in matches if m.title_index == ti), None)


def fix_keep(matches: list[Match], keep: int, problem: Problem, locked: set[int] = frozenset()) -> list[Match]:
    """This title is the episode: it keeps it, confirmed, and the other titles of this disc in
    the problem are skipped with a note saying to pick their episode."""
    out = _copy.deepcopy(matches)
    m = _edit(out, keep)
    if m is not None:
        m.confirmed, m.version = True, ""
    for ti in problem.titles:
        o = _edit(out, ti)
        if ti == keep or o is None or ti in locked:
            continue
        o.target, o.confirmed, o.version = "skip", False, ""
        o.confidence, o.source, o.held, o.held_was = "check", "manual", "", ""
        o.note = f"Title {keep} is {problem.episode_text()}. Pick this title's episode, or leave it skipped."
    return out


def fix_version(matches: list[Match], job: Job, ti: int, problem: Problem) -> list[Match]:
    """Keep this title as a second version of the episode: both keep its name, and the file gets
    " - Short cut" (or " - Version 2") so media servers list it as a second version."""
    out = _copy.deepcopy(matches)
    m = _edit(out, ti)
    if m is not None:
        m.version = version_name(job, ti, problem)
        m.confirmed = True
    return out


def extra_name(job: Job, ti: int, problem: Problem) -> str:
    name = problem.name or problem.code
    dub = dub_language(job, ti, problem)
    if dub:
        return f"{name} ({dub})"
    return f"{name} (short cut)" if is_short_cut(job, ti, problem) else f"{name} (another version)"


def fix_extra(matches: list[Match], job: Job, ti: int, problem: Problem) -> list[Match]:
    """Save this title as an extra in Featurettes, named after the episode."""
    out = _copy.deepcopy(matches)
    m = _edit(out, ti)
    if m is not None:
        m.target, m.episode, m.episode_end, m.version = "extra", None, None, ""
        m.extra_name, m.extra_folder = extra_name(job, ti, problem), "Featurettes"
        m.confidence, m.source, m.confirmed, m.note = "manual", "manual", True, ""
        m.held, m.held_was = "", ""
    return out


def fix_skip(matches: list[Match], ti: int) -> list[Match]:
    """Leave this title out: it isn't copied, and the other title keeps the episode."""
    out = _copy.deepcopy(matches)
    m = _edit(out, ti)
    if m is not None:
        m.target, m.episode, m.episode_end, m.version = "skip", None, None, ""
        m.confidence, m.source, m.confirmed, m.note = "manual", "manual", True, ""
        m.held, m.held_was = "", ""
    return out


def compare_hint(job: Job, problem: Problem, cards: dict) -> str:
    """One or two sentences to help choose, from the title cards and lengths. cards maps a title
    of this disc to its CardStatus."""
    tis = problem.titles
    read = {ti: cards.get(ti) for ti in tis}
    texts = {ti: (st.text or "").strip() for ti, st in read.items() if st is not None and st.state == "read" and st.text}
    lengths = {ti: _length(job, ti) for ti in tis}
    dubs = {ti: d for ti in tis if (d := dub_language(job, ti, problem))}
    if len(tis) > 1 and len(dubs) == 1:
        ti, dub = next(iter(dubs.items()))
        return (f"Title {ti} has no {lang_name(main_lang(job))} audio, only {dub}, so it's probably this episode "
                f"dubbed in {dub}: keep it as the {dub} version, save it as an extra, or skip it.")
    if len(tis) > 1 and len(texts) == len(tis) and len({_fold(t) for t in texts.values()}) == 1:
        shorter = [ti for ti in tis if is_short_cut(job, ti, problem)]
        if shorter:
            ti = shorter[0]
            longest = max(lengths.values())
            return (f"{_both_cards(tis)} read “{next(iter(texts.values()))}”, so {_both(tis)} are this episode. "
                    f"Title {ti} is {longest - lengths[ti]} s shorter, so it's probably a second cut: "
                    "keep it as a second version, or save it as an extra.")
        return (f"{_both_cards(tis)} read “{next(iter(texts.values()))}”. Watch {_both(tis)} to see how they differ, "
                "or keep one as a second version.")
    if len(tis) > 1 and len(texts) == len(tis):
        names = _and([f"title {ti} reads “{texts[ti]}”" for ti in sorted(tis)])
        return _cap(names) + "."
    shorter = [ti for ti in tis if is_short_cut(job, ti, problem)]
    tail = ""
    if shorter:
        one = len(shorter) == 1
        tail = (f" {_titles_text(shorter)} {'is' if one else 'are'} shorter, so {'it' if one else 'they'} may be a "
                "second cut or an extra.")
    if all(read.get(ti) is not None and read[ti].state in ("read", "none") for ti in tis):
        return "Watch each one to tell them apart." + tail
    return "Watch each one, or read their title cards to see which shows the episode's name." + tail


def _both(tis: list[int]) -> str:
    return "both" if len(tis) == 2 else "all of them"


def _both_cards(tis: list[int]) -> str:
    return "Both title cards" if len(tis) == 2 else f"All {len(tis)} title cards"


def _fold(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.casefold())


# ---------------------------------------------------------------------------
# The season grid and the picker's taken list
# ---------------------------------------------------------------------------

def season_status(job: Job, mine: list[Holder], others: list[Holder], episodes: list[Episode],
                  problems: list[Problem], discs: int = 1, library_count: int = 0) -> SeasonStatus:
    season = job.season
    holders: dict[tuple[int, int], list[Holder]] = collections.defaultdict(list)
    for h in mine + others:
        holders[(h.season, h.number)].append(h)
    taken: dict[tuple[int, int], list[tuple[int | None, str]]] = {}
    for key, hs in holders.items():
        taken[key] = [(h.title_index if h.where == "here" else None, h.label()) for h in hs]
    if season is None:
        return SeasonStatus(None, problems, [], taken, dict(holders), "", discs, library_count)
    names = {e.number: e.title for e in episodes if e.season in (None, season)}
    numbers = sorted(set(names) | {n for s, n in holders if s == season})
    bad = {(p.season, n): p.key for p in problems if p.kind != "check" for n in p.numbers}
    unsure = {(p.season, n): p for p in problems if p.kind == "check" for n in p.numbers}
    cells = []
    counts = collections.Counter()
    for n in numbers:
        key = (season, n)
        hs = holders.get(key, [])
        versions = collections.Counter(h.version for h in hs)
        dup = key in bad or any(c > 1 for c in versions.values())
        problem = bad.get(key, "")
        if dup:
            state, mark = "duplicate", f"×{len(hs)}"
        elif key in unsure:
            state, mark, problem = "check", f"? T{unsure[key].titles[0]}", unsure[key].key
        elif any(h.where == "here" for h in hs):
            h = next(h for h in hs if h.where == "here")
            state, mark = "here", f"T{h.title_index}"
        elif any(h.where == "disc" for h in hs):
            h = next(h for h in hs if h.where == "disc")
            state, mark = "disc", f"D{h.disc_no}" if h.disc_no else "Disc"
        elif hs:
            state, mark = "library", "Lib"
        else:
            state, mark = "free", ""
        counts[state] += 1
        title = names.get(n, "")
        code = ep_code(season, n) + (f" “{title}”" if title else "")
        if hs:
            where = _and([h.label().replace(", this disc", " on this disc") if h.where == "here" else
                          (h.label() if h.where == "disc" else "in the library" + (f" as {h.name}" if h.name else ""))
                          for h in hs])
            tip = f"{code}: {where}."
            if dup:
                tip = f"{code} is on {len(hs)} titles: {where}. Click to fix."
            elif state == "check":
                tip = f"{code}: {where}. Its match needs a check. Click to fix."
        else:
            tip = f"{code}: no title yet."
        cells.append(Cell(season, n, title, state, mark, problem, tip, hs))
    parts = [f"{len(numbers)} episodes"]
    if counts["duplicate"]:
        k = counts["duplicate"]
        parts.append(f"{k} {'is' if k == 1 else 'are'} on more than one title")
    if counts["check"]:
        k = counts["check"]
        parts.append(f"{k} {'needs' if k == 1 else 'need'} a check")
    if counts["free"]:
        parts.append(f"{counts['free']} with no title yet")
    summary = ". ".join([f"Season {season}: " + ", ".join(parts)]) + "."
    return SeasonStatus(season, problems, cells, taken, dict(holders), summary, discs, library_count)


# ---------------------------------------------------------------------------
# What the controllers share
# ---------------------------------------------------------------------------

class SeasonChecks:
    """The season checks on a controller. Each controller supplies job_lock, jobs(), _layout(job),
    _locked(job), _show_episodes(show_id), _library_rels(job) and playable(job, ti)."""

    def season_others(self, job: Job, refresh: bool = False) -> tuple[list[Holder], int, int]:
        """(holders on the season's other discs and in the library, discs counted, library files
        no job copied). Only refresh=True reads the other jobs and lists the season folder, so the
        GUI thread calls it without and gets what the last look found."""
        if not hasattr(self, "_season_others"):
            self._season_others: dict[str, tuple] = {}
        hit = self._season_others.get(job.id)
        sig = (job.show_id, job.show, job.year, job.season, job.destination, job.disc_label, job.disc_number)
        fresh = hit is not None and hit[0] == sig
        if not refresh:
            return hit[2] if fresh else ([], 1, 0)
        if fresh and time.monotonic() - hit[1] < 2.0:
            return hit[2]           # looked a moment ago
        if job.season is None or self._layout(job) == "movie" or not (job.show.strip() or job.show_id):
            result: tuple[list[Holder], int, int] = ([], 1, 0)
        else:
            result = self._look_around(job)
        self._season_others[job.id] = (sig, time.monotonic(), result)
        return result

    def _look_around(self, job: Job) -> tuple[list[Holder], int, int]:
        try:
            jobs = self.jobs()
        except Exception:  # noqa: BLE001 -- unreadable job files: check this disc on its own
            jobs = []
        mates = season_jobs(job, [j for j in jobs if self._layout(j) != "movie"])
        numbers = disc_numbers([job] + mates)
        others = []
        for j in mates:
            others += title_holders(j, "disc", getattr(self, "playable", None), numbers.get(j.id))
        known = {f.dest_rel for j in jobs if same_season(job, j) for f in j.files if f.dest_rel}
        try:
            rels = self._library_rels(job)
        except Exception:  # noqa: BLE001 -- an unmounted share: the jobs still count
            rels = []
        lib = library_holders(rels, known, job.season, job.destination)
        return others + lib, 1 + len(mates), len({h.name for h in lib})

    def _season_names(self, job: Job) -> dict[tuple[int, int], str]:
        names = {}
        for m in job.matches:
            if m.target == "episode" and m.episode is not None and m.episode.title:
                s = m.episode.season if m.episode.season is not None else job.season
                names[(s, m.episode.number)] = m.episode.title
        return names

    def apply_season(self, job: Job, others: list[Holder] | None = None) -> list[Problem]:
        """Hold back this disc's titles that share an episode, using what the last look at the
        other discs found. Cheap, so it runs on every change of the matches. Hold job_lock."""
        if self._layout(job) == "movie":
            return []
        if others is None:
            others = self.season_others(job)[0]
        mine = title_holders(job, "here")
        problems = find_problems(mine, others, self._season_names(job))
        apply_holds(job.matches, held_notes(problems), self._locked(job))
        return problems

    def season_problems(self, job: Job, others: list[Holder], problems: list[Problem]) -> list[Problem]:
        """Everything the Match page offers to fix, in order: duplicates that hold titles back, files
        in storage that share an episode, then matches that need a check. Hold job_lock."""
        mine = title_holders(job, "here")
        names = self._season_names(job)
        nas = find_nas_duplicates(mine, others, problems, {**self._other_names(job), **names})
        nas_keys = {(p.season, p.numbers[0]) for p in nas}
        live = [p for p in problems if not (len(p.numbers) == 1 and (p.season, p.numbers[0]) in nas_keys)]
        return live + nas + find_checks(job, self._locked(job))

    def _other_names(self, job: Job) -> dict[tuple[int, int], str]:
        eps = getattr(self, "_season_eps", {}).get(job.show_id, []) if job.show_id is not None else []
        return {(e.season, e.number): e.title for e in eps if e.title}

    def season_status(self, job: Job, refresh: bool = False) -> SeasonStatus:
        """The season as the Match page shows it: problems, the grid, and which episodes are taken.
        With refresh, the other discs and the library are looked at again (slow on a share)."""
        others, discs, lib_count = self.season_others(job, refresh=refresh)
        if not hasattr(self, "_season_eps"):
            self._season_eps: dict[int, list[Episode]] = {}
        episodes: list[Episode] = []
        if job.show_id is not None and job.season is not None and self._layout(job) != "movie":
            if refresh or job.show_id not in self._season_eps:
                try:
                    eps = self._show_episodes(job.show_id) if refresh else None
                except Exception:  # noqa: BLE001 -- offline: the grid shows the episodes the titles hold
                    eps = None
                if eps is not None:
                    self._season_eps[job.show_id] = list(eps)
            episodes = [e for e in self._season_eps.get(job.show_id, []) if e.season == job.season]
        with self.job_lock:
            before = [(m.held, m.confidence) for m in job.matches]
            problems = self.season_problems(job, others, self.apply_season(job, others))
            changed = before != [(m.held, m.confidence) for m in job.matches]
            mine = title_holders(job, "here", getattr(self, "playable", None))
            if changed:
                self._save(job)
                queue_ready = getattr(self, "_queue_ready", None)
                if queue_ready:
                    queue_ready(job)
        return season_status(job, mine, others, episodes, problems, discs, lib_count)

