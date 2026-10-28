"""Progress on the app's icon in the task manager.

Plasma (and other docks) read the Unity LauncherEntry D-Bus signal: the app names its
.desktop file and sends progress 0..1, whether to show it, and an "urgent" flag that
makes the icon ask for attention. Sent with QtDBus, so it needs no extra packages and
does nothing where no session bus exists.
"""
from __future__ import annotations

import os

# Inside the Flatpak the .desktop file is named after the app id, which Flatpak puts in FLATPAK_ID.
DESKTOP_ID = os.environ.get("FLATPAK_ID") or "disc-shelf"
_PATH = "/com/canonical/unity/launcherentry/disc_shelf"
_IFACE = "com.canonical.Unity.LauncherEntry"


class TaskbarProgress:
    def __init__(self, desktop_id: str = DESKTOP_ID, send=None):
        self.uri = f"application://{desktop_id}.desktop"
        self._send = send or _dbus_send
        self._last: tuple | None = None

    def update(self, fraction: float | None, urgent: bool = False) -> None:
        """fraction None hides the bar. Repeats of the same state aren't sent again."""
        if fraction is None:
            props = {"progress-visible": False, "urgent": urgent}
        else:
            props = {"progress": max(0.0, min(1.0, float(fraction))), "progress-visible": True, "urgent": urgent}
        # Plasma redraws on every signal, so skip changes too small to see.
        key = (round(props.get("progress", -1), 3), props["progress-visible"], urgent)
        if key == self._last:
            return
        self._last = key
        self._send(self.uri, props)

    def clear(self) -> None:
        self.update(None)


def _dbus_send(uri: str, props: dict) -> None:
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusMessage
    except ImportError:
        return
    bus = QDBusConnection.sessionBus()
    if not bus.isConnected():
        return
    msg = QDBusMessage.createSignal(_PATH, _IFACE, "Update")
    msg.setArguments([uri, props])    # PySide sends a dict as a{sv}; wrapping values again double-wraps them
    bus.send(msg)


def notify(summary: str, body: str = "", icon: str = "media-optical", run=None) -> bool:
    """A desktop notification, best effort: False when neither gdbus nor notify-send is there.

    QtDBus can't send the uint32 that Notify takes from Python, so this goes through gdbus
    (part of GLib, in the desktop runtimes and on most hosts) or notify-send."""
    import shutil
    import subprocess
    run = run or (lambda cmd: subprocess.run(cmd, capture_output=True, timeout=5).returncode == 0)
    if shutil.which("gdbus"):
        cmd = ["gdbus", "call", "--session", "--dest", "org.freedesktop.Notifications",
               "--object-path", "/org/freedesktop/Notifications", "--method", "org.freedesktop.Notifications.Notify",
               _gv("Disc Shelf"), "uint32 0", _gv(icon), _gv(summary), _gv(body), "@as []", "@a{sv} {}", "int32 -1"]
    elif shutil.which("notify-send"):
        cmd = ["notify-send", "--app-name=Disc Shelf", f"--icon={icon}", summary, body]
    else:
        return False
    try:
        return bool(run(cmd))
    except Exception:  # noqa: BLE001 -- a missing notification is never worth an error
        return False


def _gv(text: str) -> str:
    """A string in GVariant text form, which gdbus parses its arguments as."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
