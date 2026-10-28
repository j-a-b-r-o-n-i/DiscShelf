"""The Rip page's panel for titles that play the same film: what differs between them, which one
is picked and why, and a button to rip another one instead.

Like scanui.py, this imports only PySide6 and Disc Shelf's own plain modules, so gui.py can
import it. The app's stylesheet styles the labels, chips and buttons through their "role",
"chip" and "primary" properties.
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .versions import Group

TEXT, SOFT, MUTED, HINT = "#E9EBED", "#C3C8CD", "#A2A9B0", "#5A6168"
PIECE, MARKED, ON_MARKED = "#4A5560", "#F0A23B", "#1A1206"
STRIP_H, STRIP_GAP = 26, 14


def _label(text: str = "", role: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    return lab


def _chip(text: str, kind: str) -> QLabel:
    lab = QLabel(text)
    lab.setProperty("chip", kind)
    return lab


class Strips(QWidget):
    """One bar per title: the video pieces it plays, in its order. Marked pieces are only in
    that title (a longer cut) or in a different place than in the pick (shuffled copies)."""

    def __init__(self, group: Group, mono: str = "monospace"):
        super().__init__()
        self.group = group
        self.mono = mono
        n = len(group.strips)
        self.setMinimumHeight(n * STRIP_H + (n - 1) * STRIP_GAP)
        self.setAccessibleName("; ".join(f"{s.title.source}: {s.note}" for s in group.strips))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w = self.width()
        most = max(len(s.pieces) for s in self.group.strips) or 1
        unit = w / most
        f = QFont(self.mono)
        f.setPixelSize(11)
        p.setFont(f)
        for row, s in enumerate(self.group.strips):
            y = row * (STRIP_H + STRIP_GAP)
            for i, (seg, marked) in enumerate(s.pieces):
                r = QRectF(i * unit + 1, y, unit - 2, STRIP_H)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(MARKED if marked else PIECE))
                p.drawRoundedRect(r, 3, 3)
                if unit >= 28:
                    p.setPen(QPen(QColor(ON_MARKED if marked else TEXT)))
                    p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(seg))
        p.end()


class VersionsPanel(QFrame):
    """One group of titles. rip_instead(title index) asks the page to rip that title in place of
    the others in the group."""

    rip_instead = Signal(int)

    def __init__(self, group: Group, selected: set[int], mono: str = "monospace", open_: bool = True):
        super().__init__()
        self.setObjectName("needs")
        self.group = group
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 14)
        lay.setSpacing(10)

        head = QHBoxLayout()
        head.addWidget(_label(group.head, "h3"), 1)
        self.toggle = QPushButton()
        self.toggle.setProperty("role", "link")
        self.toggle.setFlat(True)
        self.toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle.setStyleSheet("QPushButton { color: #7CB8F0; border: none; background: transparent; "
                                  "font-size: 13px; padding: 0; min-height: 0; }")
        self.toggle.clicked.connect(lambda: self.set_open(self.body.isHidden()))
        head.addWidget(self.toggle)
        lay.addLayout(head)
        lay.addWidget(_label(group.why, "soft", wrap=True))

        self.body = QWidget()
        b = QVBoxLayout(self.body)
        b.setContentsMargins(0, 4, 0, 0)
        b.setSpacing(10)
        if group.same:
            b.addWidget(_label("The same in both" if len(group.titles) == 2 else f"The same in all {len(group.titles)}",
                               "field"))
            row = QHBoxLayout()
            row.setSpacing(6)
            for s in group.same:
                row.addWidget(_chip(f"✓ {s}", "good"))
            row.addStretch(1)
            b.addLayout(row)
        if group.langs:
            b.addWidget(_label("The languages differ. Each row is one language, with its audio and subtitle "
                               "tracks.", "field", wrap=True))
            b.addWidget(self._lang_table(group, selected))
        if group.strips:
            b.addWidget(_label("Each bar is one title, piece by piece. Orange pieces are "
                               + ("only in that title." if group.kind == "cut" else "in a different place than in "
                                  f"{group.pick.source or 'the picked title'}."), "field", wrap=True))
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(STRIP_GAP)
            for r, s in enumerate(group.strips):
                name = _label(s.title.source or f"Title {s.title.index}")
                name.setProperty("mono", "true")
                grid.addWidget(name, r, 0)
                note = _label(s.note, "small")
                grid.addWidget(note, r, 2)
                grid.setRowMinimumHeight(r, STRIP_H)
            strips = Strips(group, mono)
            grid.addWidget(strips, 0, 1, len(group.strips), 1)
            grid.setColumnStretch(1, 1)
            b.addLayout(grid)

        pick = QFrame()
        pick.setObjectName("doneRow")
        pl = QVBoxLayout(pick)
        pl.setContentsMargins(12, 10, 12, 10)
        pl.setSpacing(4)
        self.pick_head = _label("", "h3", wrap=True)
        pl.addWidget(self.pick_head)
        pl.addWidget(_label(group.pick_why, "small", wrap=True))
        b.addWidget(pick)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        b.addLayout(self.actions)
        lay.addWidget(self.body)
        self.set_selected(selected)
        self.set_open(open_)

    def _lang_table(self, group: Group, selected: set[int]) -> QWidget:
        w = QFrame()
        w.setObjectName("doneRow")
        g = QGridLayout(w)
        g.setContentsMargins(12, 8, 12, 8)
        g.setHorizontalSpacing(18)
        g.setVerticalSpacing(8)
        g.addWidget(_label("Language", "field"), 0, 0)
        self.col_heads = []
        for c, t in enumerate(group.titles, start=1):
            h = _label("", "field")
            self.col_heads.append((h, t))
            g.addWidget(h, 0, c)
            g.setColumnStretch(c, 1)
        for r, row in enumerate(group.langs, start=1):
            name = _label(row.name + ("  · your language" if row.yours else ""), None if row.yours else "soft")
            if row.yours:
                name.setStyleSheet("font-weight: 600; color: #F5C27A;")
            g.addWidget(name, r, 0)
            for c, text in enumerate(row.cells, start=1):
                cell = _label(text, "small" if text == "—" else None, wrap=True)
                g.addWidget(cell, r, c)
        return w

    def set_open(self, open_: bool):
        self.body.setVisible(open_)
        self.toggle.setText("Hide the comparison" if open_ else "Compare the titles")

    def set_selected(self, selected: set[int]):
        """Say which title of the group will be ripped, and offer the others."""
        g = self.group
        chosen = [t for t in g.titles if t.index in selected]
        for h, t in getattr(self, "col_heads", []):
            h.setText(f"{t.source or t.index} · {'ripped' if t.index in selected else 'skipped'}")
        if len(chosen) == 1 and chosen[0] is g.pick:
            self.pick_head.setText(f"Picked {g.pick.source or 'this title'}")
        elif len(chosen) == 1:
            self.pick_head.setText(f"You picked {chosen[0].source}. Disc Shelf would pick {g.pick.source}")
        elif chosen:
            self.pick_head.setText(f"{len(chosen)} of these titles are ticked, so the film is ripped "
                                   f"{len(chosen)} times. Disc Shelf would pick {g.pick.source}")
        else:
            self.pick_head.setText(f"None of these is ticked. Disc Shelf would pick {g.pick.source}")
        while self.actions.count():
            it = self.actions.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        offer = [t for t in g.titles if not (len(chosen) == 1 and t is chosen[0])]
        for i, t in enumerate(offer[:3]):
            only = g.only.get(t.index, "")
            text = (f"Rip {t.source} instead" if chosen else f"Rip {t.source}") + (f" ({only})" if only else "")
            btn = QPushButton(text)
            btn.setProperty("small", "true")
            if t is g.pick:
                btn.setProperty("primary", "true")
            btn.clicked.connect(lambda _c=False, ix=t.index: self.rip_instead.emit(ix))
            self.actions.addWidget(btn)
        if len(offer) > 3:
            self.actions.addWidget(_label(f"and {len(offer) - 3} more in the list above", "small"))
        self.actions.addStretch(1)
