"""Look a scanned disc up in TheDiscDB (https://thediscdb.com), a free, keyless community
database that records what each title on a disc is: which episode, the film, or which extra.

A disc is recognised by its titles' fingerprints, not by its name: a TheDiscDB title is
the same as a scanned one when the playlist (or a DVD's title number), the running time
and the size in bytes all agree. Sizes are compared exactly, because TheDiscDB stores the
byte count MakeMKV reports, and a different pressing of the same season (a reissue, an
extended cut) has different sizes for the same playlist names.

TheDiscDB numbers seasons and episodes the way TMDB does, while Disc Shelf names files from
TVmaze, and the two sometimes disagree (TMDB counts The Office's "A Benihana Christmas" as
one episode, TVmaze as two). So episodes are matched to TVmaze by name first and by number
only as a fallback.
"""
from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

from .match import _non_episode, extra_name, score_name
from .models import Disc, Episode, Match, Title

URL = "https://thediscdb.com/graphql/"
Fetch = Callable[[str, bytes], bytes]      # (url, request body) -> reply body

# Only the fields find_disc and to_matches read, so a search for a long-running show stays small.
_FIELDS = """
nodes {
  title year type
  externalids { imdb tmdb }
  releases {
    title slug upc year
    discs {
      index name format slug
      titles { index sourceFile duration size season episode itemType item { title season episode type } }
    }
  }
}"""

_STOP_WORDS = {"a", "an", "the", "of", "in", "on", "at", "to", "and", "or"}

# TheDiscDB item types for extras, as Plex extras folders (naming.EXTRAS_FOLDERS).
# Anything not listed, including plain "Extra", goes to Featurettes.
_EXTRA_FOLDERS = {
    "trailer": "Trailers", "deletedscene": "Deleted Scenes", "featurette": "Featurettes",
    "behindthescenes": "Behind The Scenes", "interview": "Interviews", "scene": "Scenes",
    "short": "Shorts", "music": "Other", "other": "Other",
}

DURATION_SLACK_S = 2       # TheDiscDB rounds to whole seconds, and MakeMKV's own rounding can differ by one


class DiscDbError(Exception):
    """A request to TheDiscDB failed or its reply could not be read. The message is one sentence."""


@dataclass
class DiscItem:
    """What TheDiscDB says one title is."""
    kind: str                    # TheDiscDB's item type, e.g. "Episode", "MainMovie", "DeletedScene"
    title: str = ""              # episode name, film name or extra name, as TheDiscDB has it
    season: int | None = None    # TMDB numbering
    episode: int | None = None
    episode_end: int | None = None   # last episode when the title holds a range such as "12-13"


@dataclass
class DiscHit:
    media_title: str
    year: int | None = None
    media_type: str = ""         # "Series" | "Movie"
    tmdb_id: str = ""
    imdb_id: str = ""
    release_title: str = ""      # e.g. "2021 Season 3 Blu-ray"
    release_slug: str = ""
    disc_name: str = ""          # e.g. "Season Three Disc Three"
    disc_format: str = ""        # "Blu-ray" | "UHD" | "DVD", spelled as TheDiscDB has it
    matched: int = 0             # TheDiscDB titles that matched a scanned title
    expected: int = 0            # TheDiscDB titles the scan should have had
    items: dict[int, DiscItem | None] = field(default_factory=dict)   # by scan title index; None: matched but unlabelled
    other_releases: list[str] = field(default_factory=list)   # releases holding the same disc, e.g. a box set


def _default_fetch(url: str, body: bytes) -> bytes:
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "User-Agent": "disc-shelf", "Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def _words(query: str) -> list[str]:
    # Each word is searched separately, so "Tron Legacy" finds "TRON: Legacy".
    words = re.findall(r"[^\s:;,.!?()\[\]/]+", query)
    kept = [w for w in words if len(w) >= 2 and w.lower() not in _STOP_WORDS]
    return kept or words


class TheDiscDb:
    def __init__(self, fetch: Fetch | None = None, url: str = URL):
        self._fetch = fetch or _default_fetch
        self._url = url
        self._cache: dict[str, Any] = {}

    def _query(self, gql: str, variables: dict) -> Any:
        body = json.dumps({"query": gql, "variables": variables}, sort_keys=True)
        if body in self._cache:
            return self._cache[body]
        try:
            raw = self._fetch(self._url, body.encode())
        except Exception as e:  # urllib raises several unrelated types; the UI only needs one
            raise DiscDbError(f"Could not reach TheDiscDB ({e}).") from e
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise DiscDbError("TheDiscDB sent a reply that is not JSON.") from e
        if not isinstance(data, dict):
            raise DiscDbError("TheDiscDB sent a reply in an unexpected format.")
        if data.get("errors"):
            msg = ((data["errors"][0] or {}).get("message") or "no reason given").rstrip(". ")
            raise DiscDbError(f"TheDiscDB refused the search ({msg}).")
        nodes = ((data.get("data") or {}).get("mediaItems") or {}).get("nodes")
        if not isinstance(nodes, list):
            raise DiscDbError("TheDiscDB sent a reply in an unexpected format.")
        self._cache[body] = nodes
        return nodes

    def search(self, query: str) -> list[dict]:
        """Media items (shows and films) whose title holds every word of query, with all their
        releases, discs and titles, as TheDiscDB sends them. The match ignores case."""
        words = _words(query.strip())
        if not words:
            return []
        names = [f"w{i}" for i in range(len(words))]
        decl = ", ".join(f"${n}: String!" for n in names)
        where = ", ".join(f"{{ title: {{ contains: ${n} }} }}" for n in names)
        gql = f"query Search({decl}) {{ mediaItems(where: {{ and: [{where}] }}, first: 50) {{{_FIELDS} }} }}"
        return self._query(gql, dict(zip(names, words)))

    def search_upc(self, upc: str) -> list[dict]:
        """Media items with a release whose barcode is upc."""
        upc = re.sub(r"\D", "", upc or "")
        if not upc:
            return []
        gql = ("query Upc($upc: String!) { mediaItems(where: { releases: { some: { upc: { eq: $upc } } } }, "
               f"first: 50) {{{_FIELDS} }} }}")
        return self._query(gql, {"upc": upc})


def _seconds(s: Any) -> int:
    total = 0
    for part in str(s or "").strip().split(":"):
        total = total * 60 + (int(part) if part.isdigit() else 0)
    return total


def _int(v: Any) -> int | None:
    s = str(v if v is not None else "").strip()
    return int(s) if s.isdigit() else None


def _key(source: str) -> str:
    # A DVD title number is "01" in TheDiscDB and in MakeMKV, but compare as numbers in case one drops the zero.
    s = (source or "").strip()
    return str(int(s)) if s.isdigit() else s.lower()


def _scan_key(t: Title) -> str:
    # DVDs have no playlist name, only MakeMKV's original title id.
    return _key(t.source or t.title_id)


def _item(dt: dict) -> DiscItem | None:
    it = dt.get("item")
    if not it:
        return None
    ep = str(it.get("episode") or dt.get("episode") or "")
    m = re.fullmatch(r"\s*(\d+)\s*[-–,]\s*(\d+)\s*", ep)
    start, end = (int(m[1]), int(m[2])) if m else (_int(ep), None)
    return DiscItem(kind=it.get("type") or dt.get("itemType") or "", title=(it.get("title") or "").strip(),
                    season=_int(it.get("season") if it.get("season") is not None else dt.get("season")),
                    episode=start, episode_end=end if end != start else None)


def _score(disc: Disc, ddisc: dict, min_seconds: int) -> tuple[int, int, dict[int, dict]]:
    """(matched, expected, {scan index: TheDiscDB title}) for one TheDiscDB disc."""
    scan = {}
    for t in disc.titles:
        scan.setdefault(_scan_key(t), []).append(t)
    pairs: dict[int, dict] = {}
    expected = matched = 0
    for dt in ddisc.get("titles") or []:
        size, dur = dt.get("size") or 0, _seconds(dt.get("duration"))
        if not size:
            continue
        # The scan drops titles under its minimum length, so TheDiscDB's short ones aren't missed.
        counts = dur > min_seconds + DURATION_SLACK_S
        hit = next((t for t in scan.get(_key(dt.get("sourceFile") or ""), [])
                    if t.size_bytes == size and abs(t.duration_s - dur) <= DURATION_SLACK_S and t.index not in pairs), None)
        if hit is not None:
            pairs[hit.index] = dt
            matched += 1
            expected += 1
        elif counts:
            expected += 1
    return matched, expected, pairs


def _accepted(matched: int, expected: int) -> bool:
    # Nearly all: one miss in ten is allowed, for a title the contributor's scan saw and ours didn't.
    if expected == 0 or matched < min(2, expected):
        return False
    return expected - matched <= expected // 10


def best_disc(disc: Disc, items: list[dict], min_seconds: int = 120) -> DiscHit | None:
    """The TheDiscDB disc whose titles match the scan best, from search results; None when none does."""
    best = None      # (rank, hit, fingerprint, discs in its release)
    for it in items or []:
        ext = it.get("externalids") or {}
        for rel in it.get("releases") or []:
            for d in rel.get("discs") or []:
                matched, expected, pairs = _score(disc, d, min_seconds)
                if not _accepted(matched, expected):
                    continue
                fp = sorted((i, dt.get("size")) for i, dt in pairs.items())
                rank = (matched, -(expected - matched))
                if best is not None and rank == best[0] and fp == best[2]:
                    # The same pressing listed again, usually as a season set and in a box set.
                    # The release with fewer discs names it more exactly ("Season 3", not "Complete Series").
                    h = best[1]
                    if len(rel.get("discs") or []) < best[3]:
                        h.other_releases.append(h.release_title)
                        h.release_title, h.release_slug = rel.get("title") or "", rel.get("slug") or ""
                        h.disc_name, h.disc_format = d.get("name") or "", d.get("format") or ""
                        best = (rank, h, fp, len(rel.get("discs") or []))
                    else:
                        h.other_releases.append(rel.get("title") or "")
                    continue
                if best is not None and rank <= best[0]:
                    continue
                hit = DiscHit(media_title=it.get("title") or "", year=it.get("year"), media_type=it.get("type") or "",
                              tmdb_id=str(ext.get("tmdb") or ""), imdb_id=str(ext.get("imdb") or ""),
                              release_title=rel.get("title") or "", release_slug=rel.get("slug") or "",
                              disc_name=d.get("name") or "", disc_format=d.get("format") or "",
                              matched=matched, expected=expected,
                              items={i: _item(dt) for i, dt in sorted(pairs.items())})
                best = (rank, hit, fp, len(rel.get("discs") or []))
    return best[1] if best else None


def find_disc(disc: Disc, query: str, db: TheDiscDb | None = None, min_seconds: int = 120) -> DiscHit | None:
    """Search TheDiscDB for query (the show or film name, or the disc's own name when empty)
    and return the disc that matches the scan. Raises DiscDbError when the search fails."""
    db = db or TheDiscDb()
    return best_disc(disc, db.search(query or disc.name), min_seconds)


def _is_episode(kind: str) -> bool:
    return kind.lower() in ("episode", "series")


def _extra_title(item: DiscItem, t: Title) -> str:
    # "Traveling Salesmen/The Return Deleted Scenes": a slash can't go in a file name.
    return re.sub(r"\s*/\s*", " and ", item.title) if item.title else extra_name(t)


def _folder(kind: str) -> str:
    return _EXTRA_FOLDERS.get(re.sub(r"[^a-z]", "", kind.lower()), "Featurettes")


def _code(season: int | None, number: int | None, end: int | None = None) -> str:
    s = f"S{season:02d}E{number:02d}" if season is not None and number is not None else f"episode {number}"
    return s + (f"-E{end:02d}" if end else "")


def _by_name(name: str, season: int | None, episodes: list[Episode]) -> tuple[list[Episode], float]:
    """TVmaze episodes that best fit name, all equally good, and their score; ([], 0) when none."""
    found = score_name(name, episodes, min_score=0.75)
    if not found:
        return [], 0.0
    top = found[0][1]
    best = [e for e, s in found if s == top]
    # TMDB and TVmaze almost always agree on the season, so it settles a tie.
    best.sort(key=lambda e: (e.season != season, e.season, e.number))
    return best, top


def _episode_match(t: Title, item: DiscItem, episodes: list[Episode], source_note: str) -> Match:
    m = Match(title_index=t.index, target="episode", confidence="close", note=source_note, source="discdb")
    theirs = _code(item.season, item.episode, item.episode_end)
    parts = [p.strip() for p in item.title.split("/") if p.strip()] if item.title else []
    start = end = None
    tied: list[Episode] = []
    loose = False
    if parts:
        cands, score = _by_name(parts[0], item.season, episodes)
        if cands:
            start = cands[0]
            # "Money" fits both "Money (1)" and "Money (2)"; that is expected when the title holds both.
            if item.episode_end is None and len(cands) > 1:
                tied = cands[1:]
            loose = score < 0.85
        if start is not None and len(parts) > 1:
            last, _ = _by_name(parts[-1], item.season, episodes)
            end = last[0] if last and last[0].season == start.season and last[0].number > start.number else None
    by_name = start is not None
    if start is None and item.episode is not None:
        start = next((e for e in episodes if e.season == item.season and e.number == item.episode), None)
    if start is None:
        if not episodes and item.season is not None and item.episode is not None:
            m.episode = Episode(season=item.season, number=item.episode, title=item.title)
            m.episode_end = item.episode_end
            m.confidence = "check"
            m.note = (f"No TVmaze episodes are loaded, so TheDiscDB's numbering ({theirs}, which follows TMDB) "
                      "is used as it is.")
            return m
        return Match(title_index=t.index, target="skip", confidence="check", source="discdb",
                     note=f"TheDiscDB says this is {theirs} “{item.title}”, but TVmaze has no such episode. "
                          "Pick it by hand.")
    m.episode = start
    if item.episode_end is not None:
        if end is None:
            want = start.number + (item.episode_end - item.episode) if item.episode is not None else start.number + 1
            end = next((e for e in episodes if e.season == start.season and e.number == want), None)
        if end is None:
            m.confidence = "check"
            m.note = (f"TheDiscDB says this title holds {theirs}, but TVmaze has no matching last episode, "
                      "so it is saved as one episode.")
            return m
        m.episode_end = end.number
    if not by_name:
        m.confidence = "check"
        name = f" “{item.title}”" if item.title else ""
        m.note = (f"TheDiscDB says this is {theirs}{name}, and TVmaze's episode with that number is "
                  f"“{start.title}”. TheDiscDB numbers episodes as TMDB does, so check it is the same one.")
        return m
    if tied:
        m.confidence = "check"
        others = ", ".join(f"{_code(e.season, e.number)} “{e.title}”" for e in tied[:2])
        m.note = f"TheDiscDB calls it “{parts[0]}”, which could also be {others} on TVmaze."
        return m
    if loose:
        m.confidence = "check"
        m.note = f"TheDiscDB calls it “{parts[0]}”, taken to be TVmaze's “{start.title}”."
        return m
    ours = _code(start.season, start.number, m.episode_end)
    if ours != theirs:
        m.note += f" TheDiscDB numbers it {theirs}."
    return m


def to_matches(hit: DiscHit, titles: list[Title], tvmaze_episodes: list[Episode]) -> list[Match]:
    """One Match per title, in title-index order, from what TheDiscDB says each title is.

    Unselected titles are skipped, as in match.match_titles, because they aren't ripped.
    Titles TheDiscDB doesn't know, or knows but nobody labelled, are handled as
    match.match_titles handles a non-episode title."""
    where = ", ".join(p for p in (hit.release_title, hit.disc_name) if p)
    source_note = f"From TheDiscDB: {where}." if where else "From TheDiscDB."
    out = []
    have_feature = False
    used: dict[tuple[int, int], int] = {}
    for t in sorted(titles, key=lambda t: t.index):
        item = hit.items.get(t.index)
        if not t.selected or item is None:
            out.append(_non_episode(t))
            continue
        if _is_episode(item.kind):
            m = _episode_match(t, item, tvmaze_episodes, source_note)
            if m.episode is not None:
                key = (m.episode.season, m.episode.number)
                if key in used:
                    same = f"Title {used[key]} is matched to the same episode."
                    m.note = f"{m.note} {same}" if m.confidence == "check" else same
                    m.confidence = "check"
                used.setdefault(key, t.index)
        elif item.kind.lower() == "mainmovie" and not have_feature:
            have_feature = True
            m = Match(title_index=t.index, target="feature", confidence="close", note=source_note, source="discdb")
        elif item.kind.lower() == "mainmovie":
            m = Match(title_index=t.index, target="extra", extra_name=_extra_title(item, t),
                      extra_folder="Other", confidence="check", source="discdb",
                      note="TheDiscDB lists a second copy of the film here, so it is saved as an extra; "
                           "rename it if it is another cut.")
        else:
            m = Match(title_index=t.index, target="extra", extra_name=_extra_title(item, t),
                      extra_folder=_folder(item.kind), confidence="close", note=source_note, source="discdb")
        out.append(m)
    return out
