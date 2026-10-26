"""What this computer has for Disc Shelf, and the fixes the app can make itself. The first-run
setup screen shows checks() and offers run_action() for the ones that carry an action.

Inside Disc Shelf's own Flatpak, ffmpeg, mkvmerge, dovi_tool, Tesseract and RapidOCR come with
the app, but MakeMKV and the kernel's sg module live on the host: MakeMKV can't be bundled
(its licence doesn't allow redistribution), so it is either the host's makemkvcon or the
MakeMKV Flatpak, and it is started with `flatpak-spawn --host`.

Nothing here starts makemkvcon or opens a drive for reading: MakeMKV is found with
`command -v` and `flatpak info`, and its key is read from ~/.MakeMKV/settings.conf. Every
outside command, file and device goes through a Host object, so the tests use a fake one.
"""
from __future__ import annotations

import glob as _glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

MAKEMKV_FLATPAK = "com.makemkv.MakeMKV"
FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
MAKEMKV_KEY_PAGE = "https://forum.makemkv.com/forum/viewtopic.php?t=1053"
SG_CONF = "/etc/modules-load.d/sg.conf"
# --watch-bus ends the host process if Disc Shelf goes away, so a rip can't outlive the app.
HOST = ["flatpak-spawn", "--host", "--watch-bus"]


@dataclass
class Check:
    key: str            # "makemkv" | "makemkv_key" | "drive" | "drive_access" | "tools" | "ocr" | "gpu"
    ok: bool
    title: str          # short, e.g. "MakeMKV"
    detail: str         # one plain sentence of the current state
    fix: str            # one plain sentence of what to do when not ok ("" when ok)
    action: str | None  # an action run_action() can perform for the user, or None


def in_flatpak() -> bool:
    return os.path.exists("/.flatpak-info")


def desktop_id() -> str:
    """The name of the app's .desktop file and icon, without the suffix: the app id inside
    the Flatpak (which names them after it), "disc-shelf" for an install from the repo.
    Qt needs it in setDesktopFileName() for the window to get its icon on Wayland."""
    return os.environ.get("FLATPAK_ID") or "disc-shelf"


class Host:
    """The real computer. Tests replace it with a fake that answers from dicts."""

    def __init__(self, sandboxed: bool | None = None):
        self.sandboxed = in_flatpak() if sandboxed is None else sandboxed
        self.home = os.path.expanduser("~")

    def run(self, cmd: list[str], timeout: float = 30) -> subprocess.CompletedProcess:
        """Run and wait. A missing program or a timeout comes back as exit 127 or 124."""
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as e:
            return subprocess.CompletedProcess(cmd, 127, "", str(e))
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, 124, "", "timed out")
        except OSError as e:
            return subprocess.CompletedProcess(cmd, 126, "", str(e))

    def spawn(self, cmd: list[str]) -> bool:
        """Start without waiting, for a browser."""
        try:
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError:
            return False

    def on_host(self, cmd: list[str]) -> list[str]:
        return HOST + cmd if self.sandboxed else list(cmd)

    def host_has(self, name: str) -> bool:
        """Whether a program is on the host's PATH (outside the sandbox when there is one)."""
        if not self.sandboxed:
            return shutil.which(name) is not None
        return self.run(HOST + ["sh", "-c", f'command -v "{name}"']).returncode == 0

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def glob(self, pattern: str) -> list[str]:
        return sorted(_glob.glob(pattern))

    def access(self, path: str, mode: int) -> bool:
        return os.access(path, mode)

    def read(self, path: str) -> str | None:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return None


_found: dict[str, list[str]] = {}


def forget() -> None:
    """Drop what find_makemkvcon() remembered, after MakeMKV is installed or removed."""
    _found.clear()


def find_makemkvcon(host: Host | None = None) -> list[str] | None:
    """The argv that runs makemkvcon: the host's makemkvcon, else the MakeMKV Flatpak; from
    inside Disc Shelf's Flatpak either one through flatpak-spawn --host. None when MakeMKV
    isn't installed. A find is remembered; "not installed" isn't, so a later install is seen."""
    if "argv" in _found:
        return list(_found["argv"])
    h = host or Host()
    argv = None
    if h.host_has("makemkvcon"):
        argv = h.on_host(["makemkvcon"])
    elif _makemkv_flatpak_info(h) is not None:
        argv = h.on_host(["flatpak", "run", "--command=makemkvcon", MAKEMKV_FLATPAK])
    if argv:
        _found["argv"] = argv
        return list(argv)
    return None


def _makemkv_flatpak_info(h: Host) -> str | None:
    """`flatpak info` for the MakeMKV Flatpak, or None when it isn't installed."""
    if not h.sandboxed and not h.which("flatpak"):
        return None
    res = h.run(h.on_host(["flatpak", "info", MAKEMKV_FLATPAK]))
    return res.stdout if res.returncode == 0 else None


def _flatpak_version(info: str) -> str:
    m = re.search(r"^\s*Version:\s*(\S+)", info or "", re.MULTILINE)
    return m.group(1) if m else ""


def _can_install_flatpaks(h: Host) -> bool:
    return h.sandboxed or h.which("flatpak") is not None


# ---------------------------------------------------------------- the checks

def _check_makemkv(h: Host) -> Check:
    title = "MakeMKV"
    argv = find_makemkvcon(h)
    if argv and MAKEMKV_FLATPAK in argv:
        version = _flatpak_version(_makemkv_flatpak_info(h) or "")
        what = f"MakeMKV {version}" if version else "MakeMKV"
        return Check("makemkv", True, title, f"{what} is installed from Flathub.", "", None)
    if argv:
        return Check("makemkv", True, title, "MakeMKV is installed on this computer.", "", None)
    if _can_install_flatpaks(h):
        return Check("makemkv", False, title, "MakeMKV is not installed. Disc Shelf needs it to read discs.",
                     "Install MakeMKV from Flathub; Disc Shelf can do this for you.", "install_makemkv")
    return Check("makemkv", False, title, "MakeMKV is not installed. Disc Shelf needs it to read discs.",
                 "Install MakeMKV from makemkv.com or your distribution's packages, then check again.", None)


_KEY_RE = re.compile(r'^\s*app_Key\s*=\s*"([^"]*)"', re.MULTILINE)


def makemkv_key(h: Host | None = None) -> str:
    """The registration key in ~/.MakeMKV/settings.conf, or "" when there is none. The
    MakeMKV Flatpak keeps the same file, because it is allowed ~/.MakeMKV in the real home."""
    h = h or Host()
    text = h.read(os.path.join(h.home, ".MakeMKV", "settings.conf")) or ""
    m = _KEY_RE.search(text)
    return m.group(1).strip() if m else ""


def _check_key(h: Host, installed: bool) -> Check:
    title = "MakeMKV key"
    key = makemkv_key(h)
    if key.startswith("T-"):
        return Check("makemkv_key", True, title,
                     "MakeMKV has the free beta key. Beta keys expire after a month or two, and MakeMKV "
                     "says so when it needs a new one.", "", None)
    if key:
        return Check("makemkv_key", True, title, "MakeMKV is registered.", "", None)
    if not installed:
        return Check("makemkv_key", False, title, "MakeMKV has no key yet.", "Install MakeMKV first.", None)
    return Check("makemkv_key", False, title,
                 "MakeMKV has no key yet. It reads Blu-rays for 30 days after it is installed, then needs one.",
                 "Copy the free beta key from the MakeMKV forum and paste it into MakeMKV under Help, Register.",
                 "open_makemkv_key")


def _drives(h: Host) -> list[str]:
    return [d for d in h.glob("/dev/sr*") if re.fullmatch(r"/dev/sr\d+", d)]


def _check_drive(h: Host) -> Check:
    title = "Optical drive"
    drives = _drives(h)
    if not drives:
        return Check("drive", False, title, "No optical drive is connected.",
                     "Plug in a Blu-ray drive and wait a few seconds.", None)
    names = ", ".join(drives)
    what = "1 optical drive" if len(drives) == 1 else f"{len(drives)} optical drives"
    return Check("drive", True, title, f"Found {what} ({names}).", "", None)


def _sg_for(h: Host, sr: str) -> list[str]:
    """The SCSI generic device(s) that belong to /dev/srN, through sysfs."""
    name = os.path.basename(sr)
    return ["/dev/" + os.path.basename(p) for p in h.glob(f"/sys/block/{name}/device/scsi_generic/sg*")]


_GROUP_FIX = ("Log out and back in. If that doesn't help, add your account to the optical group "
              "(cdrom on Fedora and Ubuntu) and log in again.")


def _check_drive_access(h: Host) -> Check:
    title = "Drive access"
    drives = _drives(h)
    if not drives:
        return Check("drive_access", False, title, "There is no drive to check yet.",
                     "Plug in a Blu-ray drive first.", None)
    unreadable = [d for d in drives if not h.access(d, os.R_OK)]
    if unreadable:
        return Check("drive_access", False, title, f"Your account can't read {', '.join(unreadable)}.",
                     _GROUP_FIX, None)
    sgs = {d: _sg_for(h, d) for d in drives}
    if not any(sgs.values()):
        return Check("drive_access", False, title,
                     "MakeMKV talks to the drive through the sg kernel module, which is not loaded.",
                     "Load the sg module now and at every start-up; Disc Shelf can do it and asks for your password.",
                     "load_sg")
    blocked = [sg for found in sgs.values() for sg in found if not h.access(sg, os.R_OK | os.W_OK)]
    if blocked:
        return Check("drive_access", False, title, f"Your account can't use {', '.join(blocked)}, "
                     "which MakeMKV needs to talk to the drive.", _GROUP_FIX, None)
    return Check("drive_access", True, title, "MakeMKV can reach the drive.", "", None)


def _resolve(h: Host, name: str) -> str | None:
    """Like pipeline._resolve_tool: the installer puts dovi_tool in ~/.local/bin."""
    found = h.which(name)
    if found:
        return found
    local = os.path.join(h.home, ".local", "bin", name)
    return local if h.access(local, os.X_OK) else None


def _ffmpeg_encoders(h: Host, ffmpeg: str) -> str:
    res = h.run([ffmpeg, "-hide_banner", "-encoders"])
    return res.stdout if res.returncode == 0 else ""


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _check_tools(h: Host, cfg) -> Check:
    title = "Video tools"
    required = [cfg.ffmpeg, cfg.ffprobe, cfg.mkvmerge, "rsync"]     # rsync copies to the NAS
    optional = {cfg.dovi_tool: "Dolby Vision conversions", "tesseract": "the backup text reader"}
    missing = [t for t in required if not _resolve(h, t)]
    absent = [t for t in optional if not _resolve(h, t)]
    notes = []
    if not missing and "libsvtav1" not in _ffmpeg_encoders(h, cfg.ffmpeg):
        notes.append("this ffmpeg can't make AV1 versions (it has no SVT-AV1)")
    if absent:
        notes.append(f"{_join(absent)} {'is' if len(absent) == 1 else 'are'} missing, which only matters for "
                     + _join([optional[t] for t in absent]))
    if missing:
        fix = ("Reinstall Disc Shelf; this copy is missing tools it comes with." if h.sandboxed else
               "Install ffmpeg, mkvtoolnix and rsync with your distribution's package manager, then check again.")
        return Check("tools", False, title, f"{_join(missing)} {'is' if len(missing) == 1 else 'are'} not installed.",
                     fix, None)
    present = [os.path.basename(t) for t in required + [t for t in optional if t not in absent]]
    detail = f"{_join(present)} are available"
    detail += f", but {'; '.join(notes)}." if notes else "."
    return Check("tools", True, title, detail, "", None)


def _check_ocr(h: Host) -> Check:
    from . import ocr
    title = "Text recognition"
    engine = ocr.engine()
    if engine == "rapidocr":
        return Check("ocr", True, title, "RapidOCR is ready to read title cards and case photos.", "", None)
    if engine == "tesseract":
        return Check("ocr", True, title, "Tesseract reads title cards and case photos. RapidOCR is not installed; "
                     "it reads stylised case fonts better.", "", None)
    fix = ("Reinstall Disc Shelf; this copy is missing its text reader." if h.sandboxed else
           "Install tesseract with your package manager, or RapidOCR with: pip install 'disc-shelf[ocr]'.")
    return Check("ocr", False, title, "No text reader is installed, so episodes can't be matched from title cards "
                 "or case photos.", fix, None)


def _check_gpu(h: Host, cfg) -> Check:
    title = "NVIDIA encoder"
    cpu = "AV1 versions are made on the processor, which is slower"
    if "av1_nvenc" not in _ffmpeg_encoders(h, cfg.ffmpeg):
        return Check("gpu", True, title, f"This ffmpeg has no NVIDIA AV1 encoder, so {cpu}.", "", None)
    if not h.glob("/dev/nvidia[0-9]*"):
        return Check("gpu", True, title, f"No NVIDIA graphics card was found, so {cpu}.", "", None)
    # One black frame: quick, and the only sure way to know the card and driver can do AV1.
    res = h.run([cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "color=black:s=256x256:d=0.1", "-frames:v", "1", "-c:v", "av1_nvenc", "-f", "null", "-"],
                timeout=30)
    if res.returncode == 0:
        return Check("gpu", True, title, "The NVIDIA graphics card can make AV1 versions, much faster than the "
                     "processor.", "", None)
    return Check("gpu", True, title, "The NVIDIA card's AV1 encoder didn't start; it needs a GeForce RTX 40-series "
                 f"card or newer and a current driver. Until then {cpu}.", "", None)


def checks(cfg, host: Host | None = None) -> list[Check]:
    """Every check, in the order the setup screen shows them. A check that fails to run
    comes back not ok with what went wrong, rather than stopping the others."""
    h = host or Host()
    out = []
    steps = [
        ("makemkv", "MakeMKV", lambda: _check_makemkv(h)),
        ("makemkv_key", "MakeMKV key", lambda: _check_key(h, out[0].ok if out else False)),
        ("drive", "Optical drive", lambda: _check_drive(h)),
        ("drive_access", "Drive access", lambda: _check_drive_access(h)),
        ("tools", "Video tools", lambda: _check_tools(h, cfg)),
        ("ocr", "Text recognition", lambda: _check_ocr(h)),
        ("gpu", "NVIDIA encoder", lambda: _check_gpu(h, cfg)),
    ]
    for key, title, step in steps:
        try:
            out.append(step())
        except Exception as e:  # a surprise in one check shouldn't hide the rest
            out.append(Check(key, False, title, f"Disc Shelf couldn't check this ({e}).", "", None))
    return out


# ---------------------------------------------------------------- the fixes

def _last_line(res: subprocess.CompletedProcess) -> str:
    lines = (res.stderr or res.stdout or "").strip().splitlines()
    return lines[-1] if lines else f"exit code {res.returncode}"


def _install_makemkv(h: Host) -> tuple[bool, str]:
    if not _can_install_flatpaks(h):
        return False, "Flatpak is not installed, so install MakeMKV from makemkv.com or your distribution's packages."
    h.run(h.on_host(["flatpak", "remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_REPO]), timeout=120)
    res = h.run(h.on_host(["flatpak", "install", "--user", "-y", "--noninteractive", "flathub", MAKEMKV_FLATPAK]),
                timeout=1800)
    forget()
    if res.returncode != 0:
        return False, f"MakeMKV could not be installed from Flathub: {_last_line(res)}"
    return True, "MakeMKV is installed from Flathub."


def _load_sg(h: Host) -> tuple[bool, str]:
    manual = f"run these in a terminal: sudo modprobe sg, then: echo sg | sudo tee {SG_CONF}"
    if not h.host_has("pkexec"):
        return False, f"This computer has no pkexec to ask for your password, so {manual}"
    script = f"modprobe sg && mkdir -p {os.path.dirname(SG_CONF)} && echo sg > {SG_CONF}"
    res = h.run(h.on_host(["pkexec", "/bin/sh", "-c", script]), timeout=300)
    if res.returncode == 0:
        return True, ("The sg kernel module is loaded, and it will load at every start-up, so MakeMKV can "
                      "talk to the drive.")
    if res.returncode == 126:
        return False, "The password window was closed, so nothing was changed."
    if res.returncode == 127:
        return False, f"Your account isn't allowed to make this change. To do it by hand, {manual}"
    return False, f"Loading the sg module failed ({_last_line(res)}). To do it by hand, {manual}"


def _open_key_page(h: Host) -> tuple[bool, str]:
    if h.spawn(["xdg-open", MAKEMKV_KEY_PAGE]):
        return True, ("The MakeMKV forum page with the free beta key is open in your browser. Copy the key and "
                      "paste it into MakeMKV under Help, Register.")
    return False, f"Couldn't open a browser. The free beta key is at {MAKEMKV_KEY_PAGE}"


def run_action(action: str, host: Host | None = None) -> tuple[bool, str]:
    """Do one fix a Check offered. Returns whether it worked and one sentence to show.
    install_makemkv can take minutes (it downloads MakeMKV), so call it off the UI thread."""
    h = host or Host()
    if action == "install_makemkv":
        return _install_makemkv(h)
    if action == "load_sg":
        return _load_sg(h)
    if action == "open_makemkv_key":
        return _open_key_page(h)
    return False, f"Disc Shelf doesn't know how to do {action!r}."
