#!/usr/bin/env python3
"""
Tests for classify.py. What matters: on a TV disc the episodes are ticked and the
play-all, the duplicate playlist and the extras are not; on a film disc only the film
is ticked, even when it carries featurettes of similar length; and the stream pick
keeps the chosen languages, never leaves a file silent, and keeps commentary and
split-out audio cores out by default.

Run:  python3 discshelf/test_classify.py    (also works under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import makemkv as mk  # noqa: E402
from discshelf.classify import classify_titles, pick_streams  # noqa: E402
from discshelf.models import Stream, Title  # noqa: E402
from discshelf.test_makemkv import TV_DISC  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def T(index, minutes, segments=(), streams=()):
    return Title(index=index, duration_s=int(minutes * 60), segments=list(segments), streams=list(streams))


def kinds(titles):
    return {t.index: (t.kind, t.selected) for t in titles}


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def eq(got, want, what):
        check(got == want, f"{what}: got {got!r}, want {want!r}")

    # The hand-written TV disc, through the real parser
    disc = mk.build_disc(mk.parse_robot(TV_DISC.splitlines()), 0)
    eq(kinds(classify_titles(disc.titles)), {
        0: ("play_all", False), 1: ("episode", True), 2: ("episode", True), 3: ("episode", True),
        4: ("episode", True), 5: ("duplicate", False), 6: ("extra", False), 7: ("short", False),
    }, "tv disc")

    # Real Pitch Perfect scan: the film is the only pick
    with open(os.path.join(HERE, "fixtures", "pitch_perfect_info.txt")) as fh:
        pp = mk.build_disc(mk.parse_robot(fh), 0)
    got = kinds(classify_titles(pp.titles, 900))
    eq(got[0], ("feature", True), "Pitch Perfect film")
    eq(got[1], ("extra", False), "Pitch Perfect 15-minute extra")
    # Under 5 minutes is a clip; the 12-minute playlist and a 5-minute featurette are extras
    check(all(got[i] == (("short", False) if pp.titles[i].duration_s < 300 else ("extra", False))
              for i in range(2, 13)), f"Pitch Perfect clips: {got}")
    # With a 2-minute floor the 12-minute playlist is seen as a play-all of three clips
    got = kinds(classify_titles(pp.titles, 900, min_title_seconds=120))
    eq(got[2], ("play_all", False), "Pitch Perfect play-all of clips")
    eq(got[0], ("feature", True), "film still the feature with many short extras")

    # A play-all with no segment information is caught by its length
    titles = [T(0, 22.0), T(1, 22.5), T(2, 21.5), T(3, 67.0), T(4, 30)]
    eq(kinds(classify_titles(titles)), {0: ("episode", True), 1: ("episode", True), 2: ("episode", True),
                                        3: ("play_all", False), 4: ("extra", False)}, "length play-all")

    # A film with two featurettes of similar length is still a film
    titles = [T(0, 118, [1]), T(1, 22, [2]), T(2, 24, [3]), T(3, 8, [4]), T(4, 3, [5])]
    eq(kinds(classify_titles(titles)), {0: ("feature", True), 1: ("extra", False), 2: ("extra", False),
                                        3: ("extra", False), 4: ("short", False)}, "film with featurettes")

    # Real Adventure Time DVD: 16 eleven-minute episodes and a 3-hour play-all of them. DVD
    # segment numbers restart in every title, so identical "1-3" lists aren't duplicates.
    with open(os.path.join(HERE, "fixtures", "adventure_time_vol2_info.txt")) as fh:
        at = mk.build_disc(mk.parse_robot(fh), 0)
    got = kinds(classify_titles(at.titles))
    eq(got[0], ("play_all", False), "Adventure Time play-all")
    check(all(got[i] == ("episode", True) for i in range(1, len(at.titles))), f"Adventure Time episodes: {got}")
    check(len(at.titles) == 17, f"Adventure Time titles: {len(at.titles)}")

    # Two short featurettes alone aren't a TV disc: short runs need 3+ titles
    titles = [T(0, 11, [1]), T(1, 12, [2])]
    eq(kinds(classify_titles(titles)), {0: ("extra", False), 1: ("extra", False)}, "two short featurettes")

    # A film with one long documentary fails the 2x test but is still picked
    titles = [T(0, 100, [1]), T(1, 55, [2])]
    eq(kinds(classify_titles(titles)), {0: ("feature", True), 1: ("extra", False)}, "film with documentary")

    # TV disc with a longer special: the special is an extra, not a film
    titles = [T(0, 44, [1]), T(1, 43, [2]), T(2, 45, [3]), T(3, 95, [4])]
    eq(kinds(classify_titles(titles)), {0: ("episode", True), 1: ("episode", True), 2: ("episode", True),
                                        3: ("extra", False)}, "tv disc with special")

    # Reclassifying resets an earlier pick
    titles = [T(0, 44, [1])]
    titles[0].selected = True
    classify_titles(titles)
    eq(titles[0].selected, False, "lone 44-minute title is not picked")

    eq(classify_titles([]), [], "empty disc")

    # Streams
    ep = disc.titles[1]
    eq(pick_streams(ep, ["eng"], ["eng"]), {"audio": [1], "subtitle": [5, 6]}, "english only")
    eq(pick_streams(ep, ["eng"], ["eng"], include_commentary=True)["audio"], [1, 3], "with commentary, after")
    eq(pick_streams(ep, ["fra", "eng"], ["spa"]), {"audio": [4, 1], "subtitle": [7]}, "language order kept")
    eq(pick_streams(ep, ["jpn"], [])["audio"], [1], "fallback to the first audio stream")
    desc = Title(index=0, streams=[Stream(0, "video"), Stream(1, "audio", lang="eng", name="Surround 5.1"),
                                   Stream(2, "audio", lang="eng", name="English Audio Description")])
    eq(pick_streams(desc, ["ENG"], ["eng"]), {"audio": [1], "subtitle": []}, "descriptive audio left out")
    eq(pick_streams(Title(index=0), ["eng"], ["eng"]), {"audio": [], "subtitle": []}, "no streams")

    for f in failures:
        print("FAIL:", f)
    print(f"classify: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_classify():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
