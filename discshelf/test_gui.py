#!/usr/bin/env python3
"""
Smoke test for the Disc Shelf window, run offscreen with the sample controller.

What matters here: the window builds, every page opens, a scan runs on a worker thread
and fills the Rip page and Disc info, and the RAW / Not Raw badge follows the
"Convert Dolby Vision to profile 8.1" checkbox on both the Rip and Settings pages.
Then a TV disc is ripped and copied title by title (matched and confirmed during the rip),
a film is copied while it rips, and the Copying settings are saved.

Run:  python3 discshelf/test_gui.py    (also works under pytest)
"""
import copy as _copy_mod
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def run():
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("gui: skipped (no PySide6)")
        return 0
    try:
        app = QApplication.instance() or QApplication([sys.argv[0]])
    except Exception as exc:  # no usable platform plugin
        print(f"gui: skipped (Qt can't start offscreen: {exc})")
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
    check(wait(lambda: not win.busy("drives") and win.rip.drive_combo.currentData() is not None),
          "the drive list never arrived")

    for key in ("queue", "rip", "match", "copy", "info", "history", "settings"):
        win.show_page(key)
        app.processEvents()
        check(win.stack.currentWidget() is win.pages[key], f"page {key} did not open")
        check(win.nav_buttons[key].isChecked(), f"nav button for {key} is not marked current")

    # The Match page opens the sample disc that is waiting for episodes.
    check(win.match.job is not None and win.match.job.stage == "match", "Match page has no waiting job")
    check(win.match.confirm_btn.text().startswith("Confirm and copy"), f"confirm button: {win.match.confirm_btn.text()}")
    check(win.history.table.rowCount() == 4, f"history rows: {win.history.table.rowCount()}")

    # The Queue: what needs you first, with the count on the nav item; checks settled in place.
    win.show_page("queue")
    win.queue.refresh()
    app.processEvents()
    q = win.queue.view
    needs = [i for i in q.items if i.section == "needs"]
    check(needs and win.queue_badge.text() == str(len(needs)) and win.queue_badge.isVisibleTo(win),
          f"queue badge: {win.queue_badge.text()} for {len(needs)}")
    cards = win.queue.findChildren(gui.NeedsCard)
    with_checks = next((c for c in cards if c.item.checks), None)
    check(with_checks is not None and with_checks.rows, "no title to check on the Queue page")
    if with_checks is not None:
        row = with_checks.item.checks[0]
        calls = []
        real_confirm, real_copy = ctl.confirm_title, win.start_copy
        ctl.confirm_title = lambda job, ti, ep: calls.append(("confirm", ti, ep.season, ep.number))
        win.start_copy = lambda job, ready_only=False, open_page=True: calls.append(("copy", ready_only, open_page))
        try:
            with_checks.confirm(row)
        finally:
            ctl.confirm_title, win.start_copy = real_confirm, real_copy
        chosen = with_checks.chosen(row.title_index)
        check(calls and calls[0] == ("confirm", row.title_index, chosen.season, chosen.number)
              and ("copy", True, False) in calls, f"confirm from the queue: {calls}")
    check(any(c.item.failed and hasattr(c, "retry") for c in cards), "no Retry copy for a failed copy")
    done = win.queue.findChildren(gui.DoneRow)
    check(len(done) == len([i for i in q.items if i.section == "done"]) >= 1, "done rows")
    seasons = win.queue.findChildren(gui.SeasonCard)
    check(len(seasons) == len(q.seasons) >= 1, "season cards")
    done_job = next(i.job for i in q.items if i.section == "done")
    win.queue.dismiss(done_job)
    app.processEvents()
    check(all(i.job.id != done_job.id for i in win.queue.view.items), "Move to History left the disc in the queue")
    check(all(j.stage not in ("done", "plex") for j in [c.job for c in win.rip.queue_cards.values()]),
          "the Rip page's queue still lists finished discs")

    # The disc already in the drive is scanned without a click; Disc info follows on its own.
    win.show_page("rip")
    check(wait(lambda: win.rip.job is not None and not win.busy("scan")), "the disc in the drive was never scanned")
    check(win.rip.rip_btn.text() == "Rip 4 titles", f"rip button: {win.rip.rip_btn.text()}")
    check("Found 6 titles." in win.rip.disc_sum.text(), f"summary: {win.rip.disc_sum.text()}")
    check(win.libre_chip.isVisibleTo(win) and win.uhd_chip.isVisibleTo(win), "LibreDrive / 4K chips not shown")
    check(wait(lambda: win.rip.cover.has_cover), "the Rip card shows no cover although the disc has one")
    check(win.rip.cover.image.pixmap() is not None and not win.rip.cover.image.pixmap().isNull(), "cover pixmap")
    check(win.rip.cover.caption.isHidden(), "the disc's own cover is captioned as from TVmaze")
    check(wait(lambda: win.info.report is not None and not win.busy("info")), "disc info never arrived")
    win.show_page("info")
    app.processEvents()
    check(win.info.report is not None and len(win.info.report.titles) == 9, "disc info titles")
    shorts = [c for t, c in win.info.title_cards if t.kind == "short"]
    check(shorts and not any(c.isVisibleTo(win) for c in shorts), "short titles are not hidden")
    win.info.short_box.setChecked(True)
    app.processEvents()
    check(all(c.isVisibleTo(win) for c in shorts), "short titles don't appear when asked for")

    # The badge follows the convert checkbox.
    win.show_page("rip")
    for page in (win.rip, win.settings):
        page.convert.setChecked(False)
        app.processEvents()
        check(page.hdr.badge.text() == "RAW - No Conversion", f"{type(page).__name__} badge: {page.hdr.badge.text()}")
        check(page.hdr.box.isHidden(), f"{type(page).__name__} shows reasons while RAW")
        page.convert.setChecked(True)
        app.processEvents()
        check(page.hdr.badge.text() == "Not Raw", f"{type(page).__name__} badge after convert: {page.hdr.badge.text()}")
        check(not page.hdr.box.isHidden(), f"{type(page).__name__} hides the reasons when Not Raw")
        page.convert.setChecked(False)
        app.processEvents()
        check(page.hdr.badge.text() == "RAW - No Conversion", f"{type(page).__name__} badge did not flip back")

    # Taking the disc out clears the Rip page; putting another in scans it with no click.
    ctl.remove_disc()
    win.poll_disc()
    check(wait(lambda: win.rip.job is None and win.disc is None), "the Rip page kept the disc after it came out")
    ctl.insert_disc(1)
    win.poll_disc()
    check(wait(lambda: win.rip.job is not None and not win.busy("scan") and win.disc is not None
               and win.disc.label != "SEVERANCE_S1_D1"), "the new disc was not scanned on its own")
    check(wait(lambda: win.rip.cover.has_cover and win.rip.cover.source == "TVmaze"), "no TVmaze poster for the TV disc")
    check(not win.rip.cover.caption.isHidden(), "the TVmaze poster has no caption")

    # A disc of 11-minute cartoons: "Hide clips under 5 min" hides only the short clips, never episodes.
    from discshelf.models import Disc, Job, Title
    disc0, job0 = win.rip.disc, win.rip.job
    toons = [Title(i, f"{i:05d}.mpls", 11 * 60, int(0.6e9), 4, kind="episode", selected=True) for i in range(4)]
    toons += [Title(10, "00010.mpls", 3 * 3600, int(9e9), 40, kind="play_all"),
              Title(11, "00011.m2ts", 40, int(0.02e9), 1, kind="short"),
              Title(12, "00012.m2ts", 95, int(0.05e9), 1, kind="short")]
    win.rip.load(Disc(disc0.drive, "TOONS_V2", "Toons", "DVD disc", titles=toons),
                 Job(id="toons", disc_label="TOONS_V2", titles=toons))
    app.processEvents()
    check(win.rip.hide_short.text() == "Hide clips under 5 min", f"hide toggle: {win.rip.hide_short.text()}")
    hidden = [t.kind for r, t in enumerate(toons) if win.rip.table.isRowHidden(r)]
    check(hidden == ["short", "short"], f"rows hidden by the toggle: {hidden}")
    check("2 are short clips" in win.rip.disc_sum.text(), f"summary: {win.rip.disc_sum.text()}")
    win.rip.load(disc0, job0)

    # The drive picker shows the disc that was just scanned.
    check(wait(lambda: not win.busy("drives") and win.rip.drive_combo.currentData() is not None
               and win.rip.drive_combo.currentData().has_disc), "the drive picker still says no disc")

    # A TV disc, ripped and copied title by title. The task manager's bar is recorded.
    from discshelf.taskbar import TaskbarProgress
    sent = []
    win.taskbar = TaskbarProgress(send=lambda _uri, props: sent.append(dict(props)))
    ctl.delay = 0.03
    job = win.rip.job
    win.rip.rip_clicked()
    check(wait(lambda: job.copy_mode == "per_title"), "the rip never chose to copy per title")
    win.open_match(job)
    app.processEvents()
    check(win.match.job is job and win.match.confirm_btn.text() == "Confirm matches",
          f"Match during the rip: {win.match.confirm_btn.text()}")
    check(not win.match.banner.isHidden(), "the copy-as-it-rips banner is hidden")
    check(wait(lambda: any(p.state == "waiting_match" for p in win.progress_of(job)), 20),
          "no title waited for its match to be checked")
    win.match.confirm()
    check(wait(lambda: not any(p.state == "waiting_match" for p in win.progress_of(job)), 10),
          "confirming didn't release the waiting title")

    def locked_row():
        for r, m in enumerate(win.match.rows()):
            cell = win.match.prog_cells.get(m.title_index)
            combo = win.match.table.cellWidget(r, 2).findChild(gui.EpisodePicker) if win.match.table.cellWidget(r, 2) else None
            if cell and cell[0].text() == "On the NAS · checked" and combo is not None and not combo.isEnabled():
                return True
        return False

    check(wait(locked_row, 20), "no title showed as on the NAS with its match locked")
    card = win.rip.queue_cards.get(job.id)
    check(wait(lambda: card is not None and len(card.graph.xs) >= 2 and not card.dual.isHidden()), "queue card graph")
    if card is not None and win.is_ripping(job):
        check(card.bar.isHidden() and not card.dual.isHidden(), "the queue card doesn't show separate Rip and Copy bars")
        check(not card.graph_box.isHidden() and "GB ripped" in card.speed_sum.text(), f"graph: {card.speed_sum.text()}")
        check(card.graph.accessibleName().startswith("Rip speed"), "the rip graph has no text summary")
    check(wait(lambda: not win.busy("rip") and job.stage == "done", 30), f"the TV rip didn't finish: {job.stage}")
    app.processEvents()
    shown = [p.get("progress") for p in sent if p.get("progress-visible")]
    check(len(shown) >= 3 and shown == sorted(shown) and shown[-1] > shown[0], f"taskbar fractions: {shown[:5]}…")
    check(bool(sent) and sent[-1].get("progress-visible") is False, f"taskbar not cleared: {sent[-1:]}")

    # A film, copied while it rips: the Copy page switches to the film layout.
    ctl.remove_disc()
    win.poll_disc()
    check(wait(lambda: win.disc is None), "the TV disc stayed after it came out")
    ctl.insert_disc(2)
    win.poll_disc()
    check(wait(lambda: win.disc is not None and win.disc.label == "PITCH_PERFECT" and not win.busy("scan")),
          "the film disc wasn't scanned")
    check(wait(lambda: not any(k.startswith("cover:") for k in win.tasks)), "the cover lookup never finished")
    check(not win.rip.cover.has_cover and win.rip.cover.caption.isHidden(), "a disc without a cover shows one")
    film = win.rip.job
    win.rip.rip_clicked()
    check(wait(lambda: film.copy_mode == "stream" and len(win.samples.get(film.id, {}).get("copy", [])) >= 2, 20),
          f"the film isn't copied while it rips: {film.copy_mode}")
    win.show_page("copy")
    app.processEvents()
    check(win.copy.job is film and win.copy.stack.currentIndex() == 2, "the Copy page isn't in the film layout")
    check(len(win.copy.f_chart.series) == 2 and len(win.copy.f_chart.legend) == 3, "the film chart lacks its series")
    check(wait(lambda: not win.busy("rip") and film.stage == "done", 30), f"the film rip didn't finish: {film.stage}")

    # The two Copying settings are saved.
    win.show_page("settings")
    for box in (win.settings.copy_as_ripped, win.settings.stream_films):
        box.setChecked(not box.isChecked())
    win.settings.save()
    saved = ctl.saved_config
    check(saved is not None and not saved.copy_as_ripped and not saved.stream_films, "the Copying settings weren't saved")
    # How far to look for a title card: 5 minutes unless changed.
    cm = win.settings.card_minutes
    check(cm.currentData() == 5 and [cm.itemData(i) for i in range(cm.count())] == [2, 5, 10, 20],
          f"title card limit choices: {cm.currentData()}")
    cm.setCurrentIndex(cm.findData(10))
    win.settings.save()
    check(ctl.saved_config.card_search_minutes == 10, "the title card limit wasn't saved")

    # A volume disc that mixes seasons: the list from the case matches it, and the picker lists every season.
    from types import SimpleNamespace
    from discshelf import fake as fakemod
    fakemod.EPISODES[(526, 2)] = [(1, "The Dundies", 22), (2, "Sexual Harassment", 22)]
    vol = [Title(i, f"{i:05d}.mpls", 22 * 60, int(1e9), 4, kind="episode", selected=True) for i in (1, 2, 3)]
    vjob = Job(id="office-vol", disc_label="OFFICE_VOL", titles=vol, show="The Office (US)", media_kind="tv",
               stage="match")
    win.show_page("match")
    win.match.open(vjob)
    check(wait(lambda: vjob.id in win.match.show_ids and not win.busy("suggest") and not win.busy("search")),
          "the volume disc's show was never found")
    check(win.match.list_btn.isVisibleTo(win) and win.match.list_btn.isEnabled(), "no Enter episode list button")

    class ListDialog:
        def __init__(self, _parent, _text, read_photo=None):
            check(read_photo is not None, "the episode list dialog got no photo reader")
            self.text = SimpleNamespace(toPlainText=lambda: "1. The Dundies\n2. DIWALI\n3. Gay Witch Hunt")

        def exec(self):
            return gui.QDialog.DialogCode.Accepted

    real_dialog, gui.EpisodeListDialog = gui.EpisodeListDialog, ListDialog
    try:
        win.match.enter_list()
        check(wait(lambda: not win.busy("suggest") and all(m.target == "episode" for m in vjob.matches)),
              f"the episode list didn't match: {vjob.matches}")
    finally:
        gui.EpisodeListDialog = real_dialog
    got = [(m.episode.season, m.episode.number) for m in vjob.matches if m.episode]
    check(got == [(2, 1), (3, 6), (3, 1)], f"matches from the list: {got}")
    check(wait(lambda: (526, None) in win.match.episodes and not win.busy("episodes")), "every season wasn't loaded")
    picker = win.match.table.cellWidget(0, 2).findChild(gui.EpisodePicker)
    check(picker is not None and picker.currentData() == "episode:2:1:", f"picker: {picker and picker.currentData()}")
    picker.open_popup()
    app.processEvents()
    shown = {r["data"].split(":")[1] for r in picker.popup.option_rows() if r["data"].startswith("episode:")}
    check(shown == {"2", "3"}, f"the picker doesn't list every season: {shown}")
    picker.popup.search.setText("diwali")
    app.processEvents()
    check([r["data"] for r in picker.popup.option_rows()] == ["episode:3:6:"],
          f"search: {[r['data'] for r in picker.popup.option_rows()]}")
    picker.popup.close()
    app.processEvents()

    # A disc already ripped: its title cards can be read from the Match page, with a bar and a column.
    from discshelf.models import RippedFile
    vjob.files = [RippedFile(title_index=i, path=f"/nonexistent/t0{i}.mkv") for i in (1, 2, 3)]
    win.match.update_actions()
    check(win.match.cards_btn.isVisibleTo(win) and win.match.cards_btn.isEnabled(), "no Read title cards button")
    ctl.delay = 0.4
    win.match.read_cards()
    check(wait(lambda: win.match.cards_box.isVisibleTo(win) and "Reading title cards" in win.match.cards_title.text()),
          f"no reading status: {win.match.cards_title.text()!r}")
    check(wait(lambda: any(c[0].text().startswith("Reading") for c in win.match.card_cells.values())),
          "no title showed as being read")
    check(wait(lambda: "Reading title cards" in win.windowTitle()), f"window title: {win.windowTitle()}")
    check(wait(lambda: not win.busy("cards") and vjob.matches[0].source == "card"), "title cards weren't read")
    ctl.delay = 0
    app.processEvents()
    win.match.refresh_cards()
    check(win.match.cards_title.text() == "Read 3 title cards" and "1 matched by their card" in win.match.cards_sub.text(),
          f"finished status: {win.match.cards_title.text()!r} {win.match.cards_sub.text()!r}")
    check(len(win.match.cards_bar.segments) == 3 and not win.match.table.isColumnHidden(6), "bar or column missing")
    check(win.match.card_cells[1][0].text() == "“THE DUNDIES”", f"card cell: {win.match.card_cells[1][0].text()}")
    check(win.windowTitle() == "Disc Shelf", f"window title after: {win.windowTitle()}")
    check("Matched 3 titles from their title cards" in win.match.saved.text(), f"cards note: {win.match.saved.text()}")
    picker0 = win.match.table.cellWidget(0, 2).findChild(gui.EpisodePicker)
    check(picker0.currentData().startswith("episode:2:1:"), f"table not redrawn after cards: {picker0.currentData()}")

    # Picking from the picker changes the match, marked as picked by hand.
    picker0.picked.emit("episode:3:1:")
    app.processEvents()
    check((vjob.matches[0].episode.season, vjob.matches[0].episode.number, vjob.matches[0].source) == (3, 1, "manual"),
          f"pick: {vjob.matches[0]}")

    # Scrolling the page over an episode picker doesn't change it.
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    picker0 = win.match.table.cellWidget(0, 2).findChild(gui.EpisodePicker)
    before = picker0.currentData()
    for dy in (-120, 120, -120):
        ev = QWheelEvent(QPointF(5, 5), QPointF(picker0.mapToGlobal(QPoint(5, 5))), QPoint(0, 0), QPoint(0, dy),
                         gui.Qt.MouseButton.NoButton, gui.Qt.KeyboardModifier.NoModifier,
                         gui.Qt.ScrollPhase.NoScrollPhase, False)
        app.sendEvent(picker0, ev)
    check(picker0.currentData() == before and picker0.popup is None, "the mouse wheel changed an episode")

    # The real dialog fills its text box from a photo of the case.
    dlg = gui.EpisodeListDialog(win, "", lambda path: ctl.episode_list_from_photo(path, 526))
    check(dlg.photo_btn.isVisibleTo(dlg) or not dlg.isVisible(), "no photo button")
    dlg.load_photo("/nonexistent/case.jpg")
    check(wait(lambda: dlg.worker is None and dlg.text.toPlainText()), "the photo never filled the list")
    check(dlg.text.toPlainText().splitlines()[0] == "The Dundies" and "Read 3 episode names" in dlg.photo_note.text(),
          f"photo list: {dlg.text.toPlainText()!r} {dlg.photo_note.text()!r}")
    dlg.deleteLater()
    del fakemod.EPISODES[(526, 2)]

    # First-run setup: checks, then two folders that must exist, then Plex; Finish saves them.
    import tempfile
    from discshelf import config as configmod
    real_save = win.save_config
    saved_cfgs = []
    win.save_config = lambda cfg: saved_cfgs.append(_copy_mod.deepcopy(cfg))
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "TV"))
        os.makedirs(os.path.join(root, "Films"))
        dlg = gui.SetupDialog(win)
        check(wait(lambda: not win.busy("setup-checks")), "the setup checks never finished")
        dlg.next_clicked()
        check(dlg.stack.currentIndex() == 1, "Next didn't move to the folders step")
        dlg.folder_edits["tv"].setText(os.path.join(root, "nope"))
        dlg.folder_edits["movie"].setText(os.path.join(root, "Films"))
        win.last_error = None
        dlg.next_clicked()
        check(dlg.stack.currentIndex() == 1 and win.last_error, "a missing folder was accepted")
        win.last_error = None
        dlg.folder_edits["tv"].setText(os.path.join(root, "TV"))
        dlg.next_clicked()
        check(dlg.stack.currentIndex() == 2 and saved_cfgs, "the folders weren't saved")
        check(win.cfg.destination("tv").path == os.path.join(root, "TV")
              and win.cfg.destination("movie").path == os.path.join(root, "Films"), "the folders weren't used")
        dlg.plex_url.setText("192.0.2.10")
        dlg.next_clicked()
        check(win.cfg.plex_url == "http://192.0.2.10:32400" and dlg.result() == gui.QDialog.DialogCode.Accepted,
              f"plex address: {win.cfg.plex_url}")
    win.save_config = real_save

    check(win.last_error is None, f"an error was shown: {win.last_error}")
    win.close()
    app.processEvents()

    for f in failures:
        print("FAIL:", f)
    print(f"gui: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_gui():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
