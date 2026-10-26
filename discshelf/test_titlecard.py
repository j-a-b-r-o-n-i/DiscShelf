#!/usr/bin/env python3
"""
Tests for titlecard.py, on OCR lines read from the 16 titles of the Adventure Time "It Came
from the Nightosphere" DVD (fixtures/adventure_time_cards.json; no video, OCR or network).
What matters: a title is never matched "close" to the wrong episode; the theme song's
captions ("JAKE THE DOG", "FINN THE HUMAN"), which are real episode names, are never taken
for a card once a second title is read; a shortened card ("WIZARD") is flagged with every
episode it could be; and read_title reads a frame a second, then densely around big text.

Run:  python3 discshelf/test_titlecard.py    (also works under pytest)
"""
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf.models import Episode, Title  # noqa: E402
from discshelf.titlecard import (CardLine, CardMatch, TitleCardError, TitleReading, _flag_repeats,  # noqa: E402
                                 _runs, card_matches, pick_episodes, read_title)

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "adventure_time_cards.json")


def load():
    with open(FIXTURE, encoding="utf-8") as f:
        data = json.load(f)
    episodes = [Episode(**e) for e in data["episodes"]]
    readings = {}
    for i, r in data["titles"].items():
        readings[int(i)] = TitleReading(frame_h=r["frame_h"], frames=r["frames"],
                                        lines=[CardLine(text, conf, t, x, y, w, h)
                                               for t, text, conf, x, y, w, h in r["lines"]])
    truth = {int(i): tuple(se) for i, se in data["truth"].items()}
    return episodes, readings, truth


def L(text, x, y, w, h, t=0.0):
    return CardLine(text, 0.9, t, x, y, w, h)


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    episodes, readings, truth = load()
    se = lambda cm: cm.episode and (cm.episode.season, cm.episode.number)  # noqa: E731
    theme = {"Jake the Dog", "Finn the Human"}

    # Every title of the disc read.
    picks = pick_episodes(readings, episodes)
    check(sorted(picks) == sorted(readings), f"one pick per title: {sorted(picks)}")
    wrong_sure = [i for i, cm in picks.items() if cm.sure and se(cm) != truth[i]]
    check(not wrong_sure, f"sure but wrong: {[(i, picks[i].episode.title) for i in wrong_sure]}")
    sure = [i for i, cm in picks.items() if cm.sure]
    check(len(sure) >= 13, f"sure readings: {len(sure)} {sure}")
    right = [i for i, cm in picks.items() if se(cm) == truth[i]]
    check(len(right) >= 15, f"right best guesses: {len(right)}")
    check(not [i for i, cm in picks.items() if cm.episode and cm.episode.title in theme],
          f"theme captions picked: {[(i, cm.episode.title) for i, cm in picks.items() if cm.episode]}")
    for i, cm in picks.items():
        check(cm.sure == (cm.note == ""), f"title {i}: a note exactly when unsure: {cm}")

    # "WIZARD" is all the card says: Finn the Wizard on this disc, but Wizard Battle fits as well.
    wiz = picks[3]
    check(not wiz.sure and "“Finn the Wizard”" in wiz.note and "“Wizard Battle”" in wiz.note
          and wiz.note.startswith("The title card reads “WIZARD”, which could be"), f"wizard: {wiz.note!r}")

    # One title alone: the theme song can't be told from a card yet, so nothing is sure.
    one = pick_episodes({6: readings[6]}, episodes)
    check(not one[6].sure and one[6].note, f"one title is never sure: {one[6]}")

    # Two titles: the captions both show are the show's own, and the cards come through.
    two = pick_episodes({1: readings[1], 6: readings[6]}, episodes)
    check(se(two[1]) == (2, 1) and se(two[6]) == (1, 1), f"two titles: {two[1].episode}, {two[6].episode}")
    check(two[6].sure, f"slumber party panic sure with two titles: {two[6]}")

    # Re-run as each title finishes: never sure and wrong along the way.
    for k in range(1, 17):
        part = pick_episodes({i: readings[i] for i in range(1, k + 1)}, episodes)
        bad = [i for i, cm in part.items() if cm.sure and se(cm) != truth[i]]
        check(not bad, f"after {k} titles, sure but wrong: {bad}")

    # A duplicate title shows exactly the same text, so it all looks like the show's own:
    # both are left unmatched rather than one being guessed.
    dup = dict(readings)
    dup[17] = readings[6]
    dp = pick_episodes(dup, episodes)
    check(not dp[6].sure and not dp[17].sure and se(dp[6]) != (1, 1), f"duplicate: {dp[6]}")
    # Two titles that do read as one episode are both unsure, and say so.
    sp = episodes[0]
    reps = {4: CardMatch(4, sp, 1.0, "X", sure=True), 9: CardMatch(9, sp, 0.9, "Y", sure=True),
            11: CardMatch(11, sp, 1.0, "Z", note="Weak.", sure=False), 12: CardMatch(12, episodes[1], 1.0, sure=True)}
    _flag_repeats(reps)
    check([reps[i].sure for i in (4, 9, 11, 12)] == [False, False, False, True], f"repeats: {reps}")
    check(reps[4].note == f"Titles 9 and 11 also read as “{sp.title}”." and
          reps[11].note == f"Weak. Titles 4 and 9 also read as “{sp.title}”.", f"repeat notes: {reps[4].note!r}")

    # A title OCR read nothing on.
    empty = dict(readings)
    empty[18] = TitleReading(frame_h=482, frames=120)
    ep = pick_episodes(empty, episodes)
    check(ep[18].episode is None and "No title card was read" in ep[18].note, f"empty: {ep[18]}")

    # card_matches: one Match per title in index order, in match_titles' contract.
    titles = ([Title(index=0, duration_s=60, kind="short")]
              + [Title(index=i, duration_s=706, kind="episode", selected=True) for i in range(1, 17)]
              + [Title(index=17, duration_s=600, kind="extra", selected=True, source="Making_Of.m2ts")])
    del picks[16]
    ms = card_matches(picks, titles, episodes)
    check([m.title_index for m in ms] == list(range(18)), f"order: {[m.title_index for m in ms]}")
    check(ms[0].target == "skip" and ms[0].confidence == "close", f"short skipped: {ms[0]}")
    check(ms[17].target == "extra" and ms[17].extra_name == "Making Of", f"extra: {ms[17]}")
    check(ms[16].target == "skip" and ms[16].confidence == "check" and "hasn't been read" in ms[16].note,
          f"unread: {ms[16]}")
    for m in ms[1:16]:
        if m.confidence == "close":
            check(m.target == "episode" and (m.episode.season, m.episode.number) == truth[m.title_index]
                  and m.note == "", f"close but wrong: {m}")
        else:
            check(m.confidence == "check" and m.note, f"check with a note: {m}")
    check(ms[3].confidence == "check" and ms[3].episode.title in ("Finn the Wizard", "Wizard Battle"), f"{ms[3]}")
    no_card = card_matches({18: ep[18]}, [Title(index=18, duration_s=706, kind="episode", selected=True)], episodes)
    check(no_card[0].target == "skip" and no_card[0].confidence == "check"
          and "No title card was read" in no_card[0].note, f"no card: {no_card[0]}")
    # A sure card whose episode is far longer than the title still needs a look.
    long_ep = [Episode(e.season, e.number, e.title, 44) for e in episodes]
    lm = card_matches(pick_episodes(readings, long_ep), titles[:2], long_ep)
    check(lm[1].confidence == "check" and "listed as 44 min" in lm[1].note, f"runtime: {lm[1]}")

    # Runs: a name split across two rows, and one OCR broke inside a row.
    rows = _runs([L("Daydream", 206, 122, 445, 99), L("Rainy Day", 230, 40, 400, 80), L("FRED", 600, 400, 50, 20)])
    texts = {" ".join(x.text for x in r) for r in rows}
    check("Rainy Day Daydream" in texts and not any("FRED" in t and " " in t.replace("FRED", "", 1).strip()
                                                     for t in texts if t != "FRED"), f"stacked: {texts}")
    rows = _runs([L("ofa MemoRy", 372, 102, 303, 54), L("emoRy", 186, 95, 186, 63)])
    check("emoRy ofa MemoRy" in {" ".join(x.text for x in r) for r in rows}, "same row joined left to right")

    failures += _test_read_title()

    for f in failures:
        print("FAIL:", f)
    print(f"titlecard: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


PNG_480 = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (854).to_bytes(4, "big") + (480).to_bytes(4, "big")


def _test_read_title():
    """read_title with a fake ffmpeg that writes frame files and a fake OCR."""
    failures = []
    calls = []
    shown_at = {}    # frame file -> seconds into the title

    def fake_run(cmd, **kw):
        calls.append(cmd)
        start = float(cmd[cmd.index("-ss") + 1])
        dur = float(cmd[cmd.index("-t") + 1])
        vf = cmd[cmd.index("-vf") + 1]
        step = float(vf.split("prev_selected_t,")[1].split(")")[0]) + 0.05
        n = 0
        while n * step < dur:
            path = cmd[-1].replace("%d", str(round(n * step * 1000)))
            with open(path, "wb") as f:
                f.write(PNG_480)
            shown_at[path] = start + n * step
            n += 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    batches = []

    def fake_ocr(paths):
        # A title card on screen from 26 s to 28 s, and small credits at 40 s.
        batches.append(paths)
        out = []
        for p in paths:
            t = shown_at[p]
            if 26 <= t < 28:
                out.append([SimpleNamespace(text="HITMAN", conf=0.9, x=300, y=80, w=200, h=70)])
            elif 40 <= t < 41:
                out.append([SimpleNamespace(text="Story by", conf=0.9, x=300, y=200, w=100, h=20)])
            else:
                out.append([])
        return out

    r = read_title("/rips/x.mkv", 0, 60, run=fake_run, read_images=fake_ocr)
    if len(batches) != 2:
        failures.append(f"read_title: two OCR batches, got {len(batches)}")
    first, dense = calls[0], calls[1:]
    if "-skip_frame" not in first or "hue=s=0" not in first[first.index("-vf") + 1]:
        failures.append(f"read_title: first pass is grey keyframes: {first}")
    if len(dense) != 1 or "-skip_frame" in dense[0] or float(dense[0][dense[0].index("-ss") + 1]) != 25.0:
        failures.append(f"read_title: one dense colour window from 25 s: {dense}")
    times = sorted({ln.t for ln in r.lines if ln.text == "HITMAN"})
    if times != [26.0, 26.25, 26.5, 26.75, 27.0, 27.25, 27.5, 27.75]:
        failures.append(f"read_title: card times {times}")
    if r.frame_h != 480 or r.frames != 60 + len(batches[1]) or not r.lines:
        failures.append(f"read_title: frames {r.frames}, height {r.frame_h}")

    def failing(cmd, **kw):
        return SimpleNamespace(returncode=1, stdout="", stderr="Invalid data found when processing input")
    try:
        read_title("/rips/x.mkv", run=failing, read_images=fake_ocr)
        failures.append("read_title: ffmpeg failure raises TitleCardError")
    except TitleCardError as e:
        if "Invalid data" not in str(e):
            failures.append(f"read_title: error says why: {e}")
    return failures


def test_titlecard():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
