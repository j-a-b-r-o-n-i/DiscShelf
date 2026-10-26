#!/usr/bin/env python3
"""
Tests for tvmaze.py with a fake fetch that returns canned JSON laid out like TVmaze's
real replies. No network. What matters: search hits carry the year and network,
specials are left out of a season, a missing runtime falls back to the show's
average, and repeated calls are served from the cache.

Run:  python3 discshelf/test_tvmaze.py    (also works under pytest)
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf.tvmaze import TvMaze, TvMazeError  # noqa: E402

SEARCH = [
    {"score": 0.91, "show": {"id": 44933, "name": "Severance", "premiered": "2022-02-18", "averageRuntime": 52,
                             "runtime": None, "network": None, "webChannel": {"id": 310, "name": "Apple TV+"}}},
    {"score": 0.4, "show": {"id": 1, "name": "Severance Pay", "premiered": None, "averageRuntime": None,
                            "network": {"id": 2, "name": "CBS"}, "webChannel": None}},
]
SHOW = {
    "id": 44933, "name": "Severance", "premiered": "2022-02-18", "averageRuntime": 52, "runtime": None,
    "network": None, "webChannel": {"name": "Apple TV+"},
    "_embedded": {"episodes": [
        {"id": 10, "name": "Good News About Hell", "season": 1, "number": 1, "runtime": 57},
        {"id": 12, "name": "Half Loop", "season": 1, "number": 3, "runtime": None},
        {"id": 11, "name": "Dear Ms. Cobel", "season": 1, "number": 2, "runtime": 53},
        {"id": 13, "name": "Behind the scenes", "season": 1, "number": None, "runtime": 20, "type": "significant_special"},
        {"id": 20, "name": "Hello, Ms. Cobel", "season": 2, "number": 1, "runtime": 49},
    ]},
}


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    calls = []

    def fetch(url):
        calls.append(url)
        if "/search/shows?q=" in url:
            return json.dumps(SEARCH).encode()
        if url.endswith("/shows/44933?embed=episodes"):
            return json.dumps(SHOW).encode()
        if url.endswith("/shows/44933"):
            return json.dumps({k: v for k, v in SHOW.items() if k != "_embedded"}).encode()
        raise OSError("404")

    tv = TvMaze(fetch=fetch)
    hits = tv.search("severance ")
    check(calls[0] == "https://api.tvmaze.com/search/shows?q=severance", f"search URL: {calls[0]}")
    check([h.id for h in hits] == [44933, 1], f"hit ids: {hits}")
    check(hits[0].year == 2022 and hits[0].network == "Apple TV+", f"first hit: {hits[0]}")
    check(hits[1].year is None and hits[1].network == "CBS", f"second hit: {hits[1]}")
    check(tv.search("   ") == [], "an empty query should not call TVmaze")

    tv.search("severance")
    check(len(calls) == 1, f"a repeated search was not cached: {calls}")
    tv.search("the office")
    check(calls[-1].endswith("q=the%20office"), f"query not URL-encoded: {calls[-1]}")

    eps = tv.episodes(44933, 1)
    check([e.number for e in eps] == [1, 2, 3], f"season 1 numbers (special left out, sorted): {eps}")
    check(eps[0].title == "Good News About Hell" and eps[0].season == 1, f"first episode: {eps[0]}")
    check(eps[2].runtime_min == 52, f"missing runtime should fall back to averageRuntime: {eps[2]}")
    n = len(calls)
    check([e.number for e in tv.episodes(44933, 2)] == [1], "season 2")
    check(len(calls) == n, "a second season of the same show was fetched again")
    every = [(e.season, e.number) for e in tv.episodes(44933, None)]
    check(every == [(1, 1), (1, 2), (1, 3), (2, 1)], f"every season: {every}")
    check(tv.seasons(44933) == [1, 2], f"seasons: {tv.seasons(44933)}")
    check(tv.show(44933).average_runtime == 52, "show() average runtime")

    try:
        tv.episodes(999, 1)
        check(False, "a failed request did not raise TvMazeError")
    except TvMazeError as e:
        check(str(e).startswith("Could not reach TVmaze"), f"error message: {e}")

    bad = TvMaze(fetch=lambda url: b"<html>")
    try:
        bad.search("x")
        check(False, "a non-JSON reply did not raise")
    except TvMazeError:
        pass

    for f in failures:
        print("FAIL:", f)
    print(f"tvmaze: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_tvmaze():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
