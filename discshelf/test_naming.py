#!/usr/bin/env python3
"""
Tests for naming.py. What matters: names are legal on the SMB share, episode, double
episode, version and movie paths follow Plex's guides exactly under every file-name scheme
(Settings → File names), extras land in a folder Plex reads, and two files never get the same path.

Run:  python3 discshelf/test_naming.py    (also works under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf.config import Config  # noqa: E402
from discshelf.models import Episode, Job, Match, Stream, Title  # noqa: E402
from discshelf.naming import (Scheme, clean, dedupe, double_title, episode_relpath, existing_show_folder,  # noqa: E402
                              extra_relpath,
                              movie_relpath, quality_label, relpath_for, safe_name, season_folder, show_folder)


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
       "Severance (2022)/Season 01/Severance (2022) - S01E03 - Half Loop.mkv")
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

    # Schemes. The default is Plex's example with the episode name.
    eq(Scheme.from_config(Config()), Scheme())
    cfg = Config(name_episode="name_quality", name_show_year=False, name_season_pad=False, name_film="edition",
                 name_safe_chars=False)
    eq(Scheme.from_config(cfg), Scheme("name_quality", False, False, "edition", False))
    code, bare = Scheme(episode="code"), Scheme(show_year=False, season_pad=False)
    q = Scheme(episode="name_quality")
    eq(episode_relpath("Bluey", 2018, 1, 20, title="Markets"),
       "Bluey (2018)/Season 01/Bluey (2018) - S01E20 - Markets.mkv")
    eq(episode_relpath("Bluey", 2018, 1, 20, title="Markets", scheme=code),
       "Bluey (2018)/Season 01/Bluey (2018) - S01E20.mkv")
    eq(episode_relpath("Bluey", 2018, 1, 20, title="Markets", scheme=bare),
       "Bluey/Season 1/Bluey - S01E20 - Markets.mkv")
    eq(episode_relpath("Bluey", 2018, 1, 20, title="Markets", quality="1080p", scheme=q),
       "Bluey (2018)/Season 01/Bluey (2018) - S01E20 - Markets [1080p].mkv")
    eq(episode_relpath("Bluey", 2018, 1, 20, title="Markets", scheme=q),      # quality unknown: no tag
       "Bluey (2018)/Season 01/Bluey (2018) - S01E20 - Markets.mkv")
    eq(season_folder(3), "Season 03")
    eq(season_folder(3, bare), "Season 3")
    eq(show_folder("Bluey", 2018, bare), "Bluey")

    # A second cut keeps the episode's code and name, so Plex lists both under one episode.
    eq(episode_relpath("Bluey", 2018, 1, 20, version="Short cut", title="Markets"),
       "Bluey (2018)/Season 01/Bluey (2018) - S01E20 - Markets - Short cut.mkv")
    eq(episode_relpath("Bluey", 2018, 1, 20, version="Short cut", title="Markets", quality="1080p", scheme=q),
       "Bluey (2018)/Season 01/Bluey (2018) - S01E20 - Markets - Short cut [1080p].mkv")
    eq(episode_relpath("Bluey", 2018, 1, 20, version="1080p", title="Markets", quality="1080p", scheme=q),
       "Bluey (2018)/Season 01/Bluey (2018) - S01E20 - Markets [1080p].mkv")
    blu = Job(id="b", show="Bluey", year=2018, season=1, version="1080p",
              titles=[Title(26, streams=[Stream(0, "video", resolution="1920x1080")])])
    eq(relpath_for(blu, Match(26, "episode", Episode(1, 20, "Markets"), version="Short cut"), "tv"),
       "Bluey (2018)/Season 01/Bluey (2018) - S01E20 - Markets - Short cut.mkv")    # Match.version wins
    eq(relpath_for(blu, Match(26, "episode", Episode(1, 20, "Markets")), "tv", q),
       "Bluey (2018)/Season 01/Bluey (2018) - S01E20 - Markets [1080p].mkv")

    # Double episodes.
    eq(double_title("Encounter at Farpoint (1)", "Encounter at Farpoint (2)"), "Encounter at Farpoint")
    eq(double_title("The Best of Both Worlds, Part 1", "The Best of Both Worlds, Part 2"), "The Best of Both Worlds")
    eq(double_title("Background Blues", "And Now Let's Talk to Some Kids"),
       "Background Blues & And Now Let's Talk to Some Kids")
    eq(double_title("Pilot", ""), "Pilot")
    eq(episode_relpath("Star Trek: The Next Generation", 1987, 1, 1, 2, title="Encounter at Farpoint (1)",
                       end_title="Encounter at Farpoint (2)"),
       "Star Trek - The Next Generation (1987)/Season 01/"
       "Star Trek - The Next Generation (1987) - S01E01-E02 - Encounter at Farpoint.mkv")
    eq(relpath_for(tv, Match(1, "episode", Episode(1, 4, "A"), episode_end=5), "tv", end_title="B"),
       "Severance (2022)/Season 01/Severance (2022) - S01E04-E05 - A & B.mkv")

    # Characters a Windows share can't store, and the two that no file name can hold.
    eq(episode_relpath("Show", None, 1, 1, title='What? "Why" A/B: C*'),
       "Show/Season 01/Show - S01E01 - What Why AB - C.mkv")
    raw = Scheme(safe_chars=False)
    eq(episode_relpath("Alien: Romulus", None, 1, 1, title='What? A/B\x00: C', scheme=raw),
       "Alien: Romulus/Season 01/Alien: Romulus - S01E01 - What? AB: C.mkv")
    eq(clean("a/b\x00c", raw), "abc")

    # A show that already has a folder in the destination: new rips join it.
    names = ["Severance", "bluey (2018)", "The Office (2001)", "Other"]
    eq(existing_show_folder(names, "Severance", 2022), "Severance")
    eq(existing_show_folder(names, "Bluey", 2018), "bluey (2018)")
    eq(existing_show_folder(names, "Bluey", None), "bluey (2018)")
    eq(existing_show_folder(names, "The Office", 2005), None)       # another show of that name
    eq(existing_show_folder(names, "Andor", 2022), None)
    eq(existing_show_folder(["Alien - Romulus"], "Alien: Romulus", None), "Alien - Romulus")
    eq(relpath_for(tv, Match(0, "episode", Episode(1, 3, "Half Loop")), "tv", show_dir="Severance"),
       "Severance/Season 01/Severance - S01E03 - Half Loop.mkv")
    eq(relpath_for(tv, Match(2, "extra", extra_name="Inside Lumon", extra_folder="Interviews"), "tv",
                   show_dir="Severance"), "Severance/Interviews/Inside Lumon.mkv")
    eq(episode_relpath("Star Trek", 1966, 1, 1, title="The Man Trap", show_dir="Star.Trek.S01.1080p",
                       prefix="Star Trek (1966)"),
       "Star.Trek.S01.1080p/Season 01/Star Trek (1966) - S01E01 - The Man Trap.mkv")

    # Films.
    ed, edq = Scheme(film="edition"), Scheme(film="edition_quality")
    eq(movie_relpath("Aliens", 1986), "Aliens (1986)/Aliens (1986).mkv")
    eq(movie_relpath("Aliens", 1986, edition="Special Edition"), "Aliens (1986)/Aliens (1986) - Special Edition.mkv")
    eq(movie_relpath("Aliens", 1986, edition="Special Edition", scheme=ed),
       "Aliens (1986)/Aliens (1986) {edition-Special Edition}.mkv")
    eq(movie_relpath("Aliens", 1986, "2160p", edition="Special Edition", quality="2160p", scheme=edq),
       "Aliens (1986)/Aliens (1986) {edition-Special Edition} [2160p].mkv")
    eq(movie_relpath("Aliens", 1986, quality="2160p", scheme=edq), "Aliens (1986)/Aliens (1986) [2160p].mkv")
    eq(movie_relpath("Aliens", 1986, "2160p", quality="2160p", scheme=ed), "Aliens (1986)/Aliens (1986) - 2160p.mkv")
    eq(movie_relpath("Aliens", 1986, scheme=Scheme(show_year=False)), "Aliens (1986)/Aliens (1986).mkv")
    eq(quality_label("3840x2160"), "2160p")
    eq(quality_label("1920x800"), "1080p")
    eq(quality_label("1280x720"), "720p")
    eq(quality_label("720x576"), "576p")
    eq(quality_label(""), "")

    for f in failures:
        print("FAIL:", f)
    print(f"naming: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_naming():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
