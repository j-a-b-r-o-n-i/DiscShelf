#!/usr/bin/env python3
"""
Tests for naming.py. What matters: names are legal on the SMB share, episode, double
episode, version and movie paths follow Plex's guides exactly, extras land in a folder
Plex reads, and two files never get the same path.

Run:  python3 discshelf/test_naming.py    (also works under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf.models import Episode, Job, Match  # noqa: E402
from discshelf.naming import (dedupe, episode_relpath, extra_relpath, movie_relpath, relpath_for,  # noqa: E402
                              safe_name, show_folder)


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def eq(got, want):
        check(got == want, f"got {got!r}, want {want!r}")

    eq(safe_name("Alien: Romulus"), "Alien - Romulus")
    eq(safe_name('What?  If <A>/"B"\\|*'), "What If AB")
    eq(safe_name("Mr. Robot..."), "Mr. Robot")
    eq(safe_name("  Trailing space . "), "Trailing space")
    eq(show_folder("Severance", 2022), "Severance (2022)")
    eq(show_folder("Severance", None), "Severance")

    eq(episode_relpath("Severance", 2022, 1, 1), "Severance (2022)/Season 01/Severance (2022) - S01E01.mkv")
    eq(episode_relpath("Severance", 2022, 1, 1, episode_end=2), "Severance (2022)/Season 01/Severance (2022) - S01E01-E02.mkv")
    eq(episode_relpath("Severance", None, 2, 10, version="2160p"), "Severance/Season 02/Severance - S02E10 - 2160p.mkv")
    eq(movie_relpath("Prometheus", 2012), "Prometheus (2012)/Prometheus (2012).mkv")
    eq(movie_relpath("Prometheus", 2012, "2160p"), "Prometheus (2012)/Prometheus (2012) - 2160p.mkv")
    eq(extra_relpath("Prometheus (2012)", "Making Of"), "Prometheus (2012)/Featurettes/Making Of.mkv")
    eq(extra_relpath("X", "Cut: one", "Deleted Scenes"), "X/Deleted Scenes/Cut - one.mkv")
    eq(extra_relpath("X", "Y", "behind the scenes"), "X/Behind The Scenes/Y.mkv")
    eq(extra_relpath("X", "Y", "Bloopers"), "X/Other/Y.mkv")

    tv = Job(id="j", show="Severance", year=2022, season=1, version="")
    eq(relpath_for(tv, Match(0, "episode", Episode(1, 3, "Half Loop", 52)), "tv"),
       "Severance (2022)/Season 01/Severance (2022) - S01E03.mkv")
    eq(relpath_for(tv, Match(1, "episode", Episode(1, 4), episode_end=5), "tv"),
       "Severance (2022)/Season 01/Severance (2022) - S01E04-E05.mkv")
    eq(relpath_for(tv, Match(2, "extra", extra_name="Inside Lumon", extra_folder="Interviews"), "tv"),
       "Severance (2022)/Interviews/Inside Lumon.mkv")
    eq(relpath_for(tv, Match(3, "skip"), "tv"), None)
    film = Job(id="f", show="Alien: Romulus", year=2024, version="2160p")
    eq(relpath_for(film, Match(0, "feature"), "movie"), "Alien - Romulus (2024)/Alien - Romulus (2024) - 2160p.mkv")
    for job, m, why in [(Job(id="n"), Match(0, "feature"), "no name"),
                        (tv, Match(0, "episode"), "episode with no Episode")]:
        try:
            relpath_for(job, m, "tv")
            check(False, f"{why}: no ValueError")
        except ValueError:
            pass

    eq(dedupe(["a/Making Of.mkv", None, "a/making of.mkv", "a/Making Of.mkv", "a/Other.mkv"]),
       ["a/Making Of.mkv", None, "a/making of (2).mkv", "a/Making Of (3).mkv", "a/Other.mkv"])
    eq(dedupe(["a/X.mkv", "a/X (2).mkv", "a/X.mkv"]), ["a/X.mkv", "a/X (2).mkv", "a/X (3).mkv"])

    for f in failures:
        print("FAIL:", f)
    print(f"naming: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_naming():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
