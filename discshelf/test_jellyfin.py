#!/usr/bin/env python3
"""
Tests for jellyfin.py and mediaserver.connect() with a fake HTTP fetch. What matters: every
request carries the API key in Jellyfin's Authorization header, a one-folder scan posts the
path Jellyfin sees (not this machine's mount path), a refused key gives a sentence that says
what to do, find_key only accepts exact titles, and connect() builds the server the settings
name, or says why it can't, or nothing with no media server.

Run:  python3 discshelf/test_jellyfin.py    (also works under pytest)
"""
import json
import os
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import config, mediaserver  # noqa: E402
from discshelf import jellyfin as jf  # noqa: E402

# Shaped like GET /Library/VirtualFolders on Jellyfin.
FOLDERS = [
    {"Name": "Movies", "Locations": ["/media/Movies"], "CollectionType": "movies", "ItemId": "f137a2dd"},
    {"Name": "Shows", "Locations": ["/media/Shows"], "CollectionType": "tvshows", "ItemId": "a656b907"},
]
ITEMS = {"Items": [
    {"Name": "Severance Recap", "Id": "111", "Type": "Series", "ProductionYear": 2023},
    {"Name": "Severance", "Id": "222", "Type": "Series", "ProductionYear": 2022},
], "TotalRecordCount": 2}
SEASONS = {"Items": [{"Name": "Season 1", "Id": "s1", "IndexNumber": 1},
                     {"Name": "Season 2", "Id": "s2", "IndexNumber": 2}]}


class FakeFetch:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers), body))
        path = urllib.parse.urlsplit(url).path
        for (m, p), (status, reply) in self.routes.items():
            if m == method and p == path:
                return status, json.dumps(reply).encode() if reply is not None else b""
        return 404, b""


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    f = FakeFetch({("GET", "/Library/VirtualFolders"): (200, FOLDERS),
                   ("POST", "/Library/Media/Updated"): (204, None),
                   ("GET", "/Items"): (200, ITEMS),
                   ("GET", "/Shows/222/Seasons"): (200, SEASONS),
                   ("GET", "/System/Info"): (200, {"ServerName": "Den"}),
                   ("GET", "/System/Info/Public"): (200, {"Id": "srv9"})})
    j = jf.Jellyfin("http://192.0.2.10:8096/", "key-1", fetch=f, device_id="dev-1")

    secs = j.sections()
    check([s["title"] for s in secs] == ["Movies", "Shows"] and secs[1]["key"] == "a656b907", f"sections: {secs}")
    auth = f.calls[0][2]["Authorization"]
    check(auth.startswith('MediaBrowser Client="Disc Shelf"') and 'Token="key-1"' in auth
          and 'DeviceId="dev-1"' in auth, f"auth header: {auth}")

    shows = j.section("shows")
    rel = "Severance (2022)"
    j.refresh(shows["key"], j.server_path(shows, rel))
    m, url, h, body = f.calls[-1]
    check(m == "POST" and url == "http://192.0.2.10:8096/Library/Media/Updated"
          and json.loads(body) == {"Updates": [{"Path": "/media/Shows/Severance (2022)", "UpdateType": "Created"}]}
          and h.get("Content-Type") == "application/json", f"scan: {m} {url} {body}")

    check(j.find_key(shows["key"], "Severance (2022)", 2022) == "222", "find_key took a near title")
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(f.calls[-1][1]).query)
    check(q.get("ParentId") == ["a656b907"] and q.get("SearchTerm") == ["Severance"], f"search query: {q}")
    check(j.find_key(shows["key"], "Andor") is None, "find_key found a show Jellyfin didn't list")
    check(j.season_key("222", 2) == "s2" and j.season_key("222", 5) is None, "season_key")
    check(j.link("s2") == "http://192.0.2.10:8096/web/#/details?id=s2&serverId=srv9", f"link: {j.link('s2')}")
    check(j.server_name() == "Den", "server_name")

    bad = jf.Jellyfin("http://nas:8096", "old", fetch=FakeFetch({("GET", "/System/Info"): (401, None)}))
    try:
        bad.server_name()
        check(False, "a refused key didn't raise")
    except mediaserver.ServerError as e:
        check("API key" in str(e) and "Settings" in str(e), f"refused key message: {e}")

    # connect(): the server the settings name, a reason when it isn't set up, nothing for "none".
    saved = os.environ.get("XDG_CONFIG_HOME")
    with tempfile.TemporaryDirectory() as d:
        os.environ["XDG_CONFIG_HOME"] = d
        try:
            cfg = config.Config(media_server="none", plex_url="http://p:32400")
            check(mediaserver.connect(cfg) == (None, "") and mediaserver.home_url(cfg) == "", "none")
            cfg.media_server = "jellyfin"
            srv, why = mediaserver.connect(cfg)
            check(srv is None and "Add your Jellyfin server" in why, f"no url: {why}")
            cfg.jellyfin_url = "http://j:8096"
            srv, why = mediaserver.connect(cfg)
            check(srv is None and "API key" in why, f"no key: {why}")
            config.save_secret("jellyfin_key", "k")
            srv, why = mediaserver.connect(cfg, f)
            check(isinstance(srv, jf.Jellyfin) and srv.key == "k" and why == "", "jellyfin connect")
            check(mediaserver.home_url(cfg) == "http://j:8096/web/", "jellyfin home")
            cfg.media_server = "plex"
            srv, why = mediaserver.connect(cfg)
            check(srv is None and "Sign in to Plex" in why, f"plex unsigned: {why}")
        finally:
            if saved is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = saved

    # Editions are Plex's own tag, so another server (or none) gets name and year.
    from discshelf import naming
    cfg = config.Config(name_film="edition_quality")
    check(naming.film_choice(cfg) == "edition_quality", "Plex lost the edition choice")
    cfg.media_server = "jellyfin"
    scheme = naming.Scheme.from_config(cfg)
    check(scheme.film == "year", "Jellyfin was given {edition-…} names")
    got = naming.movie_relpath("Aliens", 1986, edition="Special Edition", scheme=scheme)
    check(got == "Aliens (1986)/Aliens (1986) - Special Edition.mkv", f"Jellyfin edition name: {got}")

    for msg in failures:
        print("FAIL:", msg)
    print(f"jellyfin: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return failures


def test_jellyfin():
    assert not run()


if __name__ == "__main__":
    sys.exit(1 if run() else 0)
