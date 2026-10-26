#!/usr/bin/env python3
"""
Test for the searchable episode picker on the Match page, run offscreen.

What matters here: the button shows the chosen episode, the popup opens with the keyboard
in its search field, a search by name or by code finds the right episodes of a real
278-episode show, the season buttons and suggestions behave, the arrow keys and Enter pick
without leaving the search, Esc closes, the list never grows past its cap, and the mouse
wheel never changes a choice.

Run:  python3 discshelf/test_picker.py    (also works under pytest)
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "adventure_time_episodes.json")


def run():
    try:
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
        import shiboken6
    except ImportError:
        print("picker: skipped (no PySide6)")
        return 0
    try:
        app = QApplication.instance() or QApplication([sys.argv[0]])
    except Exception as exc:  # no usable platform plugin
        print(f"picker: skipped (Qt can't start offscreen: {exc})")
        return 0

    from discshelf.models import Episode
    from discshelf.picker import ROW, EpisodePicker, EpisodePopup, count_text, episode_hits, fold, query_words

    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with open(FIXTURE) as fh:
        eps = [Episode(s, n, t, rt) for s, n, t, rt in json.load(fh)]
    by = {(e.season, e.number): e for e in eps}
    check(len(eps) == 278, f"fixture has {len(eps)} episodes")

    # Search rules, without Qt.
    check(fold("Jake vs. Me-Mow")[0].split() == ["jake", "vs", "me", "mow"], f"fold: {fold('Jake vs. Me-Mow')}")
    check(episode_hits(by[(1, 14)], query_words("witchs garden")), "an apostrophe is not ignored")
    check(episode_hits(by[(5, 22)], query_words("senorita")), "accents are not ignored")
    check(episode_hits(by[(3, 8)], query_words("BATTLE wizard")), "words in another order don't match")
    check(count_text(1, "orb", None) == "1 episode matches “orb”", count_text(1, "orb", None))

    host = QWidget()
    lay = QVBoxLayout(host)
    picker = EpisodePicker()
    lay.addWidget(picker)
    host.resize(360, 80)
    host.show()
    app.processEvents()

    got = []
    picker.picked.connect(got.append)
    used = {(2, 1): 1, (3, 8): 3, (1, 11): 7}
    sugg = [(by[(1, 11)], "Its name has “Wizard”, and it aired in season 1"),
            (by[(3, 8)], "The title card reads “WIZARD”, closest to this name")]

    def setup(current="episode:3:8:", **kw):
        picker.set_options(eps, current, title_index=3, used=used, suggestions=sugg, **kw)

    setup(warn=True)
    check(picker.display_text() == "S03E08 · Wizard Battle", f"button text: {picker.display_text()!r}")
    check(picker.currentData() == "episode:3:8:", f"currentData: {picker.currentData()!r}")
    check(picker.property("warn") == "true", "warn=True gives no amber border")
    picker.set_options(eps, "skip", title_index=3)
    check(picker.display_text() == "Skip this title", f"skip text: {picker.display_text()!r}")
    check(picker.property("warn") is None, "warn stays on after warn=False")
    picker.set_options(eps, "extra", title_index=3, extra_label="Extra · Making Of")
    check(picker.display_text() == "Extra · Making Of", f"extra text: {picker.display_text()!r}")
    picker.set_options(eps, "episode:5:50:51", title_index=3, double=(by[(5, 50)], 51))
    check(picker.display_text() == "S05E50–E51 · Lemonhope Part I", f"double text: {picker.display_text()!r}")

    # The double episode is an option at the top of the list.
    picker.open_popup()
    pop = picker.popup
    check(isinstance(pop, EpisodePopup) and pop.isVisible(), "open_popup shows no popup")
    first = pop.option_rows()[0]
    check(first["data"] == "episode:5:50:51" and first["current"], f"double episode not first: {first}")
    QTest.keyClick(pop.search, Qt.Key.Key_Escape)
    app.processEvents()

    # Opening: the search field has the keyboard, the list is capped.
    setup()
    picker.open_popup()
    pop = picker.popup
    app.processEvents()
    check(pop is not None and pop.isVisible(), "popup not shown")
    check(pop.search.hasFocus(), f"focus is on {QApplication.focusWidget()!r}, not the search field")
    check(pop.search.placeholderText() == "Search by name, or S01E11", "placeholder")
    check(pop.list.height() <= 300, f"the list is {pop.list.height()} px tall with 278 episodes")
    check(pop.height() < 560, f"the popup is {pop.height()} px tall")
    check(abs(pop.width() - 420) <= 2, f"popup width {pop.width()}")
    check(pop.geometry().top() >= picker.mapToGlobal(QPoint(0, picker.height())).y(), "popup is not under the button")
    check(pop.count.text() == "278 episodes", f"count: {pop.count.text()!r}")
    rows = pop.option_rows()
    check([r["data"] for r in rows[:2]] == ["episode:1:11:", "episode:3:8:"] and rows[0]["why"],
          f"suggestions not first: {[r['data'] for r in rows[:3]]}")
    heads = [pop.list.item(i).data(ROW)["text"] for i in range(pop.list.count())
             if pop.list.item(i).data(ROW)["kind"] == "header"]
    check(heads[0] == "Suggested for this title" and heads[1:3] == ["Season 1", "Season 2"], f"headers: {heads[:4]}")
    check(all(not (pop.list.item(i).flags() & Qt.ItemFlag.ItemIsSelectable) for i in range(pop.list.count())
              if pop.list.item(i).data(ROW)["kind"] == "header"), "a header is selectable")
    cur = [r for r in rows if r["current"]]
    check(cur and all(r["data"] == "episode:3:8:" for r in cur), f"tick on {[r['data'] for r in cur]}")
    marks = {r["data"]: r["used"] for r in rows if r["used"]}
    check(marks.get("episode:2:1:") == "Title 1" and marks.get("episode:1:11:") == "Title 7",
          f"Title marks: {marks}")
    check("episode:3:8:" not in marks, "the title's own episode is marked as used by another title")

    # Typing filters on every keystroke.
    t0 = time.perf_counter()
    QTest.keyClicks(pop.search, "wizard")
    per_key = (time.perf_counter() - t0) / 6
    check(per_key < 0.05, f"filtering takes {per_key * 1000:.0f} ms a keystroke")
    eps_rows = [r for r in pop.option_rows() if not r["why"]]
    wiz = [(e.season, e.number) for e in eps if "wizard" in e.title.lower()]
    check([r["data"] for r in eps_rows] == [f"episode:{s}:{n}:" for s, n in wiz], f"wizard: {[r['data'] for r in eps_rows]}")
    check(pop.count.text() == f"{len(wiz)} episodes match “wizard”", f"count: {pop.count.text()!r}")
    w = next(r for r in eps_rows if r["data"] == "episode:5:26:")    # Wizards Only, Fools
    check(w["spans"] == [(0, 6)], f"highlight: {w['spans']}")
    check(pop.clear_action.isVisible(), "no clear button while searching")
    pop.clear_action.trigger()
    check(pop.search.text() == "" and pop.count.text() == "278 episodes", "clear did not empty the search")

    def found(q):
        pop.search.setText(q)
        return [r["data"] for r in pop.option_rows() if not r["why"]]

    check(found("s3e8") == ["episode:3:8:"], f"s3e8: {found('s3e8')}")
    check(found("3x8") == ["episode:3:8:"], f"3x8: {found('3x8')}")
    check(found("S03E08") == ["episode:3:8:"], f"S03E08: {found('S03E08')}")
    elevens = found("11")
    check(elevens == [f"episode:{e.season}:11:" for e in eps if e.number == 11], f"11: {elevens}")
    check(found("zzzz") == [] and "No episode" in pop.list.item(pop.list.count() - 1).data(ROW)["text"],
          "no empty-result message")

    # Season filter: count text, and suggestions are hidden.
    pop.search.clear()
    chip1 = next(b for b in pop.chip_buttons if b.text() == "1")
    check([b.text() for b in pop.chip_buttons][:3] == ["All", "1", "2"], "season buttons")
    chip1.click()
    check(pop.count.text() == "26 episodes in season 1", f"season count: {pop.count.text()!r}")
    rows = pop.option_rows()
    check(not any(r["why"] for r in rows), "suggestions shown under a season filter")
    check(all(r["data"].startswith("episode:1:") for r in rows), "another season under the season 1 filter")
    check(pop.search.hasFocus(), "a season button took the keyboard")
    pop.search.setText("wizard")
    check(found("wizard") == ["episode:1:11:"] and pop.count.text() == "1 episode in season 1 matches “wizard”",
          f"season search: {pop.count.text()!r}")
    pop.chip_buttons[0].click()

    # Down + Enter picks the highlighted row, staying in the search field.
    pop.search.setText("wizard")
    got.clear()
    QTest.keyClick(pop.search, Qt.Key.Key_Down)
    QTest.keyClick(pop.search, Qt.Key.Key_Down)
    check(pop.search.hasFocus(), "Down left the search field")
    hl = pop.list.currentItem().data(ROW)["data"]
    check(hl == "episode:3:8:", f"second row is {hl}")
    QTest.keyClick(pop.search, Qt.Key.Key_Down)       # past the suggestions to the first season row
    hl = pop.list.currentItem().data(ROW)["data"]
    check(hl == "episode:1:11:", f"third row is {hl}")
    QTest.keyClick(pop.search, Qt.Key.Key_Return)
    app.processEvents()
    check(got == ["episode:1:11:"], f"picked {got}")
    check(picker.popup is None and (not shiboken6.isValid(pop) or not pop.isVisible()), "the popup stayed open after a pick")
    check(picker.currentData() == "episode:1:11:" and picker.display_text() == "S01E11 · Finn the Wizard",
          f"after pick: {picker.currentData()!r} {picker.display_text()!r}")

    # Enter with nothing highlighted picks the first result.
    picker.open_popup()
    pop = picker.popup
    QTest.keyClicks(pop.search, "s2e7")
    got.clear()
    QTest.keyClick(pop.search, Qt.Key.Key_Return)
    check(got == ["episode:2:7:"], f"Enter on first result picked {got}")

    # Esc closes without picking.
    setup()
    picker.open_popup()
    pop = picker.popup
    QTest.keyClicks(pop.search, "orb")
    got.clear()
    QTest.keyClick(pop.search, Qt.Key.Key_Escape)
    app.processEvents()
    check(picker.popup is None and not got and picker.currentData() == "episode:3:8:", f"Esc picked {got}")

    # Clicking a row picks it.
    picker.open_popup()
    pop = picker.popup
    pop.search.setText("hitman")
    it = next(pop.list.item(i) for i in range(pop.list.count()) if pop.list.item(i).data(ROW).get("data") == "episode:3:4:")
    pop.list.scrollToItem(it)
    QTest.mouseClick(pop.list.viewport(), Qt.MouseButton.LeftButton, pos=pop.list.visualItemRect(it).center())
    app.processEvents()
    check(got == ["episode:3:4:"], f"click picked {got}")

    # Skip and Save as an extra.
    for btn, want in (("skip_btn", "skip"), ("extra_btn", "extra_new")):
        setup()
        got.clear()
        picker.open_popup()
        getattr(picker.popup, btn).click()
        app.processEvents()
        check(got == [want] and picker.popup is None, f"{btn} emitted {got}")
    check(picker.currentData() == "episode:3:8:", "Save as an extra changed the choice before the dialog")

    # The season it opens on.
    picker.set_options(eps, "episode:3:8:", title_index=3, season=3, suggestions=sugg)
    picker.open_popup()
    pop = picker.popup
    check(pop.count.text() == "26 episodes in season 3", f"opened on season 3: {pop.count.text()!r}")
    check(next(b for b in pop.chip_buttons if b.isChecked()).text() == "3", "season 3 button not selected")
    pop.close()

    # The wheel does nothing to the button.
    before = picker.currentData()
    ev = QWheelEvent(QPointF(10, 10), QPointF(picker.mapToGlobal(QPoint(10, 10))), QPoint(0, 0), QPoint(0, -120),
                     Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(picker, ev)
    app.processEvents()
    check(not ev.isAccepted(), "the button accepted the wheel, so the page won't scroll")
    check(picker.currentData() == before and picker.popup is None, "the wheel changed the picker")
    check(picker.focusPolicy() == Qt.FocusPolicy.StrongFocus, "the button takes focus from the wheel")

    host.close()
    app.processEvents()

    for f in failures:
        print("FAIL:", f)
    print(f"picker: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_picker():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
