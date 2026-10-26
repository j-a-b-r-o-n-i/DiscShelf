#!/usr/bin/env python3
"""
Tests for plex.py with a fake HTTP fetch. What matters: every request carries
the Disc Shelf headers and the token (but plex.tv PIN calls do not send a
token), a one-folder scan sends the path Plex sees, not this machine's mount
path, the PIN sign-in builds the URL plex.tv documents and returns the token
only once approved, and the client id is made once and then reused.

Run:  python3 discshelf/test_plex.py    (also works under pytest)
"""
import json
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import plex as px  # noqa: E402

# Shaped like GET /library/sections with Accept: application/json on this server.
SECTIONS = {"MediaContainer": {"size": 3, "allowSync": False, "title1": "Plex Library", "Directory": [
    {"allowSync": True, "art": "/:/resources/movie-fanart.jpg", "key": "1", "type": "movie", "title": "Movies",
     "agent": "tv.plex.agents.movie", "scanner": "Plex Movie", "language": "en-US",
     "Location": [{"id": 1, "path": "/volume1/Media/Movies"}]},
    {"key": "2", "type": "show", "title": "TV Shows", "agent": "tv.plex.agents.series",
     "Location": [{"id": 2, "path": "/volume1/Media/Shows"}, {"id": 7, "path": "/volume1/Media/Shows2"}]},
    {"key": "6", "type": "show", "title": "Anime", "Location": [{"id": 6, "path": "/volume1/Media/Anime"}]},
]}}
SEARCH = {"MediaContainer": {"size": 1, "librarySectionID": 2, "Metadata": [
    {"ratingKey": "9001", "key": "/library/metadata/9001/children", "type": "show", "title": "Severance", "year": 2022}]}}
PIN = {"id": 123456789, "code": "8l5lqnorc4bibkxxgvjbdrjqa", "product": "Disc Shelf", "trusted": False,
       "clientIdentifier": "cid-1", "expiresIn": 1800, "authToken": None}


class FakeFetch:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers):
        self.calls.append((method, url, dict(headers)))
        path = urllib.parse.urlsplit(url).path
        for (m, p), (status, body) in self.routes.items():
            if m == method and p == path:
                return status, json.dumps(body).encode() if body is not None else b""
        return 404, b""


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    f = FakeFetch({("GET", "/library/sections"): (200, SECTIONS),
                   ("GET", "/library/sections/2/refresh"): (200, None),
                   ("GET", "/library/sections/2/all"): (200, SEARCH)})
    p = px.Plex("http://192.0.2.10:32400/", "tok", fetch=f, client_id="cid-1")

    secs = p.sections()
    check([s["title"] for s in secs] == ["Movies", "TV Shows", "Anime"], f"sections: {secs}")
    check(secs[1] == {"key": "2", "title": "TV Shows", "type": "show",
                      "locations": ["/volume1/Media/Shows", "/volume1/Media/Shows2"]}, f"section dict: {secs[1]}")
    _, url, h = f.calls[0]
    check(url == "http://192.0.2.10:32400/library/sections", f"url: {url}")
    check(h == {"Accept": "application/json", "X-Plex-Product": "Disc Shelf", "X-Plex-Client-Identifier": "cid-1",
                "X-Plex-Token": "tok"}, f"headers: {h}")
    check("tok" not in url, "token put in the URL")

    tv = p.section("tv shows")
    check(tv is not None and tv["key"] == "2", f"section by title: {tv}")
    check(p.section("Music") is None, "missing section found")

    rel = "Severance/Season 01/Severance - S01E01 - Good News About Hell.mkv"
    check(p.plex_path(tv, rel) == "/volume1/Media/Shows/" + rel, f"plex_path: {p.plex_path(tv, rel)}")
    check(p.plex_path(tv, "/Severance/Season 01") == "/volume1/Media/Shows/Severance/Season 01", "leading slash")

    p.refresh("2", "/volume1/Media/Shows/Severance/Season 01")
    _, url, _ = f.calls[-1]
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    check(q == {"path": ["/volume1/Media/Shows/Severance/Season 01"]} and "%2F" in url and " " not in url,
          f"refresh url: {url}")
    p.refresh("2")
    check(f.calls[-1][1].endswith("/library/sections/2/refresh"), f"whole-library refresh: {f.calls[-1][1]}")

    check(p.has_items("2", "Severance"), "has_items missed the show")
    check(urllib.parse.parse_qs(urllib.parse.urlsplit(f.calls[-1][1]).query) == {"title": ["Severance"]}, "search query")
    check(not p.has_items("2", "Andor"), "has_items found a show Plex did not list")

    # Errors.
    bad = px.Plex("http://nas:32400", "old", fetch=FakeFetch({("GET", "/library/sections"): (401, None)}), client_id="c")
    try:
        bad.sections()
        check(False, "401 did not raise")
    except px.PlexError as e:
        check("Sign in" in str(e), f"401 message: {e}")
    try:
        p.refresh("99")
        check(False, "404 did not raise")
    except px.PlexError:
        pass

    # PIN sign-in.
    f = FakeFetch({("POST", "/api/v2/pins"): (201, PIN), ("GET", f"/api/v2/pins/{PIN['id']}"): (200, PIN)})
    p = px.Plex("http://192.0.2.10:32400", "", fetch=f, client_id="cid-1")
    pin_id, code, auth = p.pin_start()
    m, url, h = f.calls[0]
    check(m == "POST" and url == "https://plex.tv/api/v2/pins?strong=true", f"pin url: {m} {url}")
    check("X-Plex-Token" not in h and h["X-Plex-Client-Identifier"] == "cid-1" and h["X-Plex-Product"] == "Disc Shelf",
          f"pin headers: {h}")
    check((pin_id, code) == (PIN["id"], PIN["code"]), "pin id/code")
    check(auth == "https://app.plex.tv/auth#?clientID=cid-1&code=8l5lqnorc4bibkxxgvjbdrjqa"
                  "&context%5Bdevice%5D%5Bproduct%5D=Disc%20Shelf", f"auth url: {auth}")
    check(p.pin_poll(pin_id) is None, "unapproved PIN gave a token")
    check(f.calls[-1][1] == f"https://plex.tv/api/v2/pins/{PIN['id']}?code={PIN['code']}", f"poll url: {f.calls[-1][1]}")
    f.routes[("GET", f"/api/v2/pins/{PIN['id']}")] = (200, dict(PIN, authToken="new-token"))
    check(p.pin_poll(pin_id) == "new-token", "approved PIN gave no token")
    check(px.Plex("http://x:32400", "", fetch=f, client_id="cid-1").pin_poll(pin_id, PIN["code"]) == "new-token",
          "poll from a new instance with the code")

    # Links into Plex Web: the server's id, an exact title (a country tag may be missing in
    # Plex), the year between remakes, a season, and the link format Plex Web reads.
    shows = {"MediaContainer": {"Metadata": [
        {"ratingKey": "10", "title": "The Office", "year": 2001},
        {"ratingKey": "11", "title": "The Office", "year": 2005},
        {"ratingKey": "12", "title": "The Office Christmas Specials", "year": 2003}]}}
    seasons = {"MediaContainer": {"Metadata": [{"ratingKey": "20", "type": "season", "index": 1},
                                               {"ratingKey": "23", "type": "season", "index": 3}]}}
    f = FakeFetch({("GET", "/identity"): (200, {"MediaContainer": {"machineIdentifier": "abc123"}}),
                   ("GET", "/library/sections/2/all"): (200, shows),
                   ("GET", "/library/metadata/11/children"): (200, seasons)})
    p = px.Plex("http://192.0.2.10:32400", "tok", fetch=f, client_id="cid-1")
    check(p.identity() == "abc123", "identity")
    check((p.find("2", "The Office (US)", 2005) or {}).get("ratingKey") == "11", "find with a country tag and year")
    check((p.find("2", "the office", None) or {}).get("ratingKey") == "10", "find without a year takes the first")
    check(p.find("2", "Office") is None, "a substring counted as the title")
    check((p.season("11", 3) or {}).get("ratingKey") == "23" and p.season("11", 2) is None, "season lookup")
    check(p.web_url("abc123", "23") == "http://192.0.2.10:32400/web/index.html#!/server/abc123/details"
                                       "?key=%2Flibrary%2Fmetadata%2F23", f"web url: {p.web_url('abc123', '23')}")
    check(p.web_url("abc123") == "http://192.0.2.10:32400/web/index.html", "web url without an item")
    bad = px.Plex("http://x:32400", "tok", fetch=FakeFetch({("GET", "/identity"): (200, {"MediaContainer": {}})}),
                  client_id="cid-1")
    try:
        bad.identity()
        check(False, "an identity without an id didn't raise")
    except px.PlexError:
        pass

    # The client id is made once and kept in secrets.json.
    with tempfile.TemporaryDirectory() as tmp:
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = tmp
        try:
            a = px.Plex("http://x:32400", "t", fetch=f).client_id
            b = px.Plex("http://x:32400", "t", fetch=f).client_id
            check(a and a == b, f"client id not stable: {a} {b}")
            path = os.path.join(tmp, "disc-shelf", "secrets.json")
            check(json.load(open(path)).get("plex_client_id") == a and (os.stat(path).st_mode & 0o777) == 0o600,
                  "client id not saved privately")
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old

    for msg in failures:
        print("FAIL:", msg)
    print("ok" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


def test_plex():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
