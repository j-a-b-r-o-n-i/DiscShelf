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
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QToolTip, QVBoxLayout, QWidget,
)

from .picker import FlowLayout, mono_font
from .play import fmt_time

TEXT, SOFT, MUTED, HINT = "#E9EBED", "#C3C8CD", "#A2A9B0", "#7D858C"
LINK, ACCENT = "#7CB8F0", "#F0A23B"
BAD_BG, BAD_BORDER, BAD_TITLE, BAD_TEXT, BAD_CODE = "#241617", "#7A3B3B", "#FFD7D3", "#F1C9C5", "#FFB4AE"

# The grid's states: (fill, border, text, legend). Each cell also carries a short text (T6, D1,
# Lib, ×2) and free cells a dashed border, so no state is told by colour alone.
CELL_LOOK = {
    "here": ("#2A2419", "#8A6A33", "#F5C27A", "On this disc"),
    "disc": ("#1B2633", "#3B5A78", "#A9D1F6", "On another disc"),
    "library": ("#1F2A22", "#3E6B50", "#8FE0B0", "In the library"),
    "duplicate": ("#3A1D1D", "#E5534B", "#FFB4AE", "On more than one title"),
    "free": ("", "#7D858C", "#A2A9B0", "No title yet"),
}
CELL_ORDER = ("here", "disc", "library", "duplicate", "free")


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

class ProblemsBanner(QFrame):
    """A red box at the top of the Match page listing each problem in a sentence, each with a
    Compare link."""

    compare = Signal(str)        # the problem's key

    def __init__(self):
        super().__init__()
        self.setObjectName("problems")
        self.setStyleSheet(f"QFrame#problems {{ background: {BAD_BG}; border: 1px solid {BAD_BORDER}; border-radius: 12px; }}")
        self.setAccessibleName("Problems with the matches")
        mark = QLabel("!")
        mark.setFixedSize(22, 22)
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setStyleSheet("background: #E5534B; color: #1A0E0E; font-weight: 700; font-size: 13px; border-radius: 11px;")
        self.title = QLabel("")
        self.title.setStyleSheet(f"color: {BAD_TITLE}; font-weight: 600;")
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(mark)
        head.addWidget(self.title, 1)
        self.rows = QVBoxLayout()
        self.rows.setSpacing(8)
        self.rows.setContentsMargins(32, 0, 0, 0)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(10)
        lay.addLayout(head)
        lay.addLayout(self.rows)
        self.lines: list[tuple[QLabel, QLabel]] = []

    def set_problems(self, items: list[tuple[str, str, str]], current: str = "") -> None:
        """items: (key, sentence, what it means for copying)."""
        while self.rows.count():
            w = self.rows.takeAt(0).widget()
            if w:
                w.hide()            # deleteLater waits for the event loop; until then it would still draw
                w.deleteLater()
        self.lines = []
        n = len(items)
        self.title.setText("One episode is matched to more than one title" if n == 1 else
                           f"{n} episodes are matched to more than one title")
        for key, text, effect in items:
            row = QWidget()
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(14)
            t = QLabel(f"{text} {effect}".strip())
            t.setWordWrap(True)
            t.setStyleSheet(f"color: {BAD_TEXT}; font-size: 13px;")
            btn = _link("Comparing below" if key == current else "Compare", lambda k=key: self.compare.emit(k))
            btn.setAccessibleName(f"Compare the titles: {text}")
            rl.addWidget(t, 1)
            rl.addWidget(btn, 0, Qt.AlignmentFlag.AlignTop)
            self.rows.addWidget(row)
            self.lines.append((t, btn))
        self.setVisible(bool(items))


# ---------------------------------------------------------------------------
# Comparing the titles that hold one episode
# ---------------------------------------------------------------------------

@dataclass
class CompareTitle:
    """One title (or library file) in the compare panel, as the Match page describes it."""
    key: str                     # "here:6", "disc:<job id>:7", "library:<file name>"
    title_index: int | None      # on this disc, else None
    label: str                   # "Title 27", "Disc 1, title 12", "In the library"
    sub: str                     # "00034.m2ts · 7:14 · 1.0 GB"
    rows: list[tuple[str, str, str]] = field(default_factory=list)   # (caption, value, colour or "")
    path: str = ""               # a file that plays it, "" when none yet
    card_t: float | None = None  # seconds in where its title card names the episode
    card_named: bool = False     # its card reads the episode's name
    fixes: bool = False          # its match can be changed from here
    keep_text: str = ""          # "This is Daddy Putdown", "" when not offered
    version_text: str = "Keep as a second version"   # "Keep as the French version" for a dub
    suggest_keep: bool = False   # its card names the episode and it isn't the shorter cut
    note: str = ""               # one sentence under the buttons
    watch_tip: str = ""


class ComparePanel(QFrame):
    keep = Signal(int)
    version = Signal(int)
    extra = Signal(int)
    pick = Signal(int)
    watch = Signal(str)          # a CompareTitle key
    read_cards = Signal()
    closed = Signal()

    def __init__(self):
        super().__init__()
        self.setObjectName("compare")
        self.setStyleSheet("QFrame#compare { background: #1A1D20; border: 1px solid #3A4046; border-radius: 12px; }"
                           "QFrame#cmpTitle { background: #15181B; border: 1px solid #3A4046; border-radius: 10px; }"
                           "QFrame#cmpTitle[named=\"true\"] { border-color: #2F5A42; }"
                           "QFrame#cmpHead { background: transparent; border: none; border-bottom: 1px solid #2E3338;"
                           " border-radius: 0; }")
        self.heading = _label("", "h2")
        self.read_btn = _button("Read both title cards")
        self.read_btn.clicked.connect(self.read_cards.emit)
        self.close_btn = _button("Close")
        self.close_btn.setAccessibleName("Close the comparison")
        self.close_btn.clicked.connect(self.closed.emit)
        head = QFrame()
        head.setObjectName("cmpHead")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(20, 12, 20, 12)
        hl.setSpacing(12)
        hl.addWidget(self.heading, 1)
        hl.addWidget(self.read_btn)
        hl.addWidget(self.close_btn)
        self.hint = _label("", wrap=True)
        self.hint.setStyleSheet("color: #CFE3F5; font-size: 13px;")
        self.cards = QHBoxLayout()
        self.cards.setSpacing(14)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(head)
        body = QVBoxLayout()
        body.setContentsMargins(20, 12, 20, 18)
        body.setSpacing(12)
        body.addWidget(self.hint)
        body.addLayout(self.cards)
        lay.addLayout(body)
        self.watch_buttons: dict[str, QPushButton] = {}
        self.fix_buttons: dict[tuple[str, int], QPushButton] = {}
        self.hide()

    def set_compare(self, heading: str, hint: str, titles: list[CompareTitle], read_text: str = "",
                    reading: bool = False) -> None:
        """read_text "" hides the Read title cards button."""
        self.heading.setText(heading)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.read_btn.setVisible(bool(read_text))
        self.read_btn.setText("Reading title cards…" if reading else read_text)
        self.read_btn.setEnabled(not reading)
        while self.cards.count():
            w = self.cards.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()
        self.watch_buttons, self.fix_buttons = {}, {}
        for t in titles:
            self.cards.addWidget(self._card(t), 1)
        self.show()

    def _card(self, t: CompareTitle) -> QFrame:
        f = QFrame()
        f.setObjectName("cmpTitle")
        f.setProperty("named", "true" if t.card_named else None)
        f.setMinimumWidth(260)
        f.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay = QVBoxLayout(f)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)
        name = QLabel(t.label)
        name.setStyleSheet("font-weight: 600; font-size: 14px;")
        sub = QLabel(t.sub)
        sub.setFont(mono_font(sub.font(), 12))
        sub.setStyleSheet(f"color: {MUTED};")
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(name)
        top.addWidget(sub, 1)
        lay.addLayout(top)
        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(4)
        for r, (cap, value, colour) in enumerate(t.rows):
            c = QLabel(cap)
            c.setStyleSheet(f"color: {MUTED}; font-size: 13px;")
            v = QLabel(value)
            v.setWordWrap(True)
            v.setStyleSheet(f"color: {colour or SOFT}; font-size: 13px;")
            grid.addWidget(c, r, 0, Qt.AlignmentFlag.AlignTop)
            grid.addWidget(v, r, 1)
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        buttons = QWidget()
        flow = FlowLayout(buttons, spacing=8)
        w = watch_button("Watch")
        w.setEnabled(bool(t.path))
        w.setToolTip(t.watch_tip)
        w.setAccessibleName(f"Watch {t.label}")
        w.clicked.connect(lambda _c=False, k=t.key: self.watch.emit(k))
        flow.addWidget(w)
        self.watch_buttons[t.key] = w
        if t.fixes and t.title_index is not None:
            ti = t.title_index
            for kind, text, sig in (("keep", t.keep_text, self.keep), ("version", t.version_text, self.version),
                                    ("extra", "Save as an extra", self.extra), ("pick", "Pick another episode", self.pick)):
                if not text:
                    continue
                b = _button(text, primary=kind == "keep" and t.suggest_keep)
                b.clicked.connect(lambda _c=False, s=sig, i=ti: s.emit(i))
                flow.addWidget(b)
                self.fix_buttons[(kind, ti)] = b
        lay.addWidget(buttons)
        buttons.setMinimumHeight(flow.heightForWidth(320))
        if t.note:
            n = QLabel(t.note)
            n.setWordWrap(True)
            n.setStyleSheet(f"color: {MUTED}; font-size: 12px;")
            lay.addWidget(n)
        lay.addStretch(1)
        return f


# ---------------------------------------------------------------------------
# The season grid
# ---------------------------------------------------------------------------

class SeasonCells(QWidget):
    """One small cell per episode, painted, with a tooltip each. A duplicate cell can be clicked."""

    clicked = Signal(int)        # the episode number

    MIN_W, H, GAP = 46, 40, 4

    def __init__(self):
        super().__init__()
        self.cells = []
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def set_cells(self, cells) -> None:
        self.cells = list(cells)
        self.updateGeometry()
        self.setFixedHeight(self.heightForWidth(max(self.width(), 600)))
        summary = "; ".join(c.tip for c in self.cells if c.state in ("duplicate",)) or ""
        self.setAccessibleName("Episodes of the season")
        self.setAccessibleDescription(summary)
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
        return max(0, rows * (self.H + self.GAP) - self.GAP)

    def sizeHint(self):  # noqa: N802
        return QSize(600, self.heightForWidth(600))

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        h = self.heightForWidth(self.width())
        if h != self.height():
            self.setFixedHeight(h)

    def _rects(self) -> list[QRectF]:
        cols = self.columns(self.width())
        cw = (self.width() - self.GAP * (cols - 1)) / cols
        return [QRectF((i % cols) * (cw + self.GAP), (i // cols) * (self.H + self.GAP), cw, self.H)
                for i in range(len(self.cells))]

    def paintEvent(self, _e):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        num = mono_font(self.font(), 12)
        small = QFont(self.font())
        small.setPixelSize(10)
        small.setWeight(QFont.Weight.DemiBold)
        for c, r in zip(self.cells, self._rects()):
            fill, border, text, _legend = CELL_LOOK[c.state]
            rr = r.adjusted(0.5, 0.5, -0.5, -0.5)
            pen = QPen(QColor(border), 1)
            if c.state == "free":
                pen.setStyle(Qt.PenStyle.DashLine)
            p.setPen(pen)
            p.setBrush(QColor(fill) if fill else Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rr, 6, 6)
            p.setPen(QColor(text))
            p.setFont(num)
            top = r.adjusted(0, 3, 0, -r.height() / 2 + 3) if c.mark else r
            p.drawText(top, Qt.AlignmentFlag.AlignCenter, str(c.number))
            if c.mark:
                p.setFont(small)
                p.drawText(r.adjusted(0, r.height() / 2 - 1, 0, -3), Qt.AlignmentFlag.AlignCenter, c.mark)
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
        self.setCursor(Qt.CursorShape.PointingHandCursor if c.state == "duplicate" else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, e):  # noqa: N802
        i = self.cell_at(e.position().toPoint())
        if i is not None and self.cells[i].state == "duplicate":
            self.clicked.emit(self.cells[i].number)


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
            sample = {"here": "T6", "disc": "D1", "library": "Lib", "duplicate": "×2", "free": "&nbsp;&nbsp;&nbsp;"}[k]
            parts.append(f'<span style="background:{fill or "transparent"}; color:{CELL_LOOK[k][2]}; '
                         f'border:1px {style} {border};">&nbsp;{sample}&nbsp;</span>&nbsp;{words}: {counts[k]}')
        self.legend.setText(" &nbsp;&nbsp;&nbsp; ".join(parts))
        discs = f"{st.discs} disc{'s' if st.discs != 1 else ''} of this season"
        lib = (f" and {st.library_files} file{'s' if st.library_files != 1 else ''} already in the season folder"
               if st.library_files else "")
        self.checked.setText(f"Checked against {discs}{lib}. A re-rip of a disc replaces its earlier rip. "
                             "T is a title on this disc and D another disc's number.")
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
