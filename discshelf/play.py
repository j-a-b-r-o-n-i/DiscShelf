"""Watching a title in a video player, to tell two titles apart.

mpv and VLC can start at a given second, which puts the title card on screen straight away;
Celluloid and the desktop's default player (xdg-open) start from the beginning. Inside the
Flatpak the host's players aren't visible, so mpv or VLC on the host is started with
`flatpak-spawn --host` (the manifest already lets the app talk to org.freedesktop.Flatpak
for MakeMKV); without one the file goes to the desktop's default player through the portal.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .models import Job

HOST = ["flatpak-spawn", "--host"]      # no --watch-bus: the player may outlive the app
FLATPAK_INFO = "/.flatpak-info"


@dataclass
class Player:
    name: str                    # "mpv" | "vlc" | "celluloid" | "xdg-open" | "portal"
    argv: list[str]              # the command before the options and the file; [] for the portal
    seeks: bool                  # can start at a given second

    @property
    def label(self) -> str:
        return {"mpv": "mpv", "vlc": "VLC", "celluloid": "Celluloid"}.get(self.name, "your video player")


def playable(job: Job, ti: int) -> str:
    """The file that plays a title: the local rip while it's there, else its checked copy on the
    NAS. "" before the title is ripped (or while it rips). The NAS copy isn't looked at here,
    since a share that stopped answering would hang the window."""
    tp = next((p for p in job.titles_progress if p.title_index == ti), None)
    if tp is not None and tp.state in ("waiting", "ripping"):
        return ""
    f = next((f for f in job.files if f.title_index == ti), None)
    if f is None:
        return ""
    if f.path and not f.local_deleted and os.path.isfile(f.path):
        return f.path
    if f.state == "verified" and f.dest_rel and job.destination:
        return os.path.join(job.destination, f.dest_rel)
    return ""


def in_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or os.path.exists(FLATPAK_INFO)


def can_spawn_on_host(info_path: str = FLATPAK_INFO) -> bool:
    """The sandbox may talk to org.freedesktop.Flatpak, which flatpak-spawn --host needs."""
    try:
        with open(info_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return False
    return "org.freedesktop.Flatpak=talk" in text.replace(" ", "")


def _host_has(name: str) -> bool:
    try:
        res = subprocess.run(HOST + ["sh", "-c", f'command -v "{name}"'], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return res.returncode == 0


_found: dict[str, Player | None] = {}


def find_player(which=shutil.which, sandboxed: bool | None = None, host_spawn: bool | None = None,
                host_has=_host_has) -> Player | None:
    """The player to use. None in a Flatpak that can't reach the host's players, where the
    portal opens the file instead. A find is remembered for the session."""
    key = "player"
    if key in _found and which is shutil.which and host_has is _host_has:
        return _found[key]
    sandboxed = in_flatpak() if sandboxed is None else sandboxed
    found: Player | None = None
    if sandboxed:
        spawn = can_spawn_on_host() if host_spawn is None else host_spawn
        for name in ("mpv", "vlc") if spawn else ():
            if host_has(name):
                found = Player(name, HOST + [name], True)
                break
    else:
        for name, seeks in (("mpv", True), ("vlc", True), ("celluloid", False), ("xdg-open", False)):
            path = which(name)
            if path:
                found = Player(name, [path], seeks)
                break
    if which is shutil.which and host_has is _host_has:
        _found[key] = found
    return found


def command(player: Player, path: str, start_s: float = 0.0) -> list[str]:
    """The command that plays path from start_s seconds (players that can't seek start at 0)."""
    start = max(0, int(start_s))
    if player.name == "mpv":
        return [*player.argv, f"--start={start}", "--", path] if start else [*player.argv, "--", path]
    if player.name == "vlc":
        return [*player.argv, f"--start-time={start}", path] if start else [*player.argv, path]
    return [*player.argv, path]


def launch(path: str, start_s: float = 0.0, player: Player | None = None, popen=subprocess.Popen,
           open_url=None) -> str:
    """Start the player and return straight away. Returns "" when it started, else a sentence
    saying what to do."""
    if not path:
        return "This title has no file to play yet. It can be watched once it's ripped."
    if not os.path.exists(path):
        return (f"{path} isn't there. If it's on the NAS, check that the share is mounted, "
                "then choose Watch again.")
    player = player if player is not None else find_player()
    if player is None:
        try:
            if open_url is None:
                from PySide6.QtCore import QUrl
                from PySide6.QtGui import QDesktopServices
                ok = QDesktopServices.openUrl(QUrl.fromLocalFile(path))
            else:
                ok = open_url(path)
        except Exception:  # noqa: BLE001 -- no Qt (the command line): say so instead
            ok = False
        return "" if ok else "No video player was found. Install mpv or VLC, then choose Watch again."
    try:
        popen(command(player, path, start_s), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              start_new_session=True)
    except OSError as e:
        return f"{player.label} didn't start ({e}). Install mpv or VLC, then choose Watch again."
    return ""


def fmt_time(seconds: float) -> str:
    s = max(0, int(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"
