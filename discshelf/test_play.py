#!/usr/bin/env python3
"""
Tests for Watch (play.py): which player is used, the command that starts it at the title
card, the Flatpak's two routes (the host's mpv or VLC through flatpak-spawn, else the
desktop's default player through the portal), and which file plays a title.

No player is started: the PATH lookup, the host check and Popen are fakes.

Run:  python3 discshelf/test_play.py    (also works under pytest)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import play  # noqa: E402
from discshelf.models import Job, RippedFile, TitleProgress  # noqa: E402


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    # Outside the Flatpak: the first of mpv, vlc, celluloid, xdg-open on the PATH.
    have = {"vlc": "/usr/bin/vlc", "xdg-open": "/usr/bin/xdg-open"}
    p = play.find_player(which=have.get, sandboxed=False)
    check(p is not None and p.name == "vlc" and p.seeks, f"player: {p}")
    check(play.command(p, "/r/t.mkv", 58.4) == ["/usr/bin/vlc", "--start-time=58", "/r/t.mkv"], play.command(p, "/r/t.mkv", 58.4))
    mpv = play.Player("mpv", ["mpv"], True)
    check(play.command(mpv, "/r/t.mkv", 58) == ["mpv", "--start=58", "--", "/r/t.mkv"], play.command(mpv, "/r/t.mkv", 58))
    check(play.command(mpv, "/r/t.mkv", 0) == ["mpv", "--", "/r/t.mkv"], "mpv from the start")
    p = play.find_player(which={"xdg-open": "/usr/bin/xdg-open"}.get, sandboxed=False)
    check(p is not None and p.name == "xdg-open" and not p.seeks, f"fallback: {p}")
    check(play.command(p, "/r/t.mkv", 58) == ["/usr/bin/xdg-open", "/r/t.mkv"], "xdg-open was given a start time")
    check(play.find_player(which=lambda n: None, sandboxed=False) is None, "found a player that isn't there")

    # In the Flatpak: the host's mpv or VLC when the sandbox may spawn on the host, else the portal.
    p = play.find_player(sandboxed=True, host_spawn=True, host_has=lambda n: n == "vlc")
    check(p is not None and p.argv == ["flatpak-spawn", "--host", "vlc"] and p.seeks, f"host player: {p}")
    check(play.find_player(sandboxed=True, host_spawn=False, host_has=lambda n: True) is None,
          "used flatpak-spawn without permission")
    check(play.find_player(sandboxed=True, host_spawn=True, host_has=lambda n: False) is None,
          "found a host player that isn't there")
    with tempfile.TemporaryDirectory() as d:
        info = os.path.join(d, "flatpak-info")
        with open(info, "w") as fh:
            fh.write("[Application]\nname=io.example.App\n\n[Session Bus Policy]\norg.freedesktop.Flatpak=talk\n")
        check(play.can_spawn_on_host(info), "the session bus policy wasn't read")
        with open(info, "w") as fh:
            fh.write("[Session Bus Policy]\norg.freedesktop.Notifications=talk\n")
        check(not play.can_spawn_on_host(info), "spawn allowed without the Flatpak talk name")
        check(not play.can_spawn_on_host(os.path.join(d, "missing")), "a missing info file allowed spawning")

        # launch: a missing file says what to do; a player is started detached; the portal is the last resort.
        f = os.path.join(d, "t.mkv")
        open(f, "w").close()
        check("isn't there" in play.launch(os.path.join(d, "gone.mkv"), 0, player=mpv), "missing file")
        check("once it's ripped" in play.launch("", 0, player=mpv), "no file")
        calls = []
        check(play.launch(f, 58, player=mpv, popen=lambda cmd, **kw: calls.append((cmd, kw))) == "", "launch failed")
        check(calls and calls[0][0] == ["mpv", "--start=58", "--", f] and calls[0][1].get("start_new_session"),
              f"launch: {calls}")

        def broken(cmd, **kw):
            raise FileNotFoundError("mpv")
        check("didn't start" in play.launch(f, 0, player=mpv, popen=broken), "a player that fails to start")
        opened = []
        play._found["player"] = None          # as found in a Flatpak with no host player
        try:
            check(play.launch(f, 58, open_url=lambda path: opened.append(path) or True) == "" and opened == [f],
                  f"portal: {opened}")
            check("No video player" in play.launch(f, 0, open_url=lambda path: False), "portal failure")
        finally:
            play._found.clear()

        # Which file plays a title: the local rip, else the checked NAS copy, nothing while it rips.
        job = Job(id="j", destination="/nas/Shows")
        job.files = [RippedFile(1, f, state="ripped"),
                     RippedFile(2, os.path.join(d, "deleted.mkv"), state="verified", dest_rel="Show/Season 01/Show - S01E02.mkv",
                                local_deleted=True),
                     RippedFile(3, os.path.join(d, "x.mkv"), state="ripped")]
        job.titles_progress = [TitleProgress(1, "ripped"), TitleProgress(2, "verified"), TitleProgress(3, "ripping"),
                               TitleProgress(4, "waiting")]
        check(play.playable(job, 1) == f, "local rip")
        check(play.playable(job, 2) == "/nas/Shows/Show/Season 01/Show - S01E02.mkv", f"NAS copy: {play.playable(job, 2)}")
        check(play.playable(job, 3) == "" and play.playable(job, 4) == "", "a title still ripping plays")
    check(play.fmt_time(58) == "0:58" and play.fmt_time(3725) == "1:02:05", "fmt_time")

    for msg in failures:
        print("FAIL:", msg)
    print(f"play: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_play():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
