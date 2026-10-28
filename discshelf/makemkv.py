"""Talk to makemkvcon in robot mode (-r): list drives, scan a disc, rip titles, eject.

Robot output is one record per line, `KIND:field,field,...`, with strings quoted and
backslash-escaped. Attribute ids are MakeMKV's apdefs.h numbers; only the ones Disc
Shelf uses are named below.
"""
from __future__ import annotations

import contextlib
import csv
import os
import re
import shutil
import subprocess
import threading
import traceback
from typing import Callable, Iterable
from xml.sax.saxutils import quoteattr as xml_quote

from . import system
from .config import Config
from .models import Disc, Drive, Stream, Title

# apdefs.h attribute ids
A_TYPE, A_NAME, A_LANG, A_LANG_NAME = 1, 2, 3, 4
A_CODEC_SHORT, A_CODEC_LONG, A_CHAPTERS, A_DURATION = 6, 7, 8, 9
A_SIZE_BYTES, A_CHANNELS, A_SOURCE, A_VIDEO_SIZE = 11, 14, 16, 19
A_STREAM_FLAGS, A_ORIGINAL_TITLE_ID, A_SEGMENTS_MAP, A_OUTPUT_FILE, A_TREE_INFO = 22, 24, 26, 27, 30
A_VOLUME_NAME, A_DEFAULT_FLAGS, A_CHANNEL_LAYOUT = 32, 38, 40

# apdefs.h AP_AVStreamFlag_* bits, as words for Stream.flags
_FLAG_WORDS = [
    (1, "commentary"), (2, "commentary"), (4, "descriptive"),
    (256, "core"),          # a DTS / TrueHD core MakeMKV split out of the HD track
    (512, "secondary"), (4096, "forced"),
]

MSG_LIBREDRIVE = 1011
MSG_DONE = 5011             # "Operation successfully completed"
_SAVED_RE = re.compile(r"(\d+) titles? saved, (\d+) failed")

_KINDS = {"Video": "video", "Audio": "audio", "Subtitles": "subtitle"}

Progress = Callable[[float, str], None]


def command(cfg: Config, out_dir: str | None = None) -> list[str]:
    """The argv that starts makemkvcon. cfg.makemkvcon is used as given, except that the
    default ["makemkvcon"] falls back to whatever system.find_makemkvcon() finds (the
    MakeMKV Flatpak, or the host's MakeMKV from inside Disc Shelf's own Flatpak) when no
    makemkvcon is on the PATH.

    out_dir is the folder this run writes to. The MakeMKV Flatpak can only see ~/Videos
    and ~/.MakeMKV, so a run through `flatpak run` is given that folder as well."""
    cmd = list(cfg.makemkvcon)
    if cmd == ["makemkvcon"] and not shutil.which("makemkvcon"):
        cmd = system.find_makemkvcon() or cmd
    return _grant_folder(cmd, out_dir) if out_dir else cmd


def _grant_folder(cmd: list[str], out_dir: str) -> list[str]:
    if "run" not in cmd or system.MAKEMKV_FLATPAK not in cmd:
        return cmd
    i = cmd.index("run")
    if i == 0 or os.path.basename(cmd[i - 1]) != "flatpak":
        return cmd
    path = os.path.abspath(out_dir)
    # flatpak reads anything after the last colon as the access mode, so a path that
    # contains one needs the mode spelled out.
    grant = f"--filesystem={path}:rw" if ":" in path else f"--filesystem={path}"
    return cmd[:i + 1] + [grant] + cmd[i + 1:]


class Cancelled(Exception):
    """The rip was stopped through the cancel event."""


def parse_line(line: str) -> tuple[str, list[str]] | None:
    """('TINFO', ['0', '9', '0', '1:52:07']) for one robot line; None for anything else."""
    line = line.rstrip("\r\n")
    kind, sep, rest = line.partition(":")
    if not sep or not kind.isalpha() or not kind.isupper():
        return None
    try:
        fields = next(csv.reader([rest], doublequote=False, escapechar="\\"))
    except (csv.Error, StopIteration):
        fields = rest.split(",")
    return kind, fields


def _int(s: str, default: int = 0) -> int:
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def parse_robot(lines: Iterable[str]) -> dict:
    """Collect robot output into one dict.

    drives:   [{index, visible, enabled, flags, drive_name, disc_name, device}]
    cinfo:    {attr id: value}
    tinfo:    {title: {attr id: value}}
    sinfo:    {title: {stream: {attr id: value}}}
    messages: [{code, flags, text, format, params}]
    tcount:   int, or None if MakeMKV never got as far as counting titles
    progress: last PRGV as {current, total, max}; prgt / prgc: last {code, id, name}
    """
    out: dict = {"drives": [], "cinfo": {}, "tinfo": {}, "sinfo": {}, "messages": [],
                 "tcount": None, "progress": None, "prgt": None, "prgc": None}
    for line in lines:
        parsed = parse_line(line)
        if not parsed:
            continue
        kind, f = parsed
        if kind == "DRV" and len(f) >= 7:
            out["drives"].append({"index": _int(f[0]), "visible": _int(f[1]), "enabled": _int(f[2]),
                                  "flags": _int(f[3]), "drive_name": f[4], "disc_name": f[5], "device": f[6]})
        elif kind == "CINFO" and len(f) >= 3:
            out["cinfo"][_int(f[0])] = f[2]
        elif kind == "TINFO" and len(f) >= 4:
            out["tinfo"].setdefault(_int(f[0]), {})[_int(f[1])] = f[3]
        elif kind == "SINFO" and len(f) >= 5:
            out["sinfo"].setdefault(_int(f[0]), {}).setdefault(_int(f[1]), {})[_int(f[2])] = f[4]
        elif kind == "MSG" and len(f) >= 4:
            out["messages"].append({"code": _int(f[0]), "flags": _int(f[1]), "text": f[3],
                                    "format": f[4] if len(f) > 4 else "", "params": f[5:]})
        elif kind == "TCOUNT" and f:
            out["tcount"] = _int(f[0])
        elif kind == "PRGV" and len(f) >= 3:
            out["progress"] = {"current": _int(f[0]), "total": _int(f[1]), "max": _int(f[2])}
        elif kind in ("PRGT", "PRGC") and len(f) >= 3:
            out[kind.lower()] = {"code": _int(f[0]), "id": _int(f[1]), "name": f[2]}
    return out


def parse_duration(s: str) -> int:
    """'1:52:07' → 6727 seconds."""
    total = 0
    for part in (s or "").strip().split(":"):
        total = total * 60 + _int(part)
    return total


def expand_segments(s: str) -> list[int]:
    """'10-12,15' → [10, 11, 12, 15]. Order is kept, because a play-all lists episodes in order."""
    out: list[int] = []
    for part in (s or "").split(","):
        part = part.strip()
        if not part:
            continue
        lo, dash, hi = part.partition("-")
        if dash and lo.strip().isdigit() and hi.strip().isdigit():
            out.extend(range(int(lo), int(hi) + 1))
        elif part.isdigit():
            out.append(int(part))
    return out


def _flag_words(attrs: dict) -> str:
    words: list[str] = []
    if "d" in attrs.get(A_DEFAULT_FLAGS, ""):
        words.append("default")
    bits = _int(attrs.get(A_STREAM_FLAGS, "0"))
    for bit, word in _FLAG_WORDS:
        if bits & bit and word not in words:
            words.append(word)
    if "f" in attrs.get(A_DEFAULT_FLAGS, "") and "forced" not in words:
        words.append("forced")
    return ",".join(words)


def _last_message(parsed: dict, fallback: str) -> str:
    msgs = [m["text"] for m in parsed["messages"] if m["code"] != MSG_DONE]
    return msgs[-1] if msgs else fallback


def _drive(d: dict) -> Drive:
    return Drive(index=d["index"], device=d["device"], model=d["drive_name"].strip(),
                 disc_label=d["disc_name"],
                 has_disc=bool(d["disc_name"]) or d["visible"] == 2)


def _run_info(cfg: Config, args: list[str], run) -> dict:
    cmd = command(cfg) + ["-r", "--cache=1"] + args
    try:
        res = run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        raise RuntimeError(f"MakeMKV is not installed, or {cmd[0]!r} is not on the PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError("MakeMKV took more than 10 minutes to read the disc and was stopped.")
    parsed = parse_robot((res.stdout or "").splitlines())
    parsed["returncode"] = res.returncode
    return parsed


class ScanStopped(RuntimeError):
    """The scan was stopped from the window."""


def _run_info_live(cfg: Config, args: list[str], watch, cancel: threading.Event | None,
                   popen=subprocess.Popen, ports_of=None, poll: float = 0.25) -> dict:
    """_run_info, reading MakeMKV's output as it arrives and feeding each line to watch
    (a scanwatch.ScanWatch). There is no time limit: a 4K disc's first scan can spend many
    minutes fetching keys while printing nothing, so the window decides when to give up,
    from watch's stalled flag. cancel stops MakeMKV and raises ScanStopped.

    ports_of(pid) gives makemkvcon's connections to its server (scanwatch.server_ports
    through find_makemkvcon), or None when the process can't be seen."""
    from . import scanwatch
    if ports_of is None:
        found: dict[int, int | None] = {}

        def ports_of(pid):
            if found.get(pid) is None:      # looked for again until it appears
                found[pid] = scanwatch.find_makemkvcon(pid)
            mk = found[pid]
            return scanwatch.server_ports(mk) if mk is not None else None
    cmd = command(cfg) + ["-r", "--cache=1"] + args
    try:
        proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
    except FileNotFoundError:
        raise RuntimeError(f"MakeMKV is not installed, or {cmd[0]!r} is not on the PATH.")
    lines: list[str] = []

    def read():
        for ln in proc.stdout:
            ln = ln.rstrip("\n")
            lines.append(ln)
            watch.line(ln)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    stopped = False
    blind = False
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                stopped = True
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            if not blind:
                try:
                    ports = ports_of(proc.pid)
                except Exception:  # noqa: BLE001 -- only feeds the window's request count
                    ports = None
                blind = ports is None and watch.status().elapsed > 5
                watch.connections(ports)
            reader.join(timeout=poll)
        proc.wait()
    finally:
        reader.join(timeout=5)
        watch.finish()
    if stopped:
        raise ScanStopped("The scan was stopped.")
    parsed = parse_robot(lines)
    parsed["returncode"] = proc.returncode
    return parsed


def list_drives(cfg: Config, run=subprocess.run) -> list[Drive]:
    """Every optical drive MakeMKV can see. Slots with no drive are dropped."""
    # disc:9999 never exists, so MakeMKV lists the drives and then reports
    # "Failed to open disc"; that message is expected here and not an error.
    parsed = _run_info(cfg, ["info", "disc:9999"], run)
    return [_drive(d) for d in parsed["drives"] if d["device"]]


def build_disc(parsed: dict, drive_index: int) -> Disc:
    drv = next((d for d in parsed["drives"] if d["index"] == drive_index), None)
    drive = _drive(drv) if drv else Drive(index=drive_index)
    c = parsed["cinfo"]
    disc = Disc(drive=drive, label=c.get(A_VOLUME_NAME, ""), name=c.get(A_NAME, ""),
                disc_type=c.get(A_TYPE, ""),
                libredrive=any(m["code"] == MSG_LIBREDRIVE and "LibreDrive" in m["text"]
                               for m in parsed["messages"]))
    for t_index in sorted(parsed["tinfo"]):
        ta = parsed["tinfo"][t_index]
        title = Title(index=t_index, source=ta.get(A_SOURCE, ""), title_id=ta.get(A_ORIGINAL_TITLE_ID, ""),
                      duration_s=parse_duration(ta.get(A_DURATION, "")),
                      size_bytes=_int(ta.get(A_SIZE_BYTES, "0")),
                      chapters=_int(ta.get(A_CHAPTERS, "0")),
                      segments=expand_segments(ta.get(A_SEGMENTS_MAP, "")))
        for s_index, sa in sorted(parsed["sinfo"].get(t_index, {}).items()):
            kind = _KINDS.get(sa.get(A_TYPE, ""), sa.get(A_TYPE, "").lower())
            title.streams.append(Stream(
                index=s_index, kind=kind, codec=sa.get(A_CODEC_SHORT, ""),
                codec_long=sa.get(A_CODEC_LONG, ""), lang=sa.get(A_LANG, ""),
                lang_name=sa.get(A_LANG_NAME, ""),
                name=sa.get(A_NAME, "") or sa.get(A_TREE_INFO, ""),
                channels=sa.get(A_CHANNEL_LAYOUT, "") or sa.get(A_CHANNELS, ""),
                resolution=sa.get(A_VIDEO_SIZE, "") if kind == "video" else "",
                flags=_flag_words(sa)))
            if kind == "video" and sa.get(A_VIDEO_SIZE, "") == "3840x2160":
                disc.uhd = True
        disc.titles.append(title)
    return disc


def scan_disc(cfg: Config, drive_index: int, run=subprocess.run, min_seconds: int = 0) -> Disc:
    """Read the disc in drive `drive_index`: its titles, their streams, and whether it is UHD.

    MakeMKV numbers titles after dropping those shorter than --minlength, so rip_titles
    must be given the same min_seconds or title numbers will point at different titles.
    """
    return scan_disc_parsed(cfg, drive_index, run, min_seconds)[1]


def scan_disc_parsed(cfg: Config, drive_index: int, run=subprocess.run, min_seconds: int = 0, *,
                     watch=None, cancel: threading.Event | None = None,
                     popen=subprocess.Popen, ports_of=None) -> tuple[dict, Disc]:
    """scan_disc, also returning parse_robot's output for the Disc info report. With watch,
    MakeMKV's output is read as it arrives (see _run_info_live) and run isn't used."""
    args = [f"--minlength={int(min_seconds)}", "info", f"disc:{drive_index}"]
    if watch is not None:
        parsed = _run_info_live(cfg, args, watch, cancel, popen=popen, ports_of=ports_of)
    else:
        parsed = _run_info(cfg, args, run)
    if parsed["returncode"] != 0 or (not parsed["tinfo"] and not parsed["cinfo"]):
        raise RuntimeError("MakeMKV could not read the disc: "
                           + _last_message(parsed, f"makemkvcon exited with code {parsed['returncode']}"))
    return parsed, build_disc(parsed, drive_index)


# --- Track selection -------------------------------------------------------------
#
# MakeMKV decides which tracks go into the MKV with a "selection rule": a comma-separated
# list of action:condition tokens, all evaluated left to right, so a later token overrides
# an earlier one. Actions: +sel / -sel select or unselect a track; +N, -N, =N change its
# weight, and lighter tracks come first in the file. Conditions combine tokens with
# | & ! and parentheses. Tokens used here (from the token list in the profile schema,
# forum.makemkv.com viewtopic.php?t=4386 and p=17397): video, audio, subtitle, a 3-letter
# ISO 639-2 code, nolang, single, special (commentary, descriptive audio), core (the
# DTS / AC3 core MakeMKV splits out of an HD track), havemulti (a stereo track when a
# multichannel one exists in the same language), mvcvideo (the 3D view), forced.
# Note "havecore" is the HD track itself, so -sel:core is what keeps the HD track.

_LANG_RE = re.compile(r"^[a-z]{3}$")
SELECTION_ERRORS = ("Failed to load profile", "Failed to load conversion profile",
                    "Profile parsing error", "nvalid token", "nvalid subtoken")
PROFILE_NAME = ".discshelf-selection.mmcp.xml"


def _langs(langs: list[str], what: str) -> list[str]:
    out: list[str] = []
    for lang in langs:
        lang = (lang or "").strip().lower()
        if not _LANG_RE.match(lang):
            raise ValueError(f"{what} language {lang!r} is not a 3-letter ISO 639-2 code")
        if lang not in out:
            out.append(lang)
    return out


def selection_for(audio_langs: list[str], subtitle_langs: list[str]) -> str:
    """A MakeMKV selection rule keeping the video, the main audio in audio_langs and every
    subtitle track (forced ones included) in subtitle_langs, ordered as the lists are.

    No audio languages keeps every main audio track; no subtitle languages keeps none.
    Untagged audio (nolang) and a title's only audio track (single) are kept too, so a
    disc with odd language tags never comes out silent.
    """
    audio = _langs(audio_langs, "Audio")
    subs = _langs(subtitle_langs, "Subtitle")
    rule = ["-sel:all", "+sel:video", "-sel:mvcvideo"]
    if audio:
        rule.append(f"+sel:(audio&({'|'.join(audio + ['nolang', 'single'])}))")
    else:
        rule.append("+sel:audio")
    # commentary, descriptive audio, the lossy core of an HD track, a stereo downmix
    rule.append("-sel:(audio&(special|core|havemulti))")
    if subs:
        rule.append(f"+sel:(subtitle&({'|'.join(subs)}))")
    rule.append("=100:all")
    # The first language listed gets the lightest weight, so it is the first (default) track.
    for kind, langs in (("audio", audio), ("subtitle", subs)):
        for i, lang in enumerate(langs):
            rule.append(f"-{10 * (len(langs) - i)}:({kind}&{lang})")
    return ",".join(rule)


def profile_xml(selection: str, preferred_lang: str = "") -> str:
    """MakeMKV 2.0's default.mmcp.xml with the selection rule written in.

    The rule goes in twice: as each output's defaultSelection (the default profile only
    refers to $app_DefaultSelectionString there) and in profileSettings, which MakeMKV
    1.12.4+ says it ignores but which makemkvcon users report still works. Everything
    else is the stock profile, so only the track selection changes.
    """
    sel = xml_quote(selection)
    pref = f"\n        app_PreferredLanguage={xml_quote(preferred_lang)}" if preferred_lang else ""
    outputs = "".join(
        f'''
    <trackSettings input="{inp}">
        <output outputSettingsName="{out}" defaultSelection={sel}>
        </output>
    </trackSettings>
''' for inp, out in (("default", "copy"), ("LPCM-stereo", "lpcm"), ("LPCM-multi", "wavex")))
    return f'''<?xml version="1.0" encoding="utf-8"?>
<profile>
    <name lang="eng">Disc Shelf selection</name>
    <mkvSettings
        ignoreForcedSubtitlesFlag="true"
        useISO639Type2T="false"
        setFirstAudioTrackAsDefault="true"
        setFirstSubtitleTrackAsDefault="true"
        setFirstForcedSubtitleTrackAsDefault="true"
        insertFirstChapter00IfMissing="true"
    />
    <profileSettings{pref}
        app_DefaultSelectionString={sel}
    />
    <outputSettings name="copy" outputFormat="directCopy">
        <description lang="eng">Copy track as is</description>
    </outputSettings>
    <outputSettings name="lpcm" outputFormat="LPCM-raw">
        <description lang="eng">Save as raw LPCM</description>
    </outputSettings>
    <outputSettings name="wavex" outputFormat="LPCM-wavex">
        <description lang="eng">Save as LPCM in WAV container</description>
    </outputSettings>
{outputs}</profile>
'''


@contextlib.contextmanager
def apply_selection(selection: str | None, out_dir: str, preferred_lang: str = ""):
    """Yield the extra makemkvcon arguments that apply `selection` to one run, and remove
    the profile file afterwards. None yields no arguments (MakeMKV's own default).

    makemkvcon takes a conversion profile per run with --profile=<file>, so the user's
    ~/.MakeMKV/settings.conf (which holds their registration key) is never touched. The
    profile sits in out_dir rather than /tmp because a Flatpak MakeMKV can't see the
    host's /tmp but must be able to write out_dir anyway.
    """
    if selection is None:
        yield []
        return
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(os.path.abspath(out_dir), PROFILE_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(profile_xml(selection, preferred_lang))
    os.replace(tmp, path)
    try:
        yield [f"--profile={path}"]
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


_supported: dict[tuple[str, ...], bool] = {}


def selection_supported(cfg: Config, run=subprocess.run) -> bool:
    """Whether this makemkvcon accepts --profile, so rip_titles(selection=...) can be used.

    makemkvcon with an option and no command only checks its options and prints usage,
    in milliseconds and without opening a drive; an unknown option says "unrecognized".
    """
    base = command(cfg)
    key = tuple(base)
    if key not in _supported:
        try:
            res = run(base + ["--profile=/nonexistent.mmcp.xml"],
                      capture_output=True, text=True, timeout=30)
            out = (getattr(res, "stdout", "") or "") + (getattr(res, "stderr", "") or "")
            _supported[key] = "unrecognized option" not in out and "Use: makemkvcon" in out
        except (OSError, subprocess.SubprocessError):
            _supported[key] = False
    return _supported[key]


# --- Ripping ---------------------------------------------------------------------

def _mkv_snapshot(out_dir: str) -> dict[str, tuple[float, int]]:
    snap = {}
    for name in os.listdir(out_dir):
        # Hidden names are Disc Shelf's own temporary files (the previous title being rewritten
        # beside it), never MakeMKV's output.
        if name.lower().endswith(".mkv") and not name.startswith("."):
            try:
                st = os.stat(os.path.join(out_dir, name))
            except FileNotFoundError:
                continue
            snap[name] = (st.st_mtime, st.st_size)
    return snap


def _changed(before: dict, out_dir: str) -> list[str]:
    after = _mkv_snapshot(out_dir)
    return sorted(name for name, sig in after.items() if before.get(name) != sig)


def _ours(before: dict, out_dir: str, t_index: int) -> list[str]:
    """Files this title's run wrote: its own "<disc name>_tNN.mkv", or a name that wasn't there
    before. An older file that merely changed is not ours: the previous title's file is
    rewritten (languages, HDR) while the next title rips, and must never be taken for it."""
    own = f"_t{t_index:02d}.mkv"
    return [n for n in _changed(before, out_dir) if n.lower().endswith(own) or n not in before]


def _pick(names: list[str], t_index: int) -> str | None:
    """The title's own file among new ones: MakeMKV names it "<disc name>_tNN.mkv"."""
    own = [n for n in names if n.lower().endswith(f"_t{t_index:02d}.mkv")]
    return (own or names or [None])[0]


def _safe(cb, *args) -> None:
    # A failing UI callback must not stop a rip that is half done.
    try:
        cb(*args)
    except Exception:
        traceback.print_exc()


class _TitleWatch:
    """Watches out_dir while one title rips: reports the new file once, then its size."""

    def __init__(self, out_dir, before, t_index, on_start, on_bytes, interval):
        self.out_dir, self.before, self.t_index = out_dir, before, t_index
        self.on_start, self.on_bytes, self.interval = on_start, on_bytes, interval
        self.path: str | None = None
        self.last_size = -1
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if self.on_start or self.on_bytes:
            self.thread.start()

    def found(self) -> str | None:
        """Look for the title's file; call on_title_start the first time it is seen."""
        with self.lock:
            if self.path is None:
                name = _pick(_ours(self.before, self.out_dir, self.t_index), self.t_index)
                if name:
                    self.path = os.path.join(self.out_dir, name)
                    if self.on_start:
                        _safe(self.on_start, self.t_index, self.path)
            return self.path

    def report(self, force: bool = False):
        path = self.found()
        if path is None or not self.on_bytes:
            return
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        with self.lock:
            if force or size != self.last_size:
                self.last_size = size
                _safe(self.on_bytes, self.t_index, path, size)

    def _run(self):
        # The file is looked for often, so the copier starts early; sizes go out every interval.
        tick = min(0.25, self.interval)
        next_bytes = 0.0
        waited = 0.0
        while not self.stop.wait(tick):
            waited += tick
            if self.found() is not None and waited >= next_bytes:
                self.report(force=True)
                next_bytes = waited + self.interval

    def finish(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join()


def rip_titles(cfg: Config, drive_index: int, title_indices: list[int], out_dir: str,
               progress: Progress, cancel: threading.Event | None = None,
               popen=subprocess.Popen, min_seconds: int = 0, *,
               on_title_start: Callable[[int, str], None] | None = None,
               on_title_done: Callable[[int, str], None] | None = None,
               on_bytes: Callable[[int, str, int], None] | None = None,
               selection: str | None = None,
               preferred_lang: str = "",
               bytes_interval: float = 1.0,
               together: bool = False) -> list[str]:
    """Rip each title to an MKV in out_dir, one makemkvcon run per title, or with
    together=True all in one run (see rip_together and together_worth_it).

    progress(fraction, text) gets the overall fraction across all titles. Returns the
    new or rewritten .mkv paths in title order (MakeMKV names them "<disc name>_tNN.mkv",
    NN being the title number). Raises Cancelled if `cancel` is set, after removing the
    half-written file, and RuntimeError with MakeMKV's last message if a title fails.

    For each title, in this order: on_title_start(title, path) once MakeMKV has created
    the file (it is still being written); on_bytes(title, path, size) about every
    bytes_interval seconds while it grows, and once more with the final size;
    on_title_done(title, path) once makemkvcon has exited successfully, before the next
    title starts. on_title_start and on_bytes run on a background thread.

    selection: a MakeMKV selection rule (see selection_for) for this run only, applied
    through a temporary --profile; check selection_supported(cfg) first. None leaves
    MakeMKV's own default.
    """
    if together:
        return rip_together(cfg, drive_index, title_indices, out_dir, progress, cancel, popen, min_seconds,
                            on_title_start=on_title_start, on_title_done=on_title_done, on_bytes=on_bytes,
                            selection=selection, preferred_lang=preferred_lang, bytes_interval=bytes_interval)
    os.makedirs(out_dir, exist_ok=True)
    n = len(title_indices)
    paths: list[str] = []
    with apply_selection(selection, out_dir, preferred_lang) as extra:
        for i, t_index in enumerate(title_indices):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            label = f"Title {i + 1} of {n}"
            progress(i / n if n else 0.0, f"{label}: starting MakeMKV")
            before = _mkv_snapshot(out_dir)
            cmd = command(cfg, out_dir) + ["-r", "--progress=-same", f"--minlength={int(min_seconds)}",
                                           *extra, "mkv", f"disc:{drive_index}", str(t_index), out_dir]
            try:
                proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            except FileNotFoundError:
                raise RuntimeError(f"MakeMKV is not installed, or {cmd[0]!r} is not on the PATH.")

            stopped = threading.Event()
            failed = False      # makemkvcon can exit 0 after "0 titles saved, 1 failed"
            bad_selection = ""  # MakeMKV fell back to its default tracks; the file is not what was asked for
            if cancel is not None:
                # readline blocks while MakeMKV is quiet (opening the disc can take a minute),
                # so a separate thread watches the cancel event and stops the process.
                def watch(proc=proc):
                    while not stopped.is_set():
                        if cancel.wait(0.25):
                            if proc.poll() is None:
                                proc.terminate()
                            return
                threading.Thread(target=watch, daemon=True).start()
            tw = _TitleWatch(out_dir, before, t_index, on_title_start, on_bytes, bytes_interval)
            tw.start()

            messages: list[str] = []
            step = ""
            frac = 0.0
            try:
                for line in proc.stdout:
                    parsed = parse_line(line)
                    if not parsed:
                        continue
                    kind, f = parsed
                    if kind == "PRGV" and len(f) >= 3:
                        total, mx = _int(f[1]), _int(f[2])
                        frac = min(max(total / mx, 0.0), 1.0) if mx > 0 else 0.0
                        progress((i + frac) / n, f"{label}: {step}" if step else label)
                    elif kind in ("PRGT", "PRGC") and len(f) >= 3:
                        # PRGC is the current step, the more specific one; PRGT only fills in until it arrives
                        if kind == "PRGC" or not step:
                            step = f[2]
                        progress((i + frac) / n, f"{label}: {step}")
                    elif kind == "MSG" and len(f) >= 4:
                        messages.append(f[3])
                        m = _SAVED_RE.search(f[3])
                        if m and int(m.group(2)) > 0:
                            failed = True
                        if selection is not None and (
                                any(e in f[3] for e in SELECTION_ERRORS)
                                or ("using profile" in f[3] and PROFILE_NAME not in f[3])):
                            bad_selection = f[3]
                rc = proc.wait()
            finally:
                stopped.set()
                tw.finish()

            new = [os.path.join(out_dir, name) for name in _ours(before, out_dir, t_index)]
            cancelled = cancel is not None and cancel.is_set()
            if cancelled or rc != 0 or failed or bad_selection:
                # a half-written MKV is useless and would be mistaken for a finished one later
                for p in new:
                    try:
                        os.remove(p)
                    except OSError:
                        pass
            if cancelled:
                raise Cancelled()
            if bad_selection:
                raise RuntimeError(f"MakeMKV did not accept the track selection for title {t_index}: {bad_selection}")
            if rc != 0 or failed or not new:
                last = messages[-1] if messages else f"makemkvcon exited with code {rc}"
                raise RuntimeError(f"MakeMKV could not rip title {t_index}: {last}")
            paths.extend(new)
            # The watcher may have missed a file that appeared and finished between two looks,
            # so start and the final size are reported here too; found() never repeats a start.
            if on_title_start or on_bytes or on_title_done:
                tw.report(force=True)
                if on_title_done:
                    _safe(on_title_done, t_index, tw.path)
            progress((i + 1) / n, f"{label}: done")
    return paths


_FILE_TITLE_RE = re.compile(r"_t(\d+)\.mkv$", re.IGNORECASE)
_FAILED_TITLE_RE = re.compile(r"Failed to save title (\d+)", re.IGNORECASE)


def rip_together(cfg: Config, drive_index: int, title_indices: list[int], out_dir: str,
                 progress: Progress, cancel: threading.Event | None = None,
                 popen=subprocess.Popen, min_seconds: int = 0, *,
                 on_title_start: Callable[[int, str], None] | None = None,
                 on_title_done: Callable[[int, str], None] | None = None,
                 on_bytes: Callable[[int, str, int], None] | None = None,
                 selection: str | None = None,
                 preferred_lang: str = "",
                 bytes_interval: float = 1.0) -> list[str]:
    """Rip every title at least min_seconds long in one makemkvcon run ("all"), keeping
    title_indices and deleting the others as each finishes.

    MakeMKV opens the disc once, instead of once per title as rip_titles does (about 40 s
    each on a Blu-ray). It numbers titles the same way as an info scan with the same
    min_seconds, and writes them one after another, so a title is finished when the next
    one's file appears, or when makemkvcon exits. The callbacks are rip_titles', except that
    on_title_done runs on the background thread too, as soon as the title is finished.
    Returns the kept paths in title order. Raises Cancelled, or RuntimeError if MakeMKV
    fails or a title in title_indices wasn't written; titles finished before that have
    already been handed on."""
    os.makedirs(out_dir, exist_ok=True)
    wanted = set(title_indices)
    n = len(wanted)
    paths: dict[int, str] = {}
    lock = threading.Lock()
    failed: set[int] = set()

    with apply_selection(selection, out_dir, preferred_lang) as extra:
        progress(0.0, "Opening the disc")
        before = _mkv_snapshot(out_dir)
        cmd = command(cfg, out_dir) + ["-r", "--progress=-same", f"--minlength={int(min_seconds)}",
                                       *extra, "mkv", f"disc:{drive_index}", "all", out_dir]
        try:
            proc = popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError:
            raise RuntimeError(f"MakeMKV is not installed, or {cmd[0]!r} is not on the PATH.")

        stopped = threading.Event()
        state = {"cur": None, "started": [], "last_bytes": 0.0, "size": -1}

        def finish(ti: int, path: str) -> None:
            with lock:
                bad = ti in failed
            if ti in wanted and not bad:
                try:
                    size = os.path.getsize(path)
                except OSError:
                    return
                if on_bytes:
                    _safe(on_bytes, ti, path, size)
                paths[ti] = path
                if on_title_done:
                    _safe(on_title_done, ti, path)
            else:
                with contextlib.suppress(OSError):
                    os.remove(path)

        def look() -> None:
            """Hand on finished titles and start new ones; report the current one's size."""
            found = {}
            for name in _changed(before, out_dir):
                m = _FILE_TITLE_RE.search(name)
                if m and name not in before:
                    found[int(m.group(1))] = os.path.join(out_dir, name)
            for ti in sorted(t for t in found if t not in state["started"]):
                if state["cur"] is not None:
                    finish(*state["cur"])
                state["started"].append(ti)
                state["cur"] = (ti, found[ti])
                state["size"] = -1
                if ti in wanted and on_title_start:
                    _safe(on_title_start, ti, found[ti])
            cur = state["cur"]
            if cur is not None and cur[0] in wanted and on_bytes:
                try:
                    size = os.path.getsize(cur[1])
                except OSError:
                    return
                if size != state["size"]:
                    state["size"] = size
                    _safe(on_bytes, cur[0], cur[1], size)

        def watch_files() -> None:
            tick = min(0.25, bytes_interval)
            while not stopped.wait(tick):
                look()

        def watch_cancel() -> None:
            while not stopped.is_set():
                if cancel.wait(0.25):
                    if proc.poll() is None:
                        proc.terminate()
                    return

        watcher = threading.Thread(target=watch_files, daemon=True)
        watcher.start()
        if cancel is not None:
            threading.Thread(target=watch_cancel, daemon=True).start()

        messages: list[str] = []
        failed_count = 0
        bad_selection = ""
        frac = 0.0
        try:
            for line in proc.stdout:
                parsed = parse_line(line)
                if not parsed:
                    continue
                kind, f = parsed
                if kind == "PRGV" and len(f) >= 3:
                    total, mx = _int(f[1]), _int(f[2])
                    frac = min(max(total / mx, 0.0), 1.0) if mx > 0 else frac
                    k = sum(1 for t in state["started"] if t in wanted)
                    progress(frac, f"Title {max(1, k)} of {n}" if k else "Opening the disc")
                elif kind == "MSG" and len(f) >= 4:
                    messages.append(f[3])
                    m = _FAILED_TITLE_RE.search(f[3])
                    if m:
                        with lock:
                            failed.add(int(m.group(1)))
                    m = _SAVED_RE.search(f[3])
                    if m:
                        failed_count = int(m.group(2))
                    if selection is not None and (
                            any(e in f[3] for e in SELECTION_ERRORS)
                            or ("using profile" in f[3] and PROFILE_NAME not in f[3])):
                        bad_selection = f[3]
            rc = proc.wait()
        finally:
            stopped.set()
            watcher.join()

        cancelled = cancel is not None and cancel.is_set()
        if not (cancelled or rc != 0 or bad_selection):
            look()                  # a title that appeared after the last look
            if state["cur"] is not None:
                finish(*state["cur"])
                state["cur"] = None
        elif state["cur"] is not None:
            # a half-written MKV is useless and would be mistaken for a finished one later
            with contextlib.suppress(OSError):
                os.remove(state["cur"][1])
        if cancelled:
            raise Cancelled()
        if bad_selection:
            raise RuntimeError(f"MakeMKV did not accept the track selection: {bad_selection}")
        last = messages[-1] if messages else f"makemkvcon exited with code {rc}"
        if rc != 0:
            raise RuntimeError(f"MakeMKV stopped ripping: {last}")
        missing = sorted(wanted - set(paths))
        if missing or (failed_count and not failed):   # unpicked titles that failed don't matter
            which = ", ".join(str(t) for t in missing) or "one title"
            raise RuntimeError(f"MakeMKV could not rip title {which}: {last}")
        progress(1.0, "Done")
    return [paths[ti] for ti in sorted(paths)]


def together_worth_it(wanted: list[Title], everything: list[Title], min_seconds: int,
                      open_s: float = 40.0, rate: float = 9e6) -> bool:
    """Whether one MakeMKV run for the whole disc is quicker than one per title.

    One run saves opening the disc for every title after the first, but also rips the
    titles at least min_seconds long that weren't picked (a play-all title can be the
    whole disc again). rate is a cautious Blu-ray reading speed in bytes per second."""
    if len(wanted) < 2:
        return False
    keep = {t.index for t in wanted}
    extra = [t for t in everything if t.index not in keep and t.duration_s >= min_seconds]
    if any(not t.size_bytes for t in extra):
        return False
    return sum(t.size_bytes for t in extra) / rate < (len(wanted) - 1) * open_s


def eject(device: str, run=subprocess.run) -> bool:
    """Open the tray. A failure is not worth stopping for, so it is only reported as False."""
    try:
        return run(["eject", device], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
