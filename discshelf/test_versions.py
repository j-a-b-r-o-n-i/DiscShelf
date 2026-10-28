#!/usr/bin/env python3
"""
Tests for versions.py: titles on a disc that play the same film, and what differs between them.

The languages case is the Harry Potter and the Chamber of Secrets 4K disc (read 28 Oct 2026):
two playlists play the same 91.3 GB video file, 00100 with English and 7 European languages,
00101 with English and Japanese. What matters: they are grouped; the one with your languages
is picked; the other is labelled by what only it has ("Japanese audio"), not "Duplicate"; the
languages that aren't yours fold into one row; a longer cut is told apart from a shuffled
copy; and the classifier rips only the pick.

Run:  python3 discshelf/test_versions.py    (also works under pytest)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import versions  # noqa: E402
from discshelf.classify import classify_titles  # noqa: E402
from discshelf.models import Stream, Title  # noqa: E402

GB91 = 91332900864


def A(i, codec, lang, flags=""):
    return Stream(index=i, kind="audio", codec=codec, lang=lang, flags=flags)


def S(i, lang, flags=""):
    return Stream(index=i, kind="subtitle", codec="PGS", lang=lang, flags=flags)


def V():
    return Stream(index=0, kind="video", codec="MpegH", resolution="3840x2160")


def chamber_of_secrets():
    euro = ["fra", "deu", "ita", "spa", "spa", "por", "cat", "pol"]
    s100 = [V(), A(1, "DTS-HD MA", "eng", "default"), A(2, "DTS", "eng", "core"), A(3, "DD", "eng")]
    s100 += [A(4 + k, "DTS-HD MA" if l == "deu" else "DD", l) for k, l in enumerate(euro)]
    s100 += [S(20 + k, l) for k, l in enumerate(["eng"] + euro[:-2] + ["pol"] + euro[:-1] + ["pol"])]
    s101 = [V(), A(1, "DTS-HD MA", "eng", "default"), A(2, "DTS", "eng", "core"), A(3, "DD", "jpn"),
            S(4, "jpn"), S(5, "jpn", "forced"), S(6, "eng"), S(7, "eng", "forced")]
    return [Title(0, "00100.mpls", 9658, GB91, 37, [2], s100),
            Title(1, "00007.mpls", 340, 211617792, 34, list(range(27, 61)), [V()]),
            Title(2, "00101.mpls", 9658, GB91, 37, [2], s101)]


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # Languages: grouped, the pick has your languages, the other says what only it has.
    titles = chamber_of_secrets()
    groups = versions.find_groups(titles, ["eng"], ["eng"], min_seconds=300)
    check(len(groups) == 1 and groups[0].kind == "languages", f"groups: {[(g.kind, len(g.titles)) for g in groups]}")
    g = groups[0]
    check([t.source for t in g.titles] == ["00100.mpls", "00101.mpls"], f"order: {[t.source for t in g.titles]}")
    check(g.only == {2: "Japanese audio"}, f"only: {g.only}")
    check(g.head == "The film, twice: same video, different languages", g.head)
    check(g.why == "Both titles play the same video file. 00101.mpls has Japanese audio in place of 7 other "
                   "languages.", g.why)
    check(g.same[:4] == ["Video piece 00002.m2ts", "91.3 GB", "2:40:58", "37 chapters"] and "English audio" in g.same,
          f"same: {g.same}")
    rows = [(r.name, r.yours, r.cells) for r in g.langs]
    check(rows[0] == ("English", True, ["DTS-HD MA, Dolby Digital · 1 subtitle track", "DTS-HD MA · 2 subtitle tracks"]),
          f"English row: {rows[0]}")
    check(rows[1][0] == "7 more languages" and rows[1][2][1] == "—" and rows[1][2][0].startswith("French, German"),
          f"folded row: {rows[1]}")
    check(rows[2] == ("Japanese", False, ["—", "Dolby Digital · 4 subtitle tracks"]) or
          rows[2][0] == "Japanese", f"Japanese row: {rows[2:]}")
    check(g.pick_why == "Your languages are English audio and English subtitles. Both have them, and 00100.mpls "
                        "has more audio tracks in them.", g.pick_why)

    # With Japanese as your language, 00101 is the pick.
    g = versions.find_groups(chamber_of_secrets(), ["jpn"], ["jpn"], min_seconds=300)[0]
    check(g.pick.source == "00101.mpls" and g.only == {0: "7 more languages"}, f"Japanese pick: {g.pick.source} {g.only}")

    # The classifier rips only the pick, and labels the other.
    titles = classify_titles(chamber_of_secrets(), audio_langs=["eng"], subtitle_langs=["eng"])
    got = {t.source: (t.kind, t.selected, t.variant) for t in titles}
    check(got == {"00100.mpls": ("feature", True, ""), "00007.mpls": ("extra", False, ""),
                  "00101.mpls": ("duplicate", False, "Japanese audio")}, f"classified: {got}")

    # Two cuts: the longer plays every piece of the shorter, plus 4 scenes.
    th = Title(0, "00800.mpls", 9080, 31_800_000_000, 28, [10, 11, 12, 13, 14, 15], [A(1, "TrueHD", "eng")])
    ex = Title(1, "00801.mpls", 9733, 34_100_000_000, 32, [10, 20, 11, 21, 12, 13, 22, 14, 23, 15],
               [A(1, "TrueHD", "eng")])
    g = versions.find_groups([th, ex], ["eng"], ["eng"])[0]
    check(g.kind == "cut" and g.pick is th and g.only == {1: "Longer cut"}, f"cut: {g.kind} {g.only}")
    check("10:53 longer" in g.why and "4 scenes only it has" in g.why, g.why)
    check([m for _s, m in g.strips[1].pieces].count(True) == 4 and g.strips[1].note == "+10:53 in 4 scenes",
          f"strip: {g.strips[1]}")
    got = {t.index: (t.kind, t.selected, t.variant) for t in classify_titles([th, ex])}
    check(got == {0: ("feature", True, ""), 1: ("duplicate", False, "Longer cut")}, f"two cuts classified: {got}")

    # Shuffled copies: the only one in disc order is picked; the others are marked.
    seg = [1, 2, 3, 4, 5, 6, 7, 8]
    shuffled = [Title(0, "00812.mpls", 7120, 27_200_000_000, 16, [1, 4, 3, 2, 6, 5, 8, 7]),
                Title(1, "00800.mpls", 7120, 27_200_000_000, 16, seg),
                Title(2, "00847.mpls", 7120, 27_200_000_000, 16, [3, 2, 1, 5, 4, 7, 6, 8])]
    g = versions.find_groups(shuffled, ["eng"], ["eng"])[0]
    check(g.kind == "order" and g.pick.source == "00800.mpls", f"order pick: {g.kind} {g.pick.source}")
    check(g.strips[1].note == "6 pieces in a different place" and g.strips[0].note == "In order",
          f"order notes: {[s.note for s in g.strips]}")
    check(g.only == {0: "Different order", 2: "Different order"}, f"order only: {g.only}")

    # Same pieces and the same languages: the first is kept, the rest say so.
    a = Title(0, "00001.mpls", 5000, 10, 10, [5], [A(1, "DD", "eng")])
    b = Title(1, "00002.mpls", 5000, 10, 10, [5], [A(1, "DD", "eng")])
    g = versions.find_groups([a, b], ["eng"], ["eng"])[0]
    check(g.kind == "same" and g.pick is a and g.only == {1: "Same tracks"}, f"same: {g.kind} {g.only}")

    # Episodes of a TV disc are not versions of each other.
    eps = [Title(i, f"0000{i}.mpls", 1320, 5, 6, [i]) for i in range(4)]
    check(versions.find_groups(eps, ["eng"], ["eng"]) == [], "episodes were grouped")

    gui_checks(check)

    for f in failures:
        print("FAIL:", f)
    print(f"versions: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def gui_checks(check):
    """On the Rip page: the panel appears under the titles, and "Rip … instead" swaps the pick."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication, QPushButton
        app = QApplication.instance() or QApplication([sys.argv[0]])
    except Exception:  # noqa: BLE001 -- no Qt here: the plain checks above still ran
        return
    import time
    from discshelf import gui
    from discshelf.config import Config
    from discshelf.fake import FakeController

    def wait(cond, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    gui.make_app()
    ctl = FakeController(Config(), delay=0.01)
    ctl.insert_disc(3)
    win = gui.MainWindow(ctl, save_config=lambda cfg: None)
    win.show()
    page = win.rip
    check(wait(lambda: page.job is not None and not win.busy("scan") and page.version_panels),
          "no comparison panel for the Chamber of Secrets disc")
    if not page.version_panels:
        win.close()
        return
    panel = page.version_panels[0]
    check(panel.pick_head.text() == "Picked 00100.mpls", panel.pick_head.text())
    btns = [b for b in panel.findChildren(QPushButton) if b.text().startswith("Rip ")]
    check([b.text() for b in btns] == ["Rip 00101.mpls instead (Japanese audio)"], f"buttons: {[b.text() for b in btns]}")
    if btns:
        btns[0].click()
        app.processEvents()
        sel = [t.source for t in page.job.titles if t.selected]
        check(sel == ["00101.mpls"], f"after Rip instead: {sel}")
        panel = page.version_panels[0]
        check(panel.pick_head.text() == "You picked 00101.mpls. Disc Shelf would pick 00100.mpls",
              panel.pick_head.text())
    win.close()
    app.processEvents()


def test_versions():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
