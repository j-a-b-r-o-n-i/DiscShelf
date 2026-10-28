"""The small part of the Jellyfin API Disc Shelf needs: list libraries, scan one folder, find a show
or film, and link to it in Jellyfin's web app.

Disc Shelf signs in with an API key the user makes under Dashboard → API Keys; it is kept in
secrets.json like the Plex token. As with Plex, the folders a library reports are the server's
own paths, so callers pass paths relative to the destination folder and server_path() puts them
under the library's folder as Jellyfin sees it.
"""
from __future__ import annotations

import json
import posixpath
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .mediaserver import ServerError, norm_title

PRODUCT = "Disc Shelf"
VERSION = "0.1"

# fetch(method, url, headers, body) -> (HTTP status, body). Injectable so tests need no network.
Fetch = Callable[[str, str, dict, "bytes | None"], "tuple[int, bytes]"]


class JellyfinError(ServerError):
    """Jellyfin answered with an error, or could not be reached."""


def _urllib_fetch(method: str, url: str, headers: dict, body: bytes | None = None,
                  timeout: float = 15) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except (urllib.error.URLError, OSError) as e:
        raise JellyfinError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: {e}") from e


class Jellyfin:
    name = "Jellyfin"

    def __init__(self, url: str, key: str, fetch: Fetch | None = None, device_id: str = "disc-shelf"):
        self.url = url.rstrip("/")
        self.key = key
        self.fetch = fetch or _urllib_fetch
        self.device_id = device_id
        self._server_id = ""

    def _headers(self, body: bool = False) -> dict:
        auth = (f'MediaBrowser Client="{PRODUCT}", Device="{PRODUCT}", DeviceId="{self.device_id}", '
                f'Version="{VERSION}"')
        if self.key:
            auth += f', Token="{self.key}"'
        h = {"Accept": "application/json", "Authorization": auth}
        if body:
            h["Content-Type"] = "application/json"
        return h

    def _call(self, method: str, endpoint: str, params: dict | None = None, body: dict | None = None):
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        url = f"{self.url}{endpoint}" + (f"?{query}" if query else "")
        data = json.dumps(body).encode() if body is not None else None
        status, raw = self.fetch(method, url, self._headers(data is not None), data)
        if status == 401:
            raise JellyfinError("Jellyfin refused the API key. Make a new one in Jellyfin under "
                                "Dashboard → API Keys and paste it in Settings.")
        if not 200 <= status < 300:
            raise JellyfinError(f"Jellyfin answered {status} for {endpoint}")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise JellyfinError(f"Jellyfin sent something that is not JSON for {endpoint}") from e

    # The server

    def server_name(self) -> str:
        """The server's name. Needs a working key, so it doubles as the connection test."""
        return str(self._call("GET", "/System/Info").get("ServerName") or "Jellyfin")

    def server_id(self) -> str:
        if not self._server_id:
            self._server_id = str(self._call("GET", "/System/Info/Public").get("Id") or "")
        return self._server_id

    # Libraries

    def sections(self) -> list[dict]:
        folders = self._call("GET", "/Library/VirtualFolders") or []
        return [{"key": str(f.get("ItemId", "")), "title": f.get("Name", ""), "type": f.get("CollectionType") or "",
                 "locations": list(f.get("Locations") or [])} for f in folders]

    def section(self, title: str) -> dict | None:
        want = title.strip().casefold()
        return next((s for s in self.sections() if s["title"].casefold() == want), None)

    def refresh(self, section_key: str, path: str | None = None) -> None:
        """Ask Jellyfin to scan one folder (a path as Jellyfin sees it), or the whole library."""
        if path:
            self._call("POST", "/Library/Media/Updated", body={"Updates": [{"Path": path, "UpdateType": "Created"}]})
        else:
            self._call("POST", f"/Items/{section_key}/Refresh", {"Recursive": "true"})

    @staticmethod
    def server_path(section: dict, rel: str) -> str:
        locs = section.get("locations") or []
        if not locs:
            raise JellyfinError(f"The Jellyfin library {section.get('title', '')!r} has no folders.")
        return posixpath.join(locs[0], rel.replace("\\", "/").lstrip("/"))

    def find_key(self, section_key: str, title: str, year: int | None = None) -> str | None:
        """The id of the show or film in a library with this title, or None. Like Plex.find."""
        bare = re.sub(r"\s*\([^)]*\)\s*$", "", title).strip()
        wants = {norm_title(title), norm_title(bare)} - {""}
        for query in dict.fromkeys([bare, title]):
            if not query:
                continue
            items = self._call("GET", "/Items", {"ParentId": section_key, "Recursive": "true",
                                                 "IncludeItemTypes": "Series,Movie", "SearchTerm": query,
                                                 "Fields": "ProductionYear"}).get("Items") or []
            hits = [i for i in items if norm_title(i.get("Name") or "") in wants]
            if hits:
                hit = next((i for i in hits if year and i.get("ProductionYear") == year), hits[0])
                return str(hit.get("Id") or "") or None
        return None

    def season_key(self, show_key: str, number: int) -> str | None:
        items = self._call("GET", f"/Shows/{show_key}/Seasons").get("Items") or []
        hit = next((i for i in items if i.get("IndexNumber") == number), None)
        return str(hit["Id"]) if hit and hit.get("Id") else None

    # Links into Jellyfin's web app

    def home_url(self) -> str:
        return f"{self.url}/web/"

    def link(self, key: str) -> str:
        sid = self.server_id()
        return f"{self.url}/web/#/details?id={key}" + (f"&serverId={sid}" if sid else "")
