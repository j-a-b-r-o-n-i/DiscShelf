#!/usr/bin/env python3
"""
Tests for library.py and the Library page. What matters: episode codes are read from the
names real libraries use; a check finds a duplicate, a misnamed file, a file without an
episode number and a file in the wrong season folder; subtitles move with their video;
a rename never overwrites a file, writes an undo log, and undo puts every name back;
and the page checks a show, renames it and undoes it in the window.

Run:  python3 discshelf/test_library.py    (also works under pytest)
"""
import json
import os
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import library as L  # noqa: E402
from discshelf.models import Episode, Job, Match, RippedFile, Title  # noqa: E402
from discshelf.naming import Scheme  # noqa: E402

SHOW = "Bluey (2018)"
NAMES = ["Magic Xylophone", "Hospital", "Keepy Uppy", "Daddy Robot", "Shadowlands", "The Weekend", "BBQ", "Fruitbat"]
HITS = [SimpleNamespace(id=1, name="Bluey", year=2018), SimpleNamespace(id=2, name="Bluey", year=1976),
        SimpleNamespace(id=3, name="The Office", year=2001), SimpleNamespace(id=4, name="The Office", year=2005)]


def search(q):
    return [h for h in HITS if q.lower() in h.name.lower()]


def episodes(show_id, season):
    return [Episode(1, n, t, 7) for n, t in enumerate(NAMES, 1)] if show_id == 1 else []


def tree(root: str) -> None:
    files = {
        f"{SHOW}/Season 01/{SHOW} - S01E01.mkv": 1000,
        f"{SHOW}/Season 01/{SHOW} - S01E01.en.srt": 10,
        f"{SHOW}/Season 01/{SHOW} - S01E01-thumb.jpg": 10,
        f"{SHOW}/Season 01/{SHOW} - S01E01.nfo": 10,
        f"{SHOW}/Season 01/{SHOW} - S01E02 - Hospital.mkv": 1000,        # already named the scheme's way
        f"{SHOW}/Season 01/{SHOW} - S01E03.mkv": 1000,
        f"{SHOW}/Season 01/{SHOW} - S01E03 (2).mkv": 900,                # the old dedupe's second copy
        f"{SHOW}/Season 02/{SHOW} - S01E04.mkv": 1000,                   # in the wrong season folder
        f"{SHOW}/Season 01/Bluey.1x05.720p.mkv": 1000,                   # release-style name
        f"{SHOW}/Season 01/bonus clip.mkv": 1000,                         # no episode number
        f"{SHOW}/Season 01/{SHOW} - S01E09.mkv": 1000,                   # not on TVmaze
        f"{SHOW}/Featurettes/Making Of.mkv": 1000,                        # an extra, left alone
        f"{SHOW}/Season 01/{SHOW} - S01E06 - The Weekend - 2160p.mkv": 1000,   # a version, kept
        f"{SHOW}/Season 01/{SHOW} - S01E06 - The Weekend.mkv": 1000,
    }
    for rel, size in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(b"x" * size)


def listing(root: str) -> list[str]:
    out = []
    for d, _dirs, files in os.walk(root):
        out += [os.path.relpath(os.path.join(d, f), root) for f in files]
    return sorted(out)


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def eq(got, want, what=""):
        check(got == want, f"{what}: got {got!r}, want {want!r}")

    # Parsing the names real libraries use.
    for name, want in [("Show (2019) - S01E02.mkv", (1, 2, None, 0)),
                       ("Show.S01E02.1080p.WEB.x264-GRP.mkv", (1, 2, None, 0)),
                       ("show - s01e02 - Name.mkv", (1, 2, None, 0)),
                       ("DOUG - S01 E04-E05 - A and B (480p).mp4", (1, 4, 5, 0)),
                       ("Arthur - S03E23-E24 - A + B SDTV.mkv", (3, 23, 24, 0)),
                       ("Show - S01E01E02.mkv", (1, 1, 2, 0)),
                       ("Show - S02E10-11.mkv", (2, 10, 11, 0)),
                       ("Looney.Tunes.S1959E20.A.Hare.1080p.mkv", (1959, 20, None, 0)),
                       ("Show 1x05 Name.avi", (1, 5, None, 0)),
                       ("Show 2x09-2x10.avi", (2, 9, 10, 0)),
                       ("Bluey (2018) - S01E20 (2).mkv", (1, 20, None, 2)),
                       ("Show S01E02 1080p (2).mkv", (1, 2, None, 2)),
                       ("124. The Inner Light.mkv", (None, None, None, 0)),
                       ("Show - Season 1 1080p.mkv", (None, None, None, 0))]:
        p = L.parse_name(name)
        eq((p.season, p.number, p.end, p.copy), want, name)
    eq(L.season_of_folder("Season 01"), 1)
    eq(L.season_of_folder("Season 2 (1989-90)"), 2)
    eq(L.season_of_folder("S04"), 4)
    eq(L.season_of_folder("Specials"), 0)
    eq(L.season_of_folder("Bluey"), None)
    eq(L.show_query("Bluey (2018)"), ("Bluey", 2018, False))
    eq(L.show_query("Westworld"), ("Westworld", None, False))
    eq(L.show_query("Star.Trek.Discovery.S01-S03.COMPLETE.1080p.Bluray.x265"), ("Star Trek Discovery", None, True))
    eq(L.show_query("Silicon Valley (2014) Season 1-6 S01-S06 (1080p x265)"), ("Silicon Valley", 2014, True))
    eq(L.lookup(search, episodes, "Bluey (2018-2024)").show_id, 1)
    eq(L.lookup(search, episodes, "Bluey 2018 --Group--").show_id, 1)
    eq(L.pick_show(HITS, "Bluey", 2018)[0].id, 1)
    eq(L.pick_show(HITS, "The Office", None)[1], "several")

    tmp = tempfile.mkdtemp(prefix="disc-shelf-test-")
    try:
        root, undo_dir = os.path.join(tmp, "Shows"), os.path.join(tmp, "undo")
        tree(root)
        eq(L.list_shows(root), [SHOW], "shows")
        scan = L.scan_show(root, SHOW)
        eq(len(scan.files), 10, "videos outside extras folders")
        eq(scan.extras, [f"{SHOW}/Featurettes/Making Of.mkv"], "extras")
        e1 = next(f for f in scan.files if f.name == f"{SHOW} - S01E01.mkv")
        eq(sorted(os.path.basename(s) for s in e1.sidecars),
           [f"{SHOW} - S01E01-thumb.jpg", f"{SHOW} - S01E01.en.srt", f"{SHOW} - S01E01.nfo"], "sidecars")
        e4 = next(f for f in scan.files if f.number == 4)
        eq((e4.season, e4.folder_season), (1, 2), "season folder")

        look = L.lookup(search, episodes, SHOW)
        eq((look.name, look.year, look.show_id, look.ambiguous), ("Bluey", 2018, 1, False), "lookup")
        # Disc Shelf's records say the second S01E03 was read from its title card.
        dest = "/srv/media/Shows"
        job = Job(id="j", destination=dest, disc_number=1, titles=[Title(26, duration_s=431)],
                  matches=[Match(26, "episode", Episode(1, 3, "Keepy Uppy"), source="card", version="Short cut",
                                 note="The title card reads “KeePyUPPy”, which is closest to “Keepy Uppy”.")],
                  files=[RippedFile(26, "", dest_rel=f"{SHOW}/Season 01/{SHOW} - S01E03 (2).mkv")])
        readings = L.readings_from_jobs([job])
        plan = L.plan_show(scan, look, Scheme(), readings=readings, records_root=dest, root=root)
        kinds = sorted(p.kind for p in plan.problems)
        eq(kinds, ["duplicate", "no_number", "unknown_episode", "wrong_season"], "problems")
        dup = next(p for p in plan.problems if p.kind == "duplicate")
        check("KeePyUPPy" in dup.text and "7:11" in dup.text, f"duplicate text: {dup.text}")
        moves = {r.src: r.dst for r in plan.renames}
        s1 = f"{SHOW}/Season 01/{SHOW}"
        eq(moves.get(f"{s1} - S01E01.mkv"), f"{s1} - S01E01 - Magic Xylophone.mkv", "misnamed")
        eq(moves.get(f"{s1} - S01E03 (2).mkv"), f"{s1} - S01E03 - Keepy Uppy - Short cut.mkv", "second version")
        eq(moves.get(f"{SHOW}/Season 02/{SHOW} - S01E04.mkv"), f"{s1} - S01E04 - Daddy Robot.mkv", "wrong season")
        eq(moves.get(f"{SHOW}/Season 01/Bluey.1x05.720p.mkv"), f"{s1} - S01E05 - Shadowlands.mkv", "1x05")
        check(f"{s1} - S01E02 - Hospital.mkv" not in moves, "a named file is renamed")
        check(f"{s1} - S01E06 - The Weekend - 2160p.mkv" not in moves, "a version lost its version")
        check(f"{SHOW}/Season 01/bonus clip.mkv" not in moves, "a file with no number is renamed")
        eq(plan.named, 3, "named the Plex way")
        r1 = next(r for r in plan.renames if r.src == f"{s1} - S01E01.mkv")
        eq(sorted(d for _s, d in r1.sidecars), [f"{s1} - S01E01 - Magic Xylophone-thumb.jpg",
                                                f"{s1} - S01E01 - Magic Xylophone.en.srt",
                                                f"{s1} - S01E01 - Magic Xylophone.nfo"], "sidecar names")
        cells = {c.number: c.state for c in plan.cells}
        eq((cells[3], cells[1], cells[7], cells[9]), ("dup", "unread", "missing", "unknown"), "cells")

        # The other two ways to settle a duplicate, and another scheme.
        dup_rel = f"{s1} - S01E03 (2).mkv"
        extra = {r.src: r.dst for r in L.plan_show(scan, look, Scheme(), {dup_rel: "extra"}, readings, dest).renames}
        eq(extra[dup_rel], f"{SHOW}/Featurettes/Keepy Uppy (Short cut).mkv", "as an extra")
        aside = L.plan_show(scan, look, Scheme(), {dup_rel: "aside"}, root=root)
        eq({r.src: r.dst for r in aside.renames}[dup_rel], f"{SHOW}/Set aside/{SHOW} - S01E03 (2).mkv", "set aside")
        eq(aside.extra_files, [(f"{SHOW}/Set aside/.plexignore", "*\n")], "plexignore")
        bare = L.plan_show(scan, look, Scheme(episode="code", season_pad=False), root=root)
        eq({r.src: r.dst for r in bare.renames}[f"{s1} - S01E02 - Hospital.mkv"],
           f"{SHOW}/Season 1/{SHOW} - S01E02.mkv", "code-only scheme")

        # A folder named without the year keeps its name unless show folders are renamed too.
        os.rename(os.path.join(root, SHOW), os.path.join(root, "Bluey"))
        plain = L.plan_show(L.scan_show(root, "Bluey"), L.lookup(search, episodes, "Bluey"), Scheme(), root=root)
        eq({r.src: r.dst for r in plain.renames}["Bluey/Season 01/Bluey (2018) - S01E01.mkv"],
           "Bluey/Season 01/Bluey - S01E01 - Magic Xylophone.mkv", "kept folder")
        check(all(r.dst.startswith("Bluey/") for r in plain.renames), "a file left the show folder")
        moved = L.plan_show(L.scan_show(root, "Bluey"), L.lookup(search, episodes, "Bluey"), Scheme(), root=root,
                            rename_folder=True)
        eq({r.src: r.dst for r in moved.renames}["Bluey/Season 01/Bluey (2018) - S01E01.mkv"],
           f"{s1} - S01E01 - Magic Xylophone.mkv", "renamed folder")
        os.rename(os.path.join(root, "Bluey"), os.path.join(root, SHOW))
        # A release folder keeps its name, and its files take the show's name from TVmaze.
        rel = "Bluey.S01.720p.WEB"
        os.makedirs(os.path.join(root, rel, "extra"))
        open(os.path.join(root, rel, "extra", "Bluey.S01E02.720p.WEB.mkv"), "wb").close()
        rplan = L.plan_show(L.scan_show(root, rel), L.lookup(search, episodes, rel), Scheme(), root=root)
        eq([r.dst for r in rplan.renames], [f"{rel}/Season 01/{SHOW} - S01E02 - Hospital.mkv"], "release folder")
        shutil.rmtree(os.path.join(root, rel))

        # Rename, then undo: every name comes back, and nothing else changes.
        before = listing(root)
        log = L.apply(root, L.moves_of(aside.renames), undo_dir, "test", aside.extra_files)
        after = listing(root)
        check(f"{s1} - S01E01 - Magic Xylophone.en.srt" in after, "the subtitle didn't move")
        check(f"{SHOW}/Set aside/.plexignore" in after, "no .plexignore")
        check(not os.path.isdir(os.path.join(root, SHOW, "Season 02")), "the emptied season folder is still there")
        eq(len(after), len(before) + 1, "file count after rename")
        eq(L.last_log(undo_dir), log, "last log")
        n, skipped = L.undo(log)
        eq(listing(root), before, "undo")
        eq((n, skipped), (len(json.load(open(log))["ops"]), []), "undo count")
        eq(L.last_log(undo_dir), None, "an undone log is offered again")

        # A file in the way: nothing is renamed.
        again = L.plan_show(L.scan_show(root, SHOW), look, Scheme(), root=root)
        blocker = os.path.join(root, f"{s1} - S01E01 - Magic Xylophone.mkv")
        with open(blocker, "wb") as fh:
            fh.write(b"other")
        snapshot = listing(root)
        try:
            L.apply(root, L.moves_of(again.renames), undo_dir)
            check(False, "a rename overwrote a file")
        except L.RenameError as e:
            check("already exists" in str(e), f"refusal: {e}")
        eq(listing(root), snapshot, "a refused rename changed something")
        os.remove(blocker)

        # Two files trading names, and a change of letter case only, go through a temporary name.
        a, b = os.path.join(root, "swap", "a.mkv"), os.path.join(root, "swap", "b.mkv")
        os.makedirs(os.path.dirname(a))
        for p, data in ((a, b"A"), (b, b"B")):
            with open(p, "wb") as fh:
                fh.write(data)
        log = L.apply(root, [("swap/a.mkv", "swap/b.mkv"), ("swap/b.mkv", "swap/a.mkv")], undo_dir)
        eq((open(a, "rb").read(), open(b, "rb").read()), (b"B", b"A"), "swap")
        L.apply(root, [("swap/a.mkv", "swap/A.mkv")], undo_dir)
        check(os.path.exists(os.path.join(root, "swap", "A.mkv")), "case-only rename")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    for f in failures:
        print("FAIL:", f)
    print(f"library: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def run_gui():
    """The Library page in the window, on the sample controller's sample library."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("library gui: skipped (no PySide6)")
        return 0
    try:
        app = QApplication.instance() or QApplication([sys.argv[0]])
    except Exception as exc:  # no usable platform plugin
        print(f"library gui: skipped (Qt can't start offscreen: {exc})")
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

    gui.make_app()
    ctl = FakeController(Config(), delay=0)
    win = gui.MainWindow(ctl)
    win.resize(1360, 900)
    win.show()
    page = win.library
    win.show_page("library")
    check(win.nav_buttons["library"].isChecked(), "the Library nav item isn't marked current")
    check(wait(lambda: SHOW in page.plans), "Bluey was never checked")
    plan = page.plans.get(SHOW)
    if plan is not None:
        check(any(p.kind == "duplicate" for p in plan.problems), "no duplicate found")
        check(page.table.rowCount() == len(plan.renames) > 0, f"table rows {page.table.rowCount()}")
        check(page.rename_btn.text() == f"Rename {len(plan.renames)} files", page.rename_btn.text())
        check(page.findChildren(gui.QPushButton, "") and page.grid_lay.count() > 0, "no episode grid")
        root = page.root
        before = listing(root)
        # Another scheme for this rename only: fewer changes, and Settings is untouched.
        code = next(b for b in page.scheme_group.buttons() if b.property("key") == "code")
        code.click()
        app.processEvents()
        check(len(page.plans[SHOW].renames) < len(plan.renames), "the code-only scheme renames as much")
        check(win.cfg.name_episode == "name", "picking a scheme changed Settings")
        named = next(b for b in page.scheme_group.buttons() if b.property("key") == "name")
        named.click()
        app.processEvents()
        n = len(page.plans[SHOW].renames)
        page.rename_btn.click()
        check(wait(lambda: not win.busy("library-rename") and SHOW in page.plans and
                   page.plans[SHOW].named > plan.named), "the rename never finished")
        check("Renamed" in page.apply_note.text(), f"note after rename: {page.apply_note.text()}")
        check(any(f.endswith("S01E20 - Markets - Short cut.mkv") for f in listing(root)), "no second cut of Markets")
        check(ctl.plex_scans == [("TV Shows", SHOW)], f"Plex scans: {ctl.plex_scans}")
        check(page.undo_btn.isEnabled(), "Undo isn't offered")
        page.undo_btn.click()
        check(wait(lambda: not win.busy("library-undo") and SHOW in page.plans
                   and len(page.plans[SHOW].renames) == n), "the undo never finished")
        check(listing(root) == before, "undo didn't put every name back")
        # Every show: the release folder of The Office has two shows to pick from on TVmaze.
        page.scope_all.click()
        check(page.rename_btn.text() == "Check every show first", page.rename_btn.text())
        page.rename_btn.click()
        check(wait(lambda: len(page.plans) == len(page.shows) and not win.busy("library-all")), "check all")
        office = page.plans.get("The.Office.S03.720p.WEB")
        check(office is not None and office.lookup.ambiguous, "The Office isn't marked for a decision")
        check(page.rename_btn.text().startswith("Rename"), page.rename_btn.text())
    # Settings → File names: the example follows the choices and links to the page.
    win.show_page("settings")
    s = win.settings
    s.name_combos["name_episode"].setCurrentIndex(s.name_combos["name_episode"].findData("code"))
    check("Bluey (2018) - S01E20.mkv" in s.name_example.text(), s.name_example.text())
    s.name_combos["name_film"].setCurrentIndex(s.name_combos["name_film"].findData("edition_quality"))
    check("Aliens (1986) {edition-Special Edition} [2160p].mkv" in s.name_example.text(), s.name_example.text())
    s.save()
    check(win.cfg.name_episode == "code" and win.cfg.name_film == "edition_quality", "names weren't saved")
    check(ctl.saved_config is not None and ctl.saved_config.name_episode == "code", "names weren't saved to disk")
    win.close()

    for f in failures:
        print("FAIL:", f)
    print(f"library gui: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_library():
    assert run() == 0


def test_library_gui():
    assert run_gui() == 0


if __name__ == "__main__":
    sys.exit(run() or run_gui())
