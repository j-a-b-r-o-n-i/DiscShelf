"""The Rip page's panel while a disc scans: a clock, the scan's steps with their times, why it
is taking long, MakeMKV's requests to its server, and what can be settled meanwhile.

Like seasonui.py, this imports only PySide6 and Disc Shelf's own plain modules, so gui.py can
import it. The app's stylesheet styles the labels and buttons through their "role",
"primary" and "small" properties; the frames here carry their own colours.
"""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QRadioButton, QSizePolicy, QVBoxLayout, QWidget)

from .scanwatch import BIN_S, ScanStatus, fmt_clock

TEXT, SOFT, MUTED, HINT = "#E9EBED", "#C3C8CD", "#A2A9B0", "#7D858C"
ACCENT, GOOD, BAD = "#F0A23B", "#8FE0B0", "#F2A7A7"
BAR = "#7CB8F0"
BANNER = {   # (background, border)
    "work": ("#1F1A12", "#5A4520"),
    "stuck": ("#241413", "#6B2C28"),
}
MARK = {"done": ("✓", GOOD), "now": ("●", ACCENT), "stuck": ("!", BAD), "wait": ("○", HINT)}


def _label(text: str = "", role: str | None = None, wrap: bool = False) -> QLabel:
    lab = QLabel(text)
    if role:
        lab.setProperty("role", role)
    lab.setWordWrap(wrap)
    return lab


def _button(text: str, primary: bool = False) -> QPushButton:
    b = QPushButton(text)
    if primary:
        b.setProperty("primary", "true")
    b.setProperty("small", "true")
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


# --------------------------------------------------------------------------- words


def headline(st: ScanStatus) -> str:
    if st.stalled:
        return "The scan has stopped making progress"
    if st.getting_keys:
        return "Getting this disc's keys from MakeMKV's server"
    return "Reading the disc"


def why(st: ScanStatus) -> str:
    if st.stalled:
        what = "printed nothing and made no request to its server" if st.sees_traffic else "printed nothing"
        again = " Scanning again picks up the keys already fetched." if st.requests else ""
        return (f"MakeMKV has {what} for {round(st.quiet_s / 60)} min. That usually means the drive or the "
                f"connection is stuck.{again}")
    if st.getting_keys:
        key_s = next((s.seconds for s in st.steps if s.name.startswith("Getting")), 0.0)
        return ("4K discs use a newer copy protection, and MakeMKV looks up their keys online, one at a time. "
                f"That has taken {_minutes(key_s)} so far, and the drive waits meanwhile. The rest of the scan "
                "is quick once the keys are in.")
    if st.task:
        return f"MakeMKV is reading the disc's titles and tracks. It says: {st.task}."
    return "MakeMKV is reading the disc's titles and tracks."


def _minutes(s: float) -> str:
    if s < 90:
        return f"{max(1, round(s))} s"
    return f"about {round(s / 60)} min"


def requests_note(st: ScanStatus) -> str:
    recent = sum(st.bins[-3:]) if st.bins else 0
    if st.stalled:
        return f"none for {round(st.quiet_s / 60)} min"
    if recent >= 25:
        return "about 1 a second"
    return f"{st.requests} so far"


# --------------------------------------------------------------------------- widgets


class RequestBars(QWidget):
    """Requests to MakeMKV's server per 10 s, the last 5 minutes."""

    def __init__(self):
        super().__init__()
        self.bins: list[int] = []
        self.setFixedHeight(46)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_bins(self, bins: list[int]) -> None:
        self.bins = list(bins)
        n = sum(bins)
        self.setAccessibleDescription(f"{n} requests to MakeMKV's server in the last {len(bins) * BIN_S / 60:.0f} minutes.")
        self.update()

    def paintEvent(self, _ev):  # noqa: N802 -- Qt's name
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(QColor("#3A4046"))
        p.drawLine(0, h - 1, w, h - 1)
        if not self.bins:
            return
        top = max(12, max(self.bins))
        step = w / len(self.bins)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(BAR))
        for i, n in enumerate(self.bins):
            if n <= 0:
                continue
            bh = (h - 4) * min(n, top) / top
            p.drawRoundedRect(QRectF(i * step + 1, h - 2 - bh, max(2.0, step - 3), bh), 2, 2)


class StepRow(QWidget):
    def __init__(self):
        super().__init__()
        self.mark = QLabel()
        self.mark.setFixedWidth(18)
        self.mark.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
        self.name = QLabel()
        self.note = _label("", "small", wrap=True)
        self.time = _label("", "meta")
        self.time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        texts.setContentsMargins(0, 0, 0, 0)
        texts.addWidget(self.name)
        texts.addWidget(self.note)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(12)
        lay.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(texts, 1)
        lay.addWidget(self.time, 0, Qt.AlignmentFlag.AlignTop)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def show_step(self, step) -> None:
        glyph, colour = MARK.get(step.state, MARK["wait"])
        self.mark.setText(glyph)
        self.mark.setStyleSheet(f"color: {colour}; font-weight: 700;")
        active = step.state in ("now", "stuck")
        self.name.setText(step.name)
        self.name.setStyleSheet(f"color: {HINT if step.state == 'wait' else TEXT};"
                                f" font-weight: {600 if active else 400};")
        self.note.setText(step.note)
        self.note.setVisible(bool(step.note))
        self.time.setText(fmt_clock(step.seconds) if step.state != "wait" else "")
        bg = {"now": "#221C12", "stuck": "#2A1716"}.get(step.state, "transparent")
        self.setStyleSheet(f"StepRow {{ background: {bg}; border-radius: 8px; }}")
        self.setAccessibleName(f"{step.name}, {dict(done='done', now='in progress', stuck='no progress', wait='waiting')[step.state]}")


class ScanPanel(QFrame):
    """Shown on the Rip page while a disc scans. The window calls start() when a scan begins,
    show_status() every half second with the controller's ScanStatus, and stop() at the end."""

    stop_clicked = Signal()
    rescan_clicked = Signal()
    eject_clicked = Signal()
    after_scan_changed = Signal(str)     # "show" | "rip_film"
    notify_changed = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.snooze = 0.0            # quiet seconds after which the stalled banner shows again
        outer = QHBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(24)

        # Left: the scan itself
        left = QVBoxLayout()
        left.setSpacing(14)
        self.title = _label("Scanning the disc", "h2")
        self.clock = _label("0:00", "big")
        self.clock.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.usual = _label("", "small")
        self.usual.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock_col = QVBoxLayout()
        clock_col.setSpacing(2)
        clock_col.addWidget(self.clock)
        clock_col.addWidget(self.usual)
        top = QHBoxLayout()
        top.addWidget(self.title, 1, Qt.AlignmentFlag.AlignTop)
        top.addLayout(clock_col)
        left.addLayout(top)

        self.banner = QFrame()
        self.banner.setObjectName("scanBanner")
        bl = QVBoxLayout(self.banner)
        bl.setContentsMargins(14, 12, 14, 12)
        bl.setSpacing(6)
        self.head = _label("", "h3", wrap=True)
        self.why = _label("", "soft", wrap=True)
        self.rescan_btn = _button("Stop and scan again", primary=True)
        self.wait_btn = _button("Keep waiting")
        self.eject_btn = _button("Eject")
        self.rescan_btn.clicked.connect(lambda: self.rescan_clicked.emit())
        self.wait_btn.clicked.connect(lambda: self.keep_waiting())
        self.eject_btn.clicked.connect(lambda: self.eject_clicked.emit())
        self.stuck_row = QWidget()
        sr = QHBoxLayout(self.stuck_row)
        sr.setContentsMargins(0, 4, 0, 0)
        for b in (self.rescan_btn, self.wait_btn, self.eject_btn):
            sr.addWidget(b)
        sr.addStretch(1)
        bl.addWidget(self.head)
        bl.addWidget(self.why)
        bl.addWidget(self.stuck_row)
        left.addWidget(self.banner)

        self.rows = [StepRow() for _ in range(4)]
        steps = QVBoxLayout()
        steps.setSpacing(2)
        for r in self.rows:
            steps.addWidget(r)
        left.addLayout(steps)

        self.req_box = QWidget()
        rl = QVBoxLayout(self.req_box)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(6)
        self.req_note = _label("", "small")
        cap = QHBoxLayout()
        cap.addWidget(_label("Requests to MakeMKV's server, each 10 s", "small"), 1)
        cap.addWidget(self.req_note)
        rl.addLayout(cap)
        self.bars = RequestBars()
        rl.addWidget(self.bars)
        left.addWidget(self.req_box)

        self.stop_btn = _button("Stop scan")
        self.stop_btn.clicked.connect(lambda: self.stop_clicked.emit())
        self.foot = _label("Fill in the details on the right while you wait. Copying, encoding and matching "
                           "carry on during a scan.", "small", wrap=True)
        foot = QHBoxLayout()
        foot.setSpacing(12)
        foot.addWidget(self.stop_btn, 0, Qt.AlignmentFlag.AlignTop)
        foot.addWidget(self.foot, 1)
        left.addLayout(foot)
        left.addStretch(1)
        outer.addLayout(left, 3)

        # Right: what can be settled while it scans
        right = QVBoxLayout()
        right.setSpacing(12)
        right.addWidget(_label("While it scans", "h2"))
        right.addWidget(_label("These need the disc's name, not the drive.", "small", wrap=True))
        self.facts = QGridLayout()
        self.facts.setHorizontalSpacing(14)
        self.facts.setVerticalSpacing(8)
        self.fact_vals: dict[str, QLabel] = {}
        for r, key in enumerate(("Looks like", "On the NAS", "Films go to", "Shows go to", "HDR", "AV1 version",
                                 "Languages")):
            k = _label(key, "small")
            k.setAlignment(Qt.AlignmentFlag.AlignTop)
            v = _label("", wrap=True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.facts.addWidget(k, r, 0)
            self.facts.addWidget(v, r, 1)
            self.fact_vals[key] = v
        self.facts.setColumnStretch(1, 1)
        right.addLayout(self.facts)

        right.addWidget(_label("When the scan finishes", "h3"))
        self.show_opt = QRadioButton("Show me the titles")
        self.rip_opt = QRadioButton("Rip the main film right away")
        grp = QButtonGroup(self)
        for b in (self.show_opt, self.rip_opt):
            grp.addButton(b)
        right.addWidget(self.show_opt)
        right.addWidget(_label("Nothing starts until you choose Rip.", "small", wrap=True))
        right.addWidget(self.rip_opt)
        right.addWidget(_label("Only for a film, and only when one title is over 60 min. Otherwise it waits for "
                               "you.", "small", wrap=True))
        self.show_opt.toggled.connect(lambda on: on and self.after_scan_changed.emit("show"))
        self.rip_opt.toggled.connect(lambda on: on and self.after_scan_changed.emit("rip_film"))
        self.notify = QCheckBox("Send a desktop notification when it's ready")
        self.notify.toggled.connect(lambda on: self.notify_changed.emit(bool(on)))
        right.addWidget(self.notify)
        right.addStretch(1)
        side = QWidget()
        side.setLayout(right)
        side.setMinimumWidth(300)
        outer.addWidget(side, 2)
        self._paint_banner("work")

    # -- driving ----------------------------------------------------------------
    def start(self, label: str, drive: str, usual: str, after_scan: str, notify: bool) -> None:
        self.snooze = 0.0
        self.title.setText(f"Scanning {label}" if label else "Scanning the disc")
        self.usual.setText(usual)
        self.usual.setVisible(bool(usual))
        for b, on in ((self.show_opt, after_scan != "rip_film"), (self.rip_opt, after_scan == "rip_film")):
            b.blockSignals(True)
            b.setChecked(on)
            b.blockSignals(False)
        self.notify.blockSignals(True)
        self.notify.setChecked(notify)
        self.notify.blockSignals(False)
        self.show_status(ScanStatus())
        self.show()

    def set_fact(self, key: str, text: str) -> None:
        if key in self.fact_vals:
            self.fact_vals[key].setText(text)

    def keep_waiting(self) -> None:
        self.snooze = self._quiet + self._stall_after
        self.stuck_row.hide()
        self.show_status(self._last)

    def show_status(self, st: ScanStatus | None) -> None:
        st = st or ScanStatus()
        self._last, self._quiet, self._stall_after = st, st.quiet_s, st.stall_after
        stalled = st.stalled and st.quiet_s >= self.snooze
        if not st.stalled:
            self.snooze = 0.0
        shown = st
        if st.stalled and not stalled:     # the person chose to keep waiting
            shown = replace(st, stalled=False)
        self.clock.setText(fmt_clock(st.elapsed))
        self.clock.setStyleSheet(f"color: {BAD};" if stalled else "")
        self.head.setText(headline(shown))
        self.why.setText(why(shown))
        self._paint_banner("stuck" if stalled else "work")
        self.stuck_row.setVisible(stalled)
        self.stop_btn.setVisible(not stalled)
        steps = st.steps if stalled else [s if s.state != "stuck" else _now(s) for s in st.steps]
        for i, row in enumerate(self.rows):
            if i < len(steps):
                row.show_step(steps[i])
                row.show()
            else:
                row.hide()
        self.req_box.setVisible(st.sees_traffic and st.requests > 0)
        self.req_note.setText(requests_note(st))
        self.bars.set_bins(st.bins)

    def _paint_banner(self, kind: str) -> None:
        bg, border = BANNER[kind]
        self.banner.setStyleSheet(f"QFrame#scanBanner {{ background: {bg}; border: 1px solid {border}; "
                                  "border-radius: 10px; }")


def _now(step):
    return replace(step, state="now")
