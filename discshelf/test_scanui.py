#!/usr/bin/env python3
"""
Tests for the scan panel on the Rip page, with the sample discs.

What matters: while a 4K disc scans, the panel shows the disc's name and a guess from it, the
key step and the clock; Stop scan ends the scan with no error and leaves the drive alone
until the next disc; a stalled scan asks, and Keep waiting puts the question away; and with
"Rip the main film right away", a film starts ripping as soon as it's scanned and a
notification says so.

Run:  python3 discshelf/test_scanui.py    (also works under pytest)
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
        print("scanui: skipped (no PySide6)")
        return 0
    try:
        app = QApplication.instance() or QApplication([sys.argv[0]])
    except Exception as exc:  # no usable platform plugin
        print(f"scanui: skipped (Qt can't start offscreen: {exc})")
        return 0

    from discshelf import gui, scanwatch
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
    ctl = FakeController(Config(), delay=0.05)
    ctl.scan_key_steps = 60
    saved = []
    win = gui.MainWindow(ctl, save_config=lambda cfg: saved.append((cfg.after_scan, cfg.notify_scan_done)))
    sent = []
    gui.notify, real_notify = (lambda *a: sent.append(a) or True), gui.notify
    win.resize(1360, 900)
    win.show()
    win.isActiveWindow = lambda: False      # as if the person is in another window
    page, panel = win.rip, win.rip.scan_panel

    # The sample 4K disc is scanned as soon as the window opens; it fetches keys first.
    rows = lambda: [r.name.text() for r in panel.rows if not r.isHidden()]   # noqa: E731
    check(wait(lambda: win.busy("scan") and not panel.isHidden()
               and "Getting this disc's keys from MakeMKV's server" in rows()), f"no key step: {rows()}")
    check(panel.title.text() == "Scanning SEVERANCE_S1_D1", panel.title.text())
    check(page.disc_title.text() == "Severance", f"disc title while scanning: {page.disc_title.text()}")
    check(panel.fact_vals["Looks like"].text().startswith("Severance"), panel.fact_vals["Looks like"].text())
    check("RAW" in panel.fact_vals["HDR"].text(), panel.fact_vals["HDR"].text())
    check(panel.head.text() == "Getting this disc's keys from MakeMKV's server", panel.head.text())
    check(wait(lambda: not panel.req_box.isHidden()), "the request chart isn't shown")
    check(panel.stuck_row.isHidden() and not panel.stop_btn.isHidden(), "stalled buttons on a working scan")

    # Settings changed from the panel are saved.
    panel.rip_opt.setChecked(True)
    panel.notify.setChecked(False)
    check(saved[-2:] == [("rip_film", True), ("rip_film", False)], f"saved: {saved}")
    win.cfg.after_scan, win.cfg.notify_scan_done = "show", True

    # Stop scan: no error, panel gone, no automatic rescan of the same disc.
    panel.stop_btn.click()
    check(wait(lambda: not win.busy("scan") and panel.isHidden()), "the scan didn't stop")
    check(win.last_error is None, f"stopping showed an error: {win.last_error}")
    check(page.job is None and page.disc_title.text() == "No disc scanned yet", page.disc_title.text())
    win.poll_disc()
    app.processEvents()
    check(not win.busy("scan"), "the stopped disc was scanned again on its own")

    # A stalled scan asks; Keep waiting puts the question away until it has been quiet as long again.
    st = scanwatch.ScanStatus(elapsed=500, quiet_s=190, stalled=True, sees_traffic=True, requests=410,
                              steps=[scanwatch.ScanStep("Opening the disc", "done", 4),
                                     scanwatch.ScanStep("Getting this disc's keys from MakeMKV's server", "stuck", 300,
                                                        "410 requests, then nothing for 3:10")])
    panel.start("X", "Drive", "", "show", True)
    panel.show_status(st)
    check(not panel.stuck_row.isHidden() and panel.stop_btn.isHidden(), "a stalled scan doesn't ask")
    check(panel.head.text() == "The scan has stopped making progress", panel.head.text())
    panel.wait_btn.click()
    panel.show_status(st)
    check(panel.stuck_row.isHidden() and panel.rows[1].mark.text() == "●", "Keep waiting didn't put the question away")
    from dataclasses import replace
    panel.show_status(replace(st, quiet_s=400))
    check(not panel.stuck_row.isHidden(), "the question didn't come back after another quiet spell")
    panel.hide()

    # Rip the main film right away: Pitch Perfect starts ripping on its own, and a notification says so.
    win.cfg.after_scan = "rip_film"
    ctl.delay = 0.01
    ctl.insert_disc(2)
    win._auto_failed = False
    page.scan()
    check(wait(lambda: page.job is not None and page.job.disc_label == "PITCH_PERFECT" and win.is_running(page.job)),
          "the film didn't start ripping after the scan")
    if page.job is not None:
        check([t.index for t in page.job.titles if t.selected] == [page.job.titles[0].index],
              f"titles picked: {[t.index for t in page.job.titles if t.selected]}")
        win.stop(page.job)
        wait(lambda: not win.is_running(page.job))
    check(sent and sent[-1][0] == "Ripping Pitch Perfect", f"notification: {sent}")
    check(win.last_error is None, f"an error was shown: {win.last_error}")

    check(gui.main_film([]) is None, "a main film with no titles")
    gui.notify = real_notify
    win.close()
    app.processEvents()

    for f in failures:
        print("FAIL:", f)
    print(f"scanui: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_scanui():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
