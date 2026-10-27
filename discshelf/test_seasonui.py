#!/usr/bin/env python3
"""
Test for the Match page's fix panel, run offscreen with the sample controller's two-disc season:
disc 1 is on the NAS with two files for one episode (one French only), and disc 2 has titles 1
and 27 matched to one episode and title 19 matched by its running time only.

What matters here: the banner counts what needs a look and steps through it; a plain grid cell
opens what's known and a flagged one (or Enter on it) opens its fix, with "2 of 3" and ‹ ›; a
row's badge opens its fix too; a check match reads just its card, and when the card agrees,
Confirm is chosen and confirming it moves on, with Undo; the French-only duplicate is kept as
the French version; and the NAS duplicate is renamed through the Library page's rename, with
disc 1's job updated, and Undo puts back both the file and the record. The Queue's card opens
the Match page on the first problem.

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

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
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
    job = next(j for j in ctl.jobs() if j.disc_label.endswith("Second Half"))
    disc1 = next(j for j in ctl.jobs() if j.disc_label.endswith("First Half"))
    win.open_match(job)
    m = win.match
    check(wait(lambda: not win.busy("season") and m.season_st is not None and m.season_st.discs == 2),
          "the season was never looked at")
    app.processEvents()
    keys = [p.key for p in m.problems_list()]
    check(keys == ["duplicate:1:51", "nas:1:20", "check:19"], f"problems: {keys}")
    check(m.problems.isVisibleTo(win) and m.problems.line.text().startswith("3 things need a look"),
          f"banner: {m.problems.line.text()}")
    check(m.problems.button.text() == "Fix it", m.problems.button.text())
    check(not m.confirm_btn.isEnabled() and "Settle the problems" in m.saved.text(),
          f"confirm while held: {m.confirm_btn.isEnabled()} {m.saved.text()!r}")

    # The grid: text marks, discs numbered by rip order, flagged cells say they can be fixed.
    cells = {c.number: c for c in m.grid.cells.cells}
    check(len(cells) == 52, "season grid")
    check(cells[51].mark == "×2" and cells[20].mark == "×2" and cells[33].mark == "? T19" and cells[1].mark == "D1",
          f"grid marks: {cells[51].mark} {cells[20].mark} {cells[33].mark} {cells[1].mark}")
    check(cells[20].tip.endswith("Click to fix.") and not cells[1].tip.endswith("Click to fix."), cells[20].tip)
    check("disc 1, first half" in cells[1].tip.lower(), f"disc named by its label: {cells[1].tip}")

    # A plain cell opens what's known; a flagged one opens its fix; Enter works from the keyboard.
    m.grid_clicked(5)
    check(m.compare.isVisibleTo(win) and m.compare.view.kind == "info" and not m.compare.view.options,
          f"info view: {m.compare.view and m.compare.view.kind}")
    check("disc 1, First Half, title 21" in m.compare.view.text, m.compare.view.text)
    m.grid.cells.setFocus()
    m.grid.cells.focus_i = 19
    QTest.keyClick(m.grid.cells, Qt.Key.Key_Return)
    check(m.fix_sel == "nas:1:20" and m.compare.view.kind == "error", f"Enter on cell 20: {m.fix_sel}")
    check(m.compare.view.step == (2, 3) and m.compare.step_text.text() == "2 of 3", f"step: {m.compare.view.step}")
    m.compare.prev_btn.click()
    check(m.fix_sel == "duplicate:1:51", f"‹ went to {m.fix_sel}")
    m.problems.button.click()
    check(m.fix_sel == "nas:1:20" and m.problems.button.text() == "Next one", f"Next one: {m.fix_sel}")

    # A check match: read just its card, which agrees, then confirm; Undo takes it back.
    chip19 = next(c for c in m.findChildren(gui.ClickChip) if c.accessibleName().startswith("Title 19:"))
    check(chip19.cursor().shape() == Qt.CursorShape.PointingHandCursor and chip19.toolTip().endswith("Click to fix."),
          f"badge: {chip19.toolTip()}")
    chip19.setFocus()
    QTest.keyClick(chip19, Qt.Key.Key_Return)
    check(m.fix_sel == "check:19" and m.compare.view.kind == "warn", f"badge opened {m.fix_sel}")
    check(m.compare.selected == "read", f"suggested before reading: {m.compare.selected}")
    m.compare.apply_btn.click()
    check(wait(lambda: not win.busy("cards"), 20), "reading the card never finished")
    app.processEvents()
    labels = [o.label for o in m.compare.view.options]
    check(m.compare.selected == "confirm" and "Confirm S01E33 Trampoline (its card agrees)" in labels,
          f"after reading: {m.compare.selected} {labels}")
    m.compare.apply_btn.click()
    app.processEvents()
    t19 = next(x for x in job.matches if x.title_index == 19)
    check(t19.confirmed and t19.ready, "confirming didn't confirm title 19")
    check(m.compare.done.isVisibleTo(win) and "confirmed by you" in m.compare.done_text.text(), "no result line")
    check(m.fix_sel != "check:19" and m.fix_sel in [p.key for p in m.problems_list()], f"moved on to {m.fix_sel}")
    m.compare.undo_link.linkActivated.emit("#")
    app.processEvents()
    t19 = next(x for x in job.matches if x.title_index == 19)
    check(not t19.confirmed and "check:19" in [p.key for p in m.problems_list()], "Undo didn't bring the check back")

    # Watch from a side.
    ctl.played.clear()
    m.open_fix("duplicate:1:51")
    m.compare.watch_buttons["here:27"].click()
    check(ctl.played and ctl.played[-1][1] == 0.0, f"watch: {ctl.played}")

    # The duplicate on this disc: title 27 is French only, so keeping it as the French version is suggested.
    check(m.compare.selected == "version" and "(suggested)" in m.compare.view.options[1].label,
          f"suggested: {m.compare.selected}")
    m.compare.apply_btn.click()
    app.processEvents()
    t27 = next(x for x in job.matches if x.title_index == 27)
    check(t27.version == "French" and not t27.held, f"title 27: {t27.version!r}")
    check(dict(ctl.preview_paths(job)).get(27, "").endswith("S01E51 - Daddy Putdown - French.mkv"), "path")

    # The NAS duplicate from disc 1: rename title 26 as the French version, then undo it.
    m.open_fix("nas:1:20")
    v = m.compare.view
    check(m.compare.selected == "version" and v.options[0].label.endswith("as the French version (suggested)"),
          f"NAS fix: {v.options[0].label}")
    check(v.options[0].changes == [("Bluey (2018)/Season 01/Bluey (2018) - S01E20 (2).mkv",
                                    "Bluey (2018)/Season 01/Bluey (2018) - S01E20 - Markets - French.mkv")],
          f"changes: {v.options[0].changes}")
    check(m.compare.apply_btn.text() == "Rename 1 file" and m.compare.rescan.isVisibleTo(win), m.compare.apply_btn.text())
    check({o.key for o in v.options} == {"version", "extra", "aside", "delete", "swap"}, f"options: {[o.key for o in v.options]}")
    root = ctl.fix_root(job)
    season_dir = os.path.join(root, "Bluey (2018)", "Season 01")
    m.compare.apply_btn.click()
    check(wait(lambda: not win.busy("fix") and not win.busy("season"), 20), "the rename never finished")
    app.processEvents()
    names = os.listdir(season_dir)
    check("Bluey (2018) - S01E20 - Markets - French.mkv" in names and "Bluey (2018) - S01E20 (2).mkv" not in names,
          "the file wasn't renamed")
    t26 = next(x for x in disc1.matches if x.title_index == 26)
    f26 = next(x for x in disc1.files if x.title_index == 26)
    check(t26.version == "French" and f26.dest_rel.endswith("Markets - French.mkv"), f"disc 1 record: {t26.version}")
    check("nas:1:20" not in [p.key for p in m.problems_list()], "the NAS duplicate is still flagged")
    check("rescan Season 01" in m.compare.done_text.text() and ctl.plex_scans, m.compare.done_text.text())
    m.compare.undo_link.linkActivated.emit("#")
    check(wait(lambda: not win.busy("fix") and not win.busy("season"), 20), "undo never finished")
    app.processEvents()
    names = os.listdir(season_dir)
    check("Bluey (2018) - S01E20 (2).mkv" in names, "undo didn't put the name back")
    t26 = next(x for x in disc1.matches if x.title_index == 26)
    f26 = next(x for x in disc1.files if x.title_index == 26)
    check(t26.version == "" and f26.dest_rel.endswith("S01E20 (2).mkv"), "undo didn't restore disc 1's record")
    check("nas:1:20" in [p.key for p in m.problems_list()] and m.fix_sel == "nas:1:20",
          f"the NAS duplicate isn't back: {m.fix_sel}")

    # The Queue's card opens the Match page on the first problem.
    m.close_fix()
    win.show_page("queue")
    win.queue.refresh()
    app.processEvents()
    card = next(c for c in win.queue.findChildren(gui.NeedsCard) if c.item.job is job)
    card.open_fix(None)
    check(win.stack.currentWidget() is win.pages["match"] and m.fix_sel == m.problems_list()[0].key,
          f"queue opened {m.fix_sel}")
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
