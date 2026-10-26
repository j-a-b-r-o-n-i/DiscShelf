"""The Disc Shelf window (PySide6): Rip, Match episodes, Copy to NAS, History and Settings.

Only this module imports PySide6, so the rest of Disc Shelf stays headless. Every
Controller call blocks, so each one runs on a QThread; progress callbacks may come
from any thread and reach the widgets through queued signals.

    python3 -m discshelf.gui           # the real drive, with ~/.config/disc-shelf/config.json
    python3 -m discshelf.gui --fake    # sample discs, no drive needed
"""
from __future__ import annotations

import atexit
import bisect
import contextlib
import datetime as _dt
import copy as _copy
import math
import os
import re
import shutil
import sys
import tempfile
import threading
import time

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontDatabase, QFontMetrics, QIcon, QPainter,
                           QPainterPath, QPalette, QPen, QPixmap)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLayout, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSlider, QSpinBox, QStackedWidget,
    QTableWidget, QTableWidgetItem, QToolButton, QToolTip, QVBoxLayout, QWidget,
)

from . import config as configmod
from . import system as _system
try:
    from .classify import SHORT_S
except ImportError:                                  # the classifier is optional for the window
    SHORT_S = 300
from .models import Disc, Drive, Episode, HdrPolicy, Job, Match, RippedFile, Sample, Title, TitleProgress
from .picker import PICKER_STYLE, EpisodePicker
from .taskbar import TaskbarProgress

try:
    from .naming import EXTRAS_FOLDERS
except ImportError:                                  # the naming module is optional for the window
    EXTRAS_FOLDERS = ("Behind The Scenes", "Deleted Scenes", "Featurettes", "Interviews",
                      "Scenes", "Shorts", "Trailers", "Other")

DESKTOP_ID = _system.desktop_id()  # the Flatpak's app id, or "disc-shelf" for ~/.local/share/applications/disc-shelf.desktop
DESKTOP_ICON = DESKTOP_ID if os.environ.get("FLATPAK_ID") else "media-optical"

# The mockup's palette. Every text colour here is at least 4.5:1 on the panel colour.
BG, SIDE, PANEL, RAISED = "#111315", "#15181B", "#1A1D20", "#23272B"
BORDER, FIELD, ROWLINE = "#2E3338", "#3A4046", "#262A2E"
TEXT, SOFT, MUTED = "#E9EBED", "#C3C8CD", "#A2A9B0"
ACCENT, ON_ACCENT, LINK = "#F0A23B", "#1A1206", "#7CB8F0"

CHIPS = {   # name: (background, text, border or "")
    "good": ("#1F2A22", "#8FE0B0", ""),
    "warn": ("#2B2416", "#F5C27A", ""),
    "plain": ("#262A2E", "#C3C8CD", ""),
    "info": ("#1E2733", "#A9D1F6", ""),
    "bad": ("#2E1C1C", "#F2A7A7", ""),
    "raw": ("#1F2A22", "#8FE0B0", "#3E6B50"),
    "notraw": ("#2B2416", "#F5C27A", "#6B5527"),
}

# Chart and progress colours, from the mockups. Series colours are only ever used for marks,
# never for text; every text colour here is at least 4.5:1 on PANEL (#1A1D20).
RIP_LINE, NAS_LINE, CHECK_LINE = "#C97F22", "#4F8FCB", "#3E9B6A"
COPY_BAR, DONE_BAR, FAIL_BAR = "#7CB8F0", "#6FCF97", "#F2A7A7"
RIP_TEXT, COPY_TEXT, DONE_TEXT, FAIL_TEXT = "#F5C27A", "#A9D1F6", "#8FE0B0", "#F2A7A7"
TRACK = "#2A2F34"
BLURAY_1X = 4.5e6       # bytes per second at Blu-ray 1x
OVERLAP = ("per_title", "stream")
LANG_NAMES = {"eng": "English", "spa": "Spanish", "fra": "French", "deu": "German", "ger": "German",
              "ita": "Italian", "jpn": "Japanese", "por": "Portuguese", "nld": "Dutch", "kor": "Korean"}

STAGES = ("rip", "match", "copy", "plex")
STAGE_NAMES = ("Rip", "Match", "Copy", "In Plex")


# ---------------------------------------------------------------------------
# Text helpers (no Qt)
# ---------------------------------------------------------------------------

def fmt_size(n: int | float) -> str:
    if not n:
        return "—"
    if n >= 1e12:
        return f"{n / 1e12:.2f} TB"
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    return f"{n / 1e6:.0f} MB"


def fmt_len(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_rate(bps: float, decimals: int = 1) -> str:
    return f"{bps / 1e6:.{decimals}f} MB/s"


def fmt_gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


def fmt_elapsed(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def fmt_ago(seconds: float) -> str:
    return "now" if seconds < 1 else f"{fmt_elapsed(seconds)} ago"


def nice_step(top: float, n: float = 3.5) -> float:
    """A round gridline step (1, 2, 2.5 or 5 times a power of ten) giving about n lines up to top."""
    if top <= 0:
        return 1.0
    raw = top / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if m * mag >= raw * 0.999:
            return m * mag
    return 10 * mag


def step_value(xs: list[float], ys: list[float], x: float) -> float:
    """The last value at or before x in a series sampled at xs (0 before the first sample)."""
    i = bisect.bisect_right(xs, x) - 1
    return ys[i] if i >= 0 else 0.0


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path and (path == home or path.startswith(home + "/")) else path


_DRIVE_TYPE = re.compile(r"^(BD|DVD|CD|HD)[-+A-Z/]*$")


def drive_chip_text(model: str) -> str:
    """'BD-RE PIONEER BD-RW BDR-XD08U 1.01 DCDL000000UC' -> 'PIONEER BDR-XD08U · firmware 1.01'."""
    toks = (model or "").split()
    # MakeMKV ends the drive name with the serial number after the firmware version.
    if len(toks) >= 2 and re.fullmatch(r"[0-9A-Z]?\d+\.\d+\w*", toks[-2]):
        toks.pop()
    fw = toks.pop() if toks and re.fullmatch(r"[0-9A-Z]?\d+\.\d+\w*", toks[-1]) else ""
    name = " ".join(t for t in toks if not _DRIVE_TYPE.match(t)) or model or "Optical drive"
    return f"{name} · firmware {fw}" if fw else name


_KIND_PHRASE = {
    "episode": ("looks like an episode", "look like episodes"),
    "feature": ("looks like the film", "look like feature-length films"),
    "play_all": ("is a play-all", "are play-alls"),
    "extra": ("is an extra", "are extras"),
    "duplicate": ("is a duplicate", "are duplicates"),
    "short": ("is a short clip", "are short clips"),
    "unknown": ("is not recognised", "are not recognised"),
}


def disc_summary(titles: list[Title]) -> str:
    n = len(titles)
    parts = []
    for kind, (one, many) in _KIND_PHRASE.items():
        c = sum(1 for t in titles if (t.kind if t.kind in _KIND_PHRASE else "unknown") == kind)
        if c:
            parts.append(f"{c} {one if c == 1 else many}")
    head = f"Found {n} title{'' if n == 1 else 's'}."
    return f"{head} {', '.join(parts)}." if parts else head


KIND_LABEL = {"episode": "Episode", "feature": "Film", "play_all": "Play all", "extra": "Extra",
              "duplicate": "Duplicate", "short": "Short clip", "unknown": "Unknown"}
KIND_CHIP = {"episode": "warn", "feature": "warn", "extra": "info"}


def kind_text(t: Title) -> str:
    label = KIND_LABEL.get(t.kind, "Unknown")
    return f"{label} · skipped" if t.kind in ("play_all", "duplicate") and not t.selected else label


_VCODEC = {"mpegh": "HEVC", "hevc": "HEVC", "h265": "HEVC", "mpeg4": "AVC", "h264": "AVC", "avc": "AVC",
           "vc1": "VC-1", "vc-1": "VC-1", "mpeg2": "MPEG-2"}
_ACODEC = {"truehd": "Dolby TrueHD", "ac3": "AC-3", "eac3": "E-AC-3", "dtshd-ma": "DTS-HD MA", "dtshdma": "DTS-HD MA",
           "dts-hd ma": "DTS-HD MA", "dts": "DTS", "lpcm": "LPCM", "pcm": "LPCM", "flac": "FLAC", "aac": "AAC"}


def video_chip(stream) -> str:
    res = stream.resolution or ""
    m = re.search(r"x(\d+)", res)
    res_txt = f"{m.group(1)}p" if m else res
    codec = _VCODEC.get(stream.codec.lower(), stream.codec_long or stream.codec)
    return " · ".join(p for p in (res_txt, codec) if p) or "Video"


def _stream_words(s) -> str:
    return f"{s.flags} {s.name}".lower()


def stream_label(s) -> str:
    parts = [s.lang_name or s.lang.upper() or "Unknown language"]
    low = _stream_words(s)
    if "descri" in low:
        parts.append("audio description")
    if "commentary" in low:
        parts.append("commentary")
    if s.kind == "audio":
        codec = _ACODEC.get(s.codec.lower(), s.codec_long or s.codec)
        parts.append(f"{codec} {s.channels}".strip())
    else:
        if "sdh" in low:
            parts.append("SDH")
        if "forced" in low:
            parts.append("forced")
    return " · ".join(p for p in parts if p)


def stream_key(s) -> tuple:
    """Streams with the same key are the same track on every title of a disc."""
    return (s.kind, s.lang, s.codec.lower(), _stream_words(s).strip())


def stream_wanted(s, langs: list[str]) -> bool:
    low = _stream_words(s)
    if s.kind == "audio" and ("descri" in low or "commentary" in low):
        return False
    return s.lang in langs


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def job_name(job: Job, layout: str = "tv") -> str:
    if job.show:
        if layout == "movie":
            return job.show
        name = job.show
        if job.season is not None:
            name += f" S{job.season}"
        if job.disc_number:
            name += f" · Disc {job.disc_number}"
        return name
    return (job.disc_label or "Untitled disc").replace("_", " ").title()


def show_folder_of(job: Job) -> str:
    for f in job.files:
        if f.dest_rel:
            return f.dest_rel.split("/")[0]
    return ""


# ---------------------------------------------------------------------------
# Styling
# ---------------------------------------------------------------------------

def _write_indicator_images() -> str:
    d = tempfile.mkdtemp(prefix="disc-shelf-ui-")
    atexit.register(shutil.rmtree, d, True)
    svg = {
        "check_off": f'<rect x="0.75" y="0.75" width="16.5" height="16.5" rx="3.5" fill="{SIDE}" stroke="#7A828A" stroke-width="1.5"/>',
        "check_on": f'<rect x="0.75" y="0.75" width="16.5" height="16.5" rx="3.5" fill="{ACCENT}" stroke="{ACCENT}" stroke-width="1.5"/>'
                    f'<path d="M4.5 9.3l3 3 6-6.6" fill="none" stroke="{ON_ACCENT}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
        "check_off_dis": f'<rect x="0.75" y="0.75" width="16.5" height="16.5" rx="3.5" fill="{SIDE}" stroke="{FIELD}" stroke-width="1.5"/>',
        "check_on_dis": f'<rect x="0.75" y="0.75" width="16.5" height="16.5" rx="3.5" fill="#5A4A2E" stroke="#5A4A2E" stroke-width="1.5"/>'
                        f'<path d="M4.5 9.3l3 3 6-6.6" fill="none" stroke="#2A2116" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
        "radio_off": f'<circle cx="9" cy="9" r="8.2" fill="{SIDE}" stroke="#7A828A" stroke-width="1.5"/>',
        "radio_on": f'<circle cx="9" cy="9" r="8.2" fill="{SIDE}" stroke="{ACCENT}" stroke-width="1.5"/><circle cx="9" cy="9" r="4.2" fill="{ACCENT}"/>',
        "radio_off_dis": f'<circle cx="9" cy="9" r="8.2" fill="{SIDE}" stroke="{FIELD}" stroke-width="1.5"/>',
        "radio_on_dis": f'<circle cx="9" cy="9" r="8.2" fill="{SIDE}" stroke="#5A4A2E" stroke-width="1.5"/><circle cx="9" cy="9" r="4.2" fill="#5A4A2E"/>',
        "arrow": f'<path d="M2.5 4.5l3.5 3.5 3.5-3.5" fill="none" stroke="{MUTED}" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
    }
    for name, body in svg.items():
        size = 12 if name == "arrow" else 18
        with open(os.path.join(d, name + ".svg"), "w") as fh:
            fh.write(f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
                     f'viewBox="0 0 {size} {size}">{body}</svg>')
    return d


STYLE = """
QWidget { color: @TEXT; font-family: "@SANS"; font-size: 14px; }
QMainWindow, QWidget#page, QScrollArea, QWidget#scrollInner { background: @BG; }
QFrame#header { background: @SIDE; border: none; border-bottom: 1px solid @BORDER; }
QFrame#nav { background: @SIDE; border: none; border-right: 1px solid @BORDER; }
QFrame#card { background: @PANEL; border: 1px solid @BORDER; border-radius: 12px; }
QFrame#cardHead { background: transparent; border: none; border-bottom: 1px solid @BORDER; border-radius: 0; }
QFrame#rowLine { background: transparent; border: none; border-top: 1px solid @ROWLINE; border-radius: 0; }
QFrame#reasons { background: #1E1A12; border: 1px solid #4A3D22; border-radius: 8px; }
QFrame#note { background: @SIDE; border: none; border-radius: 8px; }
QFrame#needs { background: #1E1A12; border: 1px solid #6B5527; border-radius: 12px; }
QFrame#needsFail { background: #1F1416; border: 1px solid #6B2E2E; border-radius: 12px; }
QFrame#needsRow { background: transparent; border: none; border-top: 1px solid #3A3222; border-radius: 0; }
QFrame#doneRow { background: @PANEL; border: 1px solid @BORDER; border-radius: 10px; }
QToolButton#more { min-height: 30px; min-width: 34px; border-radius: 8px; border: 1px solid @FIELD; background: transparent; font-size: 16px; }
QToolButton#more::menu-indicator { image: none; }
QMenu { background: @PANEL; border: 1px solid @FIELD; padding: 4px; }
QMenu::item { padding: 7px 14px; border-radius: 6px; }
QMenu::item:selected { background: @RAISED; }
QFrame#encodeCard { background: #1B1726; border: 1px solid #4B3D6B; border-radius: 12px; }
QFrame#choiceCard { border-radius: 10px; border: 1px solid @FIELD; background: transparent; }
QFrame#choiceCard[checked="true"] { border-color: @ACCENT; background: #2A2419; }
QFrame#choiceCard:focus { border-color: @LINK; }
QPushButton#option { min-height: 30px; padding: 0 10px; border-radius: 8px; border: 1px solid @FIELD; background: transparent; font-size: 13px; }
QPushButton#option:checked { border-color: @ACCENT; background: #2A2419; }
QFrame#cardsBox { background: #15202B; border: 1px solid #2E4A63; border-radius: 12px; }
QFrame#footer { background: transparent; border: 1px solid @BORDER; border-radius: 10px; }
QFrame#divider { background: @BORDER; border: none; max-height: 1px; min-height: 1px; }
QLabel { background: transparent; border: none; }
QLabel[role="muted"] { color: @MUTED; }
QLabel[role="soft"] { color: @SOFT; }
QLabel[role="small"] { color: @MUTED; font-size: 13px; }
QLabel[role="field"] { color: @MUTED; font-size: 12px; }
QLabel[role="h1"] { font-size: 24px; font-weight: 700; }
QLabel[role="h2"] { font-size: 16px; font-weight: 600; }
QLabel[role="h3"] { font-size: 14px; font-weight: 600; }
QLabel[role="brand"] { font-size: 17px; font-weight: 700; }
QLabel[role="meta"] { color: @MUTED; font-size: 12px; font-family: "@MONO"; }
QLabel[role="big"] { font-size: 32px; font-weight: 700; font-family: "@MONO"; }
QLabel[role="warntext"] { color: #F5C27A; font-size: 13px; }
QLabel[role="reasonhead"] { color: #F5C27A; font-weight: 600; font-size: 13px; }
QLabel[role="reason"] { color: #E2D3B5; font-size: 13px; }
QLabel[mono="true"] { font-family: "@MONO"; }
QLabel[role="paths"] { font-family: "@MONO"; font-size: 13px; color: @SOFT; }
QLabel[role="tech"] { background: @ROWLINE; color: @TEXT; border-radius: 6px; padding: 4px 10px; font-size: 12px; font-family: "@MONO"; }
QLabel[role="hdr"] { background: #2B2416; color: #F5C27A; border-radius: 6px; padding: 4px 10px; font-size: 12px; font-family: "@MONO"; }
QLabel[role="nohdr"] { border: 1px dashed @FIELD; color: @MUTED; border-radius: 6px; padding: 3px 9px; font-size: 12px; font-family: "@MONO"; }
QLabel[role="pill"] { background: @PANEL; border: 1px solid @BORDER; border-radius: 15px; padding: 5px 12px; font-size: 13px; }
QLabel[step="done"] { border-top: 2px solid @ACCENT; color: @TEXT; padding-top: 4px; font-size: 11px; }
QLabel[step="now"] { border-top: 2px solid @TEXT; color: @TEXT; padding-top: 4px; font-size: 11px; font-weight: 600; }
QLabel[step="todo"] { border-top: 2px solid @FIELD; color: @MUTED; padding-top: 4px; font-size: 11px; }
QLabel[num="on"] { color: @ACCENT; font-family: "@MONO"; }
QLabel[num="off"] { color: @MUTED; font-family: "@MONO"; }
@CHIPS
QPushButton { min-height: 42px; padding: 0 16px; border-radius: 8px; border: 1px solid @FIELD; background: transparent; color: @TEXT; }
QPushButton:hover { background: @RAISED; }
QPushButton:pressed { background: #2A2F34; }
QPushButton:focus { border-color: @MUTED; }
QPushButton:disabled { color: #8A9198; border-color: @BORDER; }
QPushButton[primary="true"] { background: @ACCENT; color: @ON_ACCENT; border: none; font-weight: 600; padding: 0 20px; }
QPushButton[primary="true"]:hover { background: #F4B25A; }
QPushButton[primary="true"]:focus { border: 2px solid @TEXT; }
QPushButton[primary="true"]:disabled { background: #4A3D22; color: #C9B48E; }
QPushButton[small="true"] { min-height: 34px; padding: 0 12px; font-size: 13px; }
QPushButton#nav { text-align: left; border: none; border-radius: 8px; padding: 0 12px; min-height: 42px; background: transparent; }
QPushButton#nav:checked { background: @RAISED; }
QPushButton#nav:hover { background: #1D2125; }
QPushButton#nav:checked:hover { background: @RAISED; }
QLineEdit, QComboBox, QSpinBox { min-height: 42px; padding: 0 12px; border-radius: 8px; border: 1px solid @FIELD; background: @SIDE; color: @TEXT; selection-background-color: #6B5527; }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border-color: @ACCENT; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled { color: #8A9198; border-color: @BORDER; }
QLineEdit[mono="true"] { font-family: "@MONO"; font-size: 13px; }
QComboBox[warn="true"] { border-color: @ACCENT; }
QComboBox { combobox-popup: 0; }   /* a scrolling list of maxVisibleItems rows, not one as tall as the screen */
QComboBox::drop-down { border: none; width: 30px; }
QComboBox::down-arrow { image: url(@IMG/arrow.svg); width: 12px; height: 12px; }
QComboBox QAbstractItemView { background: @PANEL; border: 1px solid @FIELD; color: @TEXT; selection-background-color: #2B2416; selection-color: @TEXT; outline: none; padding: 4px; }
QSpinBox QLineEdit, QComboBox QLineEdit { min-height: 0; padding: 0; border: none; background: transparent; }
QSpinBox::up-button, QSpinBox::down-button { width: 0; border: none; subcontrol-origin: border; }
QCheckBox, QRadioButton { spacing: 10px; background: transparent; min-height: 28px; }
QCheckBox:disabled, QRadioButton:disabled { color: #8A9198; }
QCheckBox::indicator, QRadioButton::indicator, QTableView::indicator { width: 18px; height: 18px; }
QCheckBox::indicator:unchecked, QTableView::indicator:unchecked { image: url(@IMG/check_off.svg); }
QCheckBox::indicator:checked, QTableView::indicator:checked { image: url(@IMG/check_on.svg); }
QCheckBox::indicator:unchecked:disabled { image: url(@IMG/check_off_dis.svg); }
QCheckBox::indicator:checked:disabled { image: url(@IMG/check_on_dis.svg); }
QRadioButton::indicator:unchecked { image: url(@IMG/radio_off.svg); }
QRadioButton::indicator:checked { image: url(@IMG/radio_on.svg); }
QRadioButton::indicator:unchecked:disabled { image: url(@IMG/radio_off_dis.svg); }
QRadioButton::indicator:checked:disabled { image: url(@IMG/radio_on_dis.svg); }
QTableWidget { background: @PANEL; border: none; gridline-color: transparent; outline: none; }
QTableWidget::item { border-top: 1px solid @ROWLINE; padding-left: 8px; color: @TEXT; }
QTableWidget::item:focus { background: @RAISED; }
QHeaderView { background: @PANEL; border: none; }
QHeaderView::section { background: @PANEL; color: @MUTED; border: none; padding: 10px 8px; font-size: 12px; font-weight: 500; }
QProgressBar { background: #2A2F34; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; }
QProgressBar::chunk { background: @ACCENT; border-radius: 3px; }
QProgressBar[big="true"] { border-radius: 5px; max-height: 10px; min-height: 10px; }
QProgressBar[big="true"]::chunk { border-radius: 5px; }
QScrollBar:vertical { background: @BG; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: @FIELD; border-radius: 5px; min-height: 40px; margin: 2px; }
QScrollBar:horizontal { background: @BG; height: 12px; margin: 0; }
QScrollBar::handle:horizontal { background: @FIELD; border-radius: 5px; min-width: 40px; margin: 2px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
QToolTip { background: @RAISED; color: @TEXT; border: 1px solid @FIELD; padding: 6px; }
QMessageBox, QDialog { background: @PANEL; }
"""


def build_style(sans: str, mono: str, img_dir: str) -> str:
    chips = []
    for name, (bg, fg, border) in CHIPS.items():
        b = f"border: 1px solid {border};" if border else "border: none;"
        weight = "600" if border else "500"
        chips.append(f'QLabel[chip="{name}"] {{ background: {bg}; color: {fg}; {b} border-radius: 11px; '
                     f'padding: 3px 10px; font-size: 12px; font-weight: {weight}; }}')
    out = (STYLE + PICKER_STYLE).replace("@CHIPS", "\n".join(chips)).replace("@IMG", img_dir.replace("\\", "/"))
    for k, v in (("SANS", sans), ("MONO", mono), ("TEXT", TEXT), ("SOFT", SOFT), ("MUTED", MUTED), ("BG", BG),
                 ("SIDE", SIDE), ("PANEL", PANEL), ("RAISED", RAISED), ("BORDER", BORDER), ("FIELD", FIELD),
                 ("ROWLINE", ROWLINE), ("ON_ACCENT", ON_ACCENT), ("ACCENT", ACCENT)):
        out = out.replace("@" + k, v)
    return out


_FONTS: tuple[str, str] | None = None


def pick_fonts() -> tuple[str, str]:
    global _FONTS
    if _FONTS is None:
        fams = set(QFontDatabase.families())
        sans = "IBM Plex Sans" if "IBM Plex Sans" in fams else QApplication.font().family()
        mono = "IBM Plex Mono" if "IBM Plex Mono" in fams else \
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
        _FONTS = (sans, mono)
    return _FONTS


def apply_theme(app: QApplication) -> tuple[str, str]:
    app.setStyle("Fusion")
    pal = QPalette()
    for role, colour in ((QPalette.ColorRole.Window, BG), (QPalette.ColorRole.WindowText, TEXT),
                         (QPalette.ColorRole.Base, SIDE), (QPalette.ColorRole.AlternateBase, PANEL),
                         (QPalette.ColorRole.Text, TEXT), (QPalette.ColorRole.Button, PANEL),
                         (QPalette.ColorRole.ButtonText, TEXT), (QPalette.ColorRole.Highlight, ACCENT),
                         (QPalette.ColorRole.HighlightedText, ON_ACCENT), (QPalette.ColorRole.ToolTipBase, RAISED),
                         (QPalette.ColorRole.ToolTipText, TEXT), (QPalette.ColorRole.Link, LINK),
                         (QPalette.ColorRole.PlaceholderText, MUTED)):
        pal.setColor(role, QColor(colour))
    app.setPalette(pal)
    sans, mono = pick_fonts()
    app.setStyleSheet(build_style(sans, mono, _write_indicator_images()))
    return sans, mono


# ---------------------------------------------------------------------------
# Small widgets
# ---------------------------------------------------------------------------

def repolish(w: QWidget) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


def label(text: str = "", role: str | None = None, wrap: bool = False, mono: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    if mono:
        lab.setProperty("mono", "true")
    if wrap:
        lab.setWordWrap(True)
    return lab


def chip(text: str, kind: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("chip", kind)
    lab.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    return lab


def set_chip(lab: QLabel, text: str, kind: str) -> None:
    lab.setText(text)
    if lab.property("chip") != kind:
        lab.setProperty("chip", kind)
        repolish(lab)


def set_prop(w: QWidget, name: str, value) -> None:
    if w.property(name) != value:
        w.setProperty(name, value)
        repolish(w)


def button(text: str, primary: bool = False, small: bool = False) -> QPushButton:
    b = QPushButton(text)
    if primary:
        b.setProperty("primary", "true")
    if small:
        b.setProperty("small", "true")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def link(text: str, on_click) -> QLabel:
    lab = QLabel(f'<a href="#" style="color:{LINK}; text-decoration:none">{text}</a>')
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
    lab.linkActivated.connect(lambda _href: on_click())
    lab.setCursor(Qt.CursorShape.PointingHandCursor)
    return lab


def hbox(*items, spacing: int = 10, margins=(0, 0, 0, 0)) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QWidget):
            lay.addWidget(it)
        else:
            lay.addLayout(it)
    return lay


def vbox(*items, spacing: int = 10, margins=(0, 0, 0, 0)) -> QVBoxLayout:
    lay = QVBoxLayout()
    lay.setContentsMargins(*margins)
    lay.setSpacing(spacing)
    for it in items:
        if it is None:
            lay.addStretch(1)
        elif isinstance(it, int):
            lay.addSpacing(it)
        elif isinstance(it, QWidget):
            lay.addWidget(it)
        else:
            lay.addLayout(it)
    return lay


def clear_layout(lay) -> None:
    while lay.count():
        it = lay.takeAt(0)
        if it.widget():
            it.widget().hide()            # gone from view now, not when the event loop deletes it
            it.widget().deleteLater()
        elif it.layout():
            clear_layout(it.layout())


def field_spin() -> QSpinBox:
    """A number box the same height as the text fields beside it, its value inset like theirs.
    (A spin box's inner line edit ignores the stylesheet's padding, so the margin is set directly.)"""
    sb = QSpinBox()
    sb.lineEdit().setTextMargins(11, 0, 0, 0)
    probe = QLineEdit()
    probe.ensurePolished()
    sb.setFixedHeight(probe.sizeHint().height())
    return sb


def field(caption: str, widget: QWidget) -> QWidget:
    """A caption above a control, as the mockup's label-wrapped inputs."""
    w = QWidget()
    lay = vbox(label(caption, "field"), widget, spacing=6)
    w.setLayout(lay)
    return w


class SegmentBar(QWidget):
    """One small bar per title, each filled to its own fraction in its own colour."""

    def __init__(self, height: int = 8):
        super().__init__()
        self.segments: list[tuple[float, str, str]] = []     # (fraction, colour, tooltip)
        self.setFixedHeight(height)
        self.setMouseTracking(True)

    def set_segments(self, segments: list[tuple[float, str, str]]) -> None:
        self.segments = segments
        self.update()

    def _rects(self) -> list[QRectF]:
        n = len(self.segments)
        if not n:
            return []
        gap = 3.0
        w = (self.width() - gap * (n - 1)) / n
        return [QRectF(i * (w + gap), 0, w, self.height()) for i in range(n)]

    def paintEvent(self, _e):  # noqa: N802 -- Qt's name
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        for r, (frac, colour, _tip) in zip(self._rects(), self.segments):
            p.setBrush(QColor("#2A2F34"))
            p.drawRoundedRect(r, 2, 2)
            if frac > 0:
                p.setBrush(QColor(colour))
                p.drawRoundedRect(QRectF(r.x(), r.y(), r.width() * min(1.0, frac), r.height()), 2, 2)

    def event(self, e):
        if e.type() == QEvent.Type.ToolTip:
            for r, seg in zip(self._rects(), self.segments):
                if r.contains(QPointF(e.pos())):
                    QToolTip.showText(e.globalPos(), seg[2], self)
                    return True
            QToolTip.hideText()
            return True
        return super().event(e)


class ChoiceCard(QFrame):
    """A large radio choice: a title, a small mono line and a description, picked by clicking anywhere on it."""
    picked = Signal(str)

    def __init__(self, key: str, title: str, tech: str, desc: str):
        super().__init__()
        self.key = key
        self.setObjectName("choiceCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName(title)
        self.setAccessibleDescription(desc)
        self.setLayout(vbox(label(title, "h3"), label(tech, "field", mono=True), label(desc, "small", wrap=True),
                            spacing=4, margins=(14, 12, 14, 12)))
        # A word-wrapped label in a grid doesn't ask for its wrapped height, so leave room for 3 lines.
        self.setMinimumHeight(128)
        self.set_checked(False)

    def set_checked(self, on: bool) -> None:
        self.checked = on
        set_prop(self, "checked", "true" if on else "false")

    def mousePressEvent(self, e):  # noqa: N802 -- Qt's name
        self.picked.emit(self.key)

    def keyPressEvent(self, e):  # noqa: N802 -- Qt's name
        if e.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.picked.emit(self.key)
        else:
            super().keyPressEvent(e)


class Card(QFrame):
    def __init__(self, margins=(20, 16, 20, 16), spacing: int = 12):
        super().__init__()
        self.setObjectName("card")
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(*margins)
        self.lay.setSpacing(spacing)


class Option(QWidget):
    """A radio button or checkbox with a muted second line, which also toggles it."""

    def __init__(self, btn, title: str, desc: str = ""):
        super().__init__()
        self.btn = btn
        btn.setText(title)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.desc = label(desc, "small", wrap=True)
        self.desc.setContentsMargins(28, 0, 0, 0)
        self.desc.mousePressEvent = lambda _e: self.btn.isEnabled() and self.btn.click()
        lay = vbox(btn, spacing=0)
        if desc:
            lay.addWidget(self.desc)
        self.setLayout(lay)


def disc_pixmap(size: int = 88) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(FIELD), 1))
    p.setBrush(QColor(RAISED))
    p.drawEllipse(QRectF(0.5, 0.5, size - 1, size - 1))
    p.setBrush(QColor(BG))
    r = size / 8
    p.drawEllipse(QPointF(size / 2, size / 2), r, r)
    p.end()
    return pm


def cover_pixmap(path: str, w: int, h: int, radius: float = 8) -> QPixmap | None:
    """The image at path scaled to fit w x h (aspect kept) with rounded corners, or None if it can't be read."""
    src = QPixmap(path) if path else QPixmap()
    if src.isNull():
        return None
    ratio = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    scaled = src.scaled(int(w * ratio), int(h * ratio), Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(scaled.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, scaled.width(), scaled.height()), radius * ratio, radius * ratio)
    p.setClipPath(clip)
    p.drawPixmap(0, 0, scaled)
    p.end()
    out.setDevicePixelRatio(ratio)
    return out


class Cover(QWidget):
    """Cover art in a fixed box, the disc placeholder while it loads or when there is none,
    and "Cover from TVmaze" under it when the picture is a show poster rather than the disc's own."""

    def __init__(self, w: int, h: int, placeholder: int):
        super().__init__()
        self.w, self.h, self.placeholder = w, h, placeholder
        self.image = QLabel()
        self.image.setFixedSize(w, h)
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.caption = label("Cover from TVmaze", "field")
        self.caption.setStyleSheet("font-size: 11px;")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.caption.hide()
        lay = vbox(self.image, self.caption, spacing=4)
        lay.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setLayout(lay)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.has_cover = False
        self.source = ""
        self.set_cover("", "")

    def set_cover(self, path: str, source: str) -> None:
        pm = cover_pixmap(path, self.w, self.h) if path else None
        self.has_cover = pm is not None
        self.source = source if pm is not None else ""
        self.image.setPixmap(pm if pm is not None else disc_pixmap(self.placeholder))
        self.image.setAccessibleName("Disc cover" if pm is not None else "No cover for this disc")
        self.caption.setVisible(self.source == "TVmaze")


def logo_pixmap(size: int = 26) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    k = size / 24
    p.setPen(QPen(QColor(ACCENT), 1.6 * k))
    p.drawEllipse(QPointF(12 * k, 12 * k), 9.5 * k, 9.5 * k)
    p.drawEllipse(QPointF(12 * k, 12 * k), 2.5 * k, 2.5 * k)
    p.drawArc(QRectF(5.5 * k, 5.5 * k, 13 * k, 13 * k), 0, 90 * 16)
    p.end()
    return pm


class Worker(QThread):
    """Runs fn(progress) off the UI thread. progress may be called from any thread."""
    progressed = Signal(float, str)
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            result = self.fn(lambda f, text="": self.progressed.emit(float(f), str(text)))
        except Exception as exc:  # shown to the user, not a crash
            self.failed.emit(str(exc) or type(exc).__name__)
            return
        self.succeeded.emit(result)


class NoWheelComboBox(QComboBox):
    """A picker the mouse wheel never changes, so scrolling the page past a column of them
    can't change a choice by accident; the wheel scrolls the page instead. Clicking still
    opens the list, and the wheel works inside the open list."""

    def __init__(self, *args):
        super().__init__(*args)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)   # no focus from hovering a wheel over it

    def wheelEvent(self, e):  # noqa: N802 -- Qt's name
        e.ignore()


class NavButton(QPushButton):
    def __init__(self, text: str, number: str = ""):
        super().__init__()
        self.setObjectName("nav")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(text)
        self.num = label(number)
        self.num.setProperty("num", "off")
        self.text_label = label(text)
        lay = hbox(self.num if number else None, self.text_label, None, spacing=10, margins=(12, 0, 12, 0))
        if not number:
            lay.takeAt(0)
            lay.insertWidget(0, self.text_label)
        for lab in (self.num, self.text_label):
            lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setLayout(lay)
        self.toggled.connect(self._restyle)
        self._restyle(False)

    def _restyle(self, on: bool):
        set_prop(self.num, "num", "on" if on else "off")
        self.text_label.setStyleSheet(f"color: {TEXT}; font-weight: 600;" if on else f"color: {SOFT};")


class HdrStatus:
    """The RAW - No Conversion / Not Raw badge and the box listing why."""

    def __init__(self):
        self.badge = chip("RAW - No Conversion", "raw")
        self.badge.setAccessibleDescription("HDR status")
        self.box = QFrame()
        self.box.setObjectName("reasons")
        self.box_lay = vbox(label("Settings that change the HDR from the disc", "reasonhead"), spacing=6,
                            margins=(14, 10, 14, 10))
        self.box.setLayout(self.box_lay)
        self.box.hide()

    def update(self, policy: HdrPolicy) -> None:
        reasons = policy.raw_reasons()
        if reasons:
            set_chip(self.badge, "Not Raw", "notraw")
        else:
            set_chip(self.badge, "RAW - No Conversion", "raw")
        while self.box_lay.count() > 1:
            it = self.box_lay.takeAt(1)
            it.widget().deleteLater()
        for why in reasons:
            self.box_lay.addWidget(label(why, "reason", wrap=True))
        self.box.setVisible(bool(reasons))


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------

class ChartSeries:
    def __init__(self, name: str, color: str, ys: list[float], fill: bool = False, end_label: str = ""):
        self.name, self.color, self.ys, self.fill, self.end_label = name, color, list(ys), fill, end_label


class LineChart(QWidget):
    """A line chart drawn with QPainter: one y scale with round gridlines, time on x, an optional
    second row of x labels, a dashed reference line, end dots with labels that never overlap,
    a legend for two or more series, and a crosshair with a readout on hover or arrow keys.

    compact=True draws the small sparkline used on queue cards: no axes, the readout as a tooltip.
    """

    def __init__(self, compact: bool = False, height: int = 200, surface: str = PANEL):
        super().__init__()
        self.compact = compact
        self.surface = surface
        self.setMouseTracking(True)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus if compact else Qt.FocusPolicy.StrongFocus)
        self.xs: list[float] = []
        self.series: list[ChartSeries] = []
        self.unit = ""
        self.ref: tuple[float, str, str] | None = None
        self.readouts: list[str] = []
        self.x2: list[str] = []
        self.title = ""
        self.caption = ""
        self.legend: list[tuple[str, str, str]] = []      # (kind "line" | "dash", colour, text)
        self.live = True
        self.decimals = 0
        self.empty_text = "Waiting for the first readings."
        self.hover: int | None = None
        self._geo = (0.0, 1.0, 0.0, 1.0)
        self.placed_labels: list[QRectF] = []           # where the last paint put its labels, for tests

    def set_data(self, xs, series, unit: str = "", ref=None, readouts=None, x2=None, summary: str = "",
                 title: str = "", caption: str = "", legend=None, live: bool = True, decimals: int = 0):
        self.xs = [float(x) for x in xs]
        self.series = list(series)
        self.unit, self.ref, self.decimals, self.live = unit, ref, decimals, live
        self.readouts = list(readouts or [])
        self.x2 = list(x2 or [])
        self.title, self.caption = title, caption
        self.legend = list(legend or [])
        if self.hover is not None and self.hover >= len(self.xs):
            self.hover = None
        self.setAccessibleName(summary or title or "Chart")
        self.setAccessibleDescription(summary)
        self.update()

    # -- geometry -----------------------------------------------------------
    def _fonts(self):
        sans, mono = pick_fonts()
        f11 = QFont(mono)
        f11.setPixelSize(11)
        f12 = QFont(mono)
        f12.setPixelSize(12)
        f12.setWeight(QFont.Weight.DemiBold)
        head = QFont(sans)
        head.setPixelSize(13)
        head.setWeight(QFont.Weight.DemiBold)
        small = QFont(sans)
        small.setPixelSize(12)
        return f11, f12, head, small

    def _scale(self):
        top = max([max(s.ys) for s in self.series if s.ys] + [self.ref[0] if self.ref else 0.0, 0.0])
        if top <= 0:
            top = 1.0
        step = nice_step(top)
        ticks = [i * step for i in range(int(top / step + 1e-9) + 1)]
        if len(ticks) < 2:
            ticks.append(step)
        ceil_tick = ticks[-1] + step
        vtop = top * 1.15
        if ceil_tick <= vtop:
            ticks.append(ceil_tick)
        vtop = max(vtop, ticks[-1] * 1.04)
        return ticks, vtop, step

    def _tick_text(self, v: float, step: float, last: bool) -> str:
        txt = f"{v:.0f}" if step >= 1 else f"{v:.1f}"
        return f"{txt} {self.unit}" if last and self.unit else txt

    # -- painting -----------------------------------------------------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        f11, f12, head, small = self._fonts()
        W, H = self.width(), self.height()
        header = 0
        if not self.compact and (self.title or self.legend or self.caption):
            header = 26
            self._paint_header(p, W, head, small)
        ticks, vtop, step = self._scale()
        fm11 = QFontMetrics(f11)
        if self.compact:
            left, right, top, bottom = 4.0, W - 6.0, 6.0, H - 2.0
        else:
            lab_w = max(fm11.horizontalAdvance(self._tick_text(v, step, i == len(ticks) - 1))
                        for i, v in enumerate(ticks))
            rows = 2 if self.x2 else 1
            left, right = float(lab_w + 10), W - 8.0
            top, bottom = header + 8.0, H - (rows * 16 + 8.0)
        self._geo = (left, right, top, bottom)
        self.placed_labels = []

        def Y(v):
            return bottom - (v / vtop) * (bottom - top)

        n = len(self.xs)
        x0 = self.xs[0] if n else 0.0
        x1 = self.xs[-1] if n else 1.0

        def X(i):
            if n < 2 or x1 <= x0:
                return right
            return left + (self.xs[i] - x0) / (x1 - x0) * (right - left)

        # Grid
        if self.compact:
            p.setPen(QPen(QColor(BORDER), 1))
            p.drawLine(QPointF(0, bottom), QPointF(W, bottom))
        else:
            p.setFont(f11)
            for i, v in enumerate(ticks):
                y = Y(v)
                p.setPen(QPen(QColor(FIELD if v == 0 else ROWLINE), 1))
                p.drawLine(QPointF(left, y), QPointF(right, y))
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(0, y - 8, left - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                           self._tick_text(v, step, i == len(ticks) - 1))
        if not n:
            if not self.compact:
                p.setFont(small)
                p.setPen(QColor(MUTED))
                p.drawText(QRectF(left, top, right - left, bottom - top), Qt.AlignmentFlag.AlignCenter, self.empty_text)
            p.end()
            return

        self._lines = []
        for s in self.series:
            m = min(n, len(s.ys))
            self._lines.append(([X(i) for i in range(m)], [Y(s.ys[i]) for i in range(m)]))

        # Reference line, its label on whichever side of it the data leaves clear
        if self.ref and not self.compact:
            val, text, colour = self.ref
            y = Y(val)
            pen = QPen(QColor(colour), 1)
            pen.setDashPattern([4, 4])
            p.setPen(pen)
            p.drawLine(QPointF(left, y), QPointF(right, y))
            if text:
                p.setFont(small)
                fm = QFontMetrics(small)
                w, h = fm.horizontalAdvance(text), fm.height()
                cands = [QRectF(left + 6, y - 4 - h, w, h), QRectF(left + 6, y + 4, w, h),
                         QRectF(left + 6 + (right - left - w - 12) / 2, y - 4 - h, w, h),
                         QRectF(right - w - 6, y - 4 - h, w, h), QRectF(right - w - 6, y + 4, w, h)]
                r = next((c for c in cands if not self._hits_line(c)), cands[0])
                p.setPen(QColor(colour if colour in (TEXT, SOFT, MUTED) else SOFT))
                p.drawText(QPointF(r.left(), r.top() + fm.ascent()), text)
                self.placed_labels.append(r)

        # Areas, then lines
        for s in self.series:
            if not s.ys:
                continue
            path = QPainterPath(QPointF(X(0), Y(s.ys[0])))
            for i in range(1, min(n, len(s.ys))):
                path.lineTo(QPointF(X(i), Y(s.ys[i])))
            if s.fill:
                area = QPainterPath(path)
                area.lineTo(QPointF(X(min(n, len(s.ys)) - 1), bottom))
                area.lineTo(QPointF(X(0), bottom))
                area.closeSubpath()
                c = QColor(s.color)
                c.setAlphaF(0.13)
                p.fillPath(area, c)
            pen = QPen(QColor(s.color), 2)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        # End dots and their labels
        ends = [(s, X(min(n, len(s.ys)) - 1), Y(s.ys[min(n, len(s.ys)) - 1])) for s in self.series if s.ys]
        for s, ex, ey in ends:
            self._dot(p, ex, ey, s.color)
        if not self.compact:
            self._paint_end_labels(p, ends, f12, left, right, top, bottom)
            self._paint_x_labels(p, f11, X, left, right, bottom)

        # Crosshair and readout
        if self.hover is not None and 0 <= self.hover < n:
            i = self.hover
            hx = X(i)
            p.setPen(QPen(QColor(SOFT), 1))
            p.drawLine(QPointF(hx, top if not self.compact else 0), QPointF(hx, bottom))
            for s in self.series:
                if i < len(s.ys):
                    self._dot(p, hx, Y(s.ys[i]), s.color)
            if not self.compact and i < len(self.readouts):
                self._paint_tooltip(p, small, self.readouts[i], hx, top, left, right)
        p.end()

    def _dot(self, p: QPainter, x: float, y: float, colour: str):
        p.setPen(QPen(QColor(self.surface), 2))
        p.setBrush(QColor(colour))
        p.drawEllipse(QPointF(x, y), 4.5, 4.5)

    def _paint_header(self, p: QPainter, W: int, head: QFont, small: QFont):
        p.setFont(head)
        p.setPen(QColor(TEXT))
        fmh = QFontMetrics(head)
        p.drawText(QPointF(0, 16), self.title)
        x_end = W - 2.0
        title_w = fmh.horizontalAdvance(self.title) + 16
        p.setFont(small)
        fm = QFontMetrics(small)
        if self.legend:
            for kind, colour, text in reversed(self.legend):
                tw = fm.horizontalAdvance(text)
                p.setPen(QColor(SOFT))
                p.drawText(QPointF(x_end - tw, 16), text)
                kx = x_end - tw - 6 - 14
                pen = QPen(QColor(colour), 3 if kind == "line" else 1)
                if kind == "dash":
                    pen.setDashPattern([3, 3])
                else:
                    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                p.setPen(pen)
                p.drawLine(QPointF(kx, 12), QPointF(kx + 14, 12))
                x_end = kx - 16
        elif self.caption:
            avail = int(W - title_w)
            text = fm.elidedText(self.caption, Qt.TextElideMode.ElideRight, max(0, avail))
            p.setPen(QColor(MUTED))
            p.drawText(QPointF(W - 2 - fm.horizontalAdvance(text), 16), text)

    def _paint_end_labels(self, p, ends, font, left, right, top, bottom):
        p.setFont(font)
        fm = QFontMetrics(font)
        order = sorted(range(len(ends)), key=lambda k: ends[k][2])     # highest point first
        for rank, k in enumerate(order):
            s, ex, ey = ends[k]
            if not s.end_label:
                continue
            w, h = fm.horizontalAdvance(s.end_label), fm.height()
            rx = max(left + 2, ex - 10 - w)

            def at(dy):
                return QRectF(rx, ey + dy - fm.ascent(), w, h)

            above = [at(-10), at(-24), at(-38)]
            below = [at(18), at(32), at(46)]
            at_top = QRectF(rx, top + 2, w, h)
            if len(ends) == 1:
                cands = [at_top] + above + below
            elif rank == 0:
                cands = above + [at_top] + below
            else:
                cands = below + above
            inside = [r for r in cands if r.top() >= top - 2 and r.bottom() <= bottom - 2
                      and not any(r.adjusted(-2, -2, 2, 2).intersects(o) for o in self.placed_labels)]
            r = next((r for r in inside if not self._hits_line(r)), None)
            if r is None:
                continue        # no clear place: the legend and the readout carry this value
            p.setPen(QColor(TEXT))
            p.drawText(QPointF(r.left(), r.top() + fm.ascent()), s.end_label)
            self.placed_labels.append(r)

    def _hits_line(self, r: QRectF) -> bool:
        """True when any line passes through the rectangle (with a few pixels to spare)."""
        box = r.adjusted(-3, -3, 3, 3)
        for xs, ys in getattr(self, "_lines", []):
            if not xs:
                continue
            x = box.left()
            while x <= box.right():
                i = bisect.bisect_left(xs, x)
                if 0 < i < len(xs):
                    x_a, x_b = xs[i - 1], xs[i]
                    t = (x - x_a) / (x_b - x_a) if x_b > x_a else 0.0
                    y = ys[i - 1] + t * (ys[i] - ys[i - 1])
                    if box.top() <= y <= box.bottom():
                        return True
                    # a steep segment can cross the box between two probe points
                    if min(ys[i - 1], ys[i]) <= box.bottom() and max(ys[i - 1], ys[i]) >= box.top() \
                            and box.left() <= x_b <= box.right():
                        return True
                elif i == 0 and len(xs) and box.left() <= xs[0] <= box.right() and box.top() <= ys[0] <= box.bottom():
                    return True
                x += 3
        return False

    def _paint_x_labels(self, p, font, X, left, right, bottom):
        n = len(self.xs)
        p.setFont(font)
        fm = QFontMetrics(font)
        idx = sorted({0, n // 3, (2 * n) // 3, n - 1}) if n > 3 else list(range(n))
        placed: list[QRectF] = []
        rows = []
        for j, i in enumerate(idx):
            last = i == n - 1
            text = "now" if last and self.live else fmt_elapsed(self.xs[i])
            second = self.x2[i] if i < len(self.x2) else ""
            w = max(fm.horizontalAdvance(text), fm.horizontalAdvance(second))
            x = X(i)
            if i == 0:
                r = QRectF(x, bottom + 4, w, 30)
            elif last:
                r = QRectF(x - w, bottom + 4, w, 30)
            else:
                r = QRectF(x - w / 2, bottom + 4, w, 30)
            r.moveLeft(max(0.0, min(r.left(), self.width() - w - 1)))
            if any(r.adjusted(-8, 0, 8, 0).intersects(o) for o in placed):
                if not last:
                    continue
                # "now" always shows; drop the label it would overlap instead.
                placed = [o for o in placed if not r.adjusted(-8, 0, 8, 0).intersects(o)]
                rows = [row for row in rows if not r.adjusted(-8, 0, 8, 0).intersects(row[0])]
            placed.append(r)
            rows.append((r, text, second))
        for r, text, second in rows:
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(r.left(), r.top(), r.width(), 14), Qt.AlignmentFlag.AlignCenter, text)
            if second:
                p.setPen(QColor(SOFT))
                p.drawText(QRectF(r.left(), r.top() + 16, r.width(), 14), Qt.AlignmentFlag.AlignCenter, second)

    def _paint_tooltip(self, p, font, text, hx, top, left, right):
        p.setFont(font)
        fm = QFontMetrics(font)
        lines = text.split("\n")
        w = max(fm.horizontalAdvance(t) for t in lines) + 20
        h = fm.height() * len(lines) + 12
        x = hx + 12
        if x + w > self.width() - 2:
            x = hx - 12 - w
        x = max(0.0, x)
        r = QRectF(x, top + 2, w, h)
        p.setPen(QPen(QColor(FIELD), 1))
        p.setBrush(QColor(RAISED))
        p.drawRoundedRect(r, 6, 6)
        p.setPen(QColor(TEXT))
        for k, t in enumerate(lines):
            p.drawText(QPointF(r.left() + 10, r.top() + 6 + fm.ascent() + k * fm.height()), t)

    # -- interaction --------------------------------------------------------
    def index_at(self, x: float) -> int | None:
        if not self.xs:
            return None
        left, right, _t, _b = self._geo
        if len(self.xs) < 2 or right <= left:
            return len(self.xs) - 1
        frac = min(1.0, max(0.0, (x - left) / (right - left)))
        target = self.xs[0] + frac * (self.xs[-1] - self.xs[0])
        i = bisect.bisect_left(self.xs, target)
        if i >= len(self.xs):
            return len(self.xs) - 1
        if i > 0 and target - self.xs[i - 1] < self.xs[i] - target:
            return i - 1
        return i

    def mouseMoveEvent(self, ev):
        i = self.index_at(ev.position().x())
        if i != self.hover:
            self.hover = i
            self.update()
        if self.compact and i is not None and i < len(self.readouts):
            QToolTip.showText(ev.globalPosition().toPoint(), self.readouts[i], self)

    def leaveEvent(self, _ev):
        self.hover = None
        self.update()

    def focusOutEvent(self, ev):
        self.hover = None
        self.update()
        super().focusOutEvent(ev)

    def keyPressEvent(self, ev):
        if not self.xs or ev.key() not in (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Home, Qt.Key.Key_End):
            super().keyPressEvent(ev)
            return
        n = len(self.xs)
        i = self.hover if self.hover is not None else n - 1
        i = {Qt.Key.Key_Left: i - 1, Qt.Key.Key_Right: i + 1, Qt.Key.Key_Home: 0, Qt.Key.Key_End: n - 1}[ev.key()]
        self.hover = max(0, min(n - 1, i))
        self.update()


def colour_bar(colour: str, height: int = 4) -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 1000)
    bar.setTextVisible(False)
    set_bar_colour(bar, colour, height)
    return bar


def set_bar_colour(bar: QProgressBar, colour: str, height: int = 4) -> None:
    key = (colour, height)
    if bar.property("colour_key") == str(key):
        return
    bar.setProperty("colour_key", str(key))
    r = height // 2
    bar.setStyleSheet(f"QProgressBar {{ background: {TRACK}; border: none; border-radius: {r}px; "
                      f"max-height: {height}px; min-height: {height}px; }} "
                      f"QProgressBar::chunk {{ background: {colour}; border-radius: {r}px; }}")



class QueueCard(Card):
    def __init__(self, win: "MainWindow", job: Job):
        super().__init__(margins=(16, 16, 16, 16), spacing=10)
        self.win, self.job = win, job
        self.name = label("")
        self.name.setStyleSheet("font-weight: 600;")
        self.name.setWordWrap(True)
        self.eta = label("", "meta")
        top = hbox(self.name, self.eta, spacing=8)
        top.setStretch(0, 1)
        top.setAlignment(self.eta, Qt.AlignmentFlag.AlignTop)
        self.lay.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.lay.addWidget(self.bar)

        # Ripping and copying at once: one bar each.
        self.dual = QWidget()
        dl = vbox(spacing=8)
        self.rip_text, self.rip_rate, self.rip_bar = self._bar_row(dl, RIP_TEXT, ACCENT)
        self.copy_text, self.copy_rate, self.copy_bar = self._bar_row(dl, COPY_TEXT, COPY_BAR)
        self.dual.setLayout(dl)
        self.dual.hide()
        self.lay.addWidget(self.dual)

        # Rip speed since the rip started.
        self.graph_box = QFrame()
        self.graph_box.setObjectName("note")
        gl = vbox(spacing=6, margins=(12, 10, 12, 10))
        self.speed_now = label("", mono=True)
        self.speed_now.setStyleSheet("font-size: 15px; font-weight: 500;")
        self.speed_sum = label("", "field", wrap=True)
        gl.addWidget(self.speed_now)
        gl.addWidget(self.speed_sum)
        self.graph = LineChart(compact=True, height=56, surface=SIDE)
        gl.addWidget(self.graph)
        self.graph_start = label("", "field")
        self.graph_start.setStyleSheet("font-size: 11px;")
        now = label("now", "field")
        now.setStyleSheet("font-size: 11px;")
        gl.addLayout(hbox(self.graph_start, None, now))
        self.graph_box.setLayout(gl)
        self.graph_box.hide()
        self.lay.addWidget(self.graph_box)

        steps = QGridLayout()
        steps.setHorizontalSpacing(4)
        self.steps = []
        for i, name in enumerate(STAGE_NAMES):
            lab = label(name)
            lab.setProperty("step", "todo")
            steps.addWidget(lab, 0, i)
            steps.setColumnStretch(i, 1)
            self.steps.append(lab)
        self.lay.addLayout(steps)
        self.status = label("", "soft", wrap=True)
        self.status.setStyleSheet("font-size: 13px;")
        self.lay.addWidget(self.status)
        self.action = button("", small=True)
        self.action.clicked.connect(self._act)
        self.more = QToolButton()
        self.more.setText("⋯")
        self.more.setObjectName("more")
        self.more.setAccessibleName("More actions")
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.more.setMenu(QMenu(self.more))
        self.more.menu().aboutToShow.connect(self.fill_menu)
        self.lay.addLayout(hbox(None, self.more, self.action, spacing=8))
        self.refresh()

    def fill_menu(self):
        """The rarer actions, built when the menu opens so they fit the disc's state."""
        m, job, win = self.more.menu(), self.job, self.win
        m.clear()
        ctl = win.ctl
        on_nas = any(f.state == "verified" for f in job.files)
        m.addAction("Open matches", lambda: win.open_match(job))
        if hasattr(ctl, "nas_folder") and on_nas:
            m.addAction("Show the files on the NAS", lambda: win.open_folder(ctl.nas_folder(job)))
        if hasattr(ctl, "reverify") and on_nas:
            m.addAction("Check the NAS copies again", lambda: win.reverify(job))
        if hasattr(ctl, "free_local") and on_nas:
            m.addAction("Delete the local rip of titles on the NAS", lambda: win.queue.free_local(job))
        if win.is_running(job):
            m.addSeparator()
            m.addAction("Stop rip" if win.is_ripping(job) else "Stop copy", lambda: win.stop(job))

    @staticmethod
    def _bar_row(lay, text_colour: str, bar_colour: str):
        text = label("")
        text.setStyleSheet(f"color: {text_colour}; font-size: 12px;")
        rate = label("", mono=True)
        rate.setStyleSheet(f"color: {SOFT}; font-size: 12px;")
        bar = colour_bar(bar_colour, 6)
        lay.addLayout(vbox(hbox(text, None, rate, spacing=8), bar, spacing=4))
        return text, rate, bar

    def refresh(self) -> None:
        job, win = self.job, self.win
        self.name.setText(job_name(job, win.layout_of(job)))
        self.eta.setText(win.eta_text(job))
        ripping = win.is_ripping(job)
        overlap = ripping and job.copy_mode in OVERLAP
        tp = win.progress_of(job) if ripping else []
        idx = 4 if job.stage == "done" else (STAGES.index(job.stage) if job.stage in STAGES else -1)
        waiting = [p for p in tp if p.state == "waiting_match"]
        for i, lab in enumerate(self.steps):
            if overlap:
                state = ("now", "now" if waiting else "done", "now", "todo")[i]
            else:
                state = "done" if i < idx else ("now" if i == idx else "todo")
            set_prop(lab, "step", state)
        running = win.is_running(job)
        if job.stage == "done":
            frac = 1.0
        elif job.stage in ("rip", "copy") and (running or job.progress):
            frac = job.progress
        elif job.stage == "match":
            frac = 0.5
        elif job.stage == "plex":
            frac = 0.9
        else:
            frac = job.progress
        self.bar.setValue(int(max(0.0, min(1.0, frac)) * 1000))
        self.bar.setVisible(not overlap)
        self.dual.setVisible(overlap)
        if overlap:
            self.fill_bars(tp)
        self.status.setText(self.status_text(job, tp, ripping, overlap))
        self.refresh_graph()
        act = ""
        if waiting or job.stage == "match":
            act = "Match episodes"
        elif overlap:
            act = "Show copy"
        elif job.stage == "copy":
            act = "Show copy" if running else "Resume copy"
        elif job.stage == "plex":
            act = "Show copy"
        self.action.setText(act)
        self.action.setVisible(bool(act))
        self.action.setEnabled(not (act == "Resume copy" and win.busy("copy")))

    def fill_bars(self, tp: list[TitleProgress]):
        live = [p for p in tp if p.state != "skipped"]
        total = sum(p.total_bytes for p in live) or 1
        cur = next((k for k, p in enumerate(live) if p.state == "ripping"), None)
        if cur is not None:
            self.rip_text.setText(f"Rip · title {cur + 1} of {len(live)}")
        else:
            left = sum(1 for p in live if p.state == "waiting")
            self.rip_text.setText(f"Rip · {plural(left, 'title')} to go" if left else "Rip · done")
        rate = self.win.last_rate(self.job, "rip") if cur is not None else 0
        self.rip_rate.setText(f"{fmt_rate(rate)}" if rate else "")
        self.rip_bar.setValue(int(sum(p.ripped_bytes for p in live) / total * 1000))
        on_nas = sum(1 for p in live if p.state == "verified")
        text = f"Copy · {on_nas} of {len(live)} on the NAS"
        copying = next((k for k, p in enumerate(live) if p.state in ("copying", "checking")
                        or (p.state == "ripping" and p.copied_bytes)), None)
        if copying is not None:
            p = live[copying]
            pct = int(p.copied_bytes / (p.total_bytes or 1) * 100)
            text += f", title {copying + 1} at {pct}%"
        self.copy_text.setText(text)
        crate = live[copying].copy_rate if copying is not None else 0
        self.copy_rate.setText(fmt_rate(crate, 0) if crate else "")
        self.copy_bar.setValue(int(sum(p.copied_bytes for p in live) / total * 1000))

    def status_text(self, job: Job, tp: list[TitleProgress], ripping: bool, overlap: bool) -> str:
        if overlap:
            parts = ["Ripping and copying at the same time."]
            by = {m.title_index: m for m in job.matches}
            for p in tp:
                if p.state == "waiting_match":
                    m = by.get(p.title_index)
                    if m and m.target == "episode" and m.episode:
                        parts.append(f"Episode {m.episode.number} waits for you to check its match.")
                    else:
                        parts.append(f"Title {p.title_index} waits for you to check its match.")
                elif p.state == "failed":
                    parts.append(p.note or f"Title {p.title_index} failed to copy.")
            return " ".join(parts)
        text = job.status or self.win.default_status(job)
        if ripping and "MB/s" not in text:
            rate = self.win.last_rate(job, "rip")
            if rate:
                text = f"{text.rstrip('.')} · {fmt_rate(rate)}"
        return text

    def refresh_graph(self):
        job, win = self.job, self.win
        rips = win.samples.get(job.id, {}).get("rip", [])
        show = win.is_ripping(job) and len(rips) >= 2
        self.graph_box.setVisible(show)
        if not show:
            return
        xs = [s.t for s in rips]
        mbs = [s.rate / 1e6 for s in rips]
        last = rips[-1]
        avg = sum(s.rate for s in rips) / len(rips)
        peak = max(s.rate for s in rips)
        span = xs[-1] - xs[0]
        self.speed_now.setText(f"{fmt_rate(last.rate)} · {last.rate / BLURAY_1X:.1f}×")
        self.speed_sum.setText(f"{fmt_gb(last.total)} ripped · avg {fmt_rate(avg)} · peak {fmt_rate(peak)}")
        mins = max(1, round(span / 60))
        self.graph_start.setText(f"{mins} min ago")
        readouts = [f"{fmt_ago(xs[-1] - s.t)}: {fmt_rate(s.rate)} ({s.rate / BLURAY_1X:.1f}×) · "
                    f"{fmt_gb(s.total)} ripped" for s in rips]
        low = min(rips, key=lambda s: s.rate)
        dip = ""
        if low.rate < 0.75 * avg:
            dip = f", with a dip about {max(1, round((xs[-1] - low.t) / 60))} minutes ago"
        summary = (f"Rip speed since the rip started {mins} minutes ago, averaging {fmt_rate(avg)}{dip}. "
                   f"{fmt_gb(last.total)} ripped so far.")
        self.graph.set_data(xs, [ChartSeries("Rip speed", ACCENT, mbs, fill=True)], unit="MB/s",
                            readouts=readouts, summary=summary)

    def _act(self):
        text = self.action.text()
        if text == "Match episodes":
            self.win.open_match(self.job)
        elif text == "Resume copy" and not self.win.is_running(self.job):
            self.win.start_copy(self.job)
        else:
            self.win.open_copy(self.job)


def scroll_page(inner: QWidget) -> QScrollArea:
    inner.setObjectName("scrollInner")
    sa = QScrollArea()
    sa.setWidgetResizable(True)
    sa.setFrameShape(QFrame.Shape.NoFrame)
    sa.setWidget(inner)
    sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return sa


def plain_table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().hide()
    t.setShowGrid(False)
    t.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
    t.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    hh = t.horizontalHeader()
    hh.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    hh.setHighlightSections(False)
    hh.setSectionsClickable(False)
    return t


def fit_table(t: QTableWidget) -> None:
    """Size a table to its visible rows, so the page scrolls rather than the table."""
    h = t.horizontalHeader().sizeHint().height()
    for r in range(t.rowCount()):
        if not t.isRowHidden(r):
            h += t.rowHeight(r)
    t.setFixedHeight(h + 4)


def cell(*widgets, align_left: bool = True, margins=(8, 0, 8, 0)) -> QWidget:
    w = QWidget()
    lay = hbox(*widgets, spacing=8, margins=margins)
    if align_left:
        lay.addStretch(1)
    w.setLayout(lay)
    return w


def item(text: str, mono_font: QFont | None = None, muted: bool = False) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    it.setFlags(Qt.ItemFlag.ItemIsEnabled)
    if mono_font is not None:
        it.setFont(mono_font)
    if muted:
        it.setForeground(QColor(MUTED))
    return it


# ---------------------------------------------------------------------------
# Rip page
# ---------------------------------------------------------------------------

class RipPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.job: Job | None = None
        self.disc: Disc | None = None
        self.scratch_policy = _copy.deepcopy(win.cfg.hdr)
        self.stream_boxes: list[tuple[QCheckBox, object]] = []
        self.queue_cards: dict[str, QueueCard] = {}

        inner = QWidget()
        outer = hbox(spacing=20, margins=(24, 24, 24, 24))
        inner.setLayout(outer)
        col = vbox(spacing=20)
        outer.addLayout(col, 1)

        # Drive picker
        self.drive_combo = QComboBox()
        self.drive_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.drive_combo.setMinimumContentsLength(28)
        self.drive_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.drive_combo.setAccessibleName("Drive")
        self.drive_combo.currentIndexChanged.connect(lambda _i: win.update_header())
        self.refresh_btn = button("Refresh drives")
        self.refresh_btn.clicked.connect(win.refresh_drives)
        self.scan_btn = button("Scan again")
        self.scan_btn.clicked.connect(self.scan)
        col.addLayout(hbox(label("Drive", "muted"), self.drive_combo, self.refresh_btn, self.scan_btn))

        # Disc card
        disc = Card(margins=(20, 20, 20, 20))
        self.cover = Cover(96, 128, 88)
        icon = self.cover
        self.disc_meta = label("", "meta")
        self.disc_title = label("No disc scanned yet", "h1", wrap=True)
        self.disc_sum = label("Put a disc in the drive. It's read as soon as it's in.", "muted", wrap=True)
        self.match_link = link("Match episodes while it rips", self.match_clicked)
        self.match_link.setStyleSheet("font-size: 13px;")
        self.match_link.setAccessibleName("Match episodes while it rips")
        texts = vbox(self.disc_meta, self.disc_title, self.disc_sum, self.match_link, spacing=6)
        self.eject_btn = button("Eject")
        self.eject_btn.clicked.connect(self.eject)
        self.rip_btn = button("Rip 0 titles", primary=True)
        self.rip_btn.clicked.connect(self.rip_clicked)
        row = hbox(icon, texts, spacing=20)
        row.setStretch(1, 1)
        row.setAlignment(icon, Qt.AlignmentFlag.AlignTop)
        row.addLayout(hbox(self.eject_btn, self.rip_btn))
        disc.lay.addLayout(row)
        # Why a film will be copied after the rip rather than while it rips.
        self.stream_note = label("", "warntext", wrap=True)
        self.stream_note.setContentsMargins(116, 0, 0, 0)
        self.stream_note.hide()
        disc.lay.addWidget(self.stream_note)
        col.addWidget(disc)

        # Titles card
        tcard = Card(margins=(0, 0, 0, 0), spacing=0)
        head = QFrame()
        head.setObjectName("cardHead")
        self.hide_short = QCheckBox(f"Hide clips under {SHORT_S // 60} min")
        self.hide_short.setChecked(True)
        self.hide_short.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        self.hide_short.toggled.connect(self.apply_hide)
        head.setLayout(hbox(label("Titles on this disc", "h2"), None, self.hide_short, margins=(20, 12, 20, 12)))
        tcard.lay.addWidget(head)
        self.table = plain_table(["Rip", "Title", "Length", "Size", "Chapters", "Looks like"])
        hh = self.table.horizontalHeader()
        for c, w in enumerate((64, 170, 110, 110, 100)):
            self.table.setColumnWidth(c, w)
        hh.setStretchLastSection(True)
        self.table.itemChanged.connect(self.title_toggled)
        self.table_empty = label("The titles appear here once the disc is scanned.", "muted")
        self.table_empty.setContentsMargins(20, 16, 20, 16)
        tcard.lay.addWidget(self.table)
        tcard.lay.addWidget(self.table_empty)
        col.addWidget(tcard)

        # Video
        video = Card(spacing=14)
        video.lay.addWidget(label("Video", "h2"))
        self.chip_row = hbox(spacing=8)
        self.hdr = HdrStatus()
        vrow = hbox(label("On this disc", "small"), 4, self.chip_row, None, self.hdr.badge, spacing=8)
        video.lay.addLayout(vrow)
        video.lay.addWidget(self.hdr.box)
        self.keep_all = QRadioButton()
        self.keep_hdr10 = QRadioButton()
        grp = QButtonGroup(self)
        grp.addButton(self.keep_all)
        grp.addButton(self.keep_hdr10)
        video.lay.addWidget(Option(self.keep_all, "Keep HDR10 and Dolby Vision in one file",
                                   "Players without Dolby Vision play the HDR10 version instead. Screens without HDR "
                                   "get a converted picture from Plex."))
        video.lay.addWidget(Option(self.keep_hdr10, "Keep HDR10 only", "Drops the Dolby Vision layer."))
        line = QFrame()
        line.setObjectName("rowLine")
        line.setFixedHeight(1)
        video.lay.addWidget(line)
        self.convert = QCheckBox()
        video.lay.addWidget(Option(self.convert, "Convert Dolby Vision to profile 8.1",
                                   "For streaming boxes that play profile 8.1 but not disc profile 7. No re-encoding, "
                                   "but a full enhancement layer is dropped. Default set in Settings."))
        self.no_uhd = label("This disc has no 4K video, so the HDR choices don't apply to it.", "small", wrap=True)
        self.no_uhd.hide()
        video.lay.addWidget(self.no_uhd)
        # A smaller AV1 second version (4K discs only); the remux above stays RAW.
        self.av1_combo = QComboBox()
        self.av1_combo.setMinimumWidth(300)
        self.av1_combo.activated.connect(self.av1_picked)
        self.av1_row = QWidget()
        al = hbox(label("The remux stays exactly as on the disc. The AV1 version is a separate re-encode and doesn't "
                        "change the badge.", "small", wrap=True), field("AV1 version for this disc", self.av1_combo),
                  spacing=16)
        al.setStretch(0, 1)
        self.av1_row.setLayout(al)
        self.av1_row.hide()
        video.lay.addWidget(self.av1_row)
        self.keep_all.toggled.connect(self.policy_changed)
        self.convert.toggled.connect(self.policy_changed)
        note = QFrame()
        note.setObjectName("note")
        q = label("Does this set include a 1080p Blu-ray? Rip it next and it's saved as a second version of the same "
                  "episodes.", "soft", wrap=True)
        q.setStyleSheet("font-size: 13px;")
        note.setLayout(hbox(q, link("Second-version settings", lambda: win.show_page("settings")),
                            spacing=16, margins=(14, 12, 14, 12)))
        note.layout().setStretch(0, 1)
        video.lay.addWidget(note)
        col.addWidget(video)

        # Audio, subtitles, destination
        grid = QGridLayout()
        grid.setSpacing(20)
        self.audio_card = Card(spacing=8)
        self.audio_card.lay.addWidget(label("Audio", "h2"))
        self.audio_lay = vbox(spacing=4)
        self.audio_card.lay.addLayout(self.audio_lay)
        self.audio_card.lay.addStretch(1)
        self.subs_card = Card(spacing=8)
        self.subs_card.lay.addWidget(label("Subtitles", "h2"))
        self.subs_lay = vbox(spacing=4)
        self.subs_card.lay.addLayout(self.subs_lay)
        self.subs_card.lay.addStretch(1)
        dest = Card(spacing=10)
        dest.lay.addWidget(label("Copy to", "h2"))
        self.kind_combo = QComboBox()
        self.kind_combo.setAccessibleName("Media type")
        self.kind_combo.activated.connect(self.kind_changed)
        self.dest_combo = QComboBox()
        self.dest_combo.setAccessibleName("Copy to")
        self.dest_combo.activated.connect(self.dest_changed)
        for c in (self.kind_combo, self.dest_combo):
            c.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            c.setMinimumContentsLength(10)
        dest.lay.addWidget(field("Media type", self.kind_combo))
        dest.lay.addWidget(field("NAS folder", self.dest_combo))
        dest.lay.addStretch(1)
        grid.addWidget(self.audio_card, 0, 0)
        grid.addWidget(self.subs_card, 0, 1)
        grid.addWidget(dest, 0, 2)
        for c in range(3):
            grid.setColumnStretch(c, 1)
        col.addLayout(grid)
        col.addWidget(label("Video is copied as-is, with no re-encoding. Plex converts it on the NAS when a device "
                            "needs it.", "small", wrap=True))
        col.addStretch(1)

        # Queue column
        side = QWidget()
        side.setFixedWidth(320)
        self.queue_lay = vbox(spacing=12)
        side_lay = vbox(label("Queue", "h2"), self.queue_lay, None, spacing=12)
        side.setLayout(side_lay)
        self.queue_empty = label("Nothing is queued. Scan a disc to start.", "muted", wrap=True)
        self.queue_lay.addWidget(self.queue_empty)
        outer.addWidget(side, 0, Qt.AlignmentFlag.AlignTop)

        page = vbox()
        page.addWidget(scroll_page(inner))
        self.setLayout(page)
        self.fill_destinations()
        self.load(None, None)

    # -- state --------------------------------------------------------------
    @property
    def policy(self) -> HdrPolicy:
        return self.job.hdr_policy if self.job else self.scratch_policy

    def fill_destinations(self):
        cfg = self.win.cfg
        self.kind_combo.clear()
        for d in cfg.destinations:
            self.kind_combo.addItem(d.label, d.kind)
        self.dest_combo.clear()
        for d in cfg.destinations:
            self.dest_combo.addItem(f"{d.label} · {d.path}", d.path)
        self.dest_combo.addItem("Another folder…", None)
        self.sync_destination()

    def sync_destination(self):
        job = self.job
        if not job:
            return
        i = self.kind_combo.findData(job.media_kind)
        if i >= 0:
            self.kind_combo.setCurrentIndex(i)
        j = self.dest_combo.findData(job.destination)
        if j < 0 and job.destination:
            self.dest_combo.insertItem(self.dest_combo.count() - 1, f"Another folder · {job.destination}",
                                       job.destination)
            j = self.dest_combo.findData(job.destination)
        if j >= 0:
            self.dest_combo.setCurrentIndex(j)

    def kind_changed(self, _i):
        if not self.job:
            return
        d = self.win.cfg.destination(self.kind_combo.currentData())
        if d:
            self.job.media_kind, self.job.destination, self.job.library = d.kind, d.path, d.library
            self.sync_destination()
            self.update_actions()
            self.update_stream_note()

    def dest_changed(self, _i):
        if not self.job:
            return
        path = self.dest_combo.currentData()
        if path is None:
            path = QFileDialog.getExistingDirectory(self, "Choose the NAS folder to copy to",
                                                    self.job.destination or "/mnt")
            if not path:
                self.sync_destination()
                return
        self.job.destination = path
        d = next((d for d in self.win.cfg.destinations if d.path == path), None)
        if d:
            self.job.media_kind, self.job.library = d.kind, d.library
        self.sync_destination()

    def load(self, disc: Disc | None, job: Job | None):
        if disc is None or self.disc is None or disc.label != self.disc.label:
            self.cover.set_cover("", "")       # the placeholder until this disc's cover arrives
        self.disc, self.job = disc, job
        win = self.win
        if not disc or not job:
            self.disc_meta.setText("")
            self.disc_meta.hide()
            self.disc_title.setText("No disc scanned yet")
            self.disc_sum.setText("Put a disc in the drive. It's read as soon as it's in.")
            self.table.setRowCount(0)
            self.table.hide()
            self.table_empty.show()
            self.hide_short.setEnabled(False)
        else:
            dtype = "4K UHD Blu-ray" if disc.uhd else (disc.disc_type or "Disc")
            self.disc_meta.setText(" · ".join(p for p in (disc.label, dtype, fmt_size(disc.size_bytes)) if p))
            self.disc_meta.show()
            layout = win.layout_of(job)
            if job.show and layout != "movie":
                bits = [job.show]
                if job.season is not None:
                    bits.append(f"Season {job.season}")
                if job.disc_number:
                    bits.append(f"Disc {job.disc_number}")
                title = " · ".join(bits)
            else:
                title = job.show or disc.name or disc.label or "Untitled disc"
            self.disc_title.setText(title)
            self.disc_sum.setText(disc_summary(job.titles))
            self.fill_titles()
            self.table.show()
            self.table_empty.hide()
            self.hide_short.setEnabled(True)
            self.sync_destination()
        self.load_policy()
        self.fill_video()
        self.fill_streams()
        self.update_actions()
        self.update_stream_note()

    def update_stream_note(self):
        job, win = self.job, self.win
        show = bool(job) and job.stage == "rip" and not self.ripping() and win.layout_of(job) == "movie" \
            and win.cfg.stream_films
        why = ""
        if show:
            ok, why = win.stream_possible(job)
            show = not ok and bool(why)
        self.stream_note.setText(why if show else "")
        self.stream_note.setVisible(show)

    def match_clicked(self):
        if self.job is not None:
            self.win.open_match(self.job)

    def fill_titles(self):
        t = self.table
        t.blockSignals(True)
        t.setRowCount(0)
        mono = QFont(self.win.mono)
        for t_ in self.job.titles:
            r = t.rowCount()
            t.insertRow(r)
            box = QTableWidgetItem("")
            box.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            box.setCheckState(Qt.CheckState.Checked if t_.selected else Qt.CheckState.Unchecked)
            box.setData(Qt.ItemDataRole.UserRole, t_.index)
            box.setData(Qt.ItemDataRole.AccessibleTextRole, f"Rip {t_.source or t_.index}")
            t.setItem(r, 0, box)
            t.setItem(r, 1, item(t_.source or f"Title {t_.index}", mono))
            t.setItem(r, 2, item(fmt_len(t_.duration_s), mono))
            t.setItem(r, 3, item(fmt_size(t_.size_bytes), mono, muted=True))
            t.setItem(r, 4, item(str(t_.chapters), mono, muted=True))
            t.setCellWidget(r, 5, cell(chip(kind_text(t_), KIND_CHIP.get(t_.kind, "plain"))))
            t.setRowHeight(r, 46)
        t.blockSignals(False)
        self.apply_hide()

    def apply_hide(self, *_):
        if not self.job:
            return
        # Only short clips are hidden: a disc of 11-minute cartoons must still show its episodes.
        for r, t_ in enumerate(self.job.titles):
            self.table.setRowHidden(r, self.hide_short.isChecked() and t_.kind == "short" and not t_.selected)
        fit_table(self.table)

    def title_toggled(self, it: QTableWidgetItem):
        if not self.job or it.column() != 0:
            return
        idx = it.data(Qt.ItemDataRole.UserRole)
        for t_ in self.job.titles:
            if t_.index == idx:
                t_.selected = it.checkState() == Qt.CheckState.Checked
                w = self.table.cellWidget(it.row(), 5)
                lab = w.findChild(QLabel) if w else None
                if lab:
                    set_chip(lab, kind_text(t_), KIND_CHIP.get(t_.kind, "plain"))
        self.fill_streams()
        self.fill_video()
        self.update_actions()
        self.update_stream_note()

    def first_selected(self) -> Title | None:
        if not self.job:
            return None
        sel = [t for t in self.job.titles if t.selected]
        return sel[0] if sel else (self.job.titles[0] if self.job.titles else None)

    def fill_video(self):
        clear_layout(self.chip_row)
        t_ = self.first_selected()
        vids = [s for s in (t_.streams if t_ else []) if s.kind == "video"]
        if not vids:
            self.chip_row.addWidget(label("Scan a disc to see its video." if not self.job else "No video stream found.",
                                          "small"))
        else:
            self.chip_row.addWidget(label(video_chip(vids[0]), "tech"))
            hdr = next((f.hdr for f in (self.job.files if self.job else []) if f.hdr), None)
            if hdr and hdr.labels():
                for lab in hdr.labels():
                    self.chip_row.addWidget(label(lab, "hdr"))
                if hdr.hdr10plus is False:
                    self.chip_row.addWidget(label("No HDR10+", "nohdr"))
            elif hdr:
                self.chip_row.addWidget(label("SDR", "tech"))
            elif self.disc and self.disc.uhd:
                self.chip_row.addWidget(label("HDR formats are checked after ripping", "small"))
        uhd = bool(self.disc and self.disc.uhd) or not self.job
        for w in (self.keep_all, self.keep_hdr10):
            w.setEnabled(uhd and not self.ripping())
        self.convert.setEnabled(uhd and self.policy.keep == "all" and not self.ripping())
        self.no_uhd.setVisible(not uhd)

    def load_policy(self):
        p = self.policy
        for w in (self.keep_all, self.keep_hdr10, self.convert):
            w.blockSignals(True)
        self.keep_all.setChecked(p.keep == "all")
        self.keep_hdr10.setChecked(p.keep != "all")
        self.convert.setChecked(p.convert_p81)
        for w in (self.keep_all, self.keep_hdr10, self.convert):
            w.blockSignals(False)
        self.hdr.update(p)

    def policy_changed(self, *_):
        p = self.policy
        p.keep = "all" if self.keep_all.isChecked() else "hdr10"
        p.convert_p81 = self.convert.isChecked()
        self.convert.setEnabled(p.keep == "all" and self.keep_all.isEnabled())
        self.hdr.update(p)
        self.update_stream_note()

    def fill_streams(self):
        clear_layout(self.audio_lay)
        clear_layout(self.subs_lay)
        self.stream_boxes = []
        t_ = self.first_selected()
        cfg = self.win.cfg
        seen = set()
        for kind, lay, langs in (("audio", self.audio_lay, cfg.audio_langs), ("subtitle", self.subs_lay, cfg.subtitle_langs)):
            streams = [s for s in (t_.streams if t_ else []) if s.kind == kind]
            for s in streams:
                k = stream_key(s)
                if k in seen:
                    continue
                seen.add(k)
                box = QCheckBox(stream_label(s))
                box.setChecked(self._stream_on.get(k, stream_wanted(s, langs)) if hasattr(self, "_stream_on")
                               else stream_wanted(s, langs))
                box.toggled.connect(lambda on, k=k: self._remember(k, on))
                lay.addWidget(box)
                self.stream_boxes.append((box, s))
            if not streams:
                lay.addWidget(label("Scan a disc to see its tracks." if not self.job else "None on this title.", "small"))

    def _remember(self, key, on):
        if not hasattr(self, "_stream_on"):
            self._stream_on = {}
        self._stream_on[key] = on

    def ripping(self) -> bool:
        return bool(self.job) and self.win.is_running(self.job)

    def update_actions(self):
        win = self.win
        n = sum(1 for t in self.job.titles if t.selected) if self.job else 0
        rip_busy = win.busy("rip")
        if self.ripping():
            self.rip_btn.setText("Stop rip")
            set_prop(self.rip_btn, "primary", "false")
            self.rip_btn.setEnabled(True)
        else:
            self.rip_btn.setText(f"Rip {plural(n, 'title')}")
            set_prop(self.rip_btn, "primary", "true")
            self.rip_btn.setEnabled(bool(self.job) and n > 0 and not rip_busy and not win.busy("scan")
                                    and self.job.stage == "rip")
        drive_ok = self.drive_combo.count() > 0
        self.scan_btn.setEnabled(drive_ok and not rip_busy and not win.busy("scan") and not win.busy("eject"))
        self.eject_btn.setEnabled(drive_ok and not rip_busy and not win.busy("scan") and not win.busy("eject"))
        self.refresh_btn.setEnabled(not win.busy("drives") and not rip_busy)
        self.scan_btn.setText("Scanning…" if win.busy("scan") else "Scan again")
        locked = self.ripping() or (self.job is not None and self.job.stage != "rip")
        self.table.setEnabled(not locked)
        for box, _s in self.stream_boxes:
            box.setEnabled(not locked)
        self.kind_combo.setEnabled(bool(self.job) and not locked)
        self.dest_combo.setEnabled(bool(self.job) and not locked)
        self.fill_video_enabled(locked)
        film = self.job is not None and win.layout_of(self.job) == "movie"
        self.match_link.setVisible(bool(self.job) and self.job.stage == "rip" and not film)
        self.match_link.setEnabled(not win.busy("scan"))

    def fill_av1(self):
        """The per-disc AV1 choice: shown for 4K discs, defaulting to Settings."""
        cfg, job = self.win.cfg, self.job
        show = bool(job and self.disc and self.disc.uhd and hasattr(job, "av1"))
        self.av1_row.setVisible(show)
        if not show:
            return
        enc = getattr(cfg, "av1_encoder", "cpu")
        name = {"cpu": "Best quality, on the CPU", "gpu": "Fast, on the GPU"}
        other = "gpu" if enc == "cpu" else "cpu"
        self.av1_combo.clear()
        if getattr(cfg, "av1_enabled", False):
            self.av1_combo.addItem(f"Make one · {name[enc]} (from Settings)", "")
            self.av1_combo.addItem(f"Make one · {name[other]}", other)
            self.av1_combo.addItem("Don't make one for this disc", "off")
        else:
            self.av1_combo.addItem("Don't make one (off in Settings)", "")
            self.av1_combo.addItem(f"Make one · {name[enc]}", enc)
            self.av1_combo.addItem(f"Make one · {name[other]}", other)
        self.av1_combo.setCurrentIndex(max(0, self.av1_combo.findData(job.av1)))

    def av1_picked(self, _i: int):
        if self.job is not None and hasattr(self.job, "av1"):
            self.job.av1 = self.av1_combo.currentData() or ""
            try:
                self.win.ctl.save_job(self.job)
            except Exception:  # noqa: BLE001 -- a job that isn't saved yet takes the choice when it is
                pass

    def fill_video_enabled(self, locked: bool):
        uhd = bool(self.disc and self.disc.uhd) or not self.job
        self.fill_av1()
        self.av1_combo.setEnabled(not (self.job is not None and self.job.stage in ("done",)))
        self.keep_all.setEnabled(uhd and not locked)
        self.keep_hdr10.setEnabled(uhd and not locked)
        self.convert.setEnabled(uhd and not locked and self.policy.keep == "all")

    # -- actions ------------------------------------------------------------
    def scan(self, auto: bool = False):
        drive = self.drive_combo.currentData()
        if drive is None:
            return
        win, ctl = self.win, self.win.ctl

        def work(_p):
            try:
                disc = ctl.scan(drive.index)
                return disc, ctl.new_job(disc)
            except Exception:
                if auto:
                    win._auto_failed = True     # don't retry this disc until it comes out
                raise

        self.win.run_task("scan", work, ok=lambda r: self.scanned(*r), title="Couldn't read the disc",
                          next_step="Check that the disc is clean, then choose Scan again.")

    def scanned(self, disc: Disc, job: Job):
        self._stream_on = {}
        self.win.disc = disc
        self.load(disc, job)
        self.win.update_header()
        # The drive list was read before the disc went in; read it again so the picker and the
        # header dot show the disc.
        QTimer.singleShot(0, lambda: self.win.refresh_drives(poll=False))
        self.load_cover(disc, job.season)

    def load_cover(self, disc: Disc, season: int | None = None):
        """Fetch the cover on a worker: it may mount the disc or ask TVmaze."""
        fetch = getattr(self.win.ctl, "cover", None)
        if fetch is None:
            return
        idx, label_ = disc.drive.index, disc.label

        def got(r):
            path, source = r if r else ("", "")
            if self.disc is not None and self.disc.label == label_:
                self.cover.set_cover(path, source)

        self.win.run_task(f"cover:{label_}:{season}", lambda _p: fetch(idx, season), ok=got, quiet=True)
        # Disc info follows the disc just scanned, on the same drive.
        self.win.info.fill_drives(self.win.drive_list, disc.drive.index)
        self.win.info.read(disc.drive.index)

    def eject(self):
        drive = self.drive_combo.currentData()
        if drive is None:
            return

        def done(_r):
            self.win.disc = None
            self.load(None, None)
            self.win.update_header()
            self.win.refresh_drives()

        self.win.run_task("eject", lambda _p: self.win.ctl.eject(drive.index), ok=done, title="Couldn't eject the disc",
                          next_step="Press the button on the drive, or check that no other program is using it.")

    def rip_clicked(self):
        if self.ripping():
            self.win.stop(self.job)
            return
        job = self.job
        kept = {stream_key(s) for box, s in self.stream_boxes if box.isChecked()}
        offered = {stream_key(s) for _b, s in self.stream_boxes}
        for t in job.titles:
            if t.selected:
                # Tracks left in title.streams are the ones to keep; a track this title has that
                # the list didn't offer stays, so nothing is dropped unseen.
                t.streams = [s for s in t.streams if s.kind not in ("audio", "subtitle")
                             or stream_key(s) in kept or stream_key(s) not in offered]
        self.win.start_rip(job)


# ---------------------------------------------------------------------------
# Match page
# ---------------------------------------------------------------------------

class ExtraDialog(QDialog):
    def __init__(self, parent, name: str, folder: str):
        super().__init__(parent)
        self.setWindowTitle("Save as an extra")
        self.name = QLineEdit(name)
        self.folder = QComboBox()
        self.folder.addItems(list(EXTRAS_FOLDERS))
        self.folder.setCurrentText(folder if folder in EXTRAS_FOLDERS else "Featurettes")
        buttons = QDialogButtonBox()
        ok = buttons.addButton("Save as extra", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setProperty("primary", "true")
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.setLayout(vbox(field("Name of the extra", self.name), field("Plex extras folder", self.folder),
                            buttons, spacing=14, margins=(20, 20, 20, 20)))
        self.setMinimumWidth(380)


class EpisodeListDialog(QDialog):
    IMAGES = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".bmp", ".tif", ".tiff")

    def __init__(self, parent, text: str, read_photo=None):
        """read_photo(path) runs on a worker and returns a caseocr.CaseList; None hides the photo button."""
        super().__init__(parent)
        self.setWindowTitle("Enter the episode list")
        self.read_photo = read_photo
        self.text = QPlainTextEdit(text)
        self.text.setPlaceholderText("1. First episode on the case\n2. Second episode\n3. …")
        hint = label("Type or paste the episode names from the case, one per line, in the order the case lists them. "
                     "They're matched to the titles in disc order and can come from any season.", "muted", wrap=True)
        self.photo_btn = button("Read a photo of the case…")
        self.photo_btn.clicked.connect(self.choose_photo)
        self.photo_btn.setVisible(read_photo is not None)
        self.photo_note = label("", "muted", wrap=True)
        self.photo_note.hide()
        buttons = QDialogButtonBox()
        self.ok = buttons.addButton("Match episodes", QDialogButtonBox.ButtonRole.AcceptRole)
        self.ok.setProperty("primary", "true")
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.setLayout(vbox(hint, hbox(self.photo_btn, None), self.photo_note, self.text, buttons,
                            spacing=14, margins=(20, 20, 20, 20)))
        self.setMinimumSize(460, 500)
        self.setAcceptDrops(read_photo is not None)
        self.worker = None

    def choose_photo(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose a photo of the back of the case", os.path.expanduser("~"),
                                              "Images (" + " ".join(f"*{e}" for e in self.IMAGES) + ")")
        if path:
            self.load_photo(path)

    def dragEnterEvent(self, e):  # noqa: N802 -- Qt's name
        if any(u.toLocalFile().lower().endswith(self.IMAGES) for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):  # noqa: N802 -- Qt's name
        path = next((u.toLocalFile() for u in e.mimeData().urls() if u.toLocalFile().lower().endswith(self.IMAGES)), "")
        if path:
            self.load_photo(path)

    def load_photo(self, path: str):
        if self.worker is not None:
            return
        self.photo_btn.setEnabled(False)
        self.ok.setEnabled(False)
        self.photo_note.setText("Reading the photo…")
        self.photo_note.show()
        self.worker = w = Worker(lambda _p: self.read_photo(path))

        def got(case):
            if case.names:
                self.text.setPlainText("\n".join(case.names))
            self.photo_note.setText(case.summary() + (" Check the list against the case, then match."
                                                      if case.names else " Type the names instead."))

        def failed(msg):
            self.photo_note.setText(f"Couldn't read the photo: {msg} Type the names instead.")

        def finished():
            self.worker = None
            self.photo_btn.setEnabled(True)
            self.ok.setEnabled(True)
            w.deleteLater()

        w.succeeded.connect(got)
        w.failed.connect(failed)
        w.finished.connect(finished)
        w.start()


class MatchPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.job: Job | None = None
        self.show_ids: dict[str, int] = {}
        self.episodes: dict[tuple[int, int | None], list[Episode]] = {}   # season None: every season
        self.list_text: dict[str, str] = {}
        self._sig: tuple = ()
        self.plan_error = ""
        self.plan: list[RippedFile] = []

        inner = QWidget()
        col = vbox(spacing=20, margins=(24, 24, 24, 24))
        inner.setLayout(col)

        self.meta = label("", "meta")
        self.h1 = label("Match titles to episodes", "h1")
        self.sub = label("Discs often store episodes out of order, so each title is matched by its running time.",
                         "muted", wrap=True)
        col.addLayout(vbox(self.meta, self.h1, self.sub, spacing=6))

        # Fields
        self.show_combo = QComboBox()
        self.show_combo.setEditable(True)
        self.show_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.show_combo.setMinimumWidth(280)
        self.show_combo.lineEdit().setPlaceholderText("Type the show's name")
        self.show_combo.lineEdit().returnPressed.connect(self.find_show)
        self.show_combo.activated.connect(self.hit_chosen)
        self.find_btn = button("Find show")
        self.find_btn.clicked.connect(self.find_show)
        self.list_btn = button("Enter episode list…")
        self.list_btn.setToolTip("Match the titles to the episode names printed on the case, from any season.")
        self.list_btn.clicked.connect(self.enter_list)
        self.cards_btn = button("Read title cards")
        self.cards_btn.setToolTip("Match each ripped title by reading the episode name shown near its start.")
        self.cards_btn.clicked.connect(self.read_cards)
        show_w = QWidget()
        show_w.setLayout(hbox(self.show_combo, self.find_btn, spacing=8))
        self.show_field = field("Show", show_w)
        self.film_name = QLineEdit()
        self.film_name.setMinimumWidth(280)
        self.film_name.textEdited.connect(self.film_edited)
        self.film_field = field("Title", self.film_name)
        self.year = field_spin()
        self.year.setRange(0, 2100)
        self.year.setSpecialValueText("—")
        self.year.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.year.setFixedWidth(96)
        self.year.valueChanged.connect(self.film_edited)
        self.year_field = field("Year", self.year)
        self.season = field_spin()
        self.season.setRange(0, 99)
        self.season.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.season.setFixedWidth(80)
        self.season.valueChanged.connect(self.season_changed)
        self.season_field = field("Season", self.season)
        self.disc_no = field_spin()
        self.disc_no.setRange(0, 99)
        self.disc_no.setSpecialValueText("—")
        self.disc_no.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.disc_no.setFixedWidth(80)
        self.disc_no.valueChanged.connect(self.disc_changed)
        self.disc_field = field("Disc number", self.disc_no)
        self.dest = QComboBox()
        self.dest.setMinimumWidth(260)
        self.dest.activated.connect(self.dest_changed)
        self.dest_field = field("Copy to", self.dest)
        self.fields = hbox(self.show_field, self.film_field, self.year_field, self.season_field, self.disc_field,
                           self.dest_field, None, spacing=12)
        col.addLayout(self.fields)

        # While the disc rips: how the titles get to the NAS.
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        self.banner.setStyleSheet("QFrame#banner { background: #15202B; border: 1px solid #2E4A63; border-radius: 10px; }")
        self.banner_text = label("", wrap=True)
        self.banner_text.setStyleSheet("color: #CFE3F5; font-size: 13px;")
        bl = hbox(self.banner_text, link("Copying settings", lambda: win.show_page("settings")), spacing=16,
                  margins=(16, 12, 16, 12))
        bl.setStretch(0, 1)
        self.banner.setLayout(bl)
        self.banner.hide()
        col.addWidget(self.banner)
        self.suggest_timer = QTimer(self)
        self.suggest_timer.setSingleShot(True)
        self.suggest_timer.setInterval(500)
        self.suggest_timer.timeout.connect(self.refresh_suggestions)

        # Title cards: how far reading has got, one bar segment per title.
        self.cards_box = QFrame()
        self.cards_box.setObjectName("cardsBox")
        self.cards_title = label("", "h3")
        self.cards_sub = label("", "small", wrap=True)
        self.cards_stop = link("Stop reading", self.stop_cards)
        self.cards_bar = SegmentBar()
        self.cards_bar.setAccessibleName("Title cards read")
        self.cards_legend = label("", "small")
        self.cards_legend.setTextFormat(Qt.TextFormat.RichText)
        top = hbox(self.cards_title, None, self.cards_stop, spacing=16)
        self.cards_box.setLayout(vbox(top, self.cards_sub, self.cards_bar, self.cards_legend, spacing=8,
                                      margins=(18, 14, 18, 14)))
        self.cards_box.hide()
        col.addWidget(self.cards_box)
        self.cards_cancel: threading.Event | None = None
        self.card_cells: dict[int, tuple[QLabel, QProgressBar]] = {}
        self.cards_timer = QTimer(self)
        self.cards_timer.setInterval(500)
        self.cards_timer.timeout.connect(self.refresh_cards)

        # Matches
        mcard = Card(margins=(0, 0, 0, 0), spacing=0)
        head = QFrame()
        head.setObjectName("cardHead")
        self.source_note = label("Episode list from TVmaze", "small")
        self.decisions = label("", "warntext")
        head.setLayout(hbox(label("Matches", "h2"), self.source_note, None, self.decisions, self.list_btn,
                            self.cards_btn, spacing=12, margins=(20, 12, 20, 12)))
        mcard.lay.addWidget(head)
        self.table = plain_table(["Title on disc", "Length", "Episode", "Listed", "Match", "Progress", "Title card"])
        self.table.horizontalHeader().moveSection(6, 3)    # shown next to the episode it read
        for c, w in enumerate((130, 80, 350, 80, 130, 200, 210)):
            self.table.setColumnWidth(c, w)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table_empty = label("", "muted", wrap=True)
        self.table_empty.setContentsMargins(20, 16, 20, 16)
        mcard.lay.addWidget(self.table)
        mcard.lay.addWidget(self.table_empty)
        self.notes = QFrame()
        self.notes.setObjectName("rowLine")
        self.notes_text = label("", wrap=True)
        self.notes_text.setStyleSheet("color: #E2D3B5; font-size: 13px;")
        self.notes.setLayout(vbox(self.notes_text, margins=(20, 10, 20, 14)))
        mcard.lay.addWidget(self.notes)
        col.addWidget(mcard)
        self.prog_cells: dict[int, tuple[QLabel, QProgressBar]] = {}
        self._locked: set[int] = set()
        self.preview: list[tuple[int, str]] = []
        self.preview_error = ""

        # Preview
        prev = Card(spacing=10)
        self.prev_note = label("", "small", wrap=True)
        ph = hbox(label("Files Plex will see", "h2"), self.prev_note, spacing=16)
        ph.setStretch(1, 1)
        prev.lay.addLayout(ph)
        self.paths = label("", "paths")
        self.paths.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.paths.setStyleSheet("line-height: 190%;")
        prev.lay.addWidget(self.paths)
        col.addWidget(prev)

        self.saved = label("", "small", wrap=True)
        self.save_btn = button("Save and finish later")
        self.save_btn.clicked.connect(self.save_later)
        self.back_btn = button("Back to the rip")
        self.back_btn.clicked.connect(lambda: win.show_page("rip"))
        self.confirm_btn = button("Confirm and copy 0 files", primary=True)
        self.confirm_btn.clicked.connect(self.confirm)
        foot = hbox(self.saved, None, self.back_btn, self.save_btn, self.confirm_btn)
        foot.setStretch(0, 1)
        col.addLayout(foot)
        col.addStretch(1)

        self.empty = QWidget()
        self.empty.setLayout(vbox(label("Match titles to episodes", "h1"),
                                  label("No ripped disc is waiting. Rip a disc first, then match its titles here.",
                                        "muted", wrap=True), None, spacing=8, margins=(24, 24, 24, 24)))
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(scroll_page(inner))
        self.setLayout(vbox(self.stack))

    # -- helpers ------------------------------------------------------------
    def layout(self) -> str:   # noqa: A003 -- the Destination.layout of the open job
        return self.win.layout_of(self.job) if self.job else "tv"

    def picker_season(self) -> int | None:
        """The season the Episode picker lists, or None for every season: a disc that names
        no season, or one whose matches already span seasons (a volume DVD)."""
        job = self.job
        if not job.season or any(m.episode and m.episode.season != job.season for m in job.matches):
            return None
        return job.season

    def season_eps(self) -> list[Episode]:
        """Every episode of the show, for the picker to search; picker_season() is only the
        season it opens on."""
        job = self.job
        sid = self.show_ids.get(job.id)
        eps = list(self.episodes.get((sid, None), [])) if sid is not None else []
        have = {(e.season, e.number) for e in eps}
        for m in job.matches:
            if m.episode and (m.episode.season, m.episode.number) not in have:
                eps.append(m.episode)
                have.add((m.episode.season, m.episode.number))
        return sorted(eps, key=lambda e: (e.season, e.number))

    @staticmethod
    def suggestions_for(m: Match, eps: list[Episode]) -> list[tuple[Episode, str]]:
        """What the picker lists first for one title: its current match, with where it came
        from, and any other episode its note names (“could be X or Y”)."""
        out: list[tuple[Episode, str]] = []
        seen: set[tuple[int, int]] = set()
        quoted = re.findall(r"“([^”]+)”", m.note or "")
        by_name = {e.title.lower(): e for e in eps if e.title}
        read = next((q for q in quoted if q.lower() not in by_name), "")
        why = {"card": f"The title card reads “{read}”" if read else "Read from its title card",
               "discdb": "From TheDiscDB's record of this disc", "list": "From the episode list you entered",
               "runtime": "A guess from its running time", "manual": "Picked by you"}.get(m.source, "The current match")
        if m.target == "episode" and m.episode:
            out.append((m.episode, why))
            seen.add((m.episode.season, m.episode.number))
        for q in quoted:
            e = by_name.get(q.lower())
            if e and (e.season, e.number) not in seen:
                out.append((e, f"The note says it could be this one{f', from the card “{read}”' if read else ''}"))
                seen.add((e.season, e.number))
        return out

    def rows(self) -> list[Match]:
        job = self.job
        selected = {t.index for t in job.titles if t.selected}
        if job.stage == "rip":
            keep = selected            # matching before or during the rip: every title that will be ripped
        else:
            keep = {f.title_index for f in job.files} or selected
        with self.win.lock():
            return [m for m in job.matches if m.title_index in keep]

    def copy_mode(self) -> str:
        return self.win.expected_copy_mode(self.job) if self.job else ""

    @staticmethod
    def locked_state(p: TitleProgress | None) -> bool:
        return p is not None and p.state in ("copying", "checking", "verified")

    def title_of(self, idx: int) -> Title | None:
        return next((t for t in self.job.titles if t.index == idx), None)

    # -- open ---------------------------------------------------------------
    def open(self, job: Job | None):
        same = job is not None and self.job is not None and job.id == self.job.id
        self.job = job
        if not same:
            self.set_saved("")
        if not job:
            self.stack.setCurrentIndex(0)
            return
        self.stack.setCurrentIndex(1)
        self.fill_dest()
        self.fill_fields()
        self.fill_table()
        if same:
            return
        film = self.layout() == "movie"
        if film:
            if not job.matches:
                self.refresh_suggestions()
        elif job.show and job.id not in self.show_ids:
            q = job.show
            self.win.run_task("search", lambda _p: self.win.ctl.search_show(q),
                              ok=lambda hits: self.got_hits(hits, auto=True), title="Couldn't search for the show",
                              next_step="Check the internet connection, then choose Find show again.")

    def fill_dest(self):
        job = self.job
        self.dest.clear()
        for d in self.win.cfg.destinations:
            self.dest.addItem(f"{d.label} · {d.path}", d.path)
        if job.destination and self.dest.findData(job.destination) < 0:
            self.dest.addItem(f"Another folder · {job.destination}", job.destination)
        self.dest.addItem("Another folder…", None)
        self.dest.setCurrentIndex(max(0, self.dest.findData(job.destination)))

    def fill_fields(self):
        job = self.job
        film = self.layout() == "movie"
        n = len(job.files) or sum(1 for t in job.titles if t.selected)
        if job.stage == "rip":
            state = "ripping now" if self.win.is_ripping(job) else "not ripped yet"
        else:
            state = f"{plural(n, 'title')} ripped"
        self.meta.setText(f"{job_name(job, self.layout()).upper()} · {state}")
        self.h1.setText("Match the film" if film else "Match titles to episodes")
        if job.stage == "rip":
            self.sub.setText("Running times come from the scan, so you can match while the disc rips.")
        else:
            self.sub.setText("Pick which title is the film. Anything else you keep is saved as an extra." if film else
                             "Discs often store episodes out of order, so each title is matched by its running time.")
        mode = self.copy_mode()
        self.banner_text.setText({
            "per_title": "Each title goes to the NAS as soon as it's ripped and matched. A title that's already on "
                         "the NAS keeps its match.",
            "stream": "The film goes to the NAS while it rips. Its match is locked once the copy starts.",
            "after": "The titles are copied after the rip ends, once you confirm the matches.",
        }.get(mode, ""))
        self.banner.setVisible(job.stage == "rip" and bool(mode))
        for w in (self.show_field, self.season_field, self.disc_field):
            w.setVisible(not film)
        for w in (self.film_field, self.year_field):
            w.setVisible(film)
        self.source_note.setVisible(not film)
        for w in (self.season, self.disc_no, self.year, self.film_name):
            w.blockSignals(True)
        self.season.setValue(job.season or 0)
        self.disc_no.setValue(job.disc_number or 0)
        self.year.setValue(job.year or 0)
        self.film_name.setText(job.show)
        for w in (self.season, self.disc_no, self.year, self.film_name):
            w.blockSignals(False)
        if not self.show_combo.count():
            self.show_combo.setEditText(f"{job.show} ({job.year})" if job.show and job.year else job.show)
        self.table.setHorizontalHeaderLabels(["Title on disc", "Length", "Use as" if film else "Episode",
                                              "Listed", "Match", "Progress", "Title card"])
        self.table.setColumnHidden(3, film)

    def badge_cell(self, m: Match) -> QWidget:
        if m.target == "skip":
            c = chip("Skipped", "plain")
        elif m.confidence == "check":
            c = chip("Check", "warn")
        elif m.confidence == "manual":
            c = chip("Picked by you", "info")
        elif m.target == "extra":
            c = chip("Extra", "plain")
        else:
            c = chip("Close match", "good")
        if m.confidence == "check" and m.confirmed and m.target != "skip":
            set_chip(c, "Confirmed", "good")
        w = QWidget()
        lay = hbox(spacing=10, margins=(8, 4, 12, 4))
        lay.addWidget(c, 0, Qt.AlignmentFlag.AlignVCenter)
        if m.note:
            c.setToolTip(m.note)
        lay.addStretch(1)
        w.setLayout(lay)
        return w

    def match_sig(self) -> tuple:
        """What the table shows of each match, to notice matches changed in the background."""
        with self.win.lock():
            return tuple((m.title_index, m.target, m.episode and (m.episode.season, m.episode.number), m.episode_end,
                          m.confidence, m.confirmed, m.source) for m in self.job.matches)

    def fill_table(self):
        job = self.job
        self._sig = self.match_sig()
        t = self.table
        t.setRowCount(0)
        self.prog_cells = {}
        mono = QFont(self.win.mono)
        film = self.layout() == "movie"
        eps = [] if film else self.season_eps()
        rows = self.rows()
        used = {(x.episode.season, x.episode.number): x.title_index for x in rows if x.target == "episode" and x.episode}
        prog = {p.title_index: p for p in self.win.progress_of(job)}
        self._locked = {i for i, p in prog.items() if self.locked_state(p)}
        show_progress = bool(prog) or self.win.is_ripping(job)
        t.setColumnHidden(5, not show_progress)
        cards = self.card_status()
        t.setColumnHidden(6, film or not cards)
        self.card_cells = {}
        notes = []
        for m in rows:
            r = t.rowCount()
            t.insertRow(r)
            title = self.title_of(m.title_index)
            src = title.source if title and title.source else f"Title {m.title_index}"
            t.setItem(r, 0, item(src, mono))
            t.setItem(r, 1, item(fmt_len(title.duration_s) if title else "—", mono))
            locked = m.title_index in self._locked
            want = m.target if m.target in ("feature", "extra", "skip") else None
            if m.target == "episode" and m.episode:
                want = f"episode:{m.episode.season}:{m.episode.number}:{m.episode_end or ''}"
            warn = m.confidence == "check" and not m.confirmed and not locked
            if film:
                combo = NoWheelComboBox()
                combo.setMaxVisibleItems(12)
                combo.addItem(f"Film · {job.show} ({job.year})" if job.year else f"Film · {job.show or 'untitled'}",
                              "feature")
                if m.target == "extra":
                    combo.addItem(f"Extra · {m.extra_name}", "extra")
                combo.addItem("Extra…", "extra_new")
                combo.addItem("Skip this title", "skip")
                i = combo.findData(want) if want else -1
                combo.setCurrentIndex(i if i >= 0 else combo.count() - 1)
                combo.setProperty("prev", combo.currentIndex())
                combo.activated.connect(lambda _i, m=m, c=combo: self.pick(m, c))
            else:
                combo = EpisodePicker()
                combo.set_options(eps, want or "skip", title_index=m.title_index, used=used,
                                  suggestions=self.suggestions_for(m, eps), season=self.picker_season(),
                                  extra_label=f"Extra · {m.extra_name}" if m.target == "extra" else "",
                                  double=(m.episode, m.episode_end) if m.target == "episode" and m.episode
                                  and m.episode_end else None, warn=warn)
                combo.setProperty("prev_data", want or "skip")
                combo.picked.connect(lambda data, m=m, c=combo: self.pick(m, c, data))
            combo.setAccessibleName(f"Episode for {src}")
            combo.setMaximumWidth(330)
            if warn:
                combo.setProperty("warn", "true")
            combo.setEnabled(not locked)
            ep_cell = QWidget()
            el = vbox(combo, spacing=4, margins=(8, 6, 8, 6))
            if locked:
                el.addWidget(label("Already on the NAS, so the match is locked.", "field"))
                combo.setToolTip("Already on the NAS, so the match is locked.")
            ep_cell.setLayout(el)
            t.setCellWidget(r, 2, ep_cell)
            listed = f"{m.episode.runtime_min} min" if m.target == "episode" and m.episode and m.episode.runtime_min else "—"
            t.setItem(r, 3, item(listed, mono, muted=True))
            t.setCellWidget(r, 4, self.badge_cell(m))
            t.setCellWidget(r, 5, self.progress_cell(m.title_index, prog.get(m.title_index)))
            t.setCellWidget(r, 6, self.card_cell(m.title_index, cards.get(m.title_index)))
            t.setRowHeight(r, 76 if locked else 56)
            if m.note and m.target != "skip" and not (m.confirmed and m.confidence == "check"):
                tail = ""
                if job.stage == "rip" and self.copy_mode() in OVERLAP and m.confidence == "check" and not locked:
                    tail = " Check it before it's copied; it waits until you confirm."
                notes.append(f"{src}: {m.note}{tail}")
        fit_table(t)
        t.setVisible(bool(rows))
        self.table_empty.setVisible(not rows)
        self.notes_text.setText("\n".join(notes))
        self.notes.setVisible(bool(notes) and bool(rows))
        if not rows:
            busy = self.win.busy("suggest") or self.win.busy("search")
            self.table_empty.setText("Looking up the episodes…" if busy else
                                     ("Find the show above to match its episodes." if not film else
                                      "No titles were ripped from this disc."))
        self.update_counts()

    def progress_cell(self, idx: int, p: TitleProgress | None) -> QWidget:
        w = QWidget()
        text = label("")
        bar = colour_bar(ACCENT, 4)
        w.setLayout(vbox(None, text, bar, None, spacing=5, margins=(8, 6, 20, 6)))
        self.prog_cells[idx] = (text, bar)
        self.set_progress(idx, p)
        return w

    @staticmethod
    def progress_text(p: TitleProgress | None) -> tuple[str, str, str, float]:
        """(text, text colour, bar colour, fraction) for one title's Progress cell."""
        if p is None:
            return "Waiting to rip", MUTED, ACCENT, 0.0
        tot = p.total_bytes or 1
        if p.state == "ripping":
            pct = p.ripped_bytes / tot
            extra = f" · {fmt_rate(p.rip_rate)}" if p.rip_rate else ""
            return f"Ripping · {int(pct * 100)}%{extra}", RIP_TEXT, ACCENT, pct
        if p.state == "copying":
            pct = p.copied_bytes / tot
            extra = f" · {fmt_rate(p.copy_rate, 0)}" if p.copy_rate else ""
            return f"Copying to the NAS · {int(pct * 100)}%{extra}", COPY_TEXT, COPY_BAR, pct
        if p.state == "checking":
            pct = p.checked_bytes / tot
            return f"Checking the NAS copy · {int(pct * 100)}%", COPY_TEXT, COPY_BAR, pct
        if p.state == "verified":
            return "On the NAS · checked", DONE_TEXT, DONE_BAR, 1.0
        if p.state == "waiting_match":
            return "Ripped · waits for your check", RIP_TEXT, ACCENT, 1.0
        if p.state == "ripped":
            return "Ripped", SOFT, ACCENT, 1.0
        if p.state == "failed":
            return p.note or "Failed", FAIL_TEXT, FAIL_BAR, 1.0
        if p.state == "skipped":
            return "Skipped", MUTED, FIELD, 0.0
        return "Waiting to rip", MUTED, ACCENT, 0.0

    def set_progress(self, idx: int, p: TitleProgress | None):
        text, bar = self.prog_cells[idx]
        words, colour, bar_colour, frac = self.progress_text(p)
        text.setText(words)
        text.setStyleSheet(f"color: {colour}; font-size: 12px;")
        set_bar_colour(bar, bar_colour, 4)
        bar.setValue(int(max(0.0, min(1.0, frac)) * 1000))

    def refresh_progress(self):
        """Called on each progress tick of a running rip: update the Progress cells in place."""
        if self.job is None or self.stack.currentIndex() != 1:
            return
        if not self.cards_timer.isActive() and any(st.state in ("waiting", "reading")
                                                   for st in self.card_status().values()):
            self.cards_timer.start()       # cards are read as titles finish ripping
        prog = {p.title_index: p for p in self.win.progress_of(self.job)}
        locked = {i for i, p in prog.items() if self.locked_state(p)}
        shown = {m.title_index for m in self.rows()}
        if locked != self._locked or (prog and self.table.isColumnHidden(5)) \
                or any(i not in self.prog_cells for i in prog if i in shown) or self.match_sig() != self._sig:
            self.fill_table()        # a row just locked, or a title card changed a match: rebuild
            return
        for idx in self.prog_cells:
            self.set_progress(idx, prog.get(idx))
        self.fill_preview(prog)
        self.update_actions()

    def update_counts(self):
        job = self.job
        rows = self.rows()
        n_check = sum(1 for m in rows if m.confidence == "check" and m.target != "skip" and not m.confirmed
                      and m.title_index not in self._locked)
        before = " before it's copied" if job.stage == "rip" and self.copy_mode() in OVERLAP else ""
        self.decisions.setText(f"{n_check} need a decision{before}" if n_check > 1 else
                               (f"1 needs a decision{before}" if n_check == 1 else ""))
        dest = job.destination or "the NAS"
        lib = job.library
        if job.stage == "rip" and self.copy_mode() in OVERLAP:
            self.prev_note.setText(f"On the NAS share {dest}, scanned into the {lib} library as each one lands" if lib
                                   else f"On the NAS share {dest}. Plex isn't asked to scan it.")
        else:
            self.prev_note.setText(f"Copied to the NAS share {dest}, then scanned into the {lib} library" if lib else
                                   f"Copied to the NAS share {dest}. Plex isn't asked to scan it.")
        self.plan, self.plan_error = [], ""
        if job.matches and job.stage != "rip":
            try:
                with self.win.lock():
                    self.plan = [f for f in self.win.ctl.plan_paths(job) if f.dest_rel]
            except Exception as exc:  # the names can't be built yet, e.g. no show chosen
                self.plan_error = str(exc) or type(exc).__name__
        self.fill_preview({p.title_index: p for p in self.win.progress_of(job)})
        self.update_actions()

    def fill_preview(self, prog: dict[int, TitleProgress]):
        job = self.job
        self.preview, err = [], ""
        if job.matches:
            try:
                self.preview = list(self.win.ctl.preview_paths(job))
            except Exception as exc:  # e.g. no show chosen yet
                err = str(exc) or type(exc).__name__
        self.preview_error = err or self.plan_error
        base = os.path.basename(os.path.normpath(job.destination)) if job.destination else ""
        with self.win.lock():
            by = {m.title_index: m for m in job.matches}
        if self.preview_error:
            text, style = self.preview_error, "color: #F5C27A; line-height: 190%;"
        elif self.preview:
            lines = []
            for idx, rel in self.preview:
                path = f"{base}/{rel}" if base else rel
                p, m = prog.get(idx), by.get(idx)
                state = ""
                if p is not None and p.state == "verified":
                    state = "on the NAS"
                elif p is not None and p.state in ("copying", "checking"):
                    state = "copying"
                elif m is not None and m.confidence == "check" and not m.confirmed and m.target != "skip":
                    state = "waits for your check"
                lines.append((path, state))
            width = max(len(pth) for pth, _ in lines)
            text = "\n".join(f"{pth.ljust(width)}   {st}".rstrip() for pth, st in lines)
            style = "line-height: 190%;"
        else:
            text = "Nothing to copy yet. Pick an episode, film or extra for at least one title."
            style = f"color: {MUTED};"
        if self.paths.text() != text:
            self.paths.setText(text)
        if self.paths.styleSheet() != style:
            self.paths.setStyleSheet(style)

    def update_actions(self):
        if not self.job:
            return
        job = self.job
        busy = self.win.busy("copy") or self.win.busy("suggest")
        rip_stage = job.stage == "rip"
        self.back_btn.setVisible(rip_stage)
        self.save_btn.setVisible(not rip_stage)
        if rip_stage:
            self.confirm_btn.setText("Confirm matches")
            self.confirm_btn.setEnabled(bool(job.matches) and not self.win.busy("suggest"))
            self.confirm_btn.setToolTip("")
            if not self.saved.property("pinned"):
                if self.copy_mode() in OVERLAP:
                    self.saved.setText("Nothing else to do here: the rest copies as each title finishes ripping.")
                else:
                    self.saved.setText("The titles are copied once the rip ends and you confirm the matches.")
        else:
            left = self.titles_left()
            n = len(left) if left is not None else len(self.plan)
            self.confirm_btn.setText(f"Confirm and copy {plural(n, 'file')}")
            self.confirm_btn.setEnabled(n > 0 and not self.plan_error and not busy and job.stage == "match")
            self.confirm_btn.setToolTip("Another copy is running. This one can start when it finishes."
                                        if self.win.busy("copy") else "")
            if not self.saved.property("pinned"):
                self.saved.setText("")
        self.find_btn.setEnabled(not self.win.busy("search"))
        self.find_btn.setText("Searching…" if self.win.busy("search") else "Find show")
        self.list_btn.setVisible(self.layout() != "movie")
        self.list_btn.setEnabled(job.id in self.show_ids and not self.win.busy("suggest"))
        # During a rip the cards are read as each title finishes; this is for discs already ripped.
        self.cards_btn.setVisible(self.layout() != "movie" and job.stage != "rip" and bool(job.files)
                                  and hasattr(self.win.ctl, "read_cards"))
        self.cards_btn.setEnabled(job.id in self.show_ids and not self.win.busy("cards") and not self.win.busy("suggest"))
        self.cards_btn.setText("Reading title cards…" if self.win.busy("cards") else "Read title cards")

    def titles_left(self) -> list[RippedFile] | None:
        """Files still to copy after the rip, or None when no title has been copied yet."""
        prog = {p.title_index: p for p in self.win.progress_of(self.job)}
        if not any(p.state == "verified" for p in prog.values()):
            return None
        return [f for f in self.plan if f.state != "verified"
                and not (f.title_index in prog and prog[f.title_index].state == "verified")]

    def set_saved(self, text: str):
        self.saved.setText(text)
        self.saved.setProperty("pinned", bool(text))

    # -- edits --------------------------------------------------------------
    def pick(self, m: Match, combo, data: str | None = None):
        """combo is the film's dropdown or a title's EpisodePicker; data is what the picker chose."""
        data = (data or combo.currentData() or "skip").split(":")
        kind = data[0]

        def undo():
            if isinstance(combo, EpisodePicker):
                combo.set_current(combo.property("prev_data") or "skip")
            else:
                combo.setCurrentIndex(combo.property("prev"))

        if m.title_index in self._locked:
            undo()
            return
        extra = None
        if kind == "extra_new":
            dlg = ExtraDialog(self, m.extra_name or (f"Title {m.title_index:02d}"), m.extra_folder)
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.name.text().strip():
                undo()
                return
            extra = (dlg.name.text().strip(), dlg.folder.currentText())
        eps = self.season_eps()
        new = _copy.deepcopy(m)
        if extra is not None:
            new.target, new.extra_name, new.extra_folder = "extra", extra[0], extra[1]
            new.episode, new.episode_end = None, None
        elif kind == "extra":
            new.target, new.episode, new.episode_end = "extra", None, None
        elif kind == "skip":
            new.target = "skip"
        elif kind == "feature":
            new.target, new.episode, new.episode_end = "feature", None, None
        elif kind == "episode":
            season, num = int(data[1]), int(data[2])
            end = int(data[3]) if len(data) > 3 and data[3] else None
            ep = next((e for e in eps if (e.season, e.number) == (season, num)), None)
            new.target, new.episode, new.episode_end = "episode", ep, end
        new.confidence, new.note, new.confirmed, new.source = "manual", "", False, "manual"
        with self.win.lock():
            matches = [new if x.title_index == m.title_index else x for x in self.job.matches]
        self.set_matches(self.job, matches)
        self.fill_table()

    def set_matches(self, job: Job, matches: list[Match]):
        """Hand new matches to the controller, which keeps the ones a copy has already locked."""
        try:
            self.win.ctl.update_matches(job, matches)
        except Exception as exc:
            self.win.error("Couldn't change the match", str(exc),
                           "Try again. If the title is copying, wait for it to finish.")

    def find_show(self):
        q = self.show_combo.currentText().strip()
        q = re.sub(r"\s*\((19|20)\d\d\)\s*$", "", q)
        if not q:
            return
        self.win.run_task("search", lambda _p: self.win.ctl.search_show(q), ok=self.got_hits,
                          title="Couldn't search for the show",
                          next_step="Check the internet connection, then choose Find show again.")

    def got_hits(self, hits, auto: bool = False):
        if self.job is None:
            return
        text = self.show_combo.currentText()
        self.show_combo.blockSignals(True)
        self.show_combo.clear()
        for h in hits:
            extra = f" · {h.network}" if getattr(h, "network", "") else ""
            self.show_combo.addItem(f"{h.name} ({h.year}){extra}" if h.year else f"{h.name}{extra}", h)
        self.show_combo.setCurrentIndex(-1)
        self.show_combo.setEditText(text)
        self.show_combo.blockSignals(False)
        if not hits:
            self.win.error("No show found", f"TVmaze has no show called “{text}”.",
                           "Check the spelling, or try the show's original title.")
            return
        if auto:
            want = (self.job.show or "").lower()
            best = next((h for h in hits if h.name.lower() == want and (not self.job.year or h.year == self.job.year)),
                        hits[0])
            self.choose(best, suggest=not self.job.matches)
        elif len(hits) == 1:
            self.choose(hits[0])
        else:
            self.show_combo.showPopup()

    def hit_chosen(self, i: int):
        h = self.show_combo.itemData(i)
        if h is not None:
            self.choose(h)

    def choose(self, hit, suggest: bool = True):
        job = self.job
        job.show, job.year = hit.name, hit.year
        self.show_ids[job.id] = hit.id
        self.show_combo.setEditText(f"{hit.name} ({hit.year})" if hit.year else hit.name)
        self.fill_fields()
        rip = self.win.rip
        if rip.job is job and rip.disc is not None and rip.cover.source != "disc":
            rip.load_cover(rip.disc, job.season)      # a TVmaze poster can now use the season's own art
        if suggest:
            self.refresh_suggestions()
        else:
            self.load_episodes()
            self.update_counts()

    def season_changed(self, v: int):
        self.job.season = v
        self.suggest_timer.start()

    def disc_changed(self, v: int):
        self.job.disc_number = v or None
        self.suggest_timer.start()

    def film_edited(self, *_):
        self.job.show = self.film_name.text().strip()
        self.job.year = self.year.value() or None
        self.fill_table()

    def dest_changed(self, _i):
        job = self.job
        path = self.dest.currentData()
        if path is None:
            path = QFileDialog.getExistingDirectory(self, "Choose the NAS folder to copy to", job.destination or "/mnt")
            if not path:
                self.fill_dest()
                return
        was_film = self.layout() == "movie"
        job.destination = path
        d = next((d for d in self.win.cfg.destinations if d.path == path), None)
        if d:
            job.media_kind, job.library = d.kind, d.library
        self.fill_dest()
        self.fill_fields()
        if (self.layout() == "movie") != was_film:
            job.matches = []
            self.refresh_suggestions()
        else:
            self.fill_table()

    def refresh_suggestions(self):
        job = self.job
        if job is None:
            return
        sid = self.show_ids.get(job.id)
        if self.layout() != "movie" and sid is None:
            self.fill_table()
            return

        def done(matches):
            self.set_matches(job, list(matches))
            if self.job is job:
                self.fill_table()

        self.win.run_task("suggest", lambda _p: self.win.ctl.suggest_matches(job, sid), ok=done,
                          title="Couldn't match the titles",
                          next_step="Check the show and season, then pick the episodes by hand if it happens again.")
        self.load_episodes()
        self.fill_table()

    def load_episodes(self):
        job = self.job
        sid = self.show_ids.get(job.id)
        fetch = getattr(self.win.ctl, "episodes", None)
        season = None           # every season: the picker searches the whole show
        if fetch is None or sid is None or (sid, season) in self.episodes:
            return

        def done(eps):
            self.episodes[(sid, season)] = list(eps)
            if self.job is job:
                self.fill_table()

        self.win.run_task("episodes", lambda _p: fetch(sid, season), ok=done, title="Couldn't load the episode list",
                          next_step="Check the internet connection. The suggested episodes can still be used.")

    def enter_list(self):
        job = self.job
        sid = self.show_ids.get(job.id) if job else None
        if sid is None:
            return
        read = getattr(self.win.ctl, "episode_list_from_photo", None)
        dlg = EpisodeListDialog(self, self.list_text.get(job.id, ""),
                                (lambda path: read(path, sid)) if read else None)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        text = self.list_text[job.id] = dlg.text.toPlainText()

        def done(matches):
            self.set_matches(job, list(matches))
            if self.job is job:
                self.load_episodes()
                self.fill_table()

        self.win.run_task("suggest", lambda _p: self.win.ctl.match_from_list(job, sid, text), ok=done,
                          title="Couldn't match the episode list",
                          next_step="Check the names against the case, or pick the episodes by hand.")
        self.fill_table()

    def read_cards(self):
        job = self.job
        if job is None or job.id not in self.show_ids:
            return
        job.show_id = self.show_ids[job.id]

        def done(n):
            self.set_saved(f"Matched {plural(n, 'title')} from {'its' if n == 1 else 'their'} title cards."
                           if n else "No title card could be read. Enter the episode list from the case instead.")
            if self.job is job:
                self.fill_table()

        cancel = self.cards_cancel = threading.Event()
        self.win.run_task("cards", lambda p: self.win.ctl.read_cards(job, p, cancel), ok=done,
                          title="Couldn't read the title cards",
                          next_step="Enter the episode list from the case instead.",
                          done=self.refresh_cards)
        self.cards_timer.start()
        self.refresh_cards()
        self.update_actions()

    def stop_cards(self):
        if self.cards_cancel is not None:
            self.cards_cancel.set()
            self.cards_stop.setText("Stopping after the titles being read…")

    def card_status(self) -> dict:
        get = getattr(self.win.ctl, "card_status", None)
        return get(self.job) if get and self.job else {}

    CARD_COLOURS = {"sure": "#6FCF97", "check": ACCENT, "reading": LINK}

    @staticmethod
    def card_text(st) -> tuple[str, str]:
        """(text, colour) for one title's Title card cell."""
        if st is None or st.state == "waiting":
            return "Waiting", "#7D858C"
        if st.state == "reading":
            return f"Reading… {max(0, int(time.monotonic() - st.started))} s", "#A9D1F6"
        if st.state == "read":
            return f"“{st.text}”" if st.text else "Read", ("#8FE0B0" if st.sure else "#F5C27A")
        if st.state == "none":
            return "No title card found", "#7D858C"
        if st.state == "stopped":
            return "Not read", "#7D858C"
        return "Couldn't read it", "#F28B82"

    def card_cell(self, ti: int, st) -> QWidget:
        w = QWidget()
        text = label("")
        bar = colour_bar(LINK, 4)
        bar.setMaximumWidth(150)
        w.setLayout(vbox(None, text, bar, None, spacing=5, margins=(8, 6, 12, 6)))
        self.card_cells[ti] = (text, bar)
        self.set_card_cell(ti, st)
        return w

    def set_card_cell(self, ti: int, st) -> None:
        text, bar = self.card_cells[ti]
        words, colour = self.card_text(st)
        text.setText(QFontMetrics(text.font()).elidedText(words, Qt.TextElideMode.ElideRight, 190))
        text.setToolTip(words)
        mono = st is not None and st.state == "read" and bool(st.text)
        text.setStyleSheet(f"color: {colour}; font-size: 12px;" + (f" font-family: '{self.win.mono}';" if mono else ""))
        if st is not None and st.note:
            text.setToolTip(st.note)
        reading = st is not None and st.state == "reading"
        bar.setVisible(reading)
        if reading:
            # A title takes about 15 s; the bar is an estimate, held short of full until it's done.
            bar.setValue(int(1000 * min(0.95, (time.monotonic() - st.started) / 15)))

    def refresh_cards(self):
        """Update the title-card bar and cells; rebuild the table when a card changed a match."""
        job = self.job
        cards = self.card_status() if job else {}
        active = any(st.state in ("waiting", "reading") for st in cards.values())
        if not active and not self.win.busy("cards"):
            self.cards_timer.stop()
            self.win.cards_progress = None
        if job is None or not cards or self.layout() == "movie":
            self.cards_box.hide()
            self.win.update_taskbar()
            return
        if self.match_sig() != self._sig or self.table.isColumnHidden(6) or any(ti not in self.card_cells for ti in cards):
            self.fill_table()
        else:
            for ti in self.card_cells:
                self.set_card_cell(ti, cards.get(ti))
        n = len(cards)
        states = [cards[ti] for ti in sorted(cards)]
        done = sum(st.state in ("read", "none", "failed", "stopped") for st in states)
        sure = sum(st.state == "read" and st.sure for st in states)
        check = sum(st.state == "read" and not st.sure for st in states)
        none = sum(st.state in ("none", "failed") for st in states)
        reading = sum(st.state == "reading" for st in states)
        waiting = sum(st.state == "waiting" for st in states)
        stopped = sum(st.state == "stopped" for st in states)
        took = [st.seconds for st in states if st.state in ("read", "none") and st.seconds]
        if active:
            self.cards_title.setText(f"Reading title cards · {done} of {n} done")
            left = ""
            if took:
                secs = int(sum(took) / len(took) * (waiting + reading) / max(1, reading or 1))
                left = (f"about {secs // 60} min {secs % 60:02d} s left · " if secs >= 60 else
                        f"about {secs} s left · " if secs >= 5 else "a few seconds left · ")
            self.cards_sub.setText(f"{left}{reading} at a time. Each title's first 90 seconds are read with OCR, "
                                   "and matches update as each card is read.")
            self.win.cards_progress = (done, n)
        elif stopped:
            self.cards_title.setText(f"Stopped after {done - stopped} of {n} titles")
            self.cards_sub.setText("Titles not read keep their earlier match. Choose Read title cards to read them all again.")
        else:
            self.cards_title.setText(f"Read {n} title card{'s' if n != 1 else ''}")
            parts = [f"{sure} matched by their card"]
            if check:
                parts.append(f"{check} need{'s' if check == 1 else ''} a check (see the notes under the table)")
            if none:
                parts.append(f"{none} had no card that could be read")
            self.cards_sub.setText(", ".join(parts) + ".")
        self.cards_stop.setVisible(active and self.win.busy("cards"))
        if not (active and self.win.busy("cards")):
            self.cards_stop.setText("Stop reading")
        segs = []
        for ti in sorted(cards):
            st = cards[ti]
            words, _c = self.card_text(st)
            tip = f"Title {ti}: {words}"
            if st.state == "read":
                segs.append((1.0, self.CARD_COLOURS["sure" if st.sure else "check"], tip))
            elif st.state == "reading":
                segs.append((min(0.95, (time.monotonic() - st.started) / 15), self.CARD_COLOURS["reading"], tip))
            elif st.state in ("none", "failed", "stopped"):
                segs.append((1.0, "#4A5056", tip))
            else:
                segs.append((0.0, "#2A2F34", tip))
        self.cards_bar.set_segments(segs)
        sq = lambda c: f'<span style="color:{c}">■</span>'  # noqa: E731
        self.cards_legend.setText(
            f"{sq('#6FCF97')} Matched: {sure} &nbsp;&nbsp; {sq(ACCENT)} Needs a check: {check} &nbsp;&nbsp; "
            f"{sq(LINK)} Reading: {reading} &nbsp;&nbsp; {sq('#4A5056')} No card: {none + stopped} &nbsp;&nbsp; "
            f"{sq('#2A2F34')} Waiting: {waiting}")
        self.cards_box.show()
        self.win.update_taskbar()

    def save_later(self):
        try:
            self.win.ctl.save_job(self.job)
        except Exception as exc:
            self.win.error("Couldn't save the matches", str(exc), "Check there is free space in your home folder.")
            return
        self.set_saved("Saved. Pick this disc from the queue to finish it.")
        self.win.refresh_jobs()

    def confirm(self):
        job = self.job
        try:
            self.win.ctl.confirm_matches(job)
        except Exception as exc:
            self.win.error("Couldn't confirm the matches", str(exc), "Check there is free space in your home folder.")
            return
        if job.stage == "rip":
            # Ripping now or not yet: a title marked Check is copied once it's ripped.
            if self.win.is_ripping(job):
                self.set_saved("Matches confirmed. Each title is copied once it's ripped." if self.copy_mode() in OVERLAP
                               else "Matches confirmed. The titles are copied when the rip ends.")
            else:
                self.set_saved("Matches confirmed. They're used when the rip starts.")
            self.fill_table()
            return
        left = self.titles_left()
        if left is not None and not left:
            self.set_saved("Every title is already on the NAS.")
            self.fill_table()
            return
        job.stage = "copy"
        job.progress = 0.0
        job.status = "Waiting to copy."
        try:
            self.win.ctl.save_job(job)
        except Exception as exc:
            job.stage = "match"
            self.win.error("Couldn't save the matches", str(exc), "Check there is free space in your home folder.")
            return
        self.win.start_copy(job)


# ---------------------------------------------------------------------------
# Copy page
# ---------------------------------------------------------------------------

class CopyPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.job: Job | None = None

        self.stack = QStackedWidget()
        self.empty = QWidget()
        self.empty.setLayout(vbox(label("Copy to NAS", "h1"),
                                  label("Nothing is being copied. Match a ripped disc's episodes, then choose Confirm "
                                        "and copy.", "muted", wrap=True), None, spacing=8, margins=(24, 24, 24, 24)))
        self.stack.addWidget(self.empty)
        self.stack.addWidget(scroll_page(self._build_files_page()))
        self.stack.addWidget(scroll_page(self._build_film_page()))
        self.setLayout(vbox(self.stack))

    # -- layout: a disc of files ---------------------------------------------
    def _build_files_page(self) -> QWidget:
        win = self.win
        inner = QWidget()
        outer = hbox(spacing=20, margins=(24, 24, 24, 24))
        inner.setLayout(outer)
        col = vbox(spacing=20)
        outer.addLayout(col, 1)

        self.meta = label("", "meta")
        self.h1 = label("Copying to the NAS", "h1")
        self.sub = label("If the network drops, the copy picks up where it stopped.", "muted", wrap=True)
        self.pause_btn = button("Pause copy")
        self.pause_btn.clicked.connect(self.pause_clicked)
        top = hbox(vbox(self.meta, self.h1, self.sub, spacing=6), self.pause_btn, spacing=16)
        top.setStretch(0, 1)
        top.setAlignment(self.pause_btn, Qt.AlignmentFlag.AlignBottom)
        col.addLayout(top)

        prog = Card(margins=(20, 20, 20, 20), spacing=14)
        self.pct = label("0%", "big")
        self.bytes = label("", "soft")
        self.speed = label("", mono=True)
        self.left = label("", "muted")
        row = hbox(self.pct, self.bytes, self.speed, self.left, None, spacing=24)
        row.setAlignment(Qt.AlignmentFlag.AlignBaseline)
        prog.lay.addLayout(row)
        self.bar = QProgressBar()
        self.bar.setProperty("big", "true")
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        prog.lay.addWidget(self.bar)
        prog.lay.addSpacing(6)
        self.chart = LineChart(height=210)
        self.chart.empty_text = "The transfer speed appears here once the copy starts."
        prog.lay.addWidget(self.chart)
        self.route = label("", "meta")
        self.route.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        prog.lay.addWidget(self.route)
        col.addWidget(prog)

        files = Card(margins=(0, 0, 0, 0), spacing=0)
        head = QFrame()
        head.setObjectName("cardHead")
        head.setLayout(hbox(label("Files", "h2"), None, margins=(20, 14, 20, 14)))
        files.lay.addWidget(head)
        self.files_lay = vbox(spacing=0)
        files.lay.addLayout(self.files_lay)
        col.addWidget(files)
        col.addStretch(1)

        side = QWidget()
        side.setFixedWidth(320)
        side_lay = vbox(spacing=16)
        side.setLayout(side_lay)
        # Why a film that could have been copied while it ripped wasn't.
        self.why_after = Card(spacing=10)
        self.why_after.lay.addWidget(label("Why this film is copied after the rip", "h2", wrap=True))
        self.why_after_text = label("", wrap=True)
        self.why_after_text.setStyleSheet("font-size: 13px;")
        self.why_after.lay.addWidget(self.why_after_text)
        self.why_after.hide()
        side_lay.addWidget(self.why_after)

        after = Card(spacing=14)
        after.lay.addWidget(label("After copying", "h2"))
        self.step_nums, self.step_texts = self._steps(after.lay)
        side_lay.addWidget(after)

        local = Card(spacing=6)
        local.lay.addWidget(label("Local copy", "h2"))
        self.keep_group = QButtonGroup(self)
        self.keep_radios = {}
        for key in ("delete", "7days", "keep"):
            rb = QRadioButton("")
            rb.setMinimumHeight(36)
            self.keep_group.addButton(rb)
            self.keep_radios[key] = rb
            local.lay.addWidget(rb)
            rb.toggled.connect(lambda on, key=key: on and self.cleanup_changed(key))
        side_lay.addWidget(local)
        plex, self.server, self.library = self._plex_card(True)
        side_lay.addWidget(plex)
        side_lay.addStretch(1)
        outer.addWidget(side, 0, Qt.AlignmentFlag.AlignTop)
        return inner

    # -- layout: a film copied while it rips -----------------------------------
    def _build_film_page(self) -> QWidget:
        inner = QWidget()
        outer = hbox(spacing=20, margins=(24, 24, 24, 24))
        inner.setLayout(outer)
        col = vbox(spacing=20)
        outer.addLayout(col, 1)
        self.f_meta = label("", "meta")
        self.f_h1 = label("Copying while it rips", "h1")
        self.f_sub = label("A film is one title, so it goes to the NAS as it rips instead of waiting for the rip "
                           "to end.", "muted", wrap=True)
        self.f_stop = button("Stop rip and copy")
        self.f_stop.clicked.connect(lambda: self.job and self.win.stop(self.job))
        top = hbox(vbox(self.f_meta, self.f_h1, self.f_sub, spacing=6), self.f_stop, spacing=16)
        top.setStretch(0, 1)
        top.setAlignment(self.f_stop, Qt.AlignmentFlag.AlignBottom)
        col.addLayout(top)

        card = Card(margins=(20, 20, 20, 20), spacing=16)
        self.f_bars = {}
        for key, name, colour in (("rip", "Ripped", RIP_LINE), ("nas", "On the NAS", NAS_LINE),
                                  ("check", "Checked", CHECK_LINE)):
            sw = QLabel()
            sw.setFixedSize(10, 10)
            sw.setStyleSheet(f"background: {colour}; border-radius: 2px;")
            nm = label(name)
            nm.setStyleSheet("font-weight: 600;")
            txt = label("", mono=True)
            txt.setStyleSheet(f"color: {SOFT}; font-size: 13px;")
            bar = colour_bar(colour, 8)
            card.lay.addLayout(vbox(hbox(sw, nm, None, txt, spacing=8), bar, spacing=6))
            self.f_bars[key] = (txt, bar)
        line = QFrame()
        line.setObjectName("rowLine")
        line.setFixedHeight(1)
        card.lay.addWidget(line)
        self.f_chart = LineChart(height=220)
        self.f_chart.empty_text = "The chart appears once the rip starts."
        card.lay.addWidget(self.f_chart)
        self.f_dip = label("", "field", wrap=True)
        card.lay.addWidget(self.f_dip)
        self.f_route = label("", "meta", wrap=True)
        self.f_route.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        card.lay.addWidget(self.f_route)
        col.addWidget(card)
        col.addStretch(1)

        side = QWidget()
        side.setFixedWidth(320)
        side_lay = vbox(spacing=16)
        side.setLayout(side_lay)
        ends = Card(spacing=14)
        ends.lay.addWidget(label("When the rip ends", "h2"))
        self.f_step_nums, self.f_step_texts = self._steps(ends.lay)
        self.f_gain = label("In Plex about a minute after the rip ends, instead of about 10.", wrap=True)
        self.f_gain.setStyleSheet(f"color: {DONE_TEXT}; font-size: 13px;")
        ends.lay.addWidget(self.f_gain)
        side_lay.addWidget(ends)

        why = Card(spacing=10)
        self.f_why_head = label("Why this disc can be copied while it rips", "h2", wrap=True)
        why.lay.addWidget(self.f_why_head)
        self.f_why_lines = []
        for _ in range(2):
            tick = label("✓")
            tick.setStyleSheet(f"color: {DONE_TEXT};")
            txt = label("", wrap=True)
            txt.setStyleSheet("font-size: 13px;")
            r = hbox(tick, txt, spacing=8)
            r.setStretch(1, 1)
            r.setAlignment(tick, Qt.AlignmentFlag.AlignTop)
            why.lay.addLayout(r)
            self.f_why_lines.append((tick, txt))
        self.f_why_foot = label("If either changes, the film is copied after the rip instead, and this screen says "
                                "why.", "small", wrap=True)
        why.lay.addWidget(self.f_why_foot)
        side_lay.addWidget(why)
        plex, self.f_server, self.f_library = self._plex_card(False)
        side_lay.addWidget(plex)
        side_lay.addStretch(1)
        outer.addWidget(side, 0, Qt.AlignmentFlag.AlignTop)
        return inner

    @staticmethod
    def _steps(lay):
        nums, texts = [], []
        for i in range(3):
            num = label(str(i + 1))
            num.setProperty("num", "off")
            num.setFixedWidth(14)
            txt = label("", wrap=True)
            nums.append(num)
            texts.append(txt)
            r = hbox(num, txt, spacing=12)
            r.setAlignment(num, Qt.AlignmentFlag.AlignTop)
            lay.addLayout(r)
        return nums, texts

    def _plex_card(self, with_link: bool):
        plex = Card(spacing=8)
        plex.lay.addWidget(label("Plex", "h2"))
        server = label("", mono=True)
        library = label("")
        plex.lay.addWidget(label("Server", "small"))
        plex.lay.addWidget(server)
        plex.lay.addSpacing(4)
        plex.lay.addWidget(label("Library", "small"))
        plex.lay.addWidget(library)
        if with_link:
            plex.lay.addSpacing(6)
            plex.lay.addWidget(link("Open in Plex", self.open_plex))
        return plex, server, library

    # -- state --------------------------------------------------------------
    def open(self, job: Job | None):
        self.job = job
        if not job:
            self.stack.setCurrentIndex(0)
            return
        self.refresh()

    def film_layout(self) -> bool:
        job = self.job
        return job is not None and job.copy_mode == "stream" and job.stage in ("rip", "plex", "done")

    def files(self) -> list[RippedFile]:
        return [f for f in self.job.files if f.dest_rel]

    def refresh(self):
        job = self.job
        if not job:
            return
        film = self.film_layout()
        self.stack.setCurrentIndex(2 if film else 1)
        url = self.win.cfg.plex_url
        for srv, lib in ((self.server, self.library), (self.f_server, self.f_library)):
            srv.setText(re.sub(r"^https?://", "", url).rstrip("/") or "Not set. Add it in Settings.")
            lib.setText(job.library or "None. This destination has no library set.")
        if film:
            self.refresh_film()
        else:
            self.refresh_files()

    def rows(self) -> list[tuple[int | None, str, int, TitleProgress | None, RippedFile | None]]:
        """(title, path under the destination, size, its progress, its ripped file) for each file to copy."""
        job = self.job
        prog = {p.title_index: p for p in self.win.progress_of(job)}
        with self.win.lock():
            by_file = {f.title_index: _copy.copy(f) for f in job.files}
        out = []
        if prog:
            try:
                preview = list(self.win.ctl.preview_paths(job))
            except Exception:  # the names can't be built yet; fall back to the files that have one
                preview = []
            for idx, rel in preview:
                p, f = prog.get(idx), by_file.get(idx)
                size = (f.size_bytes if f else 0) or (p.total_bytes if p else 0)
                out.append((idx, rel, size, p, f))
            if out:
                return out
        for f in by_file.values():
            if f.dest_rel:
                out.append((f.title_index, f.dest_rel, f.size_bytes, prog.get(f.title_index), f))
        return out

    def refresh_files(self):
        job, win = self.job, self.win
        layout = win.layout_of(job)
        overlap = win.overlapping(job)
        running_copy = win.is_running(job) and job.stage in ("copy", "plex")
        running = running_copy or overlap
        rows = self.rows()
        total = sum(r[2] for r in rows)
        tp = [r[3] for r in rows if r[3] is not None]
        if overlap and tp:
            copied = sum(p.copied_bytes for p in tp)
            frac = copied / (total or 1)
        else:
            frac = 1.0 if job.stage in ("plex", "done") else (job.progress if job.stage == "copy" else 0.0)
            copied = frac * total
        self.meta.setText(f"{job_name(job, layout).upper()} · {plural(len(rows), 'file')} · {fmt_size(total)}")
        if job.stage == "done":
            self.h1.setText("Copied to the NAS")
            self.sub.setText("Every file was copied and its checksum compared with the local copy.")
        elif job.stage == "plex":
            self.h1.setText("Asking Plex to scan")
            self.sub.setText("The files are on the NAS and checked.")
        elif job.stage == "failed":
            self.h1.setText("The copy stopped")
            self.sub.setText(job.status or "Something went wrong during the copy.")
        elif overlap:
            self.h1.setText("Copying as it rips")
            self.sub.setText("Each title goes to the NAS as soon as it's ripped and matched.")
        elif running:
            self.h1.setText("Copying to the NAS")
            self.sub.setText("If the network drops, the copy picks up where it stopped.")
        elif job.progress > 0 and job.stage == "copy":
            self.h1.setText("Copy paused")
            self.sub.setText("Choose Resume copy to carry on. Files already copied are kept.")
        else:
            self.h1.setText("Ready to copy")
            self.sub.setText("If the network drops, the copy picks up where it stopped.")
        if overlap:
            self.pause_btn.setText("Stop rip and copy")
            set_prop(self.pause_btn, "primary", "false")
            self.pause_btn.setVisible(True)
            self.pause_btn.setEnabled(True)
        elif job.stage == "copy" and running_copy:
            self.pause_btn.setText("Pause copy")
            set_prop(self.pause_btn, "primary", "false")
            self.pause_btn.setVisible(True)
            self.pause_btn.setEnabled(True)
        elif job.stage in ("copy", "failed"):
            self.pause_btn.setText("Resume copy")
            set_prop(self.pause_btn, "primary", "true")
            self.pause_btn.setVisible(True)
            self.pause_btn.setEnabled(not win.busy("copy"))
        else:
            self.pause_btn.setVisible(False)
        self.pct.setText(f"{int(frac * 100)}%")
        self.bytes.setText(f"{fmt_size(copied) if copied else '0 GB'} of {fmt_size(total)}")
        copies = win.samples.get(job.id, {}).get("copy", [])
        rate = copies[-1].rate if copies and running else 0.0
        active = any(p.state == "copying" for p in tp) if overlap else running_copy and job.stage == "copy"
        if rate and active:
            self.speed.setText(fmt_rate(rate, 0))
            self.left.setText(f"{fmt_elapsed((total - copied) / rate)} left" if total > copied else "")
        elif running_copy and job.stage == "copy" and job.status:
            self.speed.setText("")
            self.left.setText(job.status)
        else:
            self.speed.setText("")
            self.left.setText("")
        self.bar.setValue(int(frac * 1000))
        self.fill_chart(copies, total, running)
        folder = show_folder_of(job) or (rows[0][1].split("/")[0] if rows else "")
        src = tilde(job.rip_dir or (os.path.dirname(job.files[0].path) if job.files else ""))
        dst = os.path.join(job.destination, folder) if folder else job.destination
        self.route.setText(f"{src.rstrip('/')}/ → {dst.rstrip('/')}/")
        self.fill_files(rows, frac, running)
        stage_step = {"copy": 0, "plex": 1, "done": 3}.get(job.stage, 0 if overlap else -1)
        lib = job.library
        texts = ["Compare checksums so the NAS copy is known to be identical.",
                 f"Ask Plex to scan the {lib} library." if lib else
                 "Plex isn't asked to scan, because this destination has no library set.",
                 "Check that Plex lists the film." if layout == "movie" else "Check that Plex lists every episode."]
        for i, (num, txt) in enumerate(zip(self.step_nums, self.step_texts)):
            txt.setText(texts[i])
            set_prop(num, "num", "on" if i <= stage_step else "off")
        what = "the film" if layout == "movie" else "every episode"
        labels = {"delete": f"Delete once Plex lists {what}", "7days": "Keep for 7 days, then delete", "keep": "Keep"}
        for key, rb in self.keep_radios.items():
            rb.blockSignals(True)
            rb.setText(labels[key])
            rb.setChecked(win.cfg.cleanup == key)
            rb.blockSignals(False)
        self.why_after_text.setText(job.copy_mode_note)
        self.why_after.setVisible(bool(job.copy_mode_note))

    def fill_chart(self, copies: list[Sample], total: int, live: bool):
        if not copies:
            self.chart.set_data([], [], unit="MB/s", title="Transfer speed", summary="No transfer yet.")
            return
        xs = [s.t for s in copies]
        mbs = [s.rate / 1e6 for s in copies]
        avg = sum(s.rate for s in copies) / len(copies)
        peak = max(s.rate for s in copies)
        last = copies[-1]
        readouts = [f"{fmt_elapsed(s.t)} · {fmt_rate(s.rate, 0)} · {fmt_gb(s.total)} copied" for s in copies]
        x2 = ["0 GB" if i == 0 and s.total < 0.05e9 else fmt_gb(s.total) for i, s in enumerate(copies)]
        low = min(copies, key=lambda s: s.rate)
        dip = f", with a short drop at {fmt_elapsed(low.t)} when the network slowed" if low.rate < 0.6 * avg else ""
        summary = (f"Transfer speed since the copy started: about {fmt_rate(avg, 0)} on average{dip}. "
                   f"{fmt_gb(last.total)} copied of {fmt_gb(total)}.")
        caption = f"{fmt_gb(last.total)} copied of {fmt_gb(total)} · avg {fmt_rate(avg, 0)} · peak {fmt_rate(peak, 0)}" \
                  " · hover for each moment"
        self.chart.set_data(xs, [ChartSeries("Transfer speed", ACCENT, mbs, fill=True,
                                             end_label=f"{fmt_gb(last.total)} copied")],
                            unit="MB/s", ref=(avg / 1e6, "avg", SOFT), readouts=readouts, x2=x2, summary=summary,
                            title="Transfer speed", caption=caption, live=live)

    def fill_files(self, rows, frac: float, running: bool):
        key = tuple(r[1] for r in rows)
        if key != getattr(self, "_rows_key", None):
            # Rows are rebuilt only when the file list changes; progress ticks just update their chips.
            clear_layout(self.files_lay)
            self._rows_key, self._rows = key, []
            for _idx, rel, size, _p, _f in rows:
                row = QFrame()
                row.setObjectName("rowLine")
                name = rel.split("/", 1)[1] if "/" in rel else rel
                n = label(name, mono=True)
                n.setStyleSheet("font-size: 13px;")
                n.setToolTip(rel)
                n.setMinimumWidth(100)
                n.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                sz = label(fmt_size(size), "meta")
                sz.setFixedWidth(72)
                sz.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                state = chip("Waiting", "plain")
                lay = hbox(n, sz, state, spacing=16, margins=(20, 12, 20, 12))
                lay.setStretch(0, 1)
                row.setLayout(lay)
                self.files_lay.addWidget(row)
                self._rows.append(state)
            if not rows:
                e = label("The file names appear once the titles are matched.", "muted")
                e.setContentsMargins(20, 14, 20, 14)
                self.files_lay.addWidget(e)
        total = sum(r[2] for r in rows) or 1
        explicit = any((f is not None and f.state in ("copying", "copied", "verified", "failed"))
                       or (p is not None and p.state not in ("ripped",)) for _i, _r, _s, p, f in rows)
        done_bytes = frac * total
        cum = 0
        for (_idx, _rel, size, p, f), state in zip(rows, self._rows):
            own = (done_bytes - cum) / size if size else 0
            cum += size
            fstate = f.state if f is not None else ""
            pstate = p.state if p is not None else ""
            if fstate == "verified" or pstate == "verified" or self.job.stage in ("plex", "done"):
                text, kind = "Copied and checked", "good"
            elif fstate == "copied" or pstate == "checking":
                text, kind = "Copied, checking", "warn"
            elif fstate == "failed" or pstate == "failed":
                text, kind = "Failed", "bad"
            elif fstate == "copying" or pstate == "copying" or (not explicit and 0 < own < 1):
                if p is not None and pstate == "copying":
                    own = p.copied_bytes / (p.total_bytes or 1)
                pct = int(max(0.0, min(1.0, own)) * 100)
                text, kind = (f"Copying · {pct}%" if running else f"Paused · {pct}%"), "warn"
            elif pstate == "waiting_match":
                text, kind = "Waits for your check", "warn"
            elif pstate == "ripping":
                text, kind = f"Ripping · {int(p.ripped_bytes / (p.total_bytes or 1) * 100)}%", "plain"
            elif pstate == "waiting":
                text, kind = "Waiting to rip", "plain"
            elif pstate == "skipped":
                text, kind = "Skipped", "plain"
            elif not explicit and own >= 1:
                text, kind = "Copied and checked", "good"
            else:
                text, kind = "Waiting", "plain"
            set_chip(state, text, kind)

    def refresh_film(self):
        job, win = self.job, self.win
        ripping = win.is_ripping(job)
        tp = [p for p in win.progress_of(job) if p.state != "skipped"]
        total = sum(p.total_bytes for p in tp) or sum(t.size_bytes for t in job.titles if t.selected) or 1
        ripped = sum(p.ripped_bytes for p in tp)
        on_nas = sum(p.copied_bytes for p in tp)
        checked = sum(p.checked_bytes for p in tp)
        if job.stage in ("plex", "done"):
            ripped = on_nas = checked = total
        n = len(tp) or 1
        name = job.show + (f" ({job.year})" if job.year else "") if job.show else job_name(job, "movie")
        self.f_meta.setText(f"{name.upper()} · {plural(n, 'title')} · {fmt_size(total)}")
        if job.stage == "done":
            self.f_h1.setText("Copied while it ripped")
            self.f_sub.setText("The film is on the NAS and checked.")
        elif job.stage == "plex":
            self.f_h1.setText("Asking Plex to scan")
            self.f_sub.setText("The film is on the NAS and checked.")
        elif ripping:
            self.f_h1.setText("Copying while it rips")
            self.f_sub.setText("A film is one title, so it goes to the NAS as it rips instead of waiting for the rip "
                               "to end.")
        else:
            self.f_h1.setText("Rip stopped")
            self.f_sub.setText(job.status or "The rip stopped before the film was copied.")
        self.f_stop.setVisible(ripping)
        rips = win.samples.get(job.id, {}).get("rip", [])
        copies = win.samples.get(job.id, {}).get("copy", [])
        rate = rips[-1].rate if rips and ripping and ripped < total else 0.0
        rip_txt, bar = self.f_bars["rip"]
        if rate:
            mins = max(1, round((total - ripped) / rate / 60))
            rip_txt.setText(f"{ripped / 1e9:.1f} of {total / 1e9:.1f} GB · {fmt_rate(rate)} · {mins} min left")
        else:
            rip_txt.setText(f"{ripped / 1e9:.1f} of {total / 1e9:.1f} GB" + (" · done" if ripped >= total else ""))
        bar.setValue(int(ripped / total * 1000))
        nas_txt, bar = self.f_bars["nas"]
        gap = max(0, ripped - on_nas)
        nas_txt.setText(f"{on_nas / 1e9:.1f} GB · {gap / 1e9:.1f} GB behind the rip" if ripped < total or gap
                        else f"{on_nas / 1e9:.1f} GB · all of it")
        bar.setValue(int(on_nas / total * 1000))
        chk_txt, bar = self.f_bars["check"]
        chk_txt.setText(f"{checked / 1e9:.1f} GB read back and matching")
        bar.setValue(int(checked / total * 1000))
        self.fill_film_chart(rips, copies, total, ripping)

        try:
            dest = next((rel for _i, rel in win.ctl.preview_paths(job)), "")
        except Exception:
            dest = ""
        with win.lock():
            src = job.files[0].path if job.files else f"{job.rip_dir}/"
        self.f_route.setText(f"{tilde(src)} → {os.path.join(job.destination, dest) if dest else job.destination}")

        lib = job.library
        texts = ["Send the few megabytes MakeMKV rewrites at the start of the file when it finishes.",
                 "Check those parts. The rest was read back and checked as it arrived.",
                 f"Ask Plex to scan the {lib} library." if lib else
                 "Plex isn't asked to scan, because this destination has no library set."]
        step = {"plex": 2, "done": 3}.get(job.stage, 0)
        for i, (num, txt) in enumerate(zip(self.f_step_nums, self.f_step_texts)):
            txt.setText(texts[i])
            set_prop(num, "num", "on" if i <= step else "off")
        langs = job.audio_langs or win.cfg.audio_langs
        names = [LANG_NAMES.get(code, code) for code in langs]
        lang_text = " and ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " and " + names[-1]
        lines = ["The HDR setting is RAW, so nothing rewrites the file after the rip.",
                 f"MakeMKV keeps only {lang_text or 'the chosen'} audio and subtitles during the rip, so there's no "
                 "trimming afterwards."]
        if job.copy_mode_note:
            self.f_why_head.setText("Why this film is copied this way")
            lines = [job.copy_mode_note, ""]
        else:
            self.f_why_head.setText("Why this disc can be copied while it rips")
        for (tick, txt), line in zip(self.f_why_lines, lines):
            txt.setText(line)
            tick.setVisible(bool(line) and not job.copy_mode_note)
            txt.setVisible(bool(line))
        self.f_why_foot.setVisible(not job.copy_mode_note)

    def fill_film_chart(self, rips: list[Sample], copies: list[Sample], total: int, live: bool):
        legend = [("line", RIP_LINE, "Ripped"), ("line", NAS_LINE, "On the NAS"), ("dash", MUTED, "Film size")]
        if not rips:
            self.f_chart.set_data([], [], unit="GB", title="Ripped and on the NAS", legend=legend,
                                  summary="Nothing ripped yet.")
            self.f_dip.setText("")
            return
        xs = sorted({s.t for s in rips} | {s.t for s in copies})
        rx, ry = [s.t for s in rips], [s.total / 1e9 for s in rips]
        cx, cy = [s.t for s in copies], [s.total / 1e9 for s in copies]
        rip_gb = [step_value(rx, ry, x) for x in xs]
        nas_gb = [step_value(cx, cy, x) for x in xs]
        tot = total / 1e9
        readouts = [f"{fmt_elapsed(x)} · ripped {r:.1f} GB · on the NAS {c:.1f} GB" for x, r, c in zip(xs, rip_gb, nas_gb)]
        series = [ChartSeries("Ripped", RIP_LINE, rip_gb, end_label=f"Ripped {rip_gb[-1]:.1f} GB"),
                  ChartSeries("On the NAS", NAS_LINE, nas_gb, end_label=f"On the NAS {nas_gb[-1]:.1f} GB")]
        gaps = [r - c for r, c in zip(rip_gb, nas_gb)]
        streaming = [g for g, r in zip(gaps, rip_gb) if 0 < r < tot]
        typical = sorted(streaming)[len(streaming) // 2] if streaming else 0.0
        worst_i = max(range(len(gaps)), key=lambda i: gaps[i] if rip_gb[i] < tot else -1)
        worst = gaps[worst_i]
        text = f"The NAS copy stays about {typical:.1f} GB behind the rip." if streaming else ""
        if streaming and worst > max(1.0, 2 * typical) and rip_gb[worst_i] < tot:
            a = worst_i
            while a > 0 and gaps[a - 1] > typical * 1.5:
                a -= 1
            b = worst_i
            while b < len(gaps) - 1 and gaps[b] > typical * 1.2:
                b += 1
            text += (f" At {fmt_elapsed(xs[a])} the network slowed, the gap grew to {worst:.1f} GB, and the copy "
                     f"caught up {round(xs[b] - xs[worst_i])} seconds later.")
        self.f_dip.setText(text)
        mins = max(1, round(xs[-1] / 60))
        summary = (f"Ripped and copied over {mins} minutes. Both lines rise together to {rip_gb[-1]:.1f} GB of "
                   f"{tot:.1f} GB; the NAS copy trails by about {typical:.1f} GB" +
                   (f", and briefly by {worst:.1f} GB when the network slowed." if worst > 2 * typical else "."))
        self.f_chart.set_data(xs, series, unit="GB", ref=(tot, f"{tot:.1f} GB · the whole film", MUTED),
                              readouts=readouts, summary=summary, title="Ripped and on the NAS", legend=legend,
                              live=live)

    def pause_clicked(self):
        job = self.job
        if self.win.is_running(job):
            self.win.stop(job)
            self.pause_btn.setEnabled(False)
            self.pause_btn.setText("Stopping…" if self.win.is_ripping(job) else "Pausing…")
        else:
            self.win.start_copy(job)

    def cleanup_changed(self, key: str):
        self.win.cfg.cleanup = key
        try:
            self.win.save_config(self.win.cfg)
        except Exception as exc:
            self.win.error("Couldn't save the setting", str(exc), "Check there is free space in your home folder.")

    def open_plex(self):
        url = self.win.cfg.plex_url
        QDesktopServices.openUrl(QUrl(url.rstrip("/") + "/web/index.html" if url else "https://app.plex.tv/desktop"))


# ---------------------------------------------------------------------------
# History page
# ---------------------------------------------------------------------------

def ago(iso: str) -> str:
    """'2 h ago', 'just now', 'yesterday' for an ISO time."""
    try:
        then = _dt.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    now = _dt.datetime.now(then.tzinfo) if then.tzinfo else _dt.datetime.now()
    mins = int((now - then).total_seconds() // 60)
    if mins < 2:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 24 * 60:
        return f"{mins // 60} h ago"
    return "yesterday" if mins < 48 * 60 else f"{mins // (24 * 60)} days ago"


class NeedsCard(QFrame):
    """A disc that waits for the user: titles to check, failed copies, or matched titles to copy."""

    def __init__(self, page: "QueuePage", item):
        super().__init__()
        self.page, self.item, job = page, item, item.job
        win = page.win
        failed_only = bool(item.failed) and not item.checks
        self.setObjectName("needsFail" if failed_only else "needs")
        lay = vbox(spacing=0, margins=(0, 0, 0, 0))
        self.setLayout(lay)
        head = QWidget()
        name = label(job_name(job, win.layout_of(job)))
        name.setStyleSheet("font-weight: 600;")
        reason = label(item.reason, wrap=True)
        reason.setStyleSheet(f"color: {'#F2B8B5' if failed_only else '#E2D3B5'}; font-size: 13px;")
        texts = vbox(name, reason, spacing=3)
        hl = hbox(texts, None, spacing=16, margins=(18, 14, 18, 14))
        hl.setStretch(0, 1)
        if item.checks or job.stage == "match":
            hl.addWidget(link("Open matches", lambda: win.open_match(job)))
        if any(e.state == "failed" for e in getattr(item, "encodes", [])) and hasattr(win.ctl, "retry_encode"):
            self.retry_av1 = button("Encode AV1 again", small=True)
            self.retry_av1.clicked.connect(lambda: (win.ctl.retry_encode(job), win.refresh_jobs()))
            hl.addWidget(self.retry_av1)
        if item.failed:
            self.retry = button("Retry copy", small=True)
            self.retry.clicked.connect(lambda: page.copy_ready(job))
            hl.addWidget(self.retry)
        elif item.ready_to_copy:
            # With titles still to check, the ready ones can go ahead without them.
            text = f"Copy the {item.ready_to_copy} ready now" if item.checks else \
                f"Copy {plural(item.ready_to_copy, 'title')} now"
            self.copy_btn = button(text, primary=not item.checks, small=True)
            self.copy_btn.clicked.connect(lambda: page.copy_ready(job))
            hl.addWidget(self.copy_btn)
        head.setLayout(hl)
        lay.addWidget(head)
        self.rows: dict[int, tuple[QButtonGroup, list]] = {}
        for row in item.checks:
            lay.addWidget(self.check_row(row))

    def check_row(self, row) -> QWidget:
        w = QFrame()
        w.setObjectName("needsRow")
        ti = label(f"Title {row.title_index}", mono=True)
        ti.setFixedWidth(70)
        read = label(f"Card reads “{row.read}”" if row.read else (row.note or "Needs a check"), wrap=True)
        read.setStyleSheet(f"color: {SOFT}; font-size: 13px;")
        read.setToolTip(row.note)
        group = QButtonGroup(w)
        group.setExclusive(True)
        opts = []
        ol = hbox(spacing=6)
        cur = (row.current.season, row.current.number) if row.current else None
        for ep, why in row.options[:4]:
            b = QPushButton(f"S{ep.season:02d}E{ep.number:02d} · {ep.title}")
            b.setObjectName("option")
            b.setCheckable(True)
            b.setToolTip(why)
            b.setChecked((ep.season, ep.number) == cur)
            group.addButton(b)
            ol.addWidget(b)
            opts.append((b, ep))
        if opts and not any(b.isChecked() for b, _ in opts):
            opts[0][0].setChecked(True)
        confirm = button("Confirm and copy", primary=True, small=True)
        confirm.clicked.connect(lambda _c=False, r=row: self.confirm(r))
        rl = hbox(ti, read, ol, None, confirm, spacing=12, margins=(18, 10, 18, 10))
        rl.setStretch(1, 1)
        w.setLayout(rl)
        self.rows[row.title_index] = (group, opts)
        return w

    def chosen(self, ti: int):
        group, opts = self.rows[ti]
        return next((ep for b, ep in opts if b.isChecked()), None)

    def confirm(self, row):
        ep = self.chosen(row.title_index)
        if ep is None:
            self.page.win.open_match(self.item.job)
            return
        self.page.confirm_title(self.item.job, row.title_index, ep)


ENC_ACTIVE = ("waiting", "encoding", "paused", "checking", "copying")
ENC_STEPS = (("encoding", "Encode AV1"), ("checking", "Check quality"), ("copying", "Copy to the NAS"), ("verified", "In Plex"))


def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


class EncodeCard(QFrame):
    """A disc's AV1 version being made: the current title's encode, its steps, and pause/stop."""

    def __init__(self, page: "QueuePage", job: Job):
        super().__init__()
        self.page, self.job = page, job
        self.setObjectName("encodeCard")
        win = page.win
        self.name = label(f"{job_name(job, win.layout_of(job))} · AV1 version")
        self.name.setStyleSheet("font-weight: 600;")
        self.line = label("", wrap=True)
        self.line.setStyleSheet("color: #D9CCF5; font-size: 13px;")
        self.pause = button("Pause", small=True)
        self.pause.clicked.connect(self.toggle_pause)
        self.stop = link("Stop", self.stop_clicked)
        top = hbox(vbox(self.name, self.line, spacing=3), None, self.stop, self.pause, spacing=14)
        top.setStretch(0, 1)
        self.steps = []
        sl = hbox(spacing=6)
        for _key, name in ENC_STEPS:
            lab = label(name, "small")
            lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sl.addWidget(lab, 1)
            self.steps.append(lab)
        self.left_text = label("", "small")
        self.rate = label("", mono=True)
        self.rate.setStyleSheet("font-size: 12px;")
        self.bar = colour_bar("#B392F0", 6)
        self.size_text = label("", "small")
        self.eta = label("", "small")
        self.foot = label("The remux is already in Plex. The AV1 version is added when it passes the quality check.",
                          "field", wrap=True)
        self.setLayout(vbox(top, sl, hbox(self.left_text, None, self.rate), self.bar,
                            hbox(self.size_text, None, self.eta), self.foot, spacing=10, margins=(18, 16, 18, 16)))
        self.refresh()

    def current(self):
        encs = list(getattr(self.job, "encodes", []))
        return next((e for e in encs if e.state in ("encoding", "paused", "checking", "copying")), None) or \
            next((e for e in encs if e.state == "waiting"), None), encs

    def refresh(self):
        e, encs = self.current()
        if e is None:
            return
        win = self.page.win
        todo = [x for x in encs if x.state in ENC_ACTIVE]
        n_all = len([x for x in encs if x.state != "skipped"])
        k = encs.index(e) + 1
        which = f"title {k} of {n_all} · " if n_all > 1 else ""
        enc_name = {"cpu": "Best quality, on the CPU", "gpu": "Fast, on the GPU"}.get(e.encoder, e.encoder)
        try:
            from . import av1
            qname = av1.QUALITY[e.quality].lower()
        except (ImportError, IndexError, TypeError):
            qname = ""
        pct = int(e.fraction * 100)
        what = {"waiting": "Waiting to encode", "encoding": f"Encoding · {pct}%", "paused": f"Paused at {pct}%",
                "checking": "Checking the quality", "copying": "Copying to the NAS"}[e.state]
        self.line.setText(f"{which}{what} · {enc_name}" + (f" · {qname} quality" if qname else ""))
        self.pause.setText("Resume" if e.state == "paused" else "Pause")
        self.pause.setEnabled(e.state in ("encoding", "paused"))
        order = [key for key, _ in ENC_STEPS]
        at = order.index(e.state) if e.state in order else -1
        for i, lab in enumerate(self.steps):
            on = i == at or (e.state in ("waiting", "paused") and i == 0)
            lab.setStyleSheet(f"background: {'#2C2440' if on else '#1F2226'}; color: {'#D9CCF5' if on else '#7D858C'}; "
                              "border-radius: 6px; padding: 4px 6px;")
        title = next((t for t in self.job.titles if t.index == e.title_index), None)
        total = title.duration_s if title else 0
        self.left_text.setText(f"Encoded {hms(total * e.fraction)} of {hms(total)}" if total else f"Encoded {pct}%")
        self.rate.setText("paused" if e.state == "paused" else (f"{e.fps:.1f} fps" if e.fps else ""))
        self.bar.setValue(int(e.fraction * 1000))
        src = next((f.size_bytes for f in self.job.files if f.title_index == e.title_index), 0)
        self.size_text.setText(f"Size so far {fmt_size(e.size_bytes)} · heading for about {fmt_size(e.projected_bytes)}"
                               + (f" (remux {fmt_size(src)})" if src else "") if e.size_bytes else "")
        eta = e.eta_seconds
        self.eta.setText("" if not eta or e.state != "encoding" else
                         (f"about {int(eta // 3600)} h {int(eta % 3600 // 60):02d} min left" if eta >= 3600
                          else f"about {max(1, int(eta // 60))} min left"))
        if len(todo) > 1:
            self.foot.setText(f"{plural(len(todo) - 1, 'more title')} after this one. The remuxes are already in Plex; "
                              "each AV1 version is added when it passes the quality check.")

    def toggle_pause(self):
        e, _ = self.current()
        ctl = self.page.win.ctl
        if e is not None and e.state == "paused":
            ctl.resume_encode(self.job)
        else:
            ctl.pause_encode(self.job)
        self.refresh()

    def stop_clicked(self):
        if QMessageBox.question(self, "Stop the AV1 version?",
                                "Stops this encode and skips the AV1 version for the rest of this disc. The remux "
                                "stays in Plex.") == QMessageBox.StandardButton.Yes:
            self.page.win.ctl.stop_encode(self.job)
            self.page.win.refresh_jobs()


class SeasonCard(Card):
    COLOURS = {"plex": ("#1F2A22", "#3E6B50", "#8FE0B0", "In Plex"),
               "ripping": ("#2A2419", "#8A6A33", "#F5C27A", "Ripping now"),
               "ripped": ("#15202B", "#2E4A63", "#A9D1F6", "Ripped, not on the NAS yet"),
               "todo": ("#1A1D20", "#2E3338", "#7D858C", "Not ripped yet")}

    def __init__(self, view):
        super().__init__(margins=(18, 16, 18, 16), spacing=12)
        name = label(f"{view.show} · Season {view.season}")
        name.setStyleSheet("font-weight: 600;")
        self.lay.addLayout(hbox(name, label(f"{view.in_plex} of {len(view.episodes)} episodes in Plex", "small"),
                                None, spacing=14))
        grid = QGridLayout()
        grid.setSpacing(6)
        per_row = 13
        for k, e in enumerate(view.episodes):
            bg, border, fg, what = self.COLOURS.get(e.state, self.COLOURS["todo"])
            cell = QLabel(f"E{e.number:02d}" + (f"\nDisc {e.disc}" if e.disc else "\n—"))
            cell.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cell.setMinimumHeight(48)
            cell.setStyleSheet(f"background: {bg}; border: 1px solid {border}; border-radius: 8px; color: {fg}; "
                               "font-size: 12px; padding: 4px 2px;")
            cell.setToolTip(f"S{view.season:02d}E{e.number:02d} · {e.title}: {what}")
            cell.setAccessibleName(cell.toolTip())
            grid.addWidget(cell, k // per_row, k % per_row)
        for c in range(min(per_row, max(1, len(view.episodes)))):
            grid.setColumnStretch(c, 1)
        self.lay.addLayout(grid)
        sq = lambda c: f'<span style="color:{c}">■</span>'  # noqa: E731
        legend = label(f"{sq('#6FCF97')} In Plex &nbsp;&nbsp; {sq(ACCENT)} Ripping now &nbsp;&nbsp; "
                       f"{sq(LINK)} Ripped, not on the NAS yet &nbsp;&nbsp; {sq('#3A4046')} Not ripped yet", "small")
        legend.setTextFormat(Qt.TextFormat.RichText)
        self.lay.addWidget(legend)
        if view.next_text:
            nxt = label(view.next_text, wrap=True)
            nxt.setStyleSheet("color: #CFE3F5; font-size: 13px;")
            self.lay.addWidget(nxt)


class DoneRow(QFrame):
    def __init__(self, page: "QueuePage", item):
        super().__init__()
        self.setObjectName("doneRow")
        job, win = item.job, page.win
        dot = QLabel()
        dot.setFixedSize(8, 8)
        dot.setStyleSheet("background: #6FCF97; border-radius: 4px;")
        name = label(job_name(job, win.layout_of(job)))
        name.setStyleSheet("font-weight: 600;")
        n = len([f for f in job.files if f.state == "verified"])
        what = "film" if win.layout_of(job) == "movie" else "episode"
        size = sum(f.size_bytes for f in job.files if f.state == "verified")
        when = ago(item.done_at)
        line = label(f"In Plex {when} · {job.library or 'not scanned'} · {plural(n, what)}, {fmt_size(size)}"
                     if when else f"In Plex · {plural(n, what)}, {fmt_size(size)}", "small")
        texts = vbox(name, line, spacing=2)
        plex = link("Open in Plex", lambda: page.open_plex(job))
        self.free = button(f"Delete local copy · {fmt_size(item.safe_bytes)}" if item.safe_bytes else "Local copy deleted",
                           small=True)
        self.free.setEnabled(bool(item.safe_bytes))
        self.free.setToolTip("Deletes the files in the local rip folder. The copies on the NAS were read back and match.")
        self.free.clicked.connect(lambda: page.free_local(job))
        hist = link("Move to History", lambda: page.dismiss(job))
        lay = hbox(dot, texts, None, plex, self.free, hist, spacing=14, margins=(16, 10, 16, 10))
        lay.setStretch(1, 1)
        self.setLayout(lay)


class QueuePage(QWidget):
    """What needs the user, what's running, seasons in progress, and what finished today."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.view = None
        self._shape = None
        self.cards: dict[str, QueueCard] = {}
        self.enc_cards: dict[str, EncodeCard] = {}
        # Encodes run in the controller for hours; keep their cards current while the page shows.
        self.tick = QTimer(self)
        self.tick.setInterval(1000)
        self.tick.timeout.connect(self.tick_encodes)
        self.tick.start()
        inner = QWidget()
        col = vbox(spacing=22, margins=(24, 24, 24, 24))
        inner.setLayout(col)
        self.summary = label("", "muted")
        self.note = label("", "small", wrap=True)
        self.note.hide()
        self.eject = QCheckBox("Eject the disc when its rip finishes")
        self.eject.toggled.connect(self.eject_changed)
        head = hbox(vbox(label("Queue", "h1"), self.summary, self.note, spacing=6), None, self.eject, spacing=16)
        col.addLayout(head)
        self.body = vbox(spacing=22)
        col.addLayout(self.body)
        col.addStretch(1)
        self.storage = label("", "small", wrap=True)
        self.free_all = button("", small=True)
        self.free_all.clicked.connect(lambda: self.free_local(None))
        foot = QFrame()
        foot.setObjectName("rowLine")
        fl = hbox(self.storage, None, self.free_all, spacing=16, margins=(0, 14, 0, 0))
        fl.setStretch(0, 1)
        foot.setLayout(fl)
        col.addWidget(foot)
        page = vbox()
        page.addWidget(scroll_page(inner))
        self.setLayout(page)

    # -- building ------------------------------------------------------------
    def refresh(self):
        get = getattr(self.win.ctl, "queue", None)
        if get is None:
            return
        running = {j.id for j in self.win.running.values()}
        try:
            self.view = view = get(list(self.win.job_list), running)
        except Exception as exc:  # shown in the summary rather than as an error box on every refresh
            self.summary.setText(f"Couldn't build the queue: {exc}")
            return
        self.eject.blockSignals(True)
        self.eject.setChecked(bool(getattr(self.win.cfg, "eject_after_rip", False)))
        self.eject.blockSignals(False)
        needs = [i for i in view.items if i.section == "needs"]
        working = [i for i in view.items if i.section == "working"]
        done = [i for i in view.items if i.section == "done"]
        parts = [f"{plural(len(needs), 'disc needs', 'discs need')} you" if needs else "Nothing needs you",
                 f"{len(working)} working" if working else "nothing running", f"{len(done)} done today"]
        self.summary.setText(" · ".join(parts))
        self.win.set_queue_badge(len(needs))
        shape = (tuple((i.job.id, i.section, tuple(r.title_index for r in i.checks), tuple(i.failed),
                        i.ready_to_copy, i.safe_bytes, i.reason,
                        tuple((e.title_index, e.state in ENC_ACTIVE, e.state == "failed")
                              for e in getattr(i, "encodes", []))) for i in view.items),
                 tuple((s.show, s.season, tuple((e.number, e.state) for e in s.episodes), s.next_text)
                       for s in view.seasons))
        if shape != self._shape:
            self._shape = shape
            self.rebuild(needs, working, done, view.seasons)
        else:
            for card in self.cards.values():
                card.job = next((i.job for i in working if i.job.id == card.job.id), card.job)
                card.refresh()
        safe = view.safe_bytes
        self.storage.setText(f"{self.win.cfg.rip_dir} holds {fmt_size(view.local_bytes)}; {fmt_size(safe)} of it is "
                             "already checked on the NAS and can be deleted. The clean-up setting deletes it later "
                             "on its own." if view.local_bytes else f"{self.win.cfg.rip_dir} is empty.")
        self.free_all.setText(f"Delete checked rips now · {fmt_size(safe)}" if safe else "Nothing to delete")
        self.free_all.setEnabled(bool(safe))

    def tick_encodes(self):
        if not self.enc_cards and not any(getattr(i, "encodes", None) for i in (self.view.items if self.view else [])):
            return
        if self.isVisible():
            self.refresh()
        for card in self.enc_cards.values():
            card.refresh()

    def section(self, title: str, colour: str, count: int | None = None) -> QLabel:
        lab = label(f"{title.upper()}" + (f" · {count}" if count is not None else ""))
        lab.setStyleSheet(f"color: {colour}; font-size: 12px; font-weight: 600; letter-spacing: 1px;")
        return lab

    def rebuild(self, needs, working, done, seasons):
        while self.body.count():
            it = self.body.takeAt(0)
            w = it.widget()
            if w is not None:
                w.hide()
                w.deleteLater()
            elif it.layout() is not None:
                clear_layout(it.layout())
        self.cards = {}
        if needs:
            box = vbox(self.section("Needs you", "#F5C27A", len(needs)), spacing=10)
            for i in needs:
                box.addWidget(NeedsCard(self, i))
            self.body.addLayout(box)
        box = vbox(self.section("Working now", "#A9D1F6", len(working)), spacing=10)
        self.enc_cards = {}
        for i in working:
            encoding = any(e.state in ENC_ACTIVE for e in getattr(i, "encodes", []))
            if encoding and not self.win.is_running(i.job):
                card = EncodeCard(self, i.job)
                self.enc_cards[i.job.id] = card
            else:
                card = QueueCard(self.win, i.job)
                self.cards[i.job.id] = card
            box.addWidget(card)
        if not working:
            box.addWidget(label("Nothing is ripping or copying. Put a disc in the drive to start.", "muted"))
        self.body.addLayout(box)
        if seasons:
            box = vbox(self.section("Seasons in progress", MUTED), spacing=10)
            for s in seasons:
                box.addWidget(SeasonCard(s))
            self.body.addLayout(box)
        box = vbox(spacing=10)
        top = hbox(self.section("Done", "#8FE0B0", len(done)),
                   label("Each moves to History a day after it reaches Plex.", "small"), None, spacing=14)
        if done:
            top.addWidget(link("Move all to History", lambda: [self.dismiss(i.job, refresh=False) for i in done]
                               and self.win.refresh_jobs()))
        box.addLayout(top)
        for i in done:
            box.addWidget(DoneRow(self, i))
        if not done:
            box.addWidget(label("Nothing finished in the last day. Older discs are in History.", "muted"))
        self.body.addLayout(box)

    # -- actions -------------------------------------------------------------
    def confirm_title(self, job: Job, ti: int, episode):
        try:
            self.win.ctl.confirm_title(job, ti, episode)
        except Exception as exc:
            self.win.error("Couldn't confirm the match", str(exc), "Open the matches and try again there.")
            return
        if not self.win.is_running(job) and job.stage in ("match", "failed"):
            self.copy_ready(job)
        self.win.refresh_jobs()

    def copy_ready(self, job: Job):
        self.win.start_copy(job, ready_only=True, open_page=False)
        self.win.refresh_jobs()

    def free_local(self, job: Job | None):
        try:
            freed = self.win.ctl.free_local(job)
        except Exception as exc:
            self.win.error("Couldn't delete the local copy", str(exc), "Check the rip folder's permissions.")
            return
        self.note.setText(f"Deleted {fmt_size(freed)} of local rips already checked on the NAS." if freed
                          else "Nothing was deleted: no local rip is checked on the NAS yet.")
        self.note.show()
        self.win.refresh_jobs()
        self.win.update_footer()

    def dismiss(self, job: Job, refresh: bool = True):
        self.win.ctl.dismiss(job)
        if refresh:
            self.win.refresh_jobs()
        return True

    def open_plex(self, job: Job):
        self.win.run_task(f"plex:{job.id}", lambda _p: self.win.ctl.plex_link(job),
                          ok=lambda url: QDesktopServices.openUrl(QUrl(url)), title="Couldn't open Plex",
                          next_step="Check that the Plex server is running.")

    def eject_changed(self, on: bool):
        self.win.cfg.eject_after_rip = on
        try:
            self.win.save_config(self.win.cfg)
        except Exception as exc:
            self.win.error("Couldn't save the setting", str(exc), "Check there is free space in your home folder.")


class SetupDialog(QDialog):
    """First-run setup: check this computer, choose where rips go, and connect Plex. Each step can be
    revisited from Settings → Run the setup again."""

    STEPS = ("Check this computer", "Where rips go", "Plex")

    def __init__(self, win: "MainWindow"):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Set up Disc Shelf")
        self.setMinimumSize(720, 620)
        self.step_labels = [label(f"{i + 1}  {name}", "small") for i, name in enumerate(self.STEPS)]
        self.stack = QStackedWidget()
        self.stack.addWidget(self.page_checks())
        self.stack.addWidget(self.page_folders())
        self.stack.addWidget(self.page_plex())
        self.back = button("Back")
        self.back.clicked.connect(lambda: self.go(self.stack.currentIndex() - 1))
        self.next = button("Next", primary=True)
        self.next.clicked.connect(self.next_clicked)
        self.later = link("Finish later", self.reject)
        self.setLayout(vbox(hbox(*self.step_labels, None, spacing=18), self.stack,
                            hbox(self.later, None, self.back, self.next, spacing=10),
                            spacing=16, margins=(24, 20, 24, 20)))
        self.go(0)
        self.run_checks()

    # -- step 1: checks -----------------------------------------------------
    def page_checks(self) -> QWidget:
        w = QWidget()
        self.checks_box = vbox(spacing=10)
        self.recheck = button("Check again", small=True)
        self.recheck.clicked.connect(self.run_checks)
        w.setLayout(vbox(label("Check this computer", "h2"),
                         label("Disc Shelf needs MakeMKV to read discs, and a few things set up for the drive. "
                               "Anything missing can usually be fixed from here.", "muted", wrap=True),
                         self.checks_box, hbox(None, self.recheck), None, spacing=12))
        return w

    def run_checks(self):
        try:
            from . import system
        except ImportError:
            self.show_checks(None)
            return
        self.recheck.setEnabled(False)
        self.win.run_task("setup-checks", lambda _p: system.checks(self.win.cfg), ok=self.show_checks,
                          done=lambda: self.recheck.setEnabled(True), title="Couldn't check this computer",
                          next_step="Choose Check again.")

    def show_checks(self, checks):
        clear_layout(self.checks_box)
        if checks is None:
            self.checks_box.addWidget(label("This build can't check the computer. Install MakeMKV and carry on.",
                                            "muted", wrap=True))
            return
        for c in checks:
            row = QFrame()
            row.setObjectName("doneRow")
            mark = label("✓" if c.ok else "!")
            mark.setFixedWidth(18)
            mark.setStyleSheet(f"color: {'#6FCF97' if c.ok else ACCENT}; font-weight: 700; font-size: 15px;")
            name = label(c.title)
            name.setStyleSheet("font-weight: 600;")
            text = vbox(name, label(c.detail, "small", wrap=True), spacing=2)
            if not c.ok and c.fix:
                fix = label(c.fix, wrap=True)
                fix.setStyleSheet("color: #F5C27A; font-size: 13px;")
                text.addWidget(fix)
            rl = hbox(mark, text, spacing=12, margins=(14, 10, 14, 10))
            rl.setStretch(1, 1)
            if not c.ok and c.action:
                b = button(self.ACTION_NAMES.get(c.action, "Fix it"), small=True)
                b.clicked.connect(lambda _c=False, a=c.action, b=b: self.run_action(a, b))
                rl.addWidget(b, 0, Qt.AlignmentFlag.AlignTop)
            row.setLayout(rl)
            self.checks_box.addWidget(row)

    ACTION_NAMES = {"install_makemkv": "Install MakeMKV", "load_sg": "Set up drive access",
                    "open_makemkv_key": "Get the free beta key"}

    def run_action(self, action: str, btn):
        from . import system
        btn.setEnabled(False)
        btn.setText("Working…")

        def done(r):
            ok, sentence = r
            if not ok:
                self.win.error("That didn't work", sentence, "Follow the steps in the message, then choose Check again.")
            self.run_checks()

        self.win.run_task(f"setup:{action}", lambda _p: system.run_action(action), ok=done,
                          title="That didn't work", next_step="Choose Check again.")

    # -- step 2: folders ----------------------------------------------------
    def page_folders(self) -> QWidget:
        w = QWidget()
        cfg = self.win.cfg
        self.folder_edits = {}
        rows = vbox(spacing=12)
        for kind, name, hint in (("tv", "TV shows", "for example the Shows folder on your NAS"),
                                 ("movie", "Films", "for example the Movies folder on your NAS")):
            d = cfg.destination(kind)
            edit = QLineEdit(d.path if d else "")
            edit.setPlaceholderText(hint)
            edit.setProperty("mono", "true")
            b = button("Browse")
            b.clicked.connect(lambda _c=False, e=edit, n=name: self.browse(e, n))
            rows.addWidget(field(f"{name} go to", hbox(edit, b, spacing=8)))
            self.folder_edits[kind] = edit
        w.setLayout(vbox(label("Where rips go", "h2"),
                         label("Choose the folders Plex uses for TV shows and films. A NAS share mounted on this "
                               "computer works, as does a local folder. Each disc is ripped to this computer first "
                               "and copied there when it's checked.", "muted", wrap=True),
                         rows, label("More destinations, such as a separate Anime library, can be added in "
                                     "Settings.", "small", wrap=True), None, spacing=12))
        return w

    def browse(self, edit: QLineEdit, name: str):
        path = QFileDialog.getExistingDirectory(self, f"Choose the folder for {name.lower()}",
                                                edit.text() or os.path.expanduser("~"))
        if path:
            edit.setText(path)

    def folders_ok(self) -> bool:
        bad = [k for k, e in self.folder_edits.items() if not e.text().strip() or not os.path.isdir(e.text().strip())]
        for k, e in self.folder_edits.items():
            set_prop(e, "warn", "true" if k in bad else "false")
        if bad:
            self.win.error("Choose both folders", "Each of TV shows and Films needs a folder that exists on this "
                           "computer.", "Use Browse to pick them. A NAS share has to be mounted first.")
        return not bad

    def save_folders(self):
        cfg = self.win.cfg
        for kind, name, library, layout in (("tv", "TV shows", "TV Shows", "tv"), ("movie", "Movies", "Movies", "movie")):
            path = self.folder_edits[kind].text().strip()
            d = cfg.destination(kind)
            if d:
                d.path = path
            else:
                cfg.destinations.append(configmod.Destination(kind, name, path, library, layout))
        self.win.save_config(cfg)
        self.win.settings_saved()

    # -- step 3: Plex ---------------------------------------------------------
    def page_plex(self) -> QWidget:
        w = QWidget()
        self.plex_url = QLineEdit(re.sub(r"^https?://", "", self.win.cfg.plex_url))
        self.plex_url.setPlaceholderText("192.168.1.20:32400")
        self.plex_url.setProperty("mono", "true")
        self.plex_btn = button("Sign in to Plex")
        self.plex_btn.clicked.connect(self.sign_in)
        self.plex_note = label("", "small", wrap=True)
        w.setLayout(vbox(label("Plex", "h2"),
                         label("With Plex connected, each disc is added to the right library as soon as it's "
                               "copied. This is optional: without it, Plex finds new files on its next scan.",
                               "muted", wrap=True),
                         field("Plex server address", self.plex_url), hbox(self.plex_btn, None), self.plex_note,
                         None, spacing=12))
        return w

    def save_plex(self):
        url = self.plex_url.text().strip()
        if url:
            if not re.match(r"^https?://", url):
                url = "http://" + url
            if not re.search(r":\d+$", url.split("//", 1)[1]):
                url += ":32400"
            self.win.cfg.plex_url = url
            self.win.save_config(self.win.cfg)

    def sign_in(self):
        self.save_plex()
        self.win.settings.load()
        self.win.settings.start_sign_in()
        self.plex_note.setText("Approve the code in the browser window that opens. You can finish the setup; "
                               "the sign-in completes on its own, and Settings shows when it has.")

    # -- navigation -------------------------------------------------------------
    def go(self, i: int):
        i = max(0, min(len(self.STEPS) - 1, i))
        self.stack.setCurrentIndex(i)
        for k, lab in enumerate(self.step_labels):
            lab.setStyleSheet(f"color: {TEXT if k == i else MUTED}; font-weight: {600 if k == i else 400};")
        self.back.setVisible(i > 0)
        self.next.setText("Finish" if i == len(self.STEPS) - 1 else "Next")

    def next_clicked(self):
        i = self.stack.currentIndex()
        if i == 1:
            if not self.folders_ok():
                return
            self.save_folders()
        if i == len(self.STEPS) - 1:
            self.save_plex()
            self.win.settings.load()
            self.accept()
            return
        self.go(i + 1)


class HistoryPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        inner = QWidget()
        col = vbox(spacing=20, margins=(24, 24, 24, 24))
        inner.setLayout(col)
        col.addLayout(vbox(label("History", "h1"),
                           label("Discs that finished copying, and any that stopped with an error.", "muted"),
                           spacing=6))
        card = Card(margins=(0, 0, 0, 0), spacing=0)
        self.table = plain_table(["Date", "Disc", "Copied to", "Files", "Result", ""])
        for c, w in enumerate((150, 200, 220, 60, 300, 140)):
            self.table.setColumnWidth(c, w)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.empty = label("No disc has finished yet.", "muted")
        self.empty.setContentsMargins(20, 16, 20, 16)
        card.lay.addWidget(self.table)
        card.lay.addWidget(self.empty)
        col.addWidget(card)
        col.addStretch(1)
        self.setLayout(vbox(scroll_page(inner)))

    def refresh(self):
        jobs = [j for j in self.win.job_list if j.stage in ("done", "failed")]
        t = self.table
        t.setRowCount(0)
        mono = QFont(self.win.mono)
        for job in jobs:
            r = t.rowCount()
            t.insertRow(r)
            when = (job.copied_at or job.updated or job.created or "")[:16].replace("T", " ")
            t.setItem(r, 0, item(when or "—", mono, muted=True))
            t.setItem(r, 1, item(job_name(job, self.win.layout_of(job))))
            d = next((d for d in self.win.cfg.destinations if d.path == job.destination), None)
            t.setItem(r, 2, item(f"{d.label} · {job.destination}" if d else (job.destination or "—")))
            n = sum(1 for f in job.files if f.dest_rel) or len(job.files)
            t.setItem(r, 3, item(str(n), mono))
            ok = job.stage == "done"
            c = chip("Copied and checked" if ok else "Failed", "good" if ok else "bad")
            parts = [c]
            if not ok and job.status:
                s = label(job.status, "small")
                s.setToolTip(job.status)
                s.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
                parts.append(s)
            w = cell(*parts, align_left=ok)
            if not ok:
                w.layout().setStretch(1, 1)
            t.setCellWidget(r, 4, w)
            b = button("Open folder", small=True)
            b.setAccessibleName(f"Open the NAS folder for {job_name(job)}")
            b.clicked.connect(lambda _c=False, job=job: self.open_folder(job))
            t.setCellWidget(r, 5, cell(b, margins=(8, 4, 20, 4)))
            t.setRowHeight(r, 52)
        fit_table(t)
        t.setVisible(bool(jobs))
        self.empty.setVisible(not jobs)

    def open_folder(self, job: Job):
        folder = show_folder_of(job)
        path = os.path.join(job.destination, folder) if folder else job.destination
        if not path or not os.path.isdir(path):
            path = job.destination
        if not path or not os.path.isdir(path):
            self.win.error("Couldn't open the folder", f"The folder {path or '(none)'} isn't there.",
                           "Check that the NAS share is mounted, then try again.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

class DestRow(QFrame):
    def __init__(self, page: "SettingsPage", dest: configmod.Destination, example: str):
        super().__init__()
        self.setObjectName("rowLine")
        self.dest = dest
        name = label(dest.label)
        name.setStyleSheet("font-weight: 600;")
        ex = label(example, "field", wrap=True)
        left = QWidget()
        left.setFixedWidth(130)
        left.setLayout(vbox(name, ex, spacing=4))
        self.path = QLineEdit(dest.path)
        self.path.setProperty("mono", "true")
        self.path.setAccessibleName(f"NAS folder for {dest.label}")
        browse = button("Browse")
        browse.clicked.connect(self.browse)
        pw = QWidget()
        pw.setLayout(hbox(self.path, browse, spacing=8))
        self.library = QComboBox()
        self.library.setEditable(True)
        self.library.setMinimumWidth(200)
        self.library.setAccessibleName(f"Plex library for {dest.label}")
        page.fill_library_combo(self.library, dest.library)
        lay = hbox(left, field("NAS folder", pw), field("Plex library to scan", self.library), spacing=20,
                   margins=(20, 16, 20, 16))
        lay.setStretch(1, 1)
        lay.setAlignment(left, Qt.AlignmentFlag.AlignVCenter)
        self.setLayout(lay)

    def browse(self):
        path = QFileDialog.getExistingDirectory(self, f"Choose the NAS folder for {self.dest.label}",
                                                self.path.text() or "/mnt")
        if path:
            self.path.setText(path)

    def value(self) -> configmod.Destination:
        lib = self.library.currentText().strip()
        if lib == "Don't scan":
            lib = ""
        return configmod.Destination(self.dest.kind, self.dest.label, self.path.text().strip(), lib, self.dest.layout)


class AddDestDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Add a destination")
        self.name = QLineEdit()
        self.name.setPlaceholderText("For example: Concerts")
        self.layout_combo = QComboBox()
        self.layout_combo.addItem("Shows, in season folders", "tv")
        self.layout_combo.addItem("Films, one folder each", "movie")
        buttons = QDialogButtonBox()
        ok = buttons.addButton("Add destination", QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setProperty("primary", "true")
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.setLayout(vbox(field("Name", self.name), field("How files are arranged", self.layout_combo), buttons,
                            spacing=14, margins=(20, 20, 20, 20)))
        self.setMinimumWidth(380)


class SettingsPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.libraries: list[str] = []
        self.signed_in = False
        self.pin: int | None = None
        self.poll_deadline = 0.0
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(2000)
        self.poll_timer.timeout.connect(self.poll)
        self.rows: list[DestRow] = []
        self.policy = _copy.deepcopy(win.cfg.hdr)

        inner = QWidget()
        col = vbox(spacing=20, margins=(24, 24, 24, 24))
        inner.setLayout(col)
        wrap = QWidget()
        wrap.setMaximumWidth(1040)
        wrap.setLayout(col)
        holder = vbox()
        holder.addWidget(wrap)
        inner.setLayout(holder)

        head = hbox(vbox(label("Settings", "h1"),
                         label("Where finished rips go, which Plex library scans them, and how HDR is kept.",
                               "muted", wrap=True), spacing=6), None, link("Run the setup again", win.open_setup),
                    spacing=16)
        head.setStretch(0, 1)
        col.addLayout(head)

        dests = Card(margins=(0, 0, 0, 0), spacing=0)
        head = QFrame()
        head.setObjectName("cardHead")
        head.setLayout(vbox(label("NAS destinations", "h2"),
                            label("Each disc goes to the folder for its type. You can change it for one disc on the "
                                  "Match screen.", "small", wrap=True), spacing=4, margins=(20, 14, 20, 14)))
        dests.lay.addWidget(head)
        self.rows_lay = vbox(spacing=0)
        dests.lay.addLayout(self.rows_lay)
        foot = QFrame()
        foot.setObjectName("rowLine")
        self.free_line = label("", "small", wrap=True)
        add = button("Add a destination")
        add.clicked.connect(self.add_destination)
        fl = hbox(self.free_line, None, add, spacing=20, margins=(20, 14, 20, 14))
        fl.setStretch(0, 1)
        foot.setLayout(fl)
        dests.lay.addWidget(foot)
        col.addWidget(dests)

        hdr = Card(spacing=16)
        self.hdr = HdrStatus()
        hdr.lay.addLayout(hbox(vbox(label("HDR and versions", "h2"),
                                    label("Defaults for 4K UHD discs. You can change them for one disc on the Rip "
                                          "screen.", "small", wrap=True), spacing=4), None, self.hdr.badge,
                               spacing=16))
        hdr.lay.addWidget(self.hdr.box)
        self.keep = QComboBox()
        self.keep.addItem("Keep all of them in one file (HDR10, Dolby Vision, HDR10+)", "all")
        self.keep.addItem("Keep HDR10 only", "hdr10")
        self.keep.setMaximumWidth(460)
        self.keep.currentIndexChanged.connect(self.policy_changed)
        hdr.lay.addWidget(field("When a disc has more than one HDR format", self.keep))
        self.convert = QCheckBox()
        self.convert.toggled.connect(self.policy_changed)
        hdr.lay.addWidget(Option(self.convert, "Convert Dolby Vision to profile 8.1",
                                 "Rewrites the Dolby Vision layer with dovi_tool, without re-encoding the video, so "
                                 "streaming boxes that play profile 8.1 show Dolby Vision instead of falling back to "
                                 "HDR10. On discs with a full enhancement layer, that layer is dropped."))
        self.keep_1080 = QCheckBox()
        hdr.lay.addWidget(Option(self.keep_1080, "Keep a 1080p Blu-ray from the same set as a second version",
                                 "Plex picks it for phones and remote streams, so the NAS doesn't have to convert 4K "
                                 "HDR. Adds about 20–30 GB per film."))
        names = label("Severance (2022) - S01E01 - 2160p.mkv\nSeverance (2022) - S01E01 - 1080p.mkv", "paths")
        names.setStyleSheet(f"background: {SIDE}; border-radius: 8px; padding: 10px 14px; line-height: 190%;")
        hdr.lay.addWidget(field("How the two versions are named, side by side in the same folder", names))
        col.addWidget(hdr)

        copying = Card(spacing=12)
        copying.lay.addWidget(label("Copying", "h2"))
        self.copy_as_ripped = QCheckBox()
        copying.lay.addWidget(Option(self.copy_as_ripped, "Copy each title as soon as it's ripped",
                                     "While the next title rips, the last one goes to the NAS, so a disc is in Plex a "
                                     "few minutes after the rip ends. A title waits if its match needs checking. Turn "
                                     "this off to copy the whole disc after you confirm the matches."))
        self.stream_films = QCheckBox()
        copying.lay.addWidget(Option(self.stream_films, "Copy films while they rip",
                                     "A film is one title, so it's copied to the NAS as MakeMKV writes it and checked "
                                     "as it arrives. It's in Plex about a minute after the rip ends instead of about "
                                     "10. This needs the HDR setting at RAW and MakeMKV choosing the languages during "
                                     "the rip; otherwise the film is copied after the rip, and the Copy screen says "
                                     "why."))
        col.addWidget(copying)

        matching = Card(spacing=12)
        matching.lay.addWidget(label("Matching", "h2"))
        self.read_cards = QCheckBox()
        matching.lay.addWidget(Option(self.read_cards, "Read title cards to match episodes",
                                      "Many shows put the episode's name on screen near the start. Each title's first "
                                      "90 seconds are read with OCR as soon as it's ripped, and a title whose match is "
                                      "only a guess from running times waits about 30 seconds for it. A disc TheDiscDB "
                                      "knows, or a list you enter from the case, always wins."))
        col.addWidget(matching)

        # AV1 versions of 4K discs: a smaller second copy; the remux is always kept.
        av = Card(spacing=14)
        av.lay.addWidget(label("AV1 versions", "h2"))
        av.lay.addWidget(label("A smaller second copy for phones, browsers and streaming away from home. The remux "
                               "from the disc is always kept and stays RAW.", "small", wrap=True))
        self.av1_on = QCheckBox()
        av.lay.addWidget(Option(self.av1_on, "Also make an AV1 version of 4K discs",
                                "Encoded on this computer after the remux is copied, then copied next to it. 1080p "
                                "discs are left as they are."))
        self.av1_box = QWidget()
        ab = vbox(spacing=14)
        self.av1_box.setLayout(ab)
        self.av1_cards: list[ChoiceCard] = []
        encs = QGridLayout()
        encs.setSpacing(12)
        for col_i, (key, name, tech, desc) in enumerate((
                ("cpu", "Best quality, on the CPU", "SVT-AV1 · this computer's processor",
                 "Smallest files for the quality, and the only one that rebuilds film grain. Slow: a 4K film takes "
                 "hours, at low priority."),
                ("gpu", "Fast, on the GPU", "NVENC AV1 · the graphics card",
                 "A 4K film in well under an hour, and the processor stays free. Files are about a third bigger for "
                 "the same quality."))):
            c = ChoiceCard(key, name, tech, desc)
            c.picked.connect(self.pick_encoder)
            self.av1_cards.append(c)
            encs.addWidget(c, 0, col_i)
            encs.setColumnStretch(col_i, 1)
        ab.addLayout(encs)
        self.av1_quality = QSlider(Qt.Orientation.Horizontal)
        self.av1_quality.setRange(0, 4)
        self.av1_quality.setPageStep(1)
        self.av1_quality.setAccessibleName("Quality, from smaller file to closer to the disc")
        self.av1_quality.valueChanged.connect(lambda *_: self.av1_changed())
        self.av1_qname = label("", "small")
        ab.addLayout(hbox(label("Quality"), None, self.av1_qname))
        ab.addWidget(self.av1_quality)
        ab.addLayout(hbox(label("Smaller file", "field"), None, label("Closer to the disc", "field")))
        tiles = QGridLayout()
        tiles.setSpacing(12)
        self.av1_tiles = []
        for i, (cap, sub) in enumerate((("Size of a 2 h 46 min 4K film", "the remux is about 62 GB"),
                                        ("Time to encode it", ""),
                                        ("Extra space on the NAS", "per film, on top of the remux"))):
            t = QFrame()
            t.setObjectName("note")
            big = label("")
            big.setStyleSheet("font-size: 18px; font-weight: 600;")
            small = label(sub, "field")
            t.setLayout(vbox(label(cap, "small"), big, small, spacing=4, margins=(14, 12, 14, 12)))
            tiles.addWidget(t, 0, i)
            tiles.setColumnStretch(i, 1)
            self.av1_tiles.append((big, small))
        ab.addLayout(tiles)
        self.av1_est_note = label("", "field", wrap=True)
        ab.addWidget(self.av1_est_note)
        self.av1_grain = QCheckBox()
        self.av1_grain_opt = Option(self.av1_grain, "Rebuild film grain",
                                    "Grain is removed before encoding and drawn back on playback, which keeps grainy "
                                    "films sharp at a much smaller size. Encoding takes about 1.6 times as long. "
                                    "CPU encoder only.")
        ab.addWidget(self.av1_grain_opt)
        self.av1_grain.toggled.connect(lambda *_: self.av1_changed())
        self.av1_check = QCheckBox()
        ab.addWidget(Option(self.av1_check, "Check the quality before copying",
                            "Scores 6 short clips against the disc (VMAF). Below 93, the AV1 version isn't copied "
                            "and the Queue says so."))
        self.av1_when = QComboBox()
        for key, text in (("after", "Right after the copy, at low priority"), ("idle", "Only when nothing is ripping"),
                          ("night", "Overnight, 1 am to 7 am")):
            self.av1_when.addItem(text, key)
        ab.addWidget(field("When to encode", self.av1_when))
        note = QFrame()
        note.setObjectName("cardsBox")
        nt = label("Plex picks the version each device can play. Many TV boxes can't decode AV1 (the Nvidia Shield and "
                   "Apple TVs before 2026, for example), so they keep playing the 4K remux with Dolby Vision. The AV1 "
                   "version is picked by phones, browsers and remote streams that can play it. HDR10 is kept in it; "
                   "Dolby Vision stays in the remux only.", wrap=True)
        nt.setStyleSheet("color: #CFE3F5; font-size: 13px;")
        note.setLayout(vbox(nt, margins=(14, 12, 14, 12)))
        ab.addWidget(note)
        ab.addWidget(label("How Plex sees the two versions, side by side in the same folder", "field"))
        prev = label("Dune Part Two (2024) - 2160p.mkv          HEVC · Dolby Vision + HDR10\n"
                     "Dune Part Two (2024) - 2160p AV1.mkv      AV1 · HDR10", mono=True)
        prev.setStyleSheet("font-size: 13px; color: #C3C8CD;")
        pv = QFrame()
        pv.setObjectName("note")
        pv.setLayout(vbox(prev, margins=(14, 10, 14, 10)))
        ab.addWidget(pv)
        av.lay.addWidget(self.av1_box)
        self.av1_on.toggled.connect(lambda on: self.av1_box.setVisible(on))
        col.addWidget(av)

        grid = QGridLayout()
        grid.setSpacing(20)
        local = Card(spacing=12)
        local.lay.addWidget(label("Local rip folder", "h2"))
        self.rip_dir = QLineEdit()
        self.rip_dir.setProperty("mono", "true")
        rb = button("Browse")
        rb.clicked.connect(self.browse_rip)
        rw = QWidget()
        rw.setLayout(hbox(self.rip_dir, rb, spacing=8))
        local.lay.addWidget(field("Folder on this computer", rw))
        local.lay.addWidget(label("Discs are ripped here first, so a network drop never leaves a broken file on the NAS.",
                                  "small", wrap=True))
        local.lay.addStretch(1)
        plex = Card(spacing=12)
        plex.lay.addWidget(label("Plex server", "h2"))
        self.plex_url = QLineEdit()
        self.plex_url.setProperty("mono", "true")
        plex.lay.addWidget(field("Address", self.plex_url))
        self.plex_status = label("", "small", wrap=True)
        self.sign_in = button("Sign in to Plex")
        self.sign_in.clicked.connect(self.start_sign_in)
        sl = hbox(self.plex_status, self.sign_in, spacing=12)
        sl.setStretch(0, 1)
        plex.lay.addLayout(sl)
        plex.lay.addStretch(1)
        grid.addWidget(local, 0, 0)
        grid.addWidget(plex, 0, 1)
        col.addLayout(grid)

        self.saved = label("", "small")
        credits = label('Episode data from <a href="https://www.tvmaze.com">TVmaze</a> (CC BY-SA 4.0). Disc contents '
                        'from <a href="https://thediscdb.com">TheDiscDB</a>. Disc Shelf is free software under the '
                        'GPL-3.0-or-later.', "field", wrap=True)
        credits.setOpenExternalLinks(True)
        credits.setTextFormat(Qt.TextFormat.RichText)
        self.credits = credits
        save = button("Save settings", primary=True)
        save.clicked.connect(self.save)
        col.addLayout(hbox(None, self.saved, save, spacing=16))
        col.addWidget(self.credits)
        col.addStretch(1)
        self.setLayout(vbox(scroll_page(inner)))
        self.load()

    def fill_library_combo(self, combo: QComboBox, current: str):
        combo.clear()
        for lib in self.libraries:
            combo.addItem(lib)
        combo.addItem("Don't scan")
        combo.setCurrentText(current or "Don't scan")
        combo.lineEdit().setPlaceholderText("Library name")

    def av1_encoder(self) -> str:
        return next((c.key for c in self.av1_cards if c.checked), "cpu")

    def pick_encoder(self, key: str):
        for c in self.av1_cards:
            c.set_checked(c.key == key)
        self.av1_changed()

    def av1_changed(self):
        """Update the quality name, the grain option and the three estimates (for a 2 h 46 min 4K film)."""
        from . import av1
        enc, q = self.av1_encoder(), self.av1_quality.value()
        self.av1_qname.setText(av1.QUALITY[q])
        self.av1_grain_opt.setEnabled(enc == "cpu")
        s = av1.EncodeSettings(enc, q, self.av1_grain.isChecked() and enc == "cpu")
        est = getattr(self.win.ctl, "av1_estimate", None)
        try:
            size, secs, measured = est(s) if est else av1.Stats(os.devnull).estimate(s, 3840, 9960, int(62e9))
        except Exception:  # noqa: BLE001 -- the estimate is a nicety; a broken stats file shows the table's
            size, secs, measured = av1.Stats(os.devnull).estimate(s, 3840, 9960, int(62e9))
        h, m = divmod(int(secs // 60), 60)
        (sb, _), (tb, ts), (xb, _) = self.av1_tiles
        sb.setText(f"about {size / 1e9:.0f} GB")
        tb.setText(f"about {h} h {m:02d} min" if h else f"about {m} min")
        ts.setText("the processor at low priority" if enc == "cpu" else "the processor stays free")
        xb.setText(f"+{size / 1e9:.0f} GB")
        self.av1_est_note.setText("Measured from your earlier encodes." if measured else
                                  "These are estimates. The first encodes measure the real numbers on your discs, "
                                  "and these figures update.")

    def load(self):
        cfg = self.win.cfg
        clear_layout(self.rows_lay)
        self.rows = []
        for d in cfg.destinations:
            self._add_row(d)
        self.policy = _copy.deepcopy(cfg.hdr)
        self.keep.blockSignals(True)
        self.convert.blockSignals(True)
        self.keep.setCurrentIndex(0 if self.policy.keep == "all" else 1)
        self.convert.setChecked(self.policy.convert_p81)
        self.keep.blockSignals(False)
        self.convert.blockSignals(False)
        self.convert.setEnabled(self.policy.keep == "all")
        self.hdr.update(self.policy)
        self.keep_1080.setChecked(cfg.keep_1080p_version)
        self.copy_as_ripped.setChecked(cfg.copy_as_ripped)
        self.stream_films.setChecked(cfg.stream_films)
        self.read_cards.setChecked(cfg.read_title_cards)
        self.av1_on.setChecked(getattr(cfg, "av1_enabled", False))
        self.av1_box.setVisible(self.av1_on.isChecked())
        enc = getattr(cfg, "av1_encoder", "cpu")
        for c in self.av1_cards:
            c.set_checked(c.key == enc)
        self.av1_quality.setValue(getattr(cfg, "av1_quality", 2))
        self.av1_grain.setChecked(getattr(cfg, "av1_grain", True))
        self.av1_check.setChecked(getattr(cfg, "av1_check", True))
        self.av1_when.setCurrentIndex(max(0, self.av1_when.findData(getattr(cfg, "av1_when", "after"))))
        self.av1_changed()
        self.rip_dir.setText(cfg.rip_dir)
        self.plex_url.setText(re.sub(r"^http://", "", cfg.plex_url))
        self.update_free()
        self.update_plex_status()

    def _add_row(self, d: configmod.Destination):
        names = []
        for j in self.win.job_list:
            if j.media_kind == d.kind and j.show and j.show not in names:
                names.append(j.show)
        example = ", ".join(names[:2]) or ("Show/Season 01/" if d.layout == "tv" else "Film (Year)/")
        row = DestRow(self, d, example)
        self.rows.append(row)
        self.rows_lay.addWidget(row)

    def update_free(self):
        known = [(d, self.win.free_bytes(d.path)) for d in self.win.cfg.destinations]
        known = [(d, f) for d, f in known if f is not None]
        if not known:
            self.free_line.setText("The NAS folders aren't mounted, so their free space isn't known.")
        else:
            d, f = known[0]
            self.free_line.setText(f"The NAS share at {d.path} has {fmt_size(f)} free.")

    def update_plex_status(self, waiting: str = ""):
        if waiting:
            self.plex_status.setText(waiting)
        elif self.signed_in:
            libs = ", ".join(self.libraries)
            self.plex_status.setText(f"Signed in. Libraries found: {libs}." if libs else "Signed in.")
        else:
            self.plex_status.setText("Not signed in. Sign in so Disc Shelf can ask Plex to scan new files.")
        self.sign_in.setText("Sign in again" if self.signed_in else "Sign in to Plex")
        self.sign_in.setEnabled(not self.poll_timer.isActive() and not self.win.busy("plex"))

    def set_libraries(self, libs: list[str]):
        self.libraries = list(libs)
        for row in self.rows:
            cur = row.library.currentText()
            self.fill_library_combo(row.library, "" if cur == "Don't scan" else cur)
        self.update_plex_status()

    def policy_changed(self, *_):
        self.policy.keep = self.keep.currentData()
        self.policy.convert_p81 = self.convert.isChecked()
        self.convert.setEnabled(self.policy.keep == "all")
        self.hdr.update(self.policy)

    def browse_rip(self):
        path = QFileDialog.getExistingDirectory(self, "Choose the local rip folder",
                                                os.path.expanduser(self.rip_dir.text() or "~"))
        if path:
            self.rip_dir.setText(tilde(path))

    def add_destination(self):
        dlg = AddDestDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.name.text().strip():
            return
        name = dlg.name.text().strip()
        kind = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "other"
        taken = {r.dest.kind for r in self.rows}
        base, k = kind, 2
        while kind in taken:
            kind, k = f"{base}-{k}", k + 1
        self._add_row(configmod.Destination(kind, name, "", "", dlg.layout_combo.currentData()))

    def start_sign_in(self):
        def started(r):
            pin, code, url = r
            self.pin = pin
            QDesktopServices.openUrl(QUrl(url))
            self.poll_deadline = time.monotonic() + 300
            self.poll_timer.start()
            self.update_plex_status(f"Approve code {code} in the browser window that opened. Waiting for Plex…")

        self.win.run_task("plex", lambda _p: self.win.ctl.plex_sign_in_start(), ok=started,
                          title="Couldn't start the Plex sign-in",
                          next_step="Check the internet connection, then choose Sign in to Plex again.")
        self.update_plex_status()

    def poll(self):
        if time.monotonic() > self.poll_deadline:
            self.poll_timer.stop()
            self.update_plex_status("Sign-in timed out. Choose Sign in to Plex to try again.")
            return
        if self.win.busy("plexpoll") or self.pin is None:
            return
        pin = self.pin

        def got(ok):
            if ok and self.poll_timer.isActive():
                self.poll_timer.stop()
                self.signed_in = True
                self.update_plex_status()
                self.win.load_libraries()

        self.win.run_task("plexpoll", lambda _p: self.win.ctl.plex_sign_in_poll(pin), ok=got, quiet=True)

    def save(self):
        cfg = self.win.cfg
        dests = [r.value() for r in self.rows]
        missing = [d.label for d in dests if not d.path]
        if missing:
            self.saved.setText(f"Choose a NAS folder for {missing[0]} before saving.")
            return
        rip = self.rip_dir.text().strip()
        if not rip:
            self.saved.setText("Choose a local rip folder before saving.")
            return
        url = self.plex_url.text().strip()
        if url and not re.match(r"^https?://", url):
            url = "http://" + url
        old = _copy.deepcopy(cfg)
        cfg.destinations = dests
        cfg.hdr = _copy.deepcopy(self.policy)
        cfg.keep_1080p_version = self.keep_1080.isChecked()
        cfg.copy_as_ripped = self.copy_as_ripped.isChecked()
        cfg.stream_films = self.stream_films.isChecked()
        cfg.read_title_cards = self.read_cards.isChecked()
        cfg.av1_enabled = self.av1_on.isChecked()
        cfg.av1_encoder = self.av1_encoder()
        cfg.av1_quality = self.av1_quality.value()
        cfg.av1_grain = self.av1_grain.isChecked()
        cfg.av1_check = self.av1_check.isChecked()
        cfg.av1_when = self.av1_when.currentData()
        cfg.rip_dir = rip
        cfg.plex_url = url or cfg.plex_url
        try:
            self.win.save_config(cfg)
        except Exception as exc:
            for f in ("destinations", "hdr", "keep_1080p_version", "copy_as_ripped", "stream_films",
                      "read_title_cards", "av1_enabled", "av1_encoder", "av1_quality", "av1_grain", "av1_check",
                      "av1_when", "rip_dir", "plex_url"):
                setattr(cfg, f, getattr(old, f))
            self.win.error("Couldn't save the settings", str(exc), "Check there is free space in your home folder.")
            return
        self.saved.setText("Settings saved.")
        self.win.settings_saved()



# ---------------------------------------------------------------------------
# Disc info page
# ---------------------------------------------------------------------------

class FlowLayout(QLayout):
    """Lays chips out left to right and wraps them onto new lines."""

    def __init__(self, spacing: int = 8):
        super().__init__()
        self._items = []
        self._sp = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, it):
        self._items.append(it)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, w):
        return self._place(QRect(0, 0, w, 0), dry=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._place(rect, dry=False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _place(self, rect, dry):
        x, y, line = rect.x(), rect.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > rect.right() + 1 and line > 0:
                x, y, line = rect.x(), y + line + self._sp, 0
            if not dry:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._sp
            line = max(line, hint.height())
        return y + line - rect.y()


def chip_flow(texts: list[str], kind: str = "plain", big: bool = False) -> QWidget:
    w = QWidget()
    flow = FlowLayout(8)
    for t in texts:
        c = chip(t, kind)
        if big:
            c.setStyleSheet("font-size: 13px; padding: 6px 12px; border-radius: 15px;")
        flow.addWidget(c)
    w.setLayout(flow)
    return w


def short_duration(d: str) -> str:
    return d[2:] if d.startswith("0:") else d


class DiscInfoPage(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win = win
        self.report = None

        inner = QWidget()
        col = vbox(spacing=20, margins=(24, 24, 24, 24))
        inner.setLayout(col)
        self.drive_combo = QComboBox()
        self.drive_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.drive_combo.setMinimumContentsLength(40)
        self.drive_combo.setAccessibleName("Drive")
        self.read_btn = button("Read disc info", primary=True)
        self.read_btn.clicked.connect(self.read)
        col.addLayout(hbox(label("Drive", "muted"), self.drive_combo, self.read_btn, None))
        self.body = vbox(spacing=20)
        col.addLayout(self.body)
        col.addStretch(1)
        self.setLayout(vbox(scroll_page(inner)))
        self.show_empty("Choose Read disc info to see the formats on the disc in the drive.")

    def fill_drives(self, drives: list[Drive], current_index: int | None):
        self.drive_combo.clear()
        for d in drives:
            disc = f"disc {d.disc_label}" if d.has_disc else "no disc"
            self.drive_combo.addItem(f"{drive_chip_text(d.model)} · {d.device} · {disc}", d)
        if not drives:
            self.drive_combo.addItem("No drive found", None)
        for i in range(self.drive_combo.count()):
            d = self.drive_combo.itemData(i)
            if d is not None and d.index == current_index:
                self.drive_combo.setCurrentIndex(i)

    def update_actions(self):
        w = self.win
        ok = self.drive_combo.currentData() is not None
        self.read_btn.setEnabled(ok and not (w.busy("info") or w.busy("rip") or w.busy("scan") or w.busy("eject")))
        self.read_btn.setText("Reading…" if w.busy("info") else "Read disc info")
        self.read_btn.setToolTip("The drive is busy ripping. Disc info can be read when the rip finishes."
                                 if w.busy("rip") else "")

    def read(self, drive_index: int | None = None):
        if drive_index is None:
            d = self.drive_combo.currentData()
            if d is None:
                return
            drive_index = d.index
        fn = getattr(self.win.ctl, "disc_info", None)
        if fn is None:
            self.show_empty("This version of Disc Shelf can't read disc info yet.")
            return
        idx = drive_index
        self.win.run_task("info", lambda _p: fn(idx), ok=self.load, title="Couldn't read the disc info",
                          next_step="Check that a disc is in the drive and that it isn't ripping, then choose "
                                    "Read disc info again.")

    def show_empty(self, text: str):
        clear_layout(self.body)
        card = Card(margins=(20, 20, 20, 20))
        icon = QLabel()
        icon.setPixmap(disc_pixmap())
        card.lay.addLayout(hbox(icon, vbox(label("Disc info", "h1"), label(text, "muted", wrap=True), spacing=6),
                                spacing=20))
        self.body.addWidget(card)

    def load(self, rep):
        self.report = rep
        clear_layout(self.body)

        # Hero
        hero = Card(margins=(24, 24, 24, 24), spacing=14)
        cover = Cover(150, 200, 150)
        cover.set_cover(rep.cover_path, getattr(rep, "cover_source", "") or ("disc" if rep.cover_path else ""))
        self.cover = cover
        title = label(rep.title or rep.label or "Untitled disc", wrap=True)
        title.setStyleSheet("font-size: 30px; font-weight: 700;")
        meta = label(" · ".join(p for p in (rep.label, rep.disc_type, rep.capacity, rep.layers) if p), "meta",
                     wrap=True)
        meta.setStyleSheet("font-size: 13px;")
        drive = label(drive_chip_text(rep.drive), "small")
        drive_row = hbox(drive, spacing=10)
        drive_row.addWidget(chip("LibreDrive on", "good") if rep.libredrive else chip("LibreDrive off", "bad"))
        drive_row.addStretch(1)
        texts = vbox(meta, title, drive_row, 6, spacing=8)
        if rep.highlights:
            texts.addWidget(chip_flow(rep.highlights, "warn", big=True))
        texts.addStretch(1)
        row = hbox(cover, texts, spacing=24)
        row.setStretch(1, 1)
        row.setAlignment(cover, Qt.AlignmentFlag.AlignTop)
        hero.lay.addLayout(row)
        self.body.addWidget(hero)

        # Languages
        langs = Card(spacing=14)
        langs.lay.addWidget(label("Languages", "h2"))
        g = QGridLayout()
        g.setHorizontalSpacing(24)
        g.setVerticalSpacing(8)
        g.addWidget(label(f"Audio · {len(rep.audio_languages)}", "field"), 0, 0)
        g.addWidget(label(f"Subtitles · {len(rep.subtitle_languages)}", "field"), 0, 1)
        g.addWidget(chip_flow(rep.audio_languages or ["None listed"], "info"), 1, 0)
        g.addWidget(chip_flow(rep.subtitle_languages or ["None listed"], "plain"), 1, 1)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        langs.lay.addLayout(g)
        self.body.addWidget(langs)

        # Titles
        shorts = [t for t in rep.titles if t.kind == "short"]
        head = hbox(label("Titles", "h2"), label(plural(len(rep.titles), "title") + " on this disc", "small"), None,
                    spacing=12)
        self.short_box = QCheckBox(f"Show {plural(len(shorts), 'short clip')}")
        self.short_box.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
        self.short_box.setVisible(bool(shorts))
        head.addWidget(self.short_box)
        self.body.addLayout(head)
        grid = QGridLayout()
        grid.setSpacing(20)
        self.title_cards = []
        for t in rep.titles:
            card = self.title_card(t)
            self.title_cards.append((t, card))
        self.title_grid = grid
        self.short_box.toggled.connect(lambda _on: self.place_titles())
        self.body.addLayout(grid)
        self.place_titles()

        if rep.notes:
            notes = vbox(spacing=6)
            for n in rep.notes:
                notes.addWidget(label(n, "small", wrap=True))
            self.body.addLayout(notes)

    def place_titles(self, *_):
        grid = self.title_grid
        while grid.count():
            grid.takeAt(0)
        show_short = self.short_box.isChecked()
        k = 0
        for t, card in self.title_cards:
            visible = show_short or t.kind != "short"
            card.setVisible(visible)
            if visible:
                grid.addWidget(card, k // 2, k % 2, Qt.AlignmentFlag.AlignTop)
                k += 1
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

    def title_card(self, t) -> Card:
        card = Card(spacing=12)
        src = label(t.source or f"Title {t.index}", mono=True)
        src.setStyleSheet("font-size: 15px; font-weight: 600;")
        kind = chip(KIND_LABEL.get(t.kind, "Unknown"), KIND_CHIP.get(t.kind, "plain"))
        kind.setToolTip("What this title looks like")
        facts = label(f"{short_duration(t.duration)} · {t.size} · {plural(t.chapters, 'chapter')}", "meta")
        card.lay.addLayout(hbox(src, kind, None, facts, spacing=10))
        if t.video.label or t.video.resolution:
            vl = hbox(label("Video", "field"), 8, spacing=8)
            vl.addWidget(label(t.video.label or t.video.resolution, "tech"))
            if t.video.aspect:
                vl.addWidget(label(t.video.aspect, "tech"))
            vl.addStretch(1)
            card.lay.addLayout(vl)
        if t.audio:
            g = QGridLayout()
            g.setHorizontalSpacing(16)
            g.setVerticalSpacing(6)
            for c, h in enumerate(("Language", "Format", "Channels", "Note")):
                g.addWidget(label(h, "field"), 0, c)
            for r, a in enumerate(t.audio, start=1):
                g.addWidget(label(a.lang or "Unknown"), r, 0)
                g.addWidget(label(a.codec or "—", "soft"), r, 1)
                g.addWidget(label(a.channels or "—", mono=True), r, 2)
                g.addWidget(label(a.note, "small"), r, 3)
            g.setColumnStretch(1, 1)
            g.setColumnStretch(3, 1)
            box = QFrame()
            box.setObjectName("note")
            box.setLayout(g)
            g.setContentsMargins(14, 10, 14, 10)
            card.lay.addWidget(label("Audio", "field"))
            card.lay.addWidget(box)
        if t.subtitles:
            card.lay.addWidget(label("Subtitles", "field"))
            card.lay.addWidget(chip_flow([s.lang + (" · forced" if s.forced else "") for s in t.subtitles], "plain"))
        return card

# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    sampled = Signal(str, object)              # job id, Sample: sent from the worker thread

    def __init__(self, controller, save_config=None):
        super().__init__()
        self.ctl = controller
        self.cfg = controller.cfg
        self.save_config = save_config or getattr(controller, "save_config", None) or configmod.save
        self.sans, self.mono = pick_fonts()
        self.tasks: dict[str, Worker] = {}
        self.cancels: dict[str, threading.Event] = {}
        self.running: dict[str, Job] = {}         # "rip" / "copy" -> the job
        self.live: dict[str, Job] = {}            # job id -> the object this window edits
        self.started: dict[str, tuple[float, float]] = {}
        self.drive_list: list[Drive] = []
        self.disc: Disc | None = None
        self.job_list: list[Job] = []
        self.last_error: tuple[str, str] | None = None
        self._boxes = []
        self.samples: dict[str, dict[str, list[Sample]]] = {}   # job id -> kind -> samples, this session only
        self.taskbar = TaskbarProgress(DESKTOP_ID)
        self._taskbar_max: dict[str, float] = {}
        self._attention_seen: set[str] = set()
        self._failed_runs: set[str] = set()
        self._refresh_pending = False
        self._nolock = contextlib.nullcontext()
        self.sampled.connect(self.got_sample)

        self.setWindowTitle("Disc Shelf")
        self.resize(1360, 900)
        self.setMinimumSize(1100, 680)

        root = QWidget()
        root.setObjectName("page")
        self.setCentralWidget(root)
        rl = vbox(spacing=0)
        root.setLayout(rl)

        header = QFrame()
        header.setObjectName("header")
        logo = QLabel()
        logo.setPixmap(logo_pixmap())
        self.activity = label("", "pill")
        self.drive_chip = label("", "pill")
        self.libre_chip = chip("LibreDrive on", "good")
        self.uhd_chip = chip("4K UHD ready", "warn")
        for c in (self.libre_chip, self.uhd_chip):
            c.setStyleSheet("font-size: 13px; padding: 6px 12px; border-radius: 15px;")
        header.setLayout(hbox(logo, label("Disc Shelf", "brand"), None, self.activity, self.drive_chip,
                              self.libre_chip, self.uhd_chip, spacing=8, margins=(24, 14, 24, 14)))
        rl.addWidget(header)

        body = hbox(spacing=0)
        rl.addLayout(body, 1)
        nav = QFrame()
        nav.setObjectName("nav")
        nav.setFixedWidth(240)
        nl = vbox(spacing=4, margins=(14, 20, 14, 20))
        nav.setLayout(nl)
        self.nav_buttons: dict[str, NavButton] = {}
        self.nav_buttons["queue"] = NavButton("Queue")
        self.queue_badge = label("", "badge")
        self.queue_badge.setStyleSheet(f"background: {ACCENT}; color: {ON_ACCENT}; border-radius: 9px; padding: 1px 7px; "
                                       "font-size: 12px; font-weight: 600;")
        self.queue_badge.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.queue_badge.hide()
        self.nav_buttons["queue"].layout().addWidget(self.queue_badge)
        for key, text, num in (("rip", "Rip", "1"), ("match", "Match episodes", "2"), ("copy", "Copy to NAS", "3")):
            self.nav_buttons[key] = NavButton(text, num)
        divider = QFrame()
        divider.setObjectName("divider")
        for key, text in (("info", "Disc info"), ("history", "History"), ("settings", "Settings")):
            self.nav_buttons[key] = NavButton(text)
        for key in ("queue", "rip", "match", "copy", "info"):
            nl.addWidget(self.nav_buttons[key])
        nl.addSpacing(10)
        nl.addWidget(divider)
        nl.addSpacing(10)
        for key in ("history", "settings"):
            nl.addWidget(self.nav_buttons[key])
        nl.addStretch(1)
        foot = QFrame()
        foot.setObjectName("footer")
        self.rip_free = label("", "meta")
        self.rip_free.setStyleSheet(f"color: {TEXT};")
        self.nas_name = label("NAS share", "field")
        self.nas_free = label("", "meta")
        self.nas_free.setStyleSheet(f"color: {TEXT};")
        foot.setLayout(vbox(label("Local rip folder", "field"), self.rip_free, 6, self.nas_name, self.nas_free,
                            spacing=6, margins=(12, 12, 12, 12)))
        nl.addWidget(foot)
        body.addWidget(nav)

        self.stack = QStackedWidget()
        self.queue = QueuePage(self)
        self.rip = RipPage(self)
        self.match = MatchPage(self)
        self.copy = CopyPage(self)
        self.info = DiscInfoPage(self)
        self.history = HistoryPage(self)
        self.settings = SettingsPage(self)
        self.pages = {"queue": self.queue, "rip": self.rip, "match": self.match, "copy": self.copy, "info": self.info,
                      "history": self.history, "settings": self.settings}
        for key, page in self.pages.items():
            self.stack.addWidget(page)
            self.nav_buttons[key].clicked.connect(lambda _c=False, key=key: self.show_page(key))
        body.addWidget(self.stack, 1)

        self.refresh_jobs()
        self.settings.load()
        self.update_footer()
        self.update_header()
        self.show_page("rip")
        if not self.cfg.destinations:
            QTimer.singleShot(400, self.open_setup)       # first run: nowhere to copy to yet
        self.refresh_drives()
        self.load_libraries()
        # A disc is read as soon as it's in the drive: poll the drive's status (cheap, no
        # MakeMKV) and scan when a disc appears; clear the Rip page when it comes out.
        self._auto_failed = False
        self.disc_timer = QTimer(self)
        self.disc_timer.timeout.connect(self.poll_disc)
        self.disc_timer.start(3000)

    # -- tasks --------------------------------------------------------------
    def busy(self, key: str) -> bool:
        return key in self.tasks

    def run_task(self, key: str, fn, ok=None, title: str = "", next_step: str = "", progress=None, done=None,
                 quiet: bool = False) -> None:
        if key in self.tasks:
            return
        w = Worker(fn)
        self.tasks[key] = w
        if progress:
            w.progressed.connect(progress)
        if ok:
            w.succeeded.connect(ok)

        def failed(msg):
            if not quiet:
                self.error(title or "Something stopped", msg, next_step)

        w.failed.connect(failed)

        def finished():
            if self.tasks.get(key) is w:
                del self.tasks[key]
            if done:
                done()
            self.update_actions()
            w.deleteLater()

        w.finished.connect(finished)
        w.start()
        self.update_actions()

    def error(self, title: str, message: str, next_step: str = "") -> None:
        self.last_error = (title, message)
        text = message if not next_step else f"{message}\n\n{next_step}"
        box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, self)
        box.setWindowModality(Qt.WindowModality.WindowModal)
        self._boxes.append(box)
        box.finished.connect(lambda _r, b=box: self._boxes.remove(b) if b in self._boxes else None)
        box.open()

    def update_actions(self):
        self.rip.update_actions()
        self.match.update_actions()
        self.info.update_actions()
        if self.copy.job:
            self.copy.refresh()
        self.settings.update_plex_status() if not self.settings.poll_timer.isActive() else None
        for card in self.rip.queue_cards.values():
            card.refresh()
        self.update_header()

    # -- jobs ---------------------------------------------------------------
    def layout_of(self, job: Job) -> str:
        d = self.cfg.destination(job.media_kind)
        if d and (not job.destination or d.path == job.destination):
            return d.layout
        d2 = next((d for d in self.cfg.destinations if d.path == job.destination), None)
        return (d2 or d).layout if (d2 or d) else ("movie" if job.media_kind == "movie" else "tv")

    def is_running(self, job: Job | None) -> bool:
        return job is not None and any(j.id == job.id for j in self.running.values())

    def default_status(self, job: Job) -> str:
        if job.stage == "rip":
            return "Ready to rip."
        if job.stage == "match":
            n = sum(1 for m in job.matches if m.confidence == "check" and m.target != "skip")
            return f"Ripped. {plural(n, 'title needs', 'titles need')} an episode picked." if n else \
                "Ripped. Check the matches, then copy."
        if job.stage == "copy":
            return "Copy paused. Choose Resume copy to carry on."
        if job.stage == "done":
            return "Copied and checked."
        return ""

    def eta_text(self, job: Job) -> str:
        if job.stage == "done":
            return "done"
        if job.stage == "failed":
            return "failed"
        if job.stage == "match":
            return "needs you"
        if self.is_running(job):
            left = self.time_left(job)
            if left is not None:
                return "under 1 min left" if left < 60 else f"{round(left / 60)} min left"
            t0, f0 = self.started.get(job.id, (time.monotonic(), 0.0))
            df = job.progress - f0
            if df > 0.01:
                rem = (time.monotonic() - t0) * (1 - job.progress) / df
                return "under 1 min left" if rem < 60 else f"{round(rem / 60)} min left"
            return "starting"
        if job.stage == "copy":
            return "paused"
        return ""

    def refresh_jobs(self):
        try:
            jobs = list(self.ctl.jobs())
        except Exception as exc:
            self.error("Couldn't read the job list", str(exc), "Check that your home folder is readable.")
            jobs = []
        merged = [self.live.get(j.id, j) for j in jobs]
        ids = {j.id for j in merged}
        merged = [j for j in self.live.values() if j.id not in ids] + merged
        self.job_list = merged
        self.fill_queue()
        self.history.refresh()
        if hasattr(self, "queue"):
            self.queue.refresh()

    def set_queue_badge(self, n: int) -> None:
        self.queue_badge.setText(str(n))
        self.queue_badge.setVisible(n > 0)
        self.nav_buttons["queue"].setAccessibleName(f"Queue, {plural(n, 'disc needs', 'discs need')} you" if n else "Queue")

    def fill_queue(self):
        """The Rip page's short queue: discs not finished yet. Finished ones are on the Queue page for a day."""
        show = [j for j in self.job_list if j.stage not in ("done", "plex") and not getattr(j, "dismissed", False)][:6]
        ids = [j.id for j in show]
        if ids != list(self.rip.queue_cards):
            for c in self.rip.queue_cards.values():
                c.hide()
                c.deleteLater()
            self.rip.queue_cards = {}
            lay = self.rip.queue_lay
            while lay.count():
                it = lay.takeAt(0)
                if it.widget() and it.widget() is not self.rip.queue_empty:
                    it.widget().deleteLater()
            lay.addWidget(self.rip.queue_empty)
            for j in show:
                card = QueueCard(self, j)
                self.rip.queue_cards[j.id] = card
                lay.addWidget(card)
        else:
            for j in show:
                card = self.rip.queue_cards[j.id]
                card.job = j
                card.refresh()
        self.rip.queue_empty.setVisible(not show)

    def job_progress(self, job: Job, frac: float, text: str):
        if job.id not in self.started:
            self.started[job.id] = (time.monotonic(), frac)
        job.progress = frac
        if text:
            job.status = text
        self.schedule_refresh()

    # -- live progress --------------------------------------------------------
    def lock(self):
        """The controller's job lock, held while reading what a running rip changes."""
        return getattr(self.ctl, "job_lock", None) or self._nolock

    def progress_of(self, job: Job | None) -> list[TitleProgress]:
        if job is None:
            return []
        with self.lock():
            return [_copy.copy(p) for p in job.titles_progress]

    def is_ripping(self, job: Job | None) -> bool:
        return job is not None and self.running.get("rip") is not None and self.running["rip"].id == job.id

    def overlapping(self, job: Job | None) -> bool:
        """Ripping and copying at the same time."""
        return self.is_ripping(job) and job.copy_mode in OVERLAP

    def last_rate(self, job: Job, kind: str) -> float:
        got = self.samples.get(job.id, {}).get(kind)
        return got[-1].rate if got else 0.0

    def expected_copy_mode(self, job: Job) -> str:
        """How the titles will be copied: the controller's choice once the rip starts, the settings' before."""
        if job.copy_mode:
            return job.copy_mode
        if self.layout_of(job) == "movie" and self.cfg.stream_films and self.stream_possible(job)[0]:
            return "stream"
        return "per_title" if self.cfg.copy_as_ripped else "after"

    def stream_possible(self, job: Job) -> tuple[bool, str]:
        try:
            ok, why = self.ctl.stream_possible(job)
        except Exception as exc:  # the check itself failed; say so instead of guessing
            return False, str(exc) or type(exc).__name__
        return bool(ok), why or ""

    def got_sample(self, job_id: str, sample) -> None:
        self.samples.setdefault(job_id, {}).setdefault(sample.kind, []).append(sample)
        self.schedule_refresh()

    def schedule_refresh(self) -> None:
        """Progress and samples arrive many times a second; redraw at most every 80 ms."""
        if not self._refresh_pending:
            self._refresh_pending = True
            QTimer.singleShot(80, self._flush)

    def _flush(self) -> None:
        self._refresh_pending = False
        for job in {j.id: j for j in self.running.values()}.values():
            card = self.rip.queue_cards.get(job.id)
            if card:
                card.refresh()
            card = self.queue.cards.get(job.id)
            if card:
                card.refresh()
            if self.copy.job is job:
                self.copy.refresh()
            if self.match.job is job:
                self.match.refresh_progress()
        self.update_header()
        self.update_taskbar()

    def time_left(self, job: Job) -> float | None:
        """Seconds left from the latest speed readings, or None without any."""
        got = self.samples.get(job.id, {})
        tp = [p for p in self.progress_of(job) if p.state != "skipped"]
        total = sum(p.total_bytes for p in tp)
        if self.is_ripping(job) and got.get("rip") and total:
            rate = sum(s.rate for s in got["rip"][-10:]) / len(got["rip"][-10:])
            ripped = sum(p.ripped_bytes for p in tp)
            if ripped < total and rate > 0:
                # Copying keeps pace with ripping; the last title still has to be copied after the rip.
                last = tp[-1].total_bytes if job.copy_mode in OVERLAP else 0
                crate = self.last_rate(job, "copy") or 110e6
                return (total - ripped) / rate + (last / crate if job.copy_mode == "per_title" else 0)
            return None
        if self.is_running(job) and job.stage == "copy" and got.get("copy"):
            rate = got["copy"][-1].rate
            done = got["copy"][-1].total
            size = sum(f.size_bytes for f in job.files if f.dest_rel) or total
            if rate > 0 and size > done:
                return (size - done) / rate
        return None

    def overall_fraction(self, job: Job) -> float:
        """What the task manager's bar shows: rip and copy together when they overlap."""
        if self.overlapping(job):
            tp = [p for p in self.progress_of(job) if p.state != "skipped"]
            total = sum(p.total_bytes for p in tp)
            if total:
                return (sum(p.ripped_bytes for p in tp) + sum(p.copied_bytes for p in tp)) / (2 * total)
        return job.progress

    def attention(self) -> set[str]:
        """Things that need the user: a title waiting for its match to be checked, or a failed step."""
        keys = set()
        for job in self.live.values():
            for p in self.progress_of(job):
                if p.state in ("waiting_match", "failed"):
                    keys.add(f"{job.id}:{p.title_index}:{p.state}")
            if job.stage == "failed":
                keys.add(f"{job.id}:failed")
        return keys | self._failed_runs

    def update_taskbar(self) -> None:
        keys = self.attention()
        if self.isActiveWindow():
            self._attention_seen |= keys
        urgent = bool(keys - self._attention_seen)
        job = self.running.get("rip") or self.running.get("copy")
        cards = getattr(self, "cards_progress", None)
        self.setWindowTitle(f"Disc Shelf · Reading title cards {cards[0]}/{cards[1]}" if cards else "Disc Shelf")
        if job is None:
            self.taskbar.update(cards[0] / max(1, cards[1]) if cards else None, urgent)
            return
        frac = max(self.overall_fraction(job), self._taskbar_max.get(job.id, 0.0))
        self._taskbar_max[job.id] = frac
        self.taskbar.update(frac, urgent)

    def changeEvent(self, ev):
        if ev.type() == QEvent.Type.ActivationChange and self.isActiveWindow():
            self.update_taskbar()
        super().changeEvent(ev)

    def start_rip(self, job: Job):
        cancel = threading.Event()
        self.cancels[job.id] = cancel
        self.live[job.id] = job
        job.status = "Starting the rip."
        job.progress = 0.0
        try:
            self.ctl.save_job(job)
        except Exception as exc:
            self.error("Couldn't save the job", str(exc), "Check there is free space in your home folder.")
            return
        self.running["rip"] = job
        self.started.pop(job.id, None)
        self.samples[job.id] = {"rip": [], "copy": []}
        self._taskbar_max.pop(job.id, None)
        self.refresh_jobs()

        def ok(_r):
            if cancel.is_set() and job.stage == "rip":
                job.status = "Rip stopped. The titles ripped so far are kept; choose Rip to start again."

        def done():
            self.running.pop("rip", None)
            self.cancels.pop(job.id, None)
            self.started.pop(job.id, None)
            self.refresh_jobs()
            if self.rip.job is job:
                self.rip.load(self.rip.disc, job)
            if self.match.job is job:
                self.match.open(job)
            if self.copy.job is job:
                self.copy.refresh()
            self.update_footer()
            self.update_taskbar()

        def work(p):
            try:
                return self.ctl.rip(job, p, cancel, samples=lambda smp: self.sampled.emit(job.id, smp))
            except Exception:
                self._failed_runs.add(f"{job.id}:rip:{time.monotonic()}")
                raise

        self.run_task("rip", work, ok=ok,
                      progress=lambda f, t: self.job_progress(job, f, t), done=done, title="The rip stopped",
                      next_step="The titles ripped so far are kept in the local rip folder. Fix the problem above, "
                                "then choose Rip again.")
        self.rip.load(self.rip.disc, job)

    def start_copy(self, job: Job, ready_only: bool = False, open_page: bool = True):
        """ready_only copies only titles whose match is ready (from the Queue page), leaving the others to check."""
        if self.busy("copy"):
            self.error("Another copy is running", "Disc Shelf copies one disc at a time.",
                       "This disc can be copied when the current copy finishes.")
            return
        cancel = threading.Event()
        self.cancels[job.id] = cancel
        self.live[job.id] = job
        self.running["copy"] = job
        self.started.pop(job.id, None)
        self.samples.setdefault(job.id, {})["copy"] = []
        self._taskbar_max.pop(job.id, None)
        if job.stage == "failed":
            job.stage = "copy"
        if open_page:
            self.open_copy(job)

        def ok(_r):
            if cancel.is_set() and job.stage == "copy":
                job.status = f"Copy paused at {int(job.progress * 100)}%. Choose Resume copy to carry on."

        def done():
            self.running.pop("copy", None)
            self.cancels.pop(job.id, None)
            self.started.pop(job.id, None)
            self.refresh_jobs()
            self.copy.refresh()
            if self.match.job is job:
                self.match.open(job)
            self.update_footer()
            self.update_taskbar()

        def work(p):
            try:
                go = self.ctl.copy_ready if ready_only else self.ctl.copy
                return go(job, p, cancel, samples=lambda smp: self.sampled.emit(job.id, smp))
            except Exception:
                self._failed_runs.add(f"{job.id}:copy:{time.monotonic()}")
                raise

        self.run_task("copy", work, ok=ok,
                      progress=lambda f, t: self.job_progress(job, f, t), done=done, title="The copy stopped",
                      next_step="Check that the NAS share is mounted, then choose Resume copy. Files already copied "
                                "are kept.")
        self.refresh_jobs()

    def open_setup(self) -> None:
        SetupDialog(self).exec()
        self.refresh_drives()
        self.update_footer()

    def open_folder(self, path: str) -> None:
        if path and os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            self.error("The folder isn't there", f"{path or 'The NAS folder'} can't be found.",
                       "Check that the NAS share is mounted.")

    def reverify(self, job: Job) -> None:
        def done(problems):
            if problems:
                self.error("Some NAS copies don't match", "\n".join(problems),
                           "Choose Retry copy on the Queue page to copy them again.")
            else:
                QMessageBox.information(self, "NAS copies checked",
                                        f"Every copied file of {job_name(job, self.layout_of(job))} matches the rip.")
            self.refresh_jobs()

        self.run_task(f"verify:{job.id}", lambda p: self.ctl.reverify(job, p), ok=done,
                      title="Couldn't check the NAS copies", next_step="Check that the NAS share is mounted.")

    def stop(self, job: Job):
        ev = self.cancels.get(job.id)
        if ev:
            ev.set()

    def open_match(self, job: Job):
        if job.stage != "rip" or self.is_running(job):
            self.live[job.id] = job        # a disc that isn't ripping yet stays off the queue
        self.match.open(job)
        self.show_page("match", auto=False)

    def open_copy(self, job: Job):
        self.copy.open(job)
        self.show_page("copy", auto=False)

    # -- pages --------------------------------------------------------------
    def show_page(self, key: str, auto: bool = True):
        if auto and key == "match":
            cur = self.match.job
            keep = cur is not None and (cur.stage == "match" or (cur.stage == "rip" and self.is_running(cur)))
            if not keep:
                rip = self.running.get("rip")
                job = rip if rip is not None and self.layout_of(rip) != "movie" else \
                    next((j for j in self.job_list if j.stage == "match"), None)
                if job:
                    self.live[job.id] = job
                self.match.open(job)
        if auto and key == "copy" and (self.copy.job is None or not self.is_running(self.copy.job)):
            rip = self.running.get("rip")
            job = self.running.get("copy") or (rip if rip is not None and rip.copy_mode in OVERLAP else None)
            if job is None and self.copy.job is None:
                job = next((j for j in self.job_list if j.stage in ("copy", "plex")), None) \
                    or next((j for j in self.job_list if j.stage == "done"), None)
            if job is not None or self.copy.job is None:
                self.copy.open(job)
        if key == "history":
            self.history.refresh()
        self.stack.setCurrentWidget(self.pages[key])
        for k, b in self.nav_buttons.items():
            b.setChecked(k == key)

    # -- drives, header, footer ----------------------------------------------
    def poll_disc(self):
        check = getattr(self.ctl, "disc_present", None)
        drive = self.rip.drive_combo.currentData()
        if not check or drive is None or not drive.device or self.busy("watch") or self.busy("drives"):
            return
        self.run_task("watch", lambda _p: check(drive.device), ok=lambda present: self.disc_seen(present), quiet=True)

    def disc_seen(self, present):
        if present is None or self.rip.ripping() or self.busy("rip") or self.busy("eject") or self.busy("scan"):
            return
        if not present:
            self._auto_failed = False
            if self.disc is not None:
                self.disc = None
                self.rip.load(None, None)
                self.update_header()
                self.refresh_drives()
            return
        if self.disc is None and not self._auto_failed:
            self.rip.scan(auto=True)

    def refresh_drives(self, poll: bool = True):
        def got(drives):
            self.drive_list = list(drives)
            combo = self.rip.drive_combo
            prev = combo.currentData().index if combo.currentData() else None
            combo.clear()
            for d in self.drive_list:
                disc = f"disc {d.disc_label}" if d.has_disc else "no disc"
                combo.addItem(f"{drive_chip_text(d.model)} · {d.device} · {disc}", d)
            for i in range(combo.count()):
                if combo.itemData(i).index == prev:
                    combo.setCurrentIndex(i)
            if not self.drive_list:
                combo.addItem("No drive found")
                combo.setItemData(0, None)
            cur = combo.currentData()
            self.info.fill_drives(self.drive_list, cur.index if cur else None)
            self.update_header()
            self.update_actions()
            if poll:
                QTimer.singleShot(0, self.poll_disc)    # a disc already in at start-up is read straight away

        self.run_task("drives", lambda _p: self.ctl.drives(), ok=got, title="Couldn't list the drives",
                      next_step="Check that MakeMKV is installed and the drive is plugged in, then choose "
                                "Refresh drives.")

    def update_header(self):
        d = self.rip.drive_combo.currentData()
        if d is None:
            self.drive_chip.setText("No drive found")
        else:
            dot = "#6FCF97" if d.has_disc else MUTED
            self.drive_chip.setText(f'<span style="color:{dot}">●</span>&nbsp; {drive_chip_text(d.model)}')
        self.libre_chip.setVisible(bool(self.disc and self.disc.libredrive))
        self.uhd_chip.setVisible(bool(self.disc and self.disc.uhd))
        job = self.running.get("rip") or self.running.get("copy")
        if job:
            verb = "Ripping" if self.running.get("rip") is job else (
                "Plex is scanning" if job.stage == "plex" else "Copying")
            dot = f'<span style="color:{ACCENT}">●</span>&nbsp; '
            self.activity.setText(f"{dot}{verb} {job_name(job, self.layout_of(job))} · {int(job.progress * 100)}%")
            self.activity.show()
        else:
            self.activity.hide()

    def free_bytes(self, path: str) -> int | None:
        fn = getattr(self.ctl, "free_bytes", None)
        if fn is not None:
            return fn(path)
        p = os.path.expanduser(path or "")
        if not p or not os.path.isdir(p):
            return None
        try:
            return shutil.disk_usage(p).free
        except OSError:
            return None

    def update_footer(self):
        free = self.free_bytes(self.cfg.rip_dir)
        self.rip_free.setText(f"{self.cfg.rip_dir} · {fmt_size(free)} free" if free is not None else
                              f"{self.cfg.rip_dir} · not found")
        d = self.cfg.destination("tv") or (self.cfg.destinations[0] if self.cfg.destinations else None)
        if d:
            self.nas_name.setText(f"NAS {os.path.basename(os.path.normpath(d.path)) or d.label} share")
            f = self.free_bytes(d.path)
            self.nas_free.setText(f"{fmt_size(f)} free" if f is not None else "not mounted")

    def plex_signed_in(self) -> bool:
        fn = getattr(self.ctl, "plex_signed_in", None)
        if fn is not None:
            return bool(fn())
        return bool(configmod.load_secret("plex_token"))

    def load_libraries(self):
        try:
            self.settings.signed_in = self.plex_signed_in()
        except Exception:
            self.settings.signed_in = False
        self.settings.update_plex_status()
        fn = getattr(self.ctl, "plex_libraries", None)
        if fn is None or not self.settings.signed_in:
            return
        self.run_task("libs", lambda _p: fn(), ok=lambda libs: self.settings.set_libraries(list(libs)), quiet=True)

    def settings_saved(self):
        self.rip.fill_destinations()
        self.rip.update_stream_note()
        self.update_footer()
        self.settings.update_free()

    def closeEvent(self, event):
        self.taskbar.clear()
        for ev in self.cancels.values():
            ev.set()
        self.settings.poll_timer.stop()
        for w in list(self.tasks.values()):
            w.wait(5000)
        super().closeEvent(event)


def make_app(argv=None) -> QApplication:
    app = QApplication.instance() or QApplication(list(argv or sys.argv))
    # The names the desktop matches the window against its launcher entry with, so the
    # task manager groups and pins it as "Disc Shelf", not "python3".
    app.setApplicationName(DESKTOP_ID)
    app.setDesktopFileName(DESKTOP_ID)
    app.setApplicationDisplayName("Disc Shelf")
    app.setWindowIcon(QIcon.fromTheme(DESKTOP_ICON))
    if not app.property("disc_shelf_themed"):
        apply_theme(app)
        app.setProperty("disc_shelf_themed", True)
    return app


def run_gui(controller, argv=None) -> int:
    app = make_app(argv)
    win = MainWindow(controller)
    win.show()
    # Let Python signal handlers run while Qt owns the main thread, so Ctrl+C and SIGTERM work.
    keepalive = QTimer()
    keepalive.timeout.connect(lambda: None)
    keepalive.start(500)
    return app.exec()


def _main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="python3 -m discshelf.gui", description="Open the Disc Shelf window.")
    ap.add_argument("--fake", action="store_true", help="use sample discs instead of a real drive")
    args, rest = ap.parse_known_args(argv[1:])
    if args.fake:
        from .fake import FakeController
        ctl = FakeController(configmod.Config())
    else:
        from .jobs import JobStore
        from .pipeline import Controller
        ctl = Controller(configmod.load(), JobStore())
        ctl.recover_titles()       # as scripts/disc_shelf.py does, so either way of starting resumes the same work
        ctl.recover_encodes()
    return run_gui(ctl, [argv[0], *rest])


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
