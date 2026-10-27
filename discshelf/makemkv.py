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


def scan_disc_parsed(cfg: Config, drive_index: int, run=subprocess.run, min_seconds: int = 0) -> tuple[dict, Disc]:
    """scan_disc, also returning parse_robot's output for the Disc info report."""
    parsed = _run_info(cfg, [f"--minlength={int(min_seconds)}", "info", f"disc:{drive_index}"], run)
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
               bytes_interval: float = 1.0) -> list[str]:
    """Rip each title to an MKV in out_dir, one makemkvcon run per title.

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


def eject(device: str, run=subprocess.run) -> bool:
    """Open the tray. A failure is not worth stopping for, so it is only reported as False."""
    try:
        return run(["eject", device], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
