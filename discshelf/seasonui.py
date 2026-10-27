"""The Match page's season widgets: the problems banner, the panel that compares two titles
holding one episode, the season grid, and the Watch button and its chooser.

Like picker.py, this imports only PySide6 and Disc Shelf's own plain modules, so gui.py can
import it. The app's stylesheet styles the buttons and labels through the same "role",
"primary" and "small" properties gui.py uses; the frames here carry their own colours.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap, QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QRadioButton, QSizePolicy,
    QToolTip, QVBoxLayout, QWidget,
)

from .picker import FlowLayout, mono_font
from .play import fmt_time

TEXT, SOFT, MUTED, HINT = "#E9EBED", "#C3C8CD", "#A2A9B0", "#7D858C"
LINK, ACCENT = "#7CB8F0", "#F0A23B"
BAD_BG, BAD_BORDER, BAD_TITLE, BAD_TEXT, BAD_CODE = "#241617", "#7A3B3B", "#FFD7D3", "#F1C9C5", "#FFB4AE"

# The grid's states: (fill, border, text, legend). Each cell also carries a short text (T6, D1,
# Lib, ×2, ?) and free cells a dashed border, so no state is told by colour alone.
CELL_LOOK = {
    "here": ("#2A2419", "#8A6A33", "#F5C27A", "On this disc"),
    "disc": ("#1B2633", "#3B5A78", "#A9D1F6", "On another disc"),
    "library": ("#1F2A22", "#3E6B50", "#8FE0B0", "In the library"),
    "duplicate": ("#3A1D1D", "#E5534B", "#FFB4AE", "On two titles or files"),
    "check": ("#2A2213", ACCENT, "#F5C27A", "Needs a check"),
    "free": ("", "#7D858C", "#A2A9B0", "No title yet"),
}
CELL_ORDER = ("here", "disc", "library", "duplicate", "check", "free")
FLAGGED = ("duplicate", "check")


def _label(text: str = "", role: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    return lab


def _button(text: str, primary: bool = False, small: bool = True) -> QPushButton:
    b = QPushButton(text)
    if primary:
        b.setProperty("primary", "true")
    if small:
        b.setProperty("small", "true")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


def _link(text: str, on_click) -> QLabel:
    lab = QLabel(f'<a href="#" style="color:{LINK}; text-decoration:none">{text}</a>')
    lab.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
    lab.linkActivated.connect(lambda _href: on_click())
    lab.setCursor(Qt.CursorShape.PointingHandCursor)
    return lab


def play_icon(size: int = 14, colour: str = SOFT) -> QIcon:
    """A ▶ drawn as a triangle; the app's fonts may not have the glyph."""
    screen = QGuiApplication.primaryScreen()
    ratio = screen.devicePixelRatio() if screen else 1.0
    pm = QPixmap(int(size * ratio), int(size * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath(QPointF(size * 0.25, size * 0.15))
    path.lineTo(size * 0.85, size * 0.5)
    path.lineTo(size * 0.25, size * 0.85)
    path.closeSubpath()
    p.fillPath(path, QColor(colour))
    p.end()
    return QIcon(pm)


def watch_button(text: str = "") -> QPushButton:
    """The ▶ button: icon only in a table row, "Watch" in the compare panel."""
    b = QPushButton(text)
    b.setIcon(play_icon())
    b.setIconSize(QSize(14, 14))
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    if text:
        b.setProperty("small", "true")
    else:
        b.setFixedSize(34, 34)
        b.setStyleSheet("QPushButton { min-height: 0; padding: 0; border-radius: 8px; }")
    return b


# ---------------------------------------------------------------------------
# The problems banner
# ---------------------------------------------------------------------------

class ClickChip(QLabel):
    """A chip in the match table that opens the fix panel: by mouse, or Enter or Space when it has
    the keyboard focus."""

    clicked = Signal()

    def __init__(self, text: str, kind: str):
        super().__init__(text)
        self.setProperty("chip", kind)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def mousePressEvent(self, e):  # noqa: N802 -- Qt's name
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(e)

    def keyPressEvent(self, e):  # noqa: N802
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(e)


class ProblemsBanner(QFrame):
    """One line at the top of the Match page saying what needs a look, with a button that opens
    the fix panel on the first problem, or steps to the next one."""

    next = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("problems")
        self.setStyleSheet("QFrame#problems { background: #201915; border: 1px solid #5A3A2A; border-radius: 12px; }")
        self.setAccessibleName("What needs a look")
        mark = QLabel("!")
        mark.setFixedSize(22, 22)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setStyleSheet(f"background: {ACCENT}; color: #1A1206; font-weight: 700; font-size: 13px; border-radius: 11px;")
        self.line = QLabel("")
        self.line.setWordWrap(True)
        self.line.setStyleSheet("color: #F2D2B0;")
        self.button = _button("Fix it", primary=True)
        self.button.clicked.connect(self.next.emit)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(12)
        lay.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
        lay.addWidget(self.line, 1)
        lay.addWidget(self.button)

    def set_line(self, text: str, button_text: str) -> None:
        self.line.setText(text)
        self.button.setText(button_text)
        self.setVisible(bool(text))


# ---------------------------------------------------------------------------
# The fix panel: one layout for everything flagged, and for what's known about the rest
# ---------------------------------------------------------------------------

@dataclass
class CompareTitle:
    """One side in the fix panel: a title of this disc, another disc's title, or a library file."""
    key: str                     # "here:6", "side:0"
    title_index: int | None      # on this disc, else None
    label: str                   # "Title 27", "Disc 1, First Half, title 26"
    sub: str                     # "00034.m2ts · 7:14 · 1.0 GB", or its file name on the NAS
    rows: list[tuple[str, str, str]] = field(default_factory=list)   # (caption, value, colour or "")
    path: str = ""               # a file that plays it, "" when none yet
    card_t: float | None = None  # seconds in where its title card names the episode
    card_named: bool = False     # its card reads the episode's name
    odd: bool = False            # the side the suggested fix changes, drawn with an amber edge
    watch_tip: str = ""


@dataclass
class FixOption:
    key: str
    label: str                   # "Keep title 26 as the French version (suggested)"
    what: str                    # what happens, in a sentence or two
    apply: str = ""              # the apply button's text when it's chosen: "Rename 1 file"
    changes: list[tuple[str, str]] = field(default_factory=list)   # (old, new) on the NAS


@dataclass
class FixView:
    kind: str                    # "error" | "warn" | "info" | "done"
    title: str
    text: str
    sides: list[CompareTitle] = field(default_factory=list)
    options: list[FixOption] = field(default_factory=list)
    selected: str = ""
    note: str = ""               # one line beside the apply button
    rescan: str = ""             # "Ask Plex to rescan Season 01 afterwards", "" for no checkbox
    step: tuple[int, int] | None = None   # (position from 1, how many)
    busy: str = ""               # the apply button's text while a fix runs


LOOKS = {"error": ("#7A3B3B", "#241617", "#E5534B", "!"), "warn": ("#7A5A2A", "#221C13", ACCENT, "?"),
         "done": ("#2F5A42", "#15211A", "#8FE0B0", "✓"), "info": ("#2E3338", "#1A1D20", LINK, "i")}


class ComparePanel(QFrame):
    """The fix panel. Every flagged thing on the Match page opens it: what's wrong in a sentence,
    each side with Watch, the fixes as choices that say what happens, one apply button named for
    the action, and afterwards a result line with Undo. Anything not flagged opens it as what's
    known about it."""

    apply = Signal(str)          # the chosen option's key
    chosen = Signal(str)
    undo = Signal()
    step = Signal(int)           # -1 or +1
    watch = Signal(str)          # a CompareTitle key
    closed = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("fixPanel")
        self.head = QFrame()
        self.head.setObjectName("fixHead")
        self.dot = QLabel("!")
        self.dot.setFixedSize(20, 20)
        self.dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.heading = _label("", "h2")
        self.heading.setWordWrap(True)
        self.prev_btn = _button("‹")
        self.next_btn = _button("›")
        for b, name in ((self.prev_btn, "Previous problem"), (self.next_btn, "Next problem")):
            b.setAccessibleName(name)
            b.setFixedWidth(34)
        self.prev_btn.clicked.connect(lambda: self.step.emit(-1))
        self.next_btn.clicked.connect(lambda: self.step.emit(1))
        self.step_text = _label("", "small")
        self.close_btn = _button("Close")
        self.close_btn.setAccessibleName("Close the panel")
        self.close_btn.clicked.connect(self.closed.emit)
        hl = QHBoxLayout(self.head)
        hl.setContentsMargins(16, 10, 16, 10)
        hl.setSpacing(10)
        hl.addWidget(self.dot)
        hl.addWidget(self.heading, 1)
        hl.addWidget(self.prev_btn)
        hl.addWidget(self.step_text)
        hl.addWidget(self.next_btn)
        hl.addWidget(self.close_btn)
        self.text = _label("", wrap=True)
        self.text.setStyleSheet("color: #D5D9DD;")
        self.cards = QHBoxLayout()
        self.cards.setSpacing(12)
        self.options_box = QWidget()
        self.options_lay = QVBoxLayout(self.options_box)
        self.options_lay.setContentsMargins(0, 0, 0, 0)
        self.options_lay.setSpacing(8)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(self._picked)
        self.change_box = QFrame()
        self.change_box.setObjectName("fixChange")
        cl = QVBoxLayout(self.change_box)
        cl.setContentsMargins(0, 10, 0, 0)
        cl.setSpacing(4)
        cl.addWidget(_label("What changes on the NAS", "small"))
        self.change_lines = QLabel("")
        self.change_lines.setTextFormat(Qt.TextFormat.RichText)
        self.change_lines.setWordWrap(True)
        self.change_lines.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        cl.addWidget(self.change_lines)
        self.rescan = QCheckBox("")
        self.rescan.setChecked(True)
        cl.addWidget(self.rescan)
        self.apply_btn = _button("", primary=True, small=False)
        self.apply_btn.clicked.connect(lambda: self.apply.emit(self.selected))
        self.apply_note = _label("", "small", wrap=True)
        arow = QHBoxLayout()
        arow.setSpacing(12)
        arow.addWidget(self.apply_btn)
        arow.addWidget(self.apply_note, 1)
        self.apply_row = QWidget()
        self.apply_row.setLayout(arow)
        arow.setContentsMargins(0, 0, 0, 0)
        self.done = QFrame()
        self.done.setObjectName("fixDone")
        self.done_text = QLabel("")
        self.done_text.setWordWrap(True)
        self.done_text.setStyleSheet("color: #BFEBD0; font-size: 13px;")
        self.undo_link = _link("Undo", self.undo.emit)
        self.undo_link.setAccessibleName("Undo the fix")
        dl = QHBoxLayout(self.done)
        dl.setContentsMargins(12, 10, 12, 10)
        dl.setSpacing(14)
        dl.addWidget(self.done_text, 1)
        dl.addWidget(self.undo_link, 0, Qt.AlignmentFlag.AlignTop)
        self.done.hide()
        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 16)
        body.setSpacing(14)
        body.addWidget(self.done)
        body.addWidget(self.text)
        body.addLayout(self.cards)
        body.addWidget(self.options_box)
        body.addWidget(self.change_box)
        body.addWidget(self.apply_row)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.head)
        lay.addLayout(body)
        self.view: FixView | None = None
        self.selected = ""
        self.option_frames: dict[str, QFrame] = {}
        self.watch_buttons: dict[str, QPushButton] = {}
        self._look("info")
        self.hide()

    def _look(self, kind: str) -> None:
        border, head, dot, icon = LOOKS.get(kind, LOOKS["info"])
        self.setStyleSheet(
            f"QFrame#fixPanel {{ background: #1A1D20; border: 1px solid {border}; border-radius: 12px; }}"
            f"QFrame#fixHead {{ background: {head}; border: none; border-bottom: 1px solid #2E3338;"
            " border-top-left-radius: 12px; border-top-right-radius: 12px; }"
            "QFrame#fixSide { background: #15181B; border: 1px solid #3A4046; border-radius: 10px; }"
            "QFrame#fixSide[odd=\"true\"] { border-color: #8A6A33; }"
            "QFrame#fixOption { background: transparent; border: 1px solid #3A4046; border-radius: 10px; }"
            f"QFrame#fixOption[on=\"true\"] {{ background: #2A2419; border-color: {ACCENT}; }}"
            "QFrame#fixChange { background: transparent; border: none; border-top: 1px solid #262A2E; border-radius: 0; }"
            "QFrame#fixDone { background: #15211A; border: 1px solid #2F5A42; border-radius: 10px; }")
        self.dot.setText(icon)
        self.dot.setStyleSheet(f"background: {dot}; color: #1A0E0E; font-weight: 700; font-size: 12px; border-radius: 10px;")

    def set_view(self, v: FixView) -> None:
        self.view = v
        self._look(v.kind)
        self.heading.setText(v.title)
        self.text.setText(v.text)
        self.text.setVisible(bool(v.text))
        steps = v.step is not None and v.step[1] > 1
        for w in (self.prev_btn, self.next_btn, self.step_text):
            w.setVisible(steps)
        if steps:
            self.step_text.setText(f"{v.step[0]} of {v.step[1]}")
        _clear(self.cards)
        self.watch_buttons = {}
        for t in v.sides:
            self.cards.addWidget(self._side(t), 1)
        _clear(self.options_lay)
        for b in self.group.buttons():
            self.group.removeButton(b)
        self.option_frames = {}
        for i, o in enumerate(v.options):
            self.options_lay.addWidget(self._option(i, o))
        self.options_box.setVisible(bool(v.options))
        self.selected = v.selected if any(o.key == v.selected for o in v.options) else \
            (v.options[0].key if v.options else "")
        self.rescan.setText(v.rescan)
        self._sync()
        self.show()

    def _option(self, i: int, o: FixOption) -> QFrame:
        f = QFrame()
        f.setObjectName("fixOption")
        f.setCursor(Qt.CursorShape.PointingHandCursor)
        radio = QRadioButton(o.label)
        radio.setStyleSheet("QRadioButton { font-weight: 600; }")
        radio.setAccessibleDescription(o.what)
        self.group.addButton(radio, i)
        what = QLabel(o.what)
        what.setWordWrap(True)
        what.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
        what.setContentsMargins(28, 0, 0, 0)
        lay = QVBoxLayout(f)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(2)
        lay.addWidget(radio)
        lay.addWidget(what)
        f.mousePressEvent = lambda _e, i=i: self._picked(i)
        self.option_frames[o.key] = f
        return f

    def _picked(self, i: int) -> None:
        if self.view is None or not 0 <= i < len(self.view.options):
            return
        self.selected = self.view.options[i].key
        self._sync()
        self.chosen.emit(self.selected)

    def choose(self, key: str) -> None:
        if self.view is not None and any(o.key == key for o in self.view.options):
            self.selected = key
            self._sync()

    def _sync(self) -> None:
        v = self.view
        o = next((o for o in v.options if o.key == self.selected), None) if v else None
        for k, f in self.option_frames.items():
            f.setProperty("on", "true" if k == self.selected else None)
            f.style().unpolish(f)
            f.style().polish(f)
        for i, b in enumerate(self.group.buttons()):
            b.setChecked(v is not None and self.group.id(b) < len(v.options)
                         and v.options[self.group.id(b)].key == self.selected)
        changes = o.changes if o else []
        self.change_lines.setText("<br>".join(
            f'<span style="font-family:monospace; color:#E59A94; text-decoration:line-through">{_esc(a)}</span><br>'
            f'<span style="font-family:monospace; color:#8FE0B0">{_esc(b)}</span>' for a, b in changes))
        self.change_box.setVisible(bool(changes))
        self.rescan.setVisible(bool(changes) and bool(v and v.rescan))
        self.apply_row.setVisible(o is not None)
        self.apply_btn.setText(v.busy if v and v.busy else (o.apply if o else ""))
        self.apply_btn.setEnabled(not (v and v.busy))
        self.apply_note.setText(v.note if v else "")

    def set_result(self, text: str, can_undo: bool = True) -> None:
        self.done_text.setText(text)
        self.undo_link.setVisible(can_undo)
        self.done.setVisible(bool(text))

    def _side(self, t: CompareTitle) -> QFrame:
        f = QFrame()
        f.setObjectName("fixSide")
        f.setProperty("odd", "true" if t.odd else None)
        f.setMinimumWidth(240)
        f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QVBoxLayout(f)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)
        name = QLabel(t.label)
        name.setStyleSheet("font-weight: 600;")
        name.setWordWrap(True)
        sub = QLabel(t.sub)
        sub.setFont(mono_font(sub.font(), 12))
        sub.setStyleSheet(f"color: {MUTED};")
        sub.setWordWrap(True)
        lay.addWidget(name)
        lay.addWidget(sub)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        for r, (cap, value, colour) in enumerate(t.rows):
            c = QLabel(cap)
            c.setStyleSheet("color: #8C939A; font-size: 13px;")
            v = QLabel(value)
            v.setWordWrap(True)
            v.setStyleSheet(f"color: {colour or SOFT}; font-size: 13px;")
            grid.addWidget(c, r, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(v, r, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        w = watch_button("Watch from the title card" if t.card_t is not None else "Watch from the start")
        w.setEnabled(bool(t.path))
        w.setToolTip(t.watch_tip)
        w.setAccessibleName(f"Watch {t.label}")
        w.clicked.connect(lambda _c=False, k=t.key: self.watch.emit(k))
        row = QHBoxLayout()
        row.addWidget(w)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)
        self.watch_buttons[t.key] = w
        return f


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clear(lay) -> None:
    while lay.count():
        w = lay.takeAt(0).widget()
        if w:
            w.hide()            # deleteLater waits for the event loop; until then it would still draw
            w.deleteLater()


# ---------------------------------------------------------------------------
# The season grid
# ---------------------------------------------------------------------------

class SeasonCells(QWidget):
    """One small cell per episode, painted, with a tooltip each. Clicking a cell (or Enter on the
    one with the keyboard focus, moved with the arrow keys) opens the fix panel: on its problem when
    it's flagged, else on what's known about the episode."""

    clicked = Signal(int)        # the episode number

    MIN_W, H, GAP = 46, 44, 4

    def __init__(self):
        super().__init__()
        self.cells = []
        self.focus_i = 0
        self.selected: int | None = None     # the episode the panel shows, ringed
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def set_cells(self, cells) -> None:
        self.cells = list(cells)
        self.focus_i = min(self.focus_i, max(0, len(self.cells) - 1))
        self.updateGeometry()
        self.setFixedHeight(self.heightForWidth(max(self.width(), 600)))
        flagged = [c.tip for c in self.cells if c.state in FLAGGED]
        self.setAccessibleName("Episodes of the season. Arrow keys move, Enter opens.")
        self.setAccessibleDescription(" ".join(flagged))
        self.update()

    def columns(self, w: int) -> int:
        n = len(self.cells) or 1
        fit = max(1, (w + self.GAP) // (self.MIN_W + self.GAP))
        if n <= fit:
            return n
        for c in (13, 12, 10, 8):     # an even split reads better: 52 episodes as 4 rows of 13
            if c <= fit and n % c == 0:
                return c
        return fit

    def hasHeightForWidth(self):  # noqa: N802 -- Qt's name
        return True

    def heightForWidth(self, w):  # noqa: N802
        cols = self.columns(w)
        rows = (len(self.cells) + cols - 1) // cols if self.cells else 0
        return max(0, rows * (self.H + self.GAP) - self.GAP + 4)

    def sizeHint(self):  # noqa: N802
        return QSize(600, self.heightForWidth(600))

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        h = self.heightForWidth(self.width())
        if h != self.height():
            self.setFixedHeight(h)

    def _rects(self) -> list[QRectF]:
        cols = self.columns(self.width() - 4)
        cw = (self.width() - 4 - self.GAP * (cols - 1)) / cols
        return [QRectF(2 + (i % cols) * (cw + self.GAP), 2 + (i // cols) * (self.H + self.GAP), cw, self.H)
                for i in range(len(self.cells))]

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        num = mono_font(self.font(), 12)
        small = QFont(self.font())
        small.setPixelSize(10)
        small.setWeight(QFont.Weight.DemiBold)
        for i, (c, r) in enumerate(zip(self.cells, self._rects())):
            fill, border, text, _legend = CELL_LOOK[c.state]
            flagged = c.state in FLAGGED
            rr = r.adjusted(1, 1, -1, -1) if flagged else r.adjusted(0.5, 0.5, -0.5, -0.5)
            pen = QPen(QColor(border), 2 if flagged else 1)
            if c.state == "free":
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(QColor(fill) if fill else Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rr, 6, 6)
            if c.number == self.selected or (self.hasFocus() and i == self.focus_i):
                ring = QPen(QColor(TEXT if c.number == self.selected else LINK), 2)
                p.setPen(ring)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(r.adjusted(-1.5, -1.5, 1.5, 1.5), 7, 7)
            p.setPen(QColor(text))
            p.setFont(num)
            top = r.adjusted(0, 4, 0, -r.height() / 2 + 3) if c.mark else r
            p.drawText(top, Qt.AlignmentFlag.AlignCenter, str(c.number))
            if c.mark:
                p.setFont(small)
                p.drawText(r.adjusted(0, r.height() / 2 - 1, 0, -4), Qt.AlignmentFlag.AlignCenter, c.mark)
        p.end()

    def cell_at(self, pos) -> int | None:
        for i, r in enumerate(self._rects()):
            if r.contains(QPointF(pos)):
                return i
        return None

    def mouseMoveEvent(self, e):  # noqa: N802
        i = self.cell_at(e.position().toPoint())
        if i is None:
            QToolTip.hideText()
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        c = self.cells[i]
        QToolTip.showText(e.globalPosition().toPoint(), c.tip, self)
        self.setCursor(Qt.CursorShape.PointingHandCursor if c.state in FLAGGED or c.holders
                       else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, e):  # noqa: N802
        i = self.cell_at(e.position().toPoint())
        if i is not None:
            self.focus_i = i
            self.clicked.emit(self.cells[i].number)

    def keyPressEvent(self, e):  # noqa: N802
        if not self.cells:
            return super().keyPressEvent(e)
        cols = self.columns(self.width() - 4)
        step = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -cols, Qt.Key.Key_Down: cols}.get(e.key())
        if step is not None:
            self.focus_i = max(0, min(len(self.cells) - 1, self.focus_i + step))
            c = self.cells[self.focus_i]
            r = self._rects()[self.focus_i]
            QToolTip.showText(self.mapToGlobal(r.bottomLeft().toPoint()), c.tip, self)
            self.update()
            return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit(self.cells[self.focus_i].number)
            return
        super().keyPressEvent(e)

    def focusInEvent(self, e):  # noqa: N802
        super().focusInEvent(e)
        self.update()

    def focusOutEvent(self, e):  # noqa: N802
        super().focusOutEvent(e)
        self.update()


class SeasonGrid(QFrame):
    """"Season 1 across your discs": the grid, its legend and a line saying what was checked.
    It can be folded away; the page remembers that for the session."""

    clicked = Signal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.heading = _label("", "h2")
        self.summary = _label("", "small", wrap=True)
        self.toggle = _button("Hide")
        self.toggle.setAccessibleName("Hide the season grid")
        self.toggle.clicked.connect(lambda: self.set_open(not self.open))
        head = QHBoxLayout()
        head.setSpacing(16)
        head.addWidget(self.heading)
        head.addWidget(self.summary, 1)
        head.addWidget(self.toggle)
        self.cells = SeasonCells()
        self.cells.clicked.connect(self.clicked.emit)
        self.legend = QLabel("")
        self.legend.setTextFormat(Qt.TextFormat.RichText)
        self.legend.setWordWrap(True)
        self.legend.setStyleSheet(f"color: {SOFT}; font-size: 12px;")
        self.checked = _label("", "small", wrap=True)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(10)
        bl.addWidget(self.cells)
        bl.addWidget(self.legend)
        bl.addWidget(self.checked)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(12)
        lay.addLayout(head)
        lay.addWidget(self.body)
        self.open = True

    def set_open(self, on: bool) -> None:
        self.open = on
        self.body.setVisible(on)
        self.toggle.setText("Hide" if on else "Show")
        self.toggle.setAccessibleName("Hide the season grid" if on else "Show the season grid")

    def set_status(self, st) -> None:
        if st is None or not st.cells:
            self.hide()
            return
        self.heading.setText(f"Season {st.season} across your discs")
        self.summary.setText(st.summary)
        self.cells.set_cells(st.cells)
        counts = {k: 0 for k in CELL_ORDER}
        for c in st.cells:
            counts[c.state] += 1
        parts = []
        for k in CELL_ORDER:
            fill, border, _text, words = CELL_LOOK[k]
            style = "dashed" if k == "free" else "solid"
            sample = {"here": "T6", "disc": "D1", "library": "Lib", "duplicate": "×2", "check": "?",
                      "free": "&nbsp;&nbsp;&nbsp;"}[k]
            parts.append(f'<span style="background:{fill or "transparent"}; color:{CELL_LOOK[k][2]}; '
                         f'border:1px {style} {border};">&nbsp;{sample}&nbsp;</span>&nbsp;{words}: {counts[k]}')
        self.legend.setText(" &nbsp;&nbsp;&nbsp; ".join(parts))
        discs = f"{st.discs} disc{'s' if st.discs != 1 else ''} of this season"
        lib = (f" and {st.library_files} file{'s' if st.library_files != 1 else ''} already in the season folder"
               if st.library_files else "")
        self.checked.setText(f"Checked against {discs}{lib}. A re-rip of a disc replaces its earlier rip. "
                             "T is a title on this disc and D another disc's number. Click a ×2 or ? cell to fix it, "
                             "or any other cell to see where it is.")
        self.show()


# ---------------------------------------------------------------------------
# Watch: where to start
# ---------------------------------------------------------------------------

class WatchChooser(QDialog):
    """Asked when a title's card was read: start at the card, or from the beginning."""

    def __init__(self, parent, title: str, card_t: float, seeks: bool, player_label: str):
        super().__init__(parent)
        self.setWindowTitle(f"Watch {title}")
        self.start: float | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 16)
        lay.setSpacing(12)
        lay.addWidget(_label(f"Watch {title}", "h2"))
        lay.addWidget(_label(f"Its title card is on screen at {fmt_time(card_t)}.", "muted", wrap=True))
        self.card_btn = _button(f"Start at the title card ({fmt_time(card_t)})", primary=seeks, small=False)
        self.card_btn.setEnabled(seeks)
        self.card_btn.clicked.connect(lambda: self._go(card_t))
        self.start_btn = _button("Start from the beginning", primary=not seeks, small=False)
        self.start_btn.clicked.connect(lambda: self._go(0.0))
        cancel = _button("Cancel", small=False)
        cancel.clicked.connect(self.reject)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self.card_btn)
        row.addWidget(self.start_btn)
        row.addStretch(1)
        row.addWidget(cancel)
        lay.addLayout(row)
        if not seeks:
            note = _label(f"Your player will start from the beginning: {player_label} can't be told where to start. "
                          "Install mpv or VLC to start at the title card.", "small", wrap=True)
            lay.addWidget(note)
        (self.card_btn if seeks else self.start_btn).setFocus()

    def _go(self, t: float) -> None:
        self.start = t
        self.accept()
