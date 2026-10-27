#!/usr/bin/env python3
"""
Test for the Match page's season parts, run offscreen with the sample controller's two-disc
season, where titles 1 and 27 of disc 2 are both matched to one episode.

What matters here: the red banner lists the problem and "Confirm and copy" waits for it;
Compare opens a panel with both titles; reading just those two cards fills in what each
read and where, and the hint calls the French-only one a dub; Watch asks where to start
and opens the player at the title card; "Keep as a second version" settles the problem and
the file gets " - Short cut"; the season grid shows every episode with a text mark; and
"This is …" skips the other title and opens its picker.

Run:  python3 discshelf/test_seasonui.py    (also works under pytest)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def run():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("seasonui: skipped (no PySide6)")
        return 0
    try:
        app = QApplication.instance() or QApplication([sys.argv[0]])
    except Exception as exc:  # no usable platform plugin
        print(f"seasonui: skipped (Qt can't start offscreen: {exc})")
        return 0

    from discshelf import gui
    from discshelf.config import Config
    from discshelf.fake import FakeController

    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def wait(cond, timeout=10.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if cond():
                return True
            time.sleep(0.01)
        return False

    ctl = FakeController(Config(), delay=0.002)
    win = gui.MainWindow(ctl)
    win.resize(1360, 900)
    win.show()
    wait(lambda: not win.busy("drives"))
    job = next(j for j in ctl.jobs() if j.disc_label == "BLUEY_S1_D2")
    win.open_match(job)
    m = win.match
    check(wait(lambda: not win.busy("season") and m.season_st is not None and m.season_st.discs == 2),
          "the season was never looked at")
    app.processEvents()
    check(m.problems.isVisibleTo(win) and len(m.problems.lines) == 1, "no problems banner")
    check(m.problems.lines and "Titles 1 and 27 are both S01E51 “Daddy Putdown”." in m.problems.lines[0][0].text(),
          f"banner: {m.problems.lines and m.problems.lines[0][0].text()}")
    check(not m.confirm_btn.isEnabled() and "Settle the problems" in m.saved.text(),
          f"confirm while held: {m.confirm_btn.isEnabled()} {m.saved.text()!r}")
    check(m.grid.isVisibleTo(win) and len(m.grid.cells.cells) == 52, "season grid")
    cells = {c.number: c for c in m.grid.cells.cells}
    check(cells[51].mark == "×2" and cells[20].mark == "×2" and cells[1].mark == "D1" and cells[27].mark == "T26",
          f"grid marks: {cells[51].mark} {cells[20].mark} {cells[1].mark} {cells[27].mark}")

    # Watch before anything is read: no chooser, straight from the start.
    ctl.played.clear()
    m.watch(2)
    check(ctl.played and ctl.played[-1][1] == 0.0, f"watch from the start: {ctl.played}")

    # Compare, then read just those two cards.
    m.open_compare(m.season_st.problems[0].key)
    app.processEvents()
    check(m.compare.isVisibleTo(win) and len(m.compare.watch_buttons) == 2, "compare panel")
    check(m.compare.read_btn.isVisibleTo(win) and m.compare.read_btn.text() == "Read both title cards",
          f"read button: {m.compare.read_btn.text()}")
    m.compare.read_btn.click()
    check(wait(lambda: not win.busy("cards"), 20), "reading the two cards never finished")
    app.processEvents()
    check(set(m.compare_reads) == {1, 27} and m.compare_reads[27][1] == 52.0, f"reads: {m.compare_reads}")
    check("Title 27 has no English audio, only French" in m.compare.hint.text(), f"hint: {m.compare.hint.text()}")
    check(not m.compare.read_btn.isVisibleTo(win), "Read both title cards still offered")
    keep1, keep27 = m.compare.fix_buttons.get(("keep", 1)), m.compare.fix_buttons.get(("keep", 27))
    check(keep1 is not None and keep1.property("primary") == "true" and keep27.property("primary") != "true",
          "the longer title isn't the suggested one")

    # Watch at the title card: the chooser offers it, and the player gets the time.
    class Chooser:
        def __init__(self, parent, title, card_t, seeks, label):
            self.start, self.args = card_t, (title, card_t, seeks)

        def exec(self):
            return gui.QDialog.DialogCode.Accepted
    real = gui.WatchChooser
    gui.WatchChooser = Chooser
    try:
        m.compare.watch_buttons["here:27"].click()
    finally:
        gui.WatchChooser = real
    check(ctl.played and ctl.played[-1][1] == 52.0, f"watch at the card: {ctl.played[-1:]}")
    check("at the title card (0:52)" in m.season_note.text(), f"note: {m.season_note.text()}")

    check(m.compare.fix_buttons[("version", 27)].text() == "Keep as the French version",
          f"version button: {m.compare.fix_buttons[('version', 27)].text()}")
    # Keep title 27 as the French version: the problem is settled and the file says so.
    m.compare.fix_buttons[("version", 27)].click()
    app.processEvents()
    check(not m.problems.isVisibleTo(win) and not m.compare.isVisibleTo(win), "the problem is still shown")
    t27 = next(x for x in job.matches if x.title_index == 27)
    check(t27.version == "French" and not t27.held and t27.ready, f"title 27: {t27.version!r} {t27.held!r}")
    paths = dict(ctl.preview_paths(job))
    check(paths.get(27, "").endswith("S01E51 - Daddy Putdown - French.mkv"), f"path: {paths.get(27)}")
    check(m.confirm_btn.isEnabled(), "Confirm and copy still waits")

    # Picking the same episode again for another title brings the problem back at once,
    # and "This is …" skips the other title and opens its picker.
    import copy as _copy
    new = _copy.deepcopy(job.matches)
    t2 = next(x for x in new if x.title_index == 2)
    t2.episode = _copy.deepcopy(next(x for x in job.matches if x.title_index == 1).episode)
    m.set_matches(job, new)
    m.fill_table()
    check(m.problems.isVisibleTo(win) and any("Titles 1 and 2" in l[0].text() for l in m.problems.lines),
          f"a new duplicate isn't shown: {[l[0].text() for l in m.problems.lines]}")
    p = next(p for p in m.season_st.problems if 2 in p.titles)
    m.open_compare(p.key)
    m.compare.fix_buttons[("keep", 1)].click()
    app.processEvents()
    t2 = next(x for x in job.matches if x.title_index == 2)
    check(t2.target == "skip" and "Pick this title's episode" in t2.note, f"title 2 after keep: {t2.target} {t2.note}")
    pickers = [c for c in m.findChildren(gui.EpisodePicker) if c.popup is not None]
    check(len(pickers) == 1, "the other title's picker didn't open")
    for c in pickers:
        c.popup.close()
    win.close()
    app.processEvents()

    for msg in failures:
        print("FAIL:", msg)
    print(f"seasonui: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_seasonui():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
