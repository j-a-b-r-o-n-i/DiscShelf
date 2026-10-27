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
import random
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf.models import Episode, Title  # noqa: E402
from discshelf.theme import ITEM_S, ThemeBook  # noqa: E402
from discshelf.titlecard import (CardLine, CardMatch, TitleCardError, TitleReading, _flag_repeats,  # noqa: E402
                                 _runs, card_matches, pick_episodes, place_theme, read_title, scan_title)

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
    failures += _test_scan_title()
    failures += _test_card_times()

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


PNG_360 = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (640).to_bytes(4, "big") + (360).to_bytes(4, "big")


class FakeTitle:
    """A fake ffmpeg and OCR for one title: shows[i] = (from, to, lines, colour_only), where
    lines are (text, x, y, w, h) on an 854-wide frame and colour_only text is lost on small
    grey frames (or, when it is "grey", only shows on them). fpcalc gives fps[path], or fails when the path has none."""

    def __init__(self, shows, fps=None):
        self.shows, self.fps = shows, fps or {}
        self.calls, self.at, self.batches = [], {}, []

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "fpcalc":
            fp = self.fps.get(cmd[-1])
            if fp is None:
                raise FileNotFoundError("fpcalc")
            fp = fp[:int(float(cmd[cmd.index("-length") + 1]) / ITEM_S)]
            return SimpleNamespace(returncode=0, stdout=json.dumps({"fingerprint": fp}), stderr="")
        start, dur = float(cmd[cmd.index("-ss") + 1]), float(cmd[cmd.index("-t") + 1])
        vf = cmd[cmd.index("-vf") + 1]
        step = float(vf.split("prev_selected_t,")[1].split(")")[0]) + 0.05
        small, grey = "scale=640:" in vf, "hue=s=0" in vf
        n = 0
        while n * step < dur - 1e-6 and start + n * step < 600:     # a 10-minute title
            path = cmd[-1].replace("%d", str(round(n * step * 1000)))
            with open(path, "wb") as f:
                f.write(PNG_360 if small else PNG_480)
            self.at[path] = (round(start + n * step, 2), small, grey)
            n += 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def ocr(self, paths):
        self.batches.append(paths)
        out = []
        for p in paths:
            t, small, grey = self.at[p]
            k = 640 / 854 if small else 1.0
            lines = []
            for a, b, shown, colour_only in self.shows:
                lost = (not grey) if colour_only == "grey" else colour_only and (small or grey)
                if a <= t < b and not lost:
                    lines += [SimpleNamespace(text=x[0], conf=0.9, x=x[1] * k, y=x[2] * k, w=x[3] * k, h=x[4] * k)
                              for x in shown]
            out.append(lines)
        return out

    def starts(self):
        return [float(c[c.index("-ss") + 1]) for c in self.calls if c[0] != "fpcalc"]


EPISODES = [Episode(1, n, name, 7) for n, name in enumerate(
    ["Hospital", "Mums and Dads", "Mount Mumandad", "Daddy Putdown", "Fairies", "Takeaway", "The Creek"], 1)]


def card(name, a, colour_only=False):
    """A card on screen for 2 s from a."""
    return (a, a + 2.0, [(name, 250, 180, 350, 90)], colour_only)


def _song_fp(rng, song, at_s, n=2423):
    k = int(at_s / ITEM_S)
    return ([rng.getrandbits(32) for _ in range(k)] + song + [rng.getrandbits(32) for _ in range(n)])[:n]


def _test_scan_title():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(f"scan_title: {msg}")

    def scan(shows, limit_s=300.0, eps=EPISODES, **kw):
        ft = FakeTitle(shows, kw.pop("fps", None))
        r = scan_title(kw.pop("path", "/rips/x.mkv"), eps, limit_s=limit_s, run=ft.run, read_images=ft.ocr, **kw)
        return ft, r

    def picked(r, others=()):
        rs = {1: r}
        rs.update({i + 2: o for i, o in enumerate(others)})
        return pick_episodes(rs, EPISODES)[1]

    # A card 40 s in: the scan stops there, after a few frames, not at the limit.
    ft, r = scan([card("HOSPITAL", 40.5)])
    check(r.stopped_early and r.card_t is not None and 40 <= r.card_t < 43, f"card at 40 s: {r.card_t}")
    check(max(ft.starts()) < 60 and r.frames < 40, f"stopped early: starts {ft.starts()}, {r.frames} frames")
    check(picked(r).episode.title == "Hospital", f"hospital: {picked(r)}")
    check("-skip_frame" in ft.calls[0] and "scale=640:" in ft.calls[0][ft.calls[0].index("-vf") + 1],
          f"first pass is small keyframes: {ft.calls[0]}")
    check(all(len(b) <= 12 for b in ft.batches), f"OCR in small batches: {[len(b) for b in ft.batches]}")

    # A card at 4:30 is found with a limit of 5 minutes, and not with 2.
    ft, r = scan([card("TAKEAWAY", 270.5)])
    check(r.stopped_early and 270 <= (r.card_t or 0) < 273, f"card at 4:30: {r.card_t}")
    ft, r = scan([card("TAKEAWAY", 270.5)], limit_s=120)
    check(not r.stopped_early and r.card_t is None and max(ft.starts()) < 120 and r.end_s == 120,
          f"limit 2 minutes: {r.card_t}, starts {ft.starts()}")
    check(picked(r).episode is None, "nothing read within 2 minutes")

    # A name set over two lines is read as one.
    ft, r = scan([(60.5, 62.5, [("DOWN", 330, 260, 190, 80), ("DADDY PUT", 220, 160, 400, 80)], False)])
    cm = picked(r)
    check(cm.episode is not None and cm.episode.title == "Daddy Putdown" and cm.text == "DADDY PUT DOWN",
          f"two lines: {cm.episode} {cm.text!r}")

    # A card drawn on letter by letter: half drawn it reads as another episode, and the
    # frames read in colour around it decide.
    ft, r = scan([(66.5, 67.0, [("MumanDaD", 250, 180, 300, 90)], False), card("MOUNT MUMANDAD", 67.0)])
    cm = picked(r)
    check(cm.episode is not None and cm.episode.title == "Mount Mumandad", f"half-drawn: {cm.episode} {cm.text}")
    # One frame naming an episode, with nothing around it agreeing, isn't a card: the scan goes on.
    ft, r = scan([(50.0, 50.2, [("MUMS AND DADS", 250, 180, 350, 90)], False), card("THE CREEK", 90.5)])
    check(r.card_t is not None and r.card_t >= 90, f"a flash isn't a card: {r.card_t}")
    cm = picked(r)
    check(cm.episode is not None and cm.episode.title == "The Creek", f"flash: {cm.episode}")

    # Lettering lost in small grey frames but read in colour: the big text is read again.
    ft, r = scan([(94.0, 96.5, [("FaI", 300, 200, 250, 100)], False), card("FAIRIES", 94.0, colour_only=True)])
    check(r.card_t is not None and 94 <= r.card_t < 97, f"colour re-read: {r.card_t}")

    # The theme song recognised by its sound: reading starts where it ends.
    rng = random.Random(3)
    song = [rng.getrandbits(32) for _ in range(int(30 / ITEM_S))]
    with tempfile.TemporaryDirectory() as d:
        book = ThemeBook("show", d)
        fps = {"/rips/a.mkv": _song_fp(rng, song, 10), "/rips/b.mkv": _song_fp(rng, song, 70),
               "/rips/c.mkv": _song_fp(rng, song, 120)}
        # The first title is read from the start; the theme isn't known yet.
        ft, ra = scan([card("THE CREEK", 20.5), card("HOSPITAL", 60.5)], themes=book, fps=fps, path="/rips/a.mkv")
        check(ra.theme_end_s is None and ft.starts()[0] == 0.0, f"first title from 0: {ra.theme_end_s}")
        check(ft.calls[0][:4] == ["fpcalc", "-raw", "-length", "120"]
              and ft.calls[1][:4] == ["fpcalc", "-raw", "-length", "300"],
              f"fpcalc first, two minutes then five: {ft.calls[:2]}")
        # The second learns it from the first, and starts after it.
        ft, rb = scan([card("TAKEAWAY", 30.5), card("FAIRIES", 125.5)], themes=book, fps=fps, path="/rips/b.mkv")
        check(rb.theme_end_s is not None and 99 <= rb.theme_end_s <= 101 and 99 <= ft.starts()[0] <= 101,
              f"second title after the theme: {rb.theme_end_s}, starts {ft.starts()[:2]}")
        check(rb.card_t is not None and rb.card_t >= 125, f"text before the theme is skipped: {rb.card_t}")
        # The first title's card was during what turned out to be the theme: worth reading again.
        check(place_theme(ra, book) and 8 <= ra.theme_start_s <= 11, f"place later: {ra.theme_start_s}")
        # A card the theme position hides (nothing after it within the limit) is still found.
        ft, rc = scan([card("BIKE", 5.0), card("THE CREEK", 60.5)], themes=book, fps=fps, path="/rips/c.mkv",
                      limit_s=240)
        check(rc.theme_end_s is not None and rc.card_t is not None and 60 <= rc.card_t < 63,
              f"card before the theme found: {rc.card_t}, theme {rc.theme_start_s}-{rc.theme_end_s}")
        check(rc.start_s == 0.0, f"read from the start in the end: {rc.start_s}")
        check([c[3] for c in ft.calls if c[0] == "fpcalc"] == ["120", "240"],
              f"a theme past two minutes, fingerprinted up to the limit: {ft.calls[:2]}")
        fps["/rips/e.mkv"] = _song_fp(rng, song, 40)
        ft, re_ = scan([card("HOSPITAL", 75.5)], themes=book, fps=fps, path="/rips/e.mkv")
        check([c[3] for c in ft.calls if c[0] == "fpcalc"] == ["120"] and re_.card_t is not None,
              f"a known theme found in two minutes: {ft.calls[:2]}")
        # A card on screen too briefly to confirm, after the theme: the scan reads on, then
        # back through the theme, and stops at a caption there. The card still wins.
        fps["/rips/f.mkv"] = _song_fp(rng, song, 0)
        ft, rf = scan([card("THE CREEK", 10.5), (38.0, 42.0, [("HOSPITAL", 250, 180, 350, 90)], "grey")],
                      themes=book, fps=fps, path="/rips/f.mkv", limit_s=120)
        cm = picked(rf)
        check(rf.card_t is not None and rf.card_t < 30 and cm.episode is not None and cm.episode.title == "Hospital",
              f"a caption in the theme doesn't hide the card: {rf.card_t} {cm.episode} {cm.note}")
        # No fpcalc: read from the start, no error.
        ft, rd = scan([card("SHOPS", 3), card("HOSPITAL", 44.5)], themes=book, fps={}, path="/rips/d.mkv")
        check(rd.theme_end_s is None and not rd.error and rd.card_t is not None and rd.card_t >= 44,
              f"no fpcalc: {rd.card_t} {rd.error}")

    # With no episode names to look for, the whole stretch is read, densely around big text.
    ft, r = scan([card("HOSPITAL", 40.5)], limit_s=60, eps=[])
    check(not r.stopped_early and r.end_s == 60 and any(not c[c.index("-vf") + 1].count("640") for c in ft.calls)
          and len({ln.t for ln in r.lines}) > 2, f"no names: {r.end_s}, {len(r.lines)} lines")

    # ffmpeg failing is an error, as for read_title.
    try:
        scan_title("/rips/x.mkv", EPISODES, run=lambda cmd, **kw: SimpleNamespace(
            returncode=1, stdout="", stderr="Invalid data found when processing input"), read_images=lambda p: [])
        check(False, "ffmpeg failure raises TitleCardError")
    except TitleCardError as e:
        check("Invalid data" in str(e), f"error says why: {e}")

    # Nothing on screen at all.
    ft, r = scan([], limit_s=30)
    check(r.card_t is None and not r.lines and r.frames == 15 and not r.error, f"nothing: {r.frames} {r.error}")
    return failures


def _test_card_times():
    """pick_episodes on readings scan_title would give: cards at different times after the theme."""
    failures = []

    def reading(name, t, theme=(0.0, 28.0)):
        r = TitleReading(frame_h=480, frames=20, theme_start_s=theme[0] if theme else None,
                         theme_end_s=theme[1] if theme else None, card_t=t, stopped_early=True)
        r.lines = [CardLine(name, 0.9, t, 250, 180, 350, 90), CardLine(name, 0.9, t + 0.25, 250, 180, 350, 90)]
        return r

    # Bluey puts the card 5 to 40 s after the theme: every one is sure.
    names = ["HOSPITAL", "MUMS AND DADS", "MOUNT MUMANDAD", "DADDY PUTDOWN", "FAIRIES", "TAKEAWAY", "THE CREEK"]
    offs = [5, 9, 40, 18, 33, 7, 26]
    picks = pick_episodes({i: reading(n, 28 + o) for i, (n, o) in enumerate(zip(names, offs))}, EPISODES)
    unsure = [(i, cm.note) for i, cm in picks.items() if not cm.sure]
    if unsure:
        failures.append(f"cards at different times after the theme: {unsure}")
    # Themes of different lengths: the card time is counted from where each one ends.
    rs = {i: reading(n, 60 + 7 + i, theme=(10.0 * i, 60.0 + i)) for i, n in enumerate(names[:4])}
    rs[9] = reading("THE CREEK", 300.0, theme=(0.0, 60.0))
    picks = pick_episodes(rs, EPISODES)
    if not all(picks[i].sure for i in range(4)) or picks[9].sure or "after the theme song" not in picks[9].note:
        failures.append(f"off the card time: {[(i, cm.sure, cm.note) for i, cm in picks.items()]}")
    # Two cards of episodes with alike names aren't the show's own text, even where one
    # line of one ("MUmanDaD" under "MOUnT") reads closest to the other episode.
    mums = reading("MumsanDDaDs", 60.0)
    mount = reading("MOUnT", 50.0)
    mount.lines += [CardLine("MUmanDaD", 0.9, t, 220, 280, 400, 90) for t in (50.0, 50.25)]
    picks = pick_episodes({1: mums, 2: mount}, EPISODES)
    if [(picks[i].episode and picks[i].episode.title, picks[i].sure) for i in (1, 2)] != \
            [("Mums and Dads", True), ("Mount Mumandad", True)]:
        failures.append(f"alike names: {[(cm.episode, cm.note) for cm in picks.values()]}")
    # One title read, with its theme skipped by sound: sure on its own.
    one = pick_episodes({1: reading("HOSPITAL", 40.0)}, EPISODES)[1]
    if not one.sure:
        failures.append(f"one title, theme skipped: {one}")
    # Text on screen during the theme is the show's, likely as not.
    r = reading("FAIRIES", 12.0)
    cm = pick_episodes({1: r, 2: reading("HOSPITAL", 40.0)}, EPISODES)[1]
    if cm.sure or "during the theme song" not in cm.note:
        failures.append(f"card during the theme: {cm}")
    return failures


def test_titlecard():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
