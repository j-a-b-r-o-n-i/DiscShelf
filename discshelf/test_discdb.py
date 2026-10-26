#!/usr/bin/env python3
"""
Tests for discdb.py against a trimmed real TheDiscDB search reply
(fixtures/thediscdb_search.json: Office Space, four discs of The Office and Severance
season 1, fetched 2026-10-26) and the matching TVmaze episode lists. No network.

Scans are built from TheDiscDB's own titles, as MakeMKV would report them at a 2-minute
minimum length. What matters: a disc is found by playlist, running time and exact size;
a different pressing of the same disc is not taken for it; DVDs match by title number;
a title holding two episodes becomes a double episode; TheDiscDB's TMDB numbering is
mapped to TVmaze's by episode name; and request failures raise DiscDbError.

Run:  python3 discshelf/test_discdb.py    (also works under pytest)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf.discdb import DiscDbError, TheDiscDb, best_disc, find_disc, to_matches  # noqa: E402
from discshelf.models import Disc, Drive, Episode, Title  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
with open(os.path.join(FIXTURES, "thediscdb_search.json"), encoding="utf-8") as f:
    REPLY = json.load(f)
ITEMS = REPLY["data"]["mediaItems"]["nodes"]
with open(os.path.join(FIXTURES, "tvmaze_office_severance_episodes.json"), encoding="utf-8") as f:
    TVMAZE = {show: [Episode(season=e["season"], number=e["number"], title=e["name"], runtime_min=e["runtime"])
                     for e in eps] for show, eps in json.load(f).items()}


def _secs(s):
    h, m, sec = (int(x) for x in s.split(":"))
    return h * 3600 + m * 60 + sec


def scan(release_slug, disc_name, dvd=False, size_delta=0):
    """The scan MakeMKV would give for one TheDiscDB disc, in TheDiscDB's title order."""
    for item in ITEMS:
        for rel in item["releases"]:
            for d in rel["discs"]:
                if rel["slug"] == release_slug and d["name"] == disc_name:
                    long = [t for t in d["titles"] if _secs(t["duration"]) >= 120]
                    titles = [Title(index=i, source="" if dvd else t["sourceFile"],
                                    title_id=t["sourceFile"] if dvd else "",
                                    duration_s=_secs(t["duration"]), size_bytes=t["size"] + size_delta,
                                    kind="episode", selected=True) for i, t in enumerate(long)]
                    return Disc(drive=Drive(index=0), name=item["title"], titles=titles)
    raise KeyError((release_slug, disc_name))


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    office = TVMAZE["The Office"]

    # The Office season 3 disc 3: listed in the season set and the box set with the same titles.
    disc = scan("2021-season-3-blu-ray", "Season Three Disc Three")
    hit = best_disc(disc, ITEMS)
    check(hit is not None, "Office S3 disc 3 was not found")
    if hit:
        check(hit.media_title == "The Office" and hit.tmdb_id == "2316", f"media item: {hit}")
        check(hit.release_title == "2021 Season 3 Blu-ray", f"the season set should name it: {hit.release_title}")
        check(hit.other_releases == ["2020 Complete Series Blu-ray"], f"other releases: {hit.other_releases}")
        check((hit.matched, hit.expected) == (11, 11), f"matched/expected: {hit.matched}/{hit.expected}")
        ms = to_matches(hit, disc.titles, office)
        check([m.title_index for m in ms] == list(range(11)), "one match per title, in order")
        # TMDB counts "A Benihana Christmas" as one episode, TVmaze as two, so every number is one higher.
        bf = ms[2]
        check(bf.target == "episode" and (bf.episode.season, bf.episode.number) == (3, 15)
              and bf.confidence == "close", f"Ben Franklin by name: {bf}")
        check(bf.note.startswith("From TheDiscDB: 2021 Season 3 Blu-ray, Season Three Disc Three.")
              and "S03E14" in bf.note, f"note: {bf.note}")
        check(bf.source == "discdb", "source")
        # "Traveling Salesmen/The Return", TMDB 12-13, is TVmaze 13 and 14.
        dbl = ms[3]
        check(dbl.target == "episode" and dbl.episode.number == 13 and dbl.episode_end == 14
              and dbl.confidence == "close", f"double episode: {dbl}")
        ds = ms[4]
        check(ds.target == "extra" and ds.extra_folder == "Deleted Scenes"
              and ds.extra_name == "Traveling Salesmen and The Return Deleted Scenes", f"deleted scenes: {ds}")
        check(all(m.confidence == "close" for m in ms), [m.note for m in ms if m.confidence != "close"])

        # A title that isn't selected is skipped, as match.match_titles does.
        disc.titles[0].selected = False
        check(to_matches(hit, disc.titles, office)[0].target == "skip", "unselected title should be skipped")
        disc.titles[0].selected = True

        # TVmaze renamed an episode: the number is used, but marked "check".
        renamed = [Episode(e.season, e.number, "Happy Hour" if e.title == "Cocktails" else e.title, e.runtime_min)
                   for e in office]
        m = to_matches(hit, disc.titles, renamed)[6]
        # TVmaze's 17 is "Business School", which title 0 already has by name, so both reasons are given.
        check(m.confidence == "check" and m.episode.number == 17 and "Happy Hour" not in m.note
              and "TMDB" in m.note and "Title 0 is matched to the same episode." in m.note,
              f"number fallback: {m}")
        # No such episode on TVmaze at all.
        m = to_matches(hit, disc.titles, [e for e in renamed if e.number != 17])[6]
        check(m.target == "skip" and m.confidence == "check" and "Pick it by hand" in m.note, f"no episode: {m}")
        # No TVmaze episodes loaded: TheDiscDB's numbers, marked "check".
        m = to_matches(hit, disc.titles, [])[2]
        check(m.episode.number == 14 and m.confidence == "check" and "TMDB" in m.note, f"no TVmaze list: {m}")

    # A different pressing: same playlists and lengths, sizes a few MB off.
    check(best_disc(scan("2021-season-3-blu-ray", "Season Three Disc Three", size_delta=4096), ITEMS) is None,
          "a disc with different sizes was accepted")
    # The extended-episodes box set reuses playlist names with other sizes; it is found as itself.
    sf = best_disc(scan("2026-the-complete-series-superfan-extended-episodes-blu-ray", "Season 3 Disc 3"), ITEMS)
    check(sf is not None and sf.release_title.startswith("The Complete Series - Superfan"), f"superfan: {sf}")
    # Missing most titles: not "nearly all".
    part = scan("2021-season-3-blu-ray", "Season Three Disc Three")
    part.titles = part.titles[:5]
    check(best_disc(part, ITEMS) is None, "a scan with 5 of 11 titles was accepted")

    # DVD: The Office season 2 disc 1, matched by MakeMKV's original title id.
    dvd = scan("season-two-dvd", "Disc One", dvd=True)
    dvd.titles[3].title_id = "6"     # a dropped leading zero still matches
    hit = best_disc(dvd, ITEMS)
    check(hit is not None and hit.disc_format == "DVD", f"DVD: {hit}")
    if hit:
        ms = to_matches(hit, dvd.titles, office)
        eps = [(m.episode.season, m.episode.number) for m in ms if m.target == "episode"]
        check(eps == [(2, 1), (2, 2), (2, 3), (2, 4), (2, 5), (2, 6)], f"DVD episodes: {eps}")
        # The play-all nobody labelled falls back to the usual handling of a selected non-episode.
        check(ms[2].target == "extra" and ms[2].confidence == "check" and ms[2].source == "", f"play-all: {ms[2]}")
        check(ms[8].extra_folder == "Deleted Scenes" and ms[8].extra_name == "The Dundies", f"DVD extra: {ms[8]}")
    # Blu-ray playlist names don't match a DVD's title numbers.
    swapped = scan("season-two-dvd", "Disc One", dvd=True)
    for t in swapped.titles:
        t.source, t.title_id = f"000{t.title_id}.mpls", ""
    check(best_disc(swapped, ITEMS) is None, "DVD titles matched by playlist name")

    # A film: Office Space DVD, the feature and a trailer.
    film = scan("1999-dvd", "DVD", dvd=True)
    hit = best_disc(film, ITEMS)
    check(hit is not None and hit.media_title == "Office Space" and hit.media_type == "Movie", f"film: {hit}")
    if hit:
        ms = to_matches(hit, film.titles, [])
        check(ms[0].target == "feature" and ms[0].confidence == "close", f"feature: {ms[0]}")
        check(ms[1].target == "extra" and ms[1].extra_folder == "Trailers"
              and ms[1].extra_name == "Original Theatrical Trailer", f"trailer: {ms[1]}")

    # Severance disc 1: three episodes, out of order on the disc; the 1-minute title is too short to scan.
    sev = scan("2024-season-one-bluray", "Disc 1")
    hit = best_disc(sev, ITEMS)
    check(hit is not None and (hit.matched, hit.expected) == (3, 3), f"Severance: {hit}")
    if hit:
        ms = to_matches(hit, sev.titles, TVMAZE["Severance"])
        got = [(m.episode.number, m.confidence, m.note) for m in ms]
        check(got == [(n, "close", "From TheDiscDB: Season 1 Blu-ray, Disc 1.") for n in (3, 2, 1)],
              f"Severance matches: {got}")

    # The client: query words, caching, errors.
    calls = []

    def fetch(url, body):
        calls.append((url, json.loads(body)))
        return json.dumps(REPLY).encode()

    db = TheDiscDb(fetch=fetch)
    found = find_disc(sev, "Severance: Season 1", db=db)
    check(found is not None and found.media_title == "Severance", f"find_disc: {found}")
    url, req = calls[0]
    check(url == "https://thediscdb.com/graphql/", url)
    # Punctuation and one-character words are left out of the search words.
    check(req["variables"] == {"w0": "Severance", "w1": "Season"}, f"variables: {req['variables']}")
    check("contains: $w1" in req["query"], "each word should be its own contains filter")
    db.search("Severance: Season 1")
    check(len(calls) == 1, "a repeated search was not cached")
    check(db.search("   ") == [], "an empty query should not call TheDiscDB")
    db.search_upc("8 43501 04328 8")
    check(calls[-1][1]["variables"] == {"upc": "843501043288"}, f"UPC: {calls[-1][1]['variables']}")

    def down(url, body):
        raise OSError("Connection refused")

    for fetcher, start in ((down, "Could not reach TheDiscDB"),
                           (lambda u, b: b"<html>502</html>", "TheDiscDB sent a reply that is not JSON"),
                           (lambda u, b: b'{"errors": [{"message": "Syntax Error"}]}', "TheDiscDB refused the search"),
                           (lambda u, b: b'{"data": null}', "TheDiscDB sent a reply in an unexpected format")):
        try:
            TheDiscDb(fetch=fetcher).search("x y")
            check(False, f"no error raised for {start}")
        except DiscDbError as e:
            check(str(e).startswith(start) and str(e).endswith("."), f"error message: {e}")

    for f in failures:
        print("FAIL:", f)
    print(f"discdb: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_discdb():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
