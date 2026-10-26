#!/usr/bin/env python3
"""
Tests for match.py. What matters: episodes are handed out in disc order from the
right starting number, a title as long as two episodes becomes a double episode,
running out of episodes skips the title with a note, shuffled discs are flagged
but never reordered, and unselected titles are skipped.

Run:  python3 discshelf/test_match.py    (also works under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf.match import (extra_name, feature_match, find_episode, match_list, match_titles,  # noqa: E402
                             next_episode_number, parse_episode_list)
from discshelf.models import Episode, Title  # noqa: E402


def T(index, minutes, kind="episode", selected=True, source=""):
    return Title(index=index, source=source or f"{index:05d}.mpls", duration_s=int(minutes * 60), kind=kind,
                 selected=selected)


def eps(*runtimes, season=1):
    return [Episode(season=season, number=i + 1, title=f"Ep {i + 1}", runtime_min=r) for i, r in enumerate(runtimes)]


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    check(next_episode_number(set()) == 1, "empty set")
    check(next_episode_number({1, 2, 3, 4}) == 5, "continue after disc 1")
    check(next_episode_number({1, 2, 4}) == 3, "first gap")

    # A plain disc: three episodes in order, a play-all left unselected, an extra selected.
    titles = [T(0, 130, "play_all", selected=False), T(1, 44), T(2, 43), T(3, 45), T(4, 12, "extra", source="Making_of.m2ts")]
    ms = match_titles(titles, eps(44, 44, 44, 44), start_number=1)
    check([m.title_index for m in ms] == [0, 1, 2, 3, 4], f"one match per title in order: {ms}")
    check(ms[0].target == "skip", "unselected play-all is skipped")
    check([m.episode.number for m in ms[1:4]] == [1, 2, 3], f"sequential: {ms[1:4]}")
    check(all(m.confidence == "close" for m in ms[1:4]), f"all close: {[m.confidence for m in ms[1:4]]}")
    check(ms[4].target == "extra" and ms[4].extra_name == "Making Of", f"extra: {ms[4]}")

    # Disc 2 starts at episode 4.
    ms = match_titles([T(0, 44), T(1, 44)], eps(44, 44, 44, 44, 44), start_number=4)
    check([m.episode.number for m in ms] == [4, 5], f"start number: {ms}")

    # Running out of episodes.
    ms = match_titles([T(0, 44), T(1, 44)], eps(44, 44, 44, 44), start_number=4)
    check(ms[1].target == "skip" and ms[1].confidence == "check" and "no episode 5" in ms[1].note, f"ran out: {ms[1]}")

    # A double episode: 44 min title against 22 min episodes.
    ms = match_titles([T(0, 44), T(1, 22)], eps(22, 22, 22))
    check(ms[0].episode.number == 1 and ms[0].episode_end == 2 and ms[0].confidence == "check", f"double: {ms[0]}")
    check(ms[1].episode.number == 3 and ms[1].confidence == "close", f"after double: {ms[1]}")

    # Twice as long but no next episode: a check with a plain note, no double.
    ms = match_titles([T(0, 43)], eps(22))
    check(ms[0].episode_end is None and ms[0].confidence == "check", f"no next episode: {ms[0]}")
    check(ms[0].note == "Title is 43 min but the episode is listed as 22 min, so it may hold two episodes.",
          f"note: {ms[0].note!r}")

    # Broadcast slot runtimes: 42 min titles against TVmaze's 60 min listing are fine.
    ms = match_titles([T(0, 42), T(1, 41)], eps(60, 60))
    check(all(m.confidence == "close" for m in ms), f"slot runtimes: {ms}")

    # Shuffled disc: the 30 min and 50 min episodes are swapped on the disc.
    ms = match_titles([T(0, 50), T(1, 30), T(2, 40)], eps(30, 50, 40))
    check([m.episode.number for m in ms] == [1, 2, 3], f"never reordered: {ms}")
    check(ms[0].confidence == "check" and "Order unclear" in ms[0].note and "episode 2" in ms[0].note, f"flag 0: {ms[0]}")
    check(ms[1].confidence == "check" and "episode 1" in ms[1].note, f"flag 1: {ms[1]}")
    check(ms[2].confidence == "close" and ms[2].note == "", f"in-place title untouched: {ms[2]}")

    # No runtime on TVmaze.
    ms = match_titles([T(0, 44)], [Episode(1, 1, "Pilot", None)])
    check(ms[0].confidence == "check" and "no running time" in ms[0].note, f"no runtime: {ms[0]}")

    # Deterministic.
    a = match_titles([T(0, 50), T(1, 30)], eps(30, 50))
    b = match_titles([T(1, 30), T(0, 50)], eps(30, 50))
    check(a == b, "result depends on input order")

    check(extra_name(T(7, 5, "extra", source="00800.mpls")) == "Title 07", extra_name(T(7, 5, source="00800.mpls")))
    check(extra_name(T(7, 5, source="deleted-scenes.m2ts")) == "Deleted Scenes", "words from source")

    # Films.
    ms = feature_match([T(0, 120, "feature"), T(1, 118, "feature"), T(2, 10, "extra"), T(3, 2, "short", selected=False)])
    check([m.target for m in ms] == ["feature", "extra", "extra", "skip"], f"feature targets: {ms}")
    check(ms[1].confidence == "check", "second feature flagged")
    ms = feature_match([T(0, 10, "unknown"), T(1, 115, "unknown")])
    check(ms[1].target == "feature" and ms[1].confidence == "check", f"fallback to longest: {ms}")
    check(feature_match([T(0, 10, selected=False)])[0].target == "skip", "nothing selected")

    # 11-minute cartoons against TVmaze's 15-minute slot fit.
    ms = match_titles([T(1, 11.7), T(2, 11.9), T(3, 11.8)], eps(15, 15, 15))
    check([m.confidence for m in ms] == ["close"] * 3, f"quarter-hour slot: {[m.note for m in ms]}")

    # A volume DVD that mixes seasons, matched from the list on the case.
    cartoon = [Episode(1, 1, "Slumber Party Panic", 15), Episode(1, 11, "Finn the Wizard", 15),
               Episode(2, 1, "It Came from the Nightosphere", 15), Episode(3, 3, "Memory of a Memory", 15),
               Episode(3, 8, "Wizard Battle", 15), Episode(3, 12, "The Creeps", 15)]
    names = parse_episode_list("1. IT CAME FROM THE NIGHTOSPHERE\n\n2 Wizard\n3) Memory of a Memry\n"
                               "10. SLUMBER PARTY PANIC\nThe Creeps\n")
    check(names == ["IT CAME FROM THE NIGHTOSPHERE", "Wizard", "Memory of a Memry", "SLUMBER PARTY PANIC",
                    "The Creeps"], f"parsed list: {names}")
    check(find_episode("Wizard", cartoon)[:2] == [cartoon[1], cartoon[4]], "a shortened name goes to the earliest fit")
    check(find_episode("Nothing Like It", cartoon) == [], "an unknown name matches nothing")
    titles = [T(0, 1, "short", selected=False)] + [T(i, 11.7) for i in range(1, 8)]
    ms = match_list(titles, cartoon, names + ["Not On TVmaze"])
    got = [(m.target, m.episode and (m.episode.season, m.episode.number), m.confidence) for m in ms]
    check(got == [("skip", None, "close"), ("episode", (2, 1), "close"), ("episode", (1, 11), "check"),
                  ("episode", (3, 3), "check"), ("episode", (1, 1), "close"), ("episode", (3, 12), "close"),
                  ("skip", None, "check"), ("skip", None, "check")], f"list matches: {got}")
    check("Wizard Battle" in ms[2].note and "Not On TVmaze" in ms[6].note and "6 names" in ms[7].note,
          f"list notes: {[m.note for m in ms]}")

    for f in failures:
        print("FAIL:", f)
    print(f"match: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_match():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
