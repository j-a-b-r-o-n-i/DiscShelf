"""Client for the free, keyless TVmaze API (https://api.tvmaze.com), used to name TV episodes.

TVmaze allows about 20 calls per 10 seconds per IP. A disc needs one search and
one episode-list call, and results are cached per instance, so no throttling is done here.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .models import Episode

BASE = "https://api.tvmaze.com"
Fetch = Callable[[str], bytes]


class TvMazeError(Exception):
    """A request failed or TVmaze returned something that is not JSON. The message is one sentence."""


@dataclass
class ShowHit:
    id: int
    name: str
    year: int | None = None      # from "premiered" (YYYY-MM-DD); None for shows with no date
    network: str = ""            # broadcast network or streaming service, e.g. "Apple TV+"
    average_runtime: int | None = None


def _default_fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "disc-shelf", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.read()


def _year(premiered: Any) -> int | None:
    if isinstance(premiered, str) and len(premiered) >= 4 and premiered[:4].isdigit():
        return int(premiered[:4])
    return None


def _hit(show: dict) -> ShowHit:
    # Streaming originals have webChannel instead of network.
    net = (show.get("network") or show.get("webChannel") or {}).get("name") or ""
    return ShowHit(id=int(show["id"]), name=show.get("name") or "", year=_year(show.get("premiered")),
                   network=net, average_runtime=show.get("averageRuntime") or show.get("runtime"))


class TvMaze:
    def __init__(self, fetch: Fetch | None = None, base: str = BASE):
        self._fetch = fetch or _default_fetch
        self._base = base.rstrip("/")
        self._cache: dict[str, Any] = {}

    def _get(self, path: str) -> Any:
        url = self._base + path
        if url in self._cache:
            return self._cache[url]
        try:
            raw = self._fetch(url)
        except Exception as e:  # urllib raises several unrelated types; the UI only needs one
            raise TvMazeError(f"Could not reach TVmaze ({e}).") from e
        try:
            data = json.loads(raw)
        except ValueError as e:
            raise TvMazeError("TVmaze sent a reply that is not JSON.") from e
        self._cache[url] = data
        return data

    def search(self, query: str) -> list[ShowHit]:
        """Shows matching the query, best match first (TVmaze's own order, by score)."""
        query = query.strip()
        if not query:
            return []
        data = self._get("/search/shows?q=" + urllib.parse.quote(query))
        return [_hit(item["show"]) for item in data if isinstance(item, dict) and item.get("show")]

    def show(self, show_id: int) -> ShowHit:
        return _hit(self._get(f"/shows/{int(show_id)}"))

    def episodes(self, show_id: int, season: int | None) -> list[Episode]:
        """Regular episodes of one season, in episode-number order, or of every season when
        season is None (a volume DVD that mixes seasons).

        Specials are left out: TVmaze gives them no episode number, and Plex files them under Season 00.
        An episode with no runtime gets the show's average runtime, so matching still has a number to compare.
        """
        # One call with the episodes embedded gives the show's averageRuntime as well.
        data = self._get(f"/shows/{int(show_id)}?embed=episodes")
        fallback = data.get("averageRuntime") or data.get("runtime")
        eps = (data.get("_embedded") or {}).get("episodes") or []
        out = []
        for e in eps:
            if e.get("number") is None or e.get("season") is None or season not in (None, e["season"]):
                continue
            out.append(Episode(season=int(e["season"]), number=int(e["number"]), title=e.get("name") or "",
                               runtime_min=e.get("runtime") or fallback))
        out.sort(key=lambda e: (e.season, e.number))
        return out

    def seasons(self, show_id: int) -> list[int]:
        """Season numbers that have at least one numbered episode."""
        data = self._get(f"/shows/{int(show_id)}?embed=episodes")
        eps = (data.get("_embedded") or {}).get("episodes") or []
        return sorted({e["season"] for e in eps if e.get("number") is not None and e.get("season") is not None})

    def poster_url(self, show_id: int, season: int | None = None) -> str | None:
        """The season's poster when TVmaze has one, else the show's; None when neither exists."""
        if season is not None:
            for s in self._get(f"/shows/{int(show_id)}/seasons") or []:
                if s.get("number") == season and (s.get("image") or {}).get("original"):
                    return s["image"]["original"]
        img = self._get(f"/shows/{int(show_id)}?embed=episodes").get("image") or {}
        return img.get("original") or img.get("medium")

    def download(self, url: str) -> bytes:
        """Fetch an image TVmaze links to (static.tvmaze.com). Not cached: the caller keeps the file."""
        try:
            return self._fetch(url)
        except Exception as e:
            raise TvMazeError(f"Could not download the poster ({e}).") from e
