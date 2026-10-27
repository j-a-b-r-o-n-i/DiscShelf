"""The searchable episode picker on the Match page.

A button that shows the chosen episode and opens a popup with a search field, season
buttons and a scrolling list of every episode. When the page says which episodes are taken
(by another title of this disc, another disc of the season, or a file in the library), the
disc's own season is listed as "Free in season N" first and "Taken in season N" after, each
taken one saying where; taken episodes can still be picked. A volume DVD can list a whole show (278
episodes of Adventure Time), so the list scrolls inside a fixed-height box and the search
field stays at the top.

This module imports only PySide6 and the models, so gui.py can import it. Its stylesheet
rules are in PICKER_STYLE, which gui.py appends to its own.

Choices are the same data strings the old episode combo used:
"episode:<season>:<number>:" (or with the last episode number of a double episode after
the final colon), "extra", "extra_new", "skip".
"""
from __future__ import annotations

import re
import unicodedata

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

from .models import Episode

# The app's palette (the same values as gui.py's constants).
BG, SIDE, PANEL, RAISED = "#111315", "#15181B", "#1A1D20", "#23272B"
BORDER, FIELD, ROWLINE = "#2E3338", "#3A4046", "#262A2E"
TEXT, SOFT, MUTED = "#E9EBED", "#C3C8CD", "#A2A9B0"
ACCENT, LINK = "#F0A23B", "#7CB8F0"
POP_BG = "#1E2226"           # the popup, a step above the panels
HINT = "#7D858C"             # the key hint line; only used for 12px help text
DISABLED = "#8A9198"
MARK_BG, MARK_TEXT = "#4A3A1C", "#F5C27A"      # the matched part of a name
CURRENT_BG = "#2A2419"       # the row already chosen for this title
FOCUS_BG = "#2E3338"         # the row the arrow keys are on
HOVER_BG = RAISED

POPUP_WIDTH = 420
LIST_MAX_H = 300

PICKER_STYLE = """
QPushButton#episodePicker { min-height: 40px; padding: 0 12px; border-radius: 8px; border: 1px solid #3A4046; background: #15181B; text-align: left; }
QPushButton#episodePicker:hover { background: #1D2125; }
QPushButton#episodePicker:focus { border-color: #A2A9B0; }
QPushButton#episodePicker[warn="true"] { border-color: #F0A23B; }
QPushButton#episodePicker[open="true"] { border-color: #F0A23B; }
QPushButton#episodePicker:disabled { border-color: #2E3338; background: #15181B; }
QFrame#pickerPopup { background: #1E2226; border: 1px solid #3A4046; border-radius: 10px; }
QFrame#pickerTop { background: transparent; border: none; border-bottom: 1px solid #2E3338; border-radius: 0; }
QFrame#pickerFoot { background: transparent; border: none; border-top: 1px solid #2E3338; border-radius: 0; }
QLineEdit#pickerSearch { min-height: 38px; padding: 0 4px; border-radius: 8px; border: 1px solid #F0A23B; background: #15181B; color: #E9EBED; }
QPushButton#seasonChip { min-height: 26px; max-height: 26px; min-width: 14px; padding: 0 6px; border-radius: 13px; border: 1px solid #3A4046; background: transparent; color: #C3C8CD; font-size: 12px; }
QPushButton#seasonChip:hover { border-color: #5A6168; background: transparent; }
QPushButton#seasonChip:checked { border-color: #F0A23B; background: #2A2419; color: #F5C27A; }
QPushButton#pickerLink { min-height: 0; padding: 2px 0; border: none; background: transparent; color: #7CB8F0; font-size: 13px; }
QPushButton#pickerLink:hover { color: #A9D1F6; background: transparent; }
QPushButton#pickerLink:focus { color: #A9D1F6; text-decoration: underline; }
QListWidget#pickerList { background: transparent; border: none; outline: none; padding: 0; }
QListWidget#pickerList QScrollBar:vertical { background: transparent; width: 10px; }
QLabel#pickerCount { color: #A2A9B0; font-size: 13px; }
QLabel#pickerHint { color: #7D858C; font-size: 12px; }
"""

# ---------------------------------------------------------------------------
# Search (no Qt)
# ---------------------------------------------------------------------------

_APOSTROPHES = "'’ʼ`"
_CODE = re.compile(r"^s?(\d{1,2})(?:e|x)(\d{1,3})$")
_SEASON = re.compile(r"^s(\d{1,2})$")


def fold(text: str) -> tuple[str, list[int]]:
    """Lower-case text with accents dropped, apostrophes removed and other punctuation turned
    into spaces, plus, for each character of the result, its index in the original text
    (so a match can be highlighted in the original)."""
    out, where = [], []
    for i, ch in enumerate(text):
        if ch in _APOSTROPHES:
            continue
        for c in unicodedata.normalize("NFKD", ch):
            if unicodedata.combining(c):
                continue
            c = c.lower()
            out.append(c if c.isalnum() else " ")
            where.append(i)
    return "".join(out), where


def query_words(q: str) -> list[str]:
    return fold(q)[0].split()


def episode_hits(ep: Episode, words: list[str], folded_title: str | None = None) -> bool:
    """Every word must match: "s3e8", "3x8" or "s03e08" is that season and episode, "s3" is
    that season, a bare number is the episode number, and anything else must appear in the
    name (any order, ignoring case, accents and punctuation)."""
    name = fold(ep.title)[0] if folded_title is None else folded_title
    for w in words:
        m = _CODE.match(w)
        if m:
            if (ep.season, ep.number) != (int(m.group(1)), int(m.group(2))):
                return False
            continue
        m = _SEASON.match(w)
        if m:
            if ep.season != int(m.group(1)):
                return False
            continue
        if w.isdigit():
            if ep.number != int(w):
                return False
            continue
        if w not in name:
            return False
    return True


def highlight_spans(title: str, words: list[str]) -> list[tuple[int, int]]:
    """(start, end) character ranges of title to highlight for the name words of a search."""
    folded, where = fold(title)
    spans = []
    for w in words:
        if w.isdigit() or _CODE.match(w) or _SEASON.match(w):
            continue
        i = folded.find(w)
        if i >= 0:
            spans.append((where[i], where[i + len(w) - 1] + 1))
    spans.sort()
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]))
        else:
            merged.append((a, b))
    return merged


def ep_code(season: int, number: int, end: int | None = None) -> str:
    code = f"S{season:02d}E{number:02d}"
    return f"{code}–E{end:02d}" if end else code


def ep_data(ep: Episode, end: int | None = None) -> str:
    return f"episode:{ep.season}:{ep.number}:{end or ''}"


def parse_data(data: str) -> tuple[int, int, int | None] | None:
    """(season, number, episode_end) for an "episode:…" data string, else None."""
    parts = (data or "").split(":")
    if parts[0] != "episode" or len(parts) < 3:
        return None
    try:
        return int(parts[1]), int(parts[2]), (int(parts[3]) if len(parts) > 3 and parts[3] else None)
    except ValueError:
        return None


def count_text(n: int, q: str, season: int | None) -> str:
    where = f" in season {season}" if season else ""
    if query_words(q):
        verb = "matches" if n == 1 else "match"
        return f"{n} episode{'' if n == 1 else 's'}{where} {verb} “{q.strip()}”"
    return f"{n} episode{'' if n == 1 else 's'}{where}"


# ---------------------------------------------------------------------------
# Fonts and icons
# ---------------------------------------------------------------------------

_MONO: str | None = None


def mono_family() -> str:
    global _MONO
    if _MONO is None:
        _MONO = "IBM Plex Mono" if "IBM Plex Mono" in set(QFontDatabase.families()) else \
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).family()
    return _MONO


def mono_font(base: QFont, px: int = 13) -> QFont:
    f = QFont(base)
    f.setFamily(mono_family())
    f.setPixelSize(px)
    return f


def search_icon(size: int = 15, colour: str = MUTED) -> QPixmap:
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size / 24, size / 24)
    pen = QPen(QColor(colour), 2.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawEllipse(4, 4, 14, 14)
    p.drawLine(16, 16, 20.5, 20.5)
    p.end()
    return pm


def cross_icon(size: int = 14, colour: str = MUTED) -> QPixmap:
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(QColor(colour), 1.6)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    m = size * 0.28
    p.drawLine(m, m, size - m, size - m)
    p.drawLine(size - m, m, m, size - m)
    p.end()
    return pm


def draw_tick(p: QPainter, r: QRect, colour: str = ACCENT) -> None:
    """A check mark drawn as lines; the app's fonts have no ✓ glyph."""
    pen = QPen(QColor(colour), 2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    cx, cy = r.center().x(), r.center().y()
    p.drawPolyline([QPointF(cx - 5, cy), QPointF(cx - 1.5, cy + 3.5), QPointF(cx + 5, cy - 4)])


# ---------------------------------------------------------------------------
# A layout that wraps the season buttons onto more lines when a show has many seasons
# ---------------------------------------------------------------------------

class FlowLayout(QLayout):
    def __init__(self, parent=None, spacing: int = 5):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, it):  # noqa: N802 -- Qt's name
        self._items.append(it)

    def count(self):
        return len(self._items)

    def itemAt(self, i):  # noqa: N802
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):  # noqa: N802
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, w):  # noqa: N802
        return self._place(QRect(0, 0, w, 0), move=False)

    def setGeometry(self, r):  # noqa: N802
        super().setGeometry(r)
        self._place(r, move=True)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        return s

    def _place(self, r: QRect, move: bool) -> int:
        x, y, line = r.x(), r.y(), 0
        for it in self._items:
            hint = it.sizeHint()
            if x + hint.width() > r.right() + 1 and line:
                x, y, line = r.x(), y + line + self._gap, 0
            if move:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line = max(line, hint.height())
        return y + line - r.y()


# ---------------------------------------------------------------------------
# The result list
# ---------------------------------------------------------------------------

ROW = Qt.ItemDataRole.UserRole + 1     # a dict describing the row


class ResultDelegate(QStyledItemDelegate):
    """Draws a result row: code, name with the matched text marked, "Title 9" when another
    title has the episode, and a tick on the current choice. Also headers and the divider."""

    def sizeHint(self, option, index):  # noqa: N802
        row = index.data(ROW) or {}
        kind = row.get("kind")
        h = {"header": 28, "divider": 13, "empty": 64}.get(kind, 50 if row.get("why") else 34)
        return QSize(POPUP_WIDTH - 2, h)

    def paint(self, p: QPainter, option, index):
        row = index.data(ROW) or {}
        kind = row.get("kind")
        r = option.rect
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = QFont(option.font)
        base.setPixelSize(14)
        if kind == "header":
            f = QFont(base)
            f.setPixelSize(11)
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.5)
            p.setFont(f)
            p.setPen(QColor(MUTED))
            p.drawText(r.adjusted(12, 8, -12, -2), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       row["text"].upper())
            p.restore()
            return
        if kind == "divider":
            p.fillRect(QRect(r.left(), r.center().y(), r.width(), 1), QColor(BORDER))
            p.restore()
            return
        if kind == "empty":
            f = QFont(base)
            f.setPixelSize(13)
            p.setFont(f)
            p.setPen(QColor(MUTED))
            p.drawText(r.adjusted(12, 8, -12, -8), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                       | Qt.TextFlag.TextWordWrap, row["text"])
            p.restore()
            return

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)
        bg = None
        if row.get("current"):
            bg = "#3A3020" if selected else CURRENT_BG
        elif selected:
            bg = FOCUS_BG
        elif hover:
            bg = HOVER_BG
        if bg:
            p.fillRect(r, QColor(bg))
        if selected:
            p.fillRect(QRect(r.left(), r.top(), 2, r.height()), QColor(ACCENT))

        two = bool(row.get("why"))
        line_h = 20
        top = r.top() + (8 if two else (r.height() - line_h) // 2)
        x = r.left() + 12
        right = r.right() - 12

        # Tick, then "Title N", from the right.
        if row.get("current"):
            draw_tick(p, QRect(right - 14, top, 14, line_h))
        right -= 14 + 10
        if row.get("used"):
            f = QFont(base)
            f.setPixelSize(12)
            p.setFont(f)
            p.setPen(QColor(MUTED))
            w = QFontMetrics(f).horizontalAdvance(row["used"])
            p.drawText(QRect(right - w, top, w, line_h), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       row["used"])
            right -= w + 10

        if row.get("code"):
            mf = mono_font(base, 13)
            p.setFont(mf)
            p.setPen(QColor(MUTED))
            w = QFontMetrics(mf).horizontalAdvance(row["code"])
            p.drawText(QRect(x, top, w, line_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, row["code"])
            x += w + 10

        name = row.get("name", "")
        fm = QFontMetrics(base)
        avail = max(20, right - x)
        shown = fm.elidedText(name, Qt.TextElideMode.ElideRight, avail)
        keep = len(shown) if shown == name else max(0, len(shown) - 1)
        p.setFont(base)
        colour = QColor(MUTED if row.get("used") else TEXT)
        cx = x
        pos = 0
        pieces = []
        for a, b in row.get("spans", []):
            a, b = min(a, keep), min(b, keep)
            if a > pos:
                pieces.append((shown[pos:a], False))
            if b > a:
                pieces.append((shown[a:b], True))
            pos = max(pos, b)
        pieces.append((shown[pos:], False))
        for text, hit in pieces:
            if not text:
                continue
            w = fm.horizontalAdvance(text)
            if hit:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(MARK_BG))
                p.drawRoundedRect(QRect(cx - 1, top + 1, w + 2, line_h - 2), 3, 3)
                p.setPen(QColor(MARK_TEXT))
            else:
                p.setPen(colour)
            p.drawText(QRect(cx, top, w + 1, line_h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)
            cx += w

        if two:
            f = QFont(base)
            f.setPixelSize(12)
            p.setFont(f)
            p.setPen(QColor(MUTED))
            why = QFontMetrics(f).elidedText(row["why"], Qt.TextElideMode.ElideRight, max(20, r.right() - 12 - 24 - x))
            p.drawText(QRect(x, top + line_h, r.right() - x, 18), Qt.AlignmentFlag.AlignLeft
                       | Qt.AlignmentFlag.AlignVCenter, why)
        p.restore()


class EpisodePopup(QWidget):
    """The open picker: search field, season buttons, results, and the Extra and Skip links."""

    chosen = Signal(str)
    closed = Signal()

    def __init__(self, picker: "EpisodePicker"):
        super().__init__(picker, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)      # a click on the button closes, not reopens
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.picker = picker
        o = picker._opts
        self.episodes: list[Episode] = o["episodes"]
        self.current: str = o["current"]
        self.title_index: int = o["title_index"]
        self.used: dict = o["used"] or {}
        self.taken: dict | None = o.get("taken")
        self.group_season: int | None = o.get("group_season")
        self.suggestions = o["suggestions"] or []
        self.extra_label: str = o["extra_label"]
        self.double = o["double"]
        self._folded = [fold(e.title)[0] for e in self.episodes]
        seasons = sorted({e.season for e in self.episodes})
        self.season: int | None = o["season"] if o["season"] in seasons else None

        frame = QFrame(self)
        frame.setObjectName("pickerPopup")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        self.search = QLineEdit()
        self.search.setObjectName("pickerSearch")
        self.search.setPlaceholderText("Search by name, or S01E11")
        self.search.setAccessibleName("Search episodes")
        self.search.addAction(QIcon(search_icon()), QLineEdit.ActionPosition.LeadingPosition)
        self.clear_action = self.search.addAction(QIcon(cross_icon()), QLineEdit.ActionPosition.TrailingPosition)
        self.clear_action.setToolTip("Clear the search")
        self.clear_action.setVisible(False)
        self.clear_action.triggered.connect(self.search.clear)
        self.search.textChanged.connect(self._typed)
        self.search.installEventFilter(self)

        self.chips = QButtonGroup(self)
        self.chips.setExclusive(True)
        chip_row = QWidget()
        flow = FlowLayout(chip_row, spacing=5)
        for s in [None, *seasons]:
            b = QPushButton("All" if s is None else str(s))
            b.setObjectName("seasonChip")
            b.setCheckable(True)
            b.setChecked(s == self.season)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)        # the search field keeps the keyboard
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setAccessibleName("All seasons" if s is None else f"Season {s}")
            b.setProperty("season", s)
            b.clicked.connect(lambda _c=False, s=s: self.set_season(s))
            self.chips.addButton(b)
            flow.addWidget(b)
        self.chip_buttons = self.chips.buttons()

        top = QFrame()
        top.setObjectName("pickerTop")
        tl = QVBoxLayout(top)
        tl.setContentsMargins(10, 10, 10, 10)
        tl.setSpacing(8)
        tl.addWidget(self.search)
        tl.addWidget(chip_row)

        self.list = QListWidget()
        self.list.setObjectName("pickerList")
        self._delegate = ResultDelegate(self.list)
        self.list.setItemDelegate(self._delegate)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.list.setUniformItemSizes(False)
        self.list.setMouseTracking(True)
        self.list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover)
        self.list.setMaximumHeight(LIST_MAX_H)
        self.list.itemClicked.connect(self._clicked)

        self.count = QLabel()
        self.count.setObjectName("pickerCount")
        self.extra_btn = self._link("Save as an extra…", "extra_new")
        self.skip_btn = self._link("Skip this title", "skip")
        foot = QFrame()
        foot.setObjectName("pickerFoot")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(12, 10, 12, 4)
        fl.setSpacing(14)
        fl.addWidget(self.count)
        fl.addStretch(1)
        fl.addWidget(self.extra_btn)
        fl.addWidget(self.skip_btn)
        self.hint = QLabel("↑ ↓ to move · Enter to pick · Esc to close")
        self.hint.setObjectName("pickerHint")

        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(top)
        lay.addWidget(self.list)
        lay.addWidget(foot)
        hint_box = QHBoxLayout()
        hint_box.setContentsMargins(12, 0, 12, 10)
        hint_box.addWidget(self.hint)
        lay.addLayout(hint_box)

        self.setFixedWidth(POPUP_WIDTH)
        # The flow layout's height depends on its width, which is known only now: the frame's
        # 1 px border on each side and the top section's 10 px margins.
        chip_row.setFixedHeight(flow.heightForWidth(POPUP_WIDTH - 2 - 20))
        self.refill()

    def _link(self, text: str, data: str) -> QPushButton:
        b = QPushButton(text)
        b.setObjectName("pickerLink")
        b.setCursor(Qt.CursorShape.PointingHandCursor)
        b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        b.clicked.connect(lambda: self.chosen.emit(data))
        return b

    # -- results ----------------------------------------------------------
    def rows(self) -> tuple[list[dict], int]:
        """The rows to show for the search and season, and how many episodes match."""
        q = self.search.text()
        words = query_words(q)
        season = self.season
        out: list[dict] = []

        def ep_row(e: Episode, data: str, code: str, why: str = "") -> dict:
            return {"kind": "option", "data": data, "code": code, "name": e.title or code, "why": why,
                    "spans": highlight_spans(e.title, words) if words else [],
                    "used": self.where(e), "current": data == self.current}

        # The current choice when it isn't an ordinary episode, so it stays pickable.
        special = []
        if self.double is not None:
            e, end = self.double
            if not words or episode_hits(e, words):
                special.append(ep_row(e, ep_data(e, end), ep_code(e.season, e.number, end)))
        if self.current == "extra" and not words:
            special.append({"kind": "option", "data": "extra", "code": "", "name": self.extra_label or "Extra",
                            "why": "", "spans": [], "used": "", "current": True})
        if special:
            out.append({"kind": "header", "text": "Chosen now"})
            out.extend(special)

        if self.suggestions and not season:
            sugg = [ep_row(e, ep_data(e), ep_code(e.season, e.number), why)
                    for e, why in self.suggestions if not words or episode_hits(e, words)]
            if sugg:
                out.append({"kind": "header", "text": "Suggested for this title"})
                out.extend(sugg)
        if out:
            out.append({"kind": "divider"})
        lead = len(out)

        n = 0
        last_season = None
        group = self.group_season if self.taken is not None else None
        if group is not None and season in (None, group):
            mine = [e for e, folded in zip(self.episodes, self._folded)
                    if e.season == group and (not words or episode_hits(e, words, folded))]
            free = [e for e in mine if not self.where(e)]
            taken = [e for e in mine if self.where(e)]
            out.append({"kind": "header", "text": f"Free in season {group} · {len(free)}"})
            if not free:
                out.append({"kind": "empty", "text": (f"No free episode of season {group} matches “{q.strip()}”."
                                                      if words else f"None. Every episode of season {group} has a "
                                                      "title already, so this one may be a second version or an extra.")})
            out.extend(ep_row(e, ep_data(e), ep_code(e.season, e.number)) for e in free)
            if taken:
                out.append({"kind": "header", "text": f"Taken in season {group} · {len(taken)}"})
                out.extend(ep_row(e, ep_data(e), ep_code(e.season, e.number)) for e in taken)
            n += len(mine)
            last_season = group
        for e, folded in zip(self.episodes, self._folded):
            if season and e.season != season:
                continue
            if group is not None and e.season == group:
                continue
            if words and not episode_hits(e, words, folded):
                continue
            if e.season != last_season:
                out.append({"kind": "header", "text": f"Season {e.season}"})
                last_season = e.season
            out.append(ep_row(e, ep_data(e), ep_code(e.season, e.number)))
            n += 1
        if not n:
            scope = f"season {season}" if season else "any season"
            text = (f"No episode in {scope} has “{q.strip()}” in its name or number. Try fewer letters"
                    + (", or All seasons." if season else ".")) if words else f"No episodes in {scope}."
            if out and out[-1]["kind"] == "divider":
                out.pop()
            if out and out[-1]["kind"] == "empty":
                out.pop()
            out.append({"kind": "empty", "text": text})
        elif lead and lead == len(out):
            out.pop()
        return out, n

    def where(self, e: Episode) -> str:
        """Where else this episode is, "" when it's free (or only this title has it)."""
        if self.taken is not None:
            return " · ".join(text for ti, text in self.taken.get((e.season, e.number), []) if ti != self.title_index)
        other = self.used.get((e.season, e.number))
        return f"Title {other}" if other is not None and other != self.title_index else ""

    def free_count(self) -> int | None:
        if self.taken is None or self.group_season is None:
            return None
        return sum(1 for e in self.episodes if e.season == self.group_season and not self.where(e))

    def refill(self):
        rows, n = self.rows()
        lw = self.list
        lw.setUpdatesEnabled(False)
        lw.clear()
        for row in rows:
            it = QListWidgetItem()
            it.setData(ROW, row)
            if row["kind"] == "option":
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                it.setData(Qt.ItemDataRole.AccessibleTextRole, " ".join(
                    x for x in (row["code"], row["name"], row["why"], row["used"], "chosen" if row["current"] else "") if x))
                if row["why"]:
                    it.setToolTip(row["why"])
            else:
                it.setFlags(Qt.ItemFlag.NoItemFlags)
            it.setSizeHint(self._delegate.sizeHint(None, _Index(row)))
            lw.addItem(it)
        lw.setCurrentRow(-1)
        lw.clearSelection()
        lw.setUpdatesEnabled(True)
        content = sum(lw.item(i).sizeHint().height() for i in range(lw.count())) + 2
        lw.setFixedHeight(min(LIST_MAX_H, max(64, content)))
        self.count.setText(count_text(n, self.search.text(), self.season))
        free = self.free_count()
        if free is not None and not query_words(self.search.text()):
            self.count.setText(f"{self.count.text()} · {free} free")
        self.clear_action.setVisible(bool(self.search.text()))
        if self.isVisible():
            self.adjustSize()
            self.place()

    def _typed(self, _text: str):
        self.refill()
        self.list.scrollToTop()

    def set_season(self, season: int | None):
        self.season = season
        for b in self.chip_buttons:
            b.setChecked(b.property("season") == season)
        self.refill()
        self.list.scrollToTop()
        self.search.setFocus()

    def options(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(self.list.count())
                if self.list.item(i).flags() & Qt.ItemFlag.ItemIsSelectable]

    def option_rows(self) -> list[dict]:
        return [it.data(ROW) for it in self.options()]

    def move_highlight(self, step: int):
        lw = self.list
        sel = [i for i in range(lw.count()) if lw.item(i).flags() & Qt.ItemFlag.ItemIsSelectable]
        if not sel:
            return
        cur = lw.currentRow()
        if cur not in sel:
            target = sel[0] if step > 0 else sel[-1]
        else:
            k = sel.index(cur) + step
            target = sel[max(0, min(len(sel) - 1, k))]
        lw.setCurrentRow(target)
        lw.scrollToItem(lw.item(target))
        if target == sel[0]:
            lw.scrollToTop()           # show the header above the first row too

    def pick_highlighted(self):
        it = self.list.currentItem()
        if it is None or not it.isSelected() or not (it.flags() & Qt.ItemFlag.ItemIsSelectable):
            opts = self.options()
            it = opts[0] if opts else None
        if it is not None:
            self.chosen.emit(it.data(ROW)["data"])

    def _clicked(self, it: QListWidgetItem):
        if it.flags() & Qt.ItemFlag.ItemIsSelectable:
            self.chosen.emit(it.data(ROW)["data"])

    # -- keys -------------------------------------------------------------
    def eventFilter(self, obj, e):  # noqa: N802
        if obj is self.search and e.type() == QEvent.Type.KeyPress:
            k = e.key()
            if k == Qt.Key.Key_Down:
                self.move_highlight(1)
                return True
            if k == Qt.Key.Key_Up:
                self.move_highlight(-1)
                return True
            if k == Qt.Key.Key_PageDown:
                self.move_highlight(8)
                return True
            if k == Qt.Key.Key_PageUp:
                self.move_highlight(-8)
                return True
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.pick_highlighted()
                return True
            if k == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, e)

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(e)

    # -- placing ----------------------------------------------------------
    def place(self):
        """Under the button, or above it when the screen has no room below."""
        b = self.picker
        below = b.mapToGlobal(QPoint(0, b.height() + 4))
        above = b.mapToGlobal(QPoint(0, -4))
        screen = QGuiApplication.screenAt(below) or b.screen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 10000, 10000)
        h = self.sizeHint().height()
        y = below.y()
        if y + h > area.bottom() and above.y() - h >= area.top():
            y = above.y() - h
        x = max(area.left(), min(below.x(), area.right() - self.width()))
        self.move(x, max(area.top(), y))

    def showEvent(self, e):  # noqa: N802
        super().showEvent(e)
        self.search.setFocus(Qt.FocusReason.PopupFocusReason)

    def closeEvent(self, e):  # noqa: N802
        self.closed.emit()
        super().closeEvent(e)


class _Index:
    """Enough of a QModelIndex for ResultDelegate.sizeHint."""

    def __init__(self, row):
        self._row = row

    def data(self, _role):
        return self._row


# ---------------------------------------------------------------------------
# The button
# ---------------------------------------------------------------------------

class EpisodePicker(QPushButton):
    """A button showing the chosen episode; clicking it opens EpisodePopup.

    The mouse wheel never changes it (the wheel scrolls the page), and hovering doesn't
    take the keyboard focus."""

    picked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("episodePicker")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.popup: EpisodePopup | None = None
        self._opts = {"episodes": [], "current": "skip", "title_index": 0, "used": None, "suggestions": None,
                      "season": None, "extra_label": "", "double": None, "taken": None, "group_season": None}
        self._code, self._name = "", "Skip this title"
        self.clicked.connect(self.open_popup)

    # -- options ----------------------------------------------------------
    def set_options(self, episodes: list[Episode], current: str, *, title_index: int,
                    used: dict[tuple[int, int], int] | None = None,
                    suggestions: list[tuple[Episode, str]] | None = None,
                    season: int | None = None,
                    extra_label: str = "",
                    double: tuple[Episode, int] | None = None,
                    warn: bool = False,
                    taken: dict[tuple[int, int], list[tuple[int | None, str]]] | None = None,
                    group_season: int | None = None) -> None:
        """taken, when given, says where each episode is held: (title of this disc or None, "Title 6,
        this disc" / "Disc 1, title 7" / "In the library"); group_season is the disc's own season,
        listed as free and taken episodes. Without taken, used ({episode: title}) names the title."""
        self._opts = {"episodes": list(episodes), "current": current or "skip", "title_index": title_index,
                      "used": dict(used or {}), "suggestions": list(suggestions or []), "season": season,
                      "extra_label": extra_label, "double": double,
                      "taken": dict(taken) if taken is not None else None, "group_season": group_season}
        self._set_prop("warn", "true" if warn else None)
        self._show_current()

    def currentData(self) -> str:  # noqa: N802 -- the QComboBox name the Match page used
        return self._opts["current"]

    def set_current(self, data: str) -> None:
        """Change the choice without emitting picked (e.g. to undo a pick the page refused)."""
        self._opts["current"] = data or "skip"
        self._show_current()

    def display_text(self) -> str:
        return f"{self._code} · {self._name}" if self._code else self._name

    def _show_current(self):
        cur = self._opts["current"]
        parsed = parse_data(cur)
        if parsed:
            s, n, end = parsed
            ep = None
            if self._opts["double"] is not None and end:
                ep = self._opts["double"][0]
            if ep is None or (ep.season, ep.number) != (s, n):
                ep = next((e for e in self._opts["episodes"] if (e.season, e.number) == (s, n)), None)
            self._code, self._name = ep_code(s, n, end), (ep.title if ep and ep.title else "")
            if not self._name:
                self._code, self._name = "", ep_code(s, n, end)
        elif cur == "extra":
            self._code, self._name = "", self._opts["extra_label"] or "Extra"
        elif cur == "feature":
            self._code, self._name = "", "Film"
        else:
            self._code, self._name = "", "Skip this title"
        text = self.display_text()
        self.setToolTip(text)
        self.setAccessibleName(f"Episode: {text}")
        self.update()

    def _set_prop(self, name: str, value):
        if self.property(name) != value:
            self.setProperty(name, value)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    # -- popup ------------------------------------------------------------
    def open_popup(self) -> None:
        if self.popup is not None:
            return
        pop = EpisodePopup(self)
        pop.chosen.connect(self._chosen)
        pop.closed.connect(self._popup_closed)
        self.popup = pop
        self._set_prop("open", "true")
        pop.adjustSize()
        pop.place()
        pop.show()
        pop.search.setFocus(Qt.FocusReason.PopupFocusReason)
        # Start with the current choice in view.
        for i in range(pop.list.count()):
            row = pop.list.item(i).data(ROW)
            if row.get("current"):
                pop.list.scrollToItem(pop.list.item(i), QAbstractItemView.ScrollHint.PositionAtCenter)
                break

    def _popup_closed(self):
        self.popup = None
        self._set_prop("open", None)

    def _chosen(self, data: str):
        if self.popup is not None:
            self.popup.close()
        if data != "extra_new":         # an action; the page sets "extra" once the dialog is accepted
            self._opts["current"] = data
            self._show_current()
        self.picked.emit(data)

    # -- input ------------------------------------------------------------
    def wheelEvent(self, e):  # noqa: N802 -- Qt's name
        e.ignore()

    def keyPressEvent(self, e):  # noqa: N802
        text = e.text()
        if text and text.isprintable() and not text.isspace() and not (e.modifiers() & ~Qt.KeyboardModifier.ShiftModifier
                                                                       & ~Qt.KeyboardModifier.KeypadModifier):
            self.open_popup()           # typing on the button starts a search
            if self.popup is not None:
                self.popup.search.insert(text)
            return
        if e.key() in (Qt.Key.Key_Down, Qt.Key.Key_F4) and self.popup is None:
            self.open_popup()
            return
        super().keyPressEvent(e)

    # -- drawing ----------------------------------------------------------
    def sizeHint(self):  # noqa: N802
        return QSize(300, 40)

    def minimumSizeHint(self):  # noqa: N802
        return QSize(120, 40)

    def paintEvent(self, e):  # noqa: N802
        super().paintEvent(e)           # the frame, from the stylesheet; the button has no text of its own
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(13, 0, -13, 0)
        enabled = self.isEnabled()
        icon = search_icon(14, MUTED if enabled else BORDER)
        p.drawPixmap(r.right() - 14, (self.height() - 14) // 2, icon)
        right = r.right() - 14 - 10
        x = r.left()
        base = QFont(self.font())
        if self._code:
            mf = mono_font(base, 13)
            p.setFont(mf)
            p.setPen(QColor(MUTED if enabled else DISABLED))
            w = QFontMetrics(mf).horizontalAdvance(self._code)
            p.drawText(QRect(x, 0, w, self.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       self._code)
            x += w + 10
        p.setFont(base)
        p.setPen(QColor(TEXT if enabled else DISABLED))
        name = QFontMetrics(base).elidedText(self._name, Qt.TextElideMode.ElideRight, max(10, right - x))
        p.drawText(QRect(x, 0, right - x, self.height()), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   name)
        p.end()
