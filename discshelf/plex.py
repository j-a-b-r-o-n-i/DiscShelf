"""The small part of the Plex API Disc Shelf needs: list libraries, scan one folder, sign in with a PIN.

Plex usually runs on the NAS, so the paths it reports are the NAS's own (for example
/volume1/Media/Shows), not this machine's network-mount paths. Callers therefore
work with paths relative to the destination folder and plex_path() puts them
under the library's location as Plex sees it.
"""
from __future__ import annotations

import json
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable

from . import config
from .mediaserver import ServerError, norm_title as _norm

PRODUCT = "Disc Shelf"
PLEX_TV = "https://plex.tv/api/v2"
AUTH_APP = "https://app.plex.tv/auth#?"

# fetch(method, url, headers) -> (HTTP status, body). Injectable so tests need no network.
Fetch = Callable[[str, str, dict], "tuple[int, bytes]"]


class PlexError(ServerError):
    """Plex or plex.tv answered with an error, or could not be reached."""


def _urllib_fetch(method: str, url: str, headers: dict, timeout: float = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, OSError) as e:
        raise PlexError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: {e}") from e


def install_client_id() -> str:
    """A stable id for this install; Plex lists each id as one device under the account."""
    cid = config.load_secret("plex_client_id")
    if not cid:
        cid = str(uuid.uuid4())
        config.save_secret("plex_client_id", cid)
    return cid


class Plex:
    name = "Plex"

    def __init__(self, url: str, token: str, fetch: Fetch | None = None, client_id: str | None = None):
        self.url = url.rstrip("/")
        self.token = token
        self.fetch = fetch or _urllib_fetch
        self.client_id = client_id or install_client_id()
        self._pin_codes: dict[int, str] = {}

    def _headers(self, with_token: bool = True) -> dict:
        h = {"Accept": "application/json", "X-Plex-Product": PRODUCT, "X-Plex-Client-Identifier": self.client_id}
        if with_token and self.token:
            h["X-Plex-Token"] = self.token
        return h

    def _call(self, method: str, url: str, with_token: bool = True) -> dict:
        status, body = self.fetch(method, url, self._headers(with_token))
        if status == 401:
            raise PlexError("Plex refused the sign-in token. Sign in to Plex again in Settings.")
        if not 200 <= status < 300:
            raise PlexError(f"Plex answered {status} for {urllib.parse.urlsplit(url).path}")
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise PlexError(f"Plex sent something that is not JSON for {urllib.parse.urlsplit(url).path}") from e

    def _get(self, endpoint: str, **params) -> dict:
        query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return self._call("GET", f"{self.url}{endpoint}" + (f"?{query}" if query else ""))

    # Libraries

    def sections(self) -> list[dict]:
        dirs = self._get("/library/sections").get("MediaContainer", {}).get("Directory") or []
        return [{"key": str(d.get("key", "")), "title": d.get("title", ""), "type": d.get("type", ""),
                 "locations": [loc.get("path", "") for loc in d.get("Location") or []]} for d in dirs]

    def section(self, title: str) -> dict | None:
        want = title.strip().casefold()
        return next((s for s in self.sections() if s["title"].casefold() == want), None)

    def refresh(self, section_key: str, path: str | None = None) -> None:
        """Ask Plex to scan a library, or just one folder of it (a path as Plex sees it)."""
        self._get(f"/library/sections/{section_key}/refresh", path=path)

    @staticmethod
    def plex_path(section: dict, rel: str) -> str:
        """rel (relative to the destination folder) under the section's first location."""
        locs = section.get("locations") or []
        if not locs:
            raise PlexError(f"The Plex library {section.get('title', '')!r} has no folders.")
        return posixpath.join(locs[0], rel.replace("\\", "/").lstrip("/"))

    def has_items(self, section_key: str, show_or_title: str) -> bool:
        """Best effort: does the library list a show or film with this title yet?"""
        mc = self._get(f"/library/sections/{section_key}/all", title=show_or_title).get("MediaContainer", {})
        want = show_or_title.casefold()
        # Plex's title filter matches substrings, so check the titles themselves.
        return any(want in (m.get("title") or "").casefold() for m in mc.get("Metadata") or [])

    # Links into Plex Web

    def identity(self) -> str:
        """The server's machineIdentifier, which Plex Web links use to name the server."""
        mid = self._get("/identity").get("MediaContainer", {}).get("machineIdentifier") or ""
        if not mid:
            raise PlexError("Plex didn't say which server it is.")
        return str(mid)

    def find(self, section_key: str, title: str, year: int | None = None) -> dict | None:
        """The show or film in a library with this title, or None. A year, when given, picks
        between remakes; a name Disc Shelf gave a country tag ("The Office (US)") also
        matches Plex's title without it."""
        bare = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
        wants = {_norm(title), _norm(bare)} - {""}
        for query in dict.fromkeys([title, bare]):
            if not query:
                continue
            mc = self._get(f"/library/sections/{section_key}/all", title=query).get("MediaContainer", {})
            hits = [m for m in mc.get("Metadata") or [] if _norm(m.get("title") or "") in wants]
            if hits:
                # Plex's title filter matches substrings, so only exact names count; the year breaks a tie.
                return next((m for m in hits if year and m.get("year") == year), hits[0])
        return None

    def children(self, rating_key: str) -> list[dict]:
        """A show's seasons (or a season's episodes)."""
        return self._get(f"/library/metadata/{rating_key}/children").get("MediaContainer", {}).get("Metadata") or []

    def season(self, show_key: str, number: int) -> dict | None:
        return next((c for c in self.children(show_key)
                     if c.get("type") == "season" and c.get("index") == number), None)

    def web_url(self, machine_id: str, rating_key: str | None = None) -> str:
        """Plex Web's page for one item, or its home page without one."""
        root = f"{self.url}/web/index.html"
        if not rating_key:
            return root
        key = urllib.parse.quote(f"/library/metadata/{rating_key}", safe="")
        return f"{root}#!/server/{machine_id}/details?key={key}"

    # The calls mediaserver.py names, shared with jellyfin.Jellyfin

    server_path = plex_path

    def find_key(self, section_key: str, title: str, year: int | None = None) -> str | None:
        item = self.find(section_key, title, year)
        return str(item["ratingKey"]) if item and item.get("ratingKey") else None

    def season_key(self, show_key: str, number: int) -> str | None:
        season = self.season(show_key, number)
        return str(season["ratingKey"]) if season and season.get("ratingKey") else None

    def home_url(self) -> str:
        return self.web_url("")

    def link(self, key: str) -> str:
        return self.web_url(self.identity(), key)

    # PIN sign-in (https://forums.plex.tv/t/authenticating-with-plex/609370)

    def pin_start(self) -> tuple[int, str, str]:
        """(pin id, code, URL for the user to open and approve this app)."""
        data = self._call("POST", f"{PLEX_TV}/pins?strong=true", with_token=False)
        pin_id, code = int(data["id"]), str(data["code"])
        self._pin_codes[pin_id] = code
        frag = urllib.parse.urlencode({"clientID": self.client_id, "code": code,
                                       "context[device][product]": PRODUCT}, quote_via=urllib.parse.quote)
        return pin_id, code, AUTH_APP + frag

    def pin_poll(self, pin_id: int, code: str | None = None) -> str | None:
        """The auth token once the user has approved the PIN, None while it is still waiting."""
        code = code or self._pin_codes.get(pin_id)
        query = f"?{urllib.parse.urlencode({'code': code})}" if code else ""
        data = self._call("GET", f"{PLEX_TV}/pins/{pin_id}{query}", with_token=False)
        return data.get("authToken") or None
