"""The rip timeline on a disc's queue card: what the drive did for each title, and what's left.

Along the top, each title's number. Below it the reading speed, drawn only while a title is
being read: MakeMKV opening the disc before each title is a hatched band with no line, so the
line never slopes across it. Under that, three rows: the disc (opening, then reading), keeping
the chosen languages, and copying to storage. Titles that read slowly get a red marker. After
"now", the titles still to rip are drawn dashed, from ripstats' estimate, up to the time the
rip should end.

The words that go with it (the readout, the summary, a title's details, the estimate) are made
by the functions at the end, so tests can check them without painting.
"""
from __future__ import annotations

import datetime as _dt
import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from . import ripstats
from .models import Sample, Span

# The app's palette (gui.py); kept here so this module doesn't import the window.
SURFACE, BORDER, ROWLINE, FIELD = "#15181B", "#2E3338", "#262A2E", "#3A4046"
TEXT, SOFT, MUTED, DIM = "#E9EBED", "#C3C8CD", "#A2A9B0", "#6B7278"
ACCENT, COPY, LANGS, SLOW, END = "#F0A23B", "#7CB8F0", "#A2A9B0", "#E5534B", "#8FE0B0"
BLURAY_1X = 4.5e6
TEN_MIN = 600.0
NONE = -1             # no title (title numbers start at 0 on some discs)

LANES = (("disc", "Disc"), ("languages", "Languages"), ("copy", "Copy to storage"))


def _nice(top: float) -> float:
    raw = top / 3.0
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    return next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)


def _scale_top(rates: list[float]) -> float:
    """The highest speed the axis has to show, in MB/s. A lone sample far above the rest (a file
    counted in one go) would flatten everything else, so it is clipped at the top instead."""
    vals = sorted(r for r in rates if r > 0)
    if not vals:
        return 1.0
    usual = vals[int(0.95 * (len(vals) - 1))]
    return max(1.0, min(vals[-1], usual * 1.5) if len(vals) >= 10 else vals[-1])


def fmt_dur(s: float) -> str:
    s = int(round(s))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {s // 60 % 60:02d} min"


def fmt_mb(b: float) -> str:
    return f"{b / 1e9:.1f} GB" if b >= 10e9 else f"{b / 1e6:,.0f} MB"


class RipTimeline(QWidget):
    """See the module docstring. set_data() takes the rip's samples and spans as the window
    keeps them; clicking a title emits picked(title), or picked(NONE) where there's none."""

    picked = Signal(int)

    def __init__(self, height: int = 196):
        super().__init__()
        self.setMouseTracking(True)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.samples: list[Sample] = []
        self.spans: list[Span] = []
        self.est: ripstats.Estimate | None = None
        self.now = 0.0
        self.wall = _dt.datetime.now()
        self.order: list[int] = []
        self.whole = True
        self.selected = NONE
        self.boxes: dict[int, tuple[float, float]] = {}     # title -> (x from, x to), from the last paint
        self.number_of: dict[int, int] = {}                 # title -> its place in the rip, from 1

    def set_data(self, samples, spans, est, now: float, order: list[int], wall=None, whole: bool = True,
                 selected: int = NONE, summary: str = "") -> None:
        self.samples = [s for s in samples if s.kind == "rip"]
        self.spans = list(spans)
        self.est, self.now, self.order = est, now, list(order)
        self.number_of = {ti: k + 1 for k, ti in enumerate(order)}
        self.wall = wall or _dt.datetime.now()
        self.whole, self.selected = whole, selected
        self.setAccessibleName("Rip timeline")
        self.setAccessibleDescription(summary)
        self.update()

    # -- geometry -------------------------------------------------------------
    def _range(self) -> tuple[float, float]:
        starts = [s.start for s in self.spans] + [s.t for s in self.samples[:1]]
        t0 = min(starts) if starts else 0.0
        end = self.now
        if self.est is not None and self.est.planned:
            end = max(end, self.est.planned[-1].end)
        if self.whole:
            return t0, max(end, t0 + 60)
        return max(t0, self.now - TEN_MIN), self.now + 60

    def clock(self, t: float) -> _dt.datetime:
        return self.wall - _dt.timedelta(seconds=self.now - t)

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        mono = QFont("IBM Plex Mono")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setPixelSize(11)
        sans = QFont(self.font())
        sans.setPixelSize(11)
        W, H = self.width(), self.height()
        fm = QFontMetrics(sans)
        L = max(fm.horizontalAdvance(t) for _k, t in LANES) + 14
        R = W - 6.0
        num_y, top, bottom = 12.0, 22.0, 104.0
        lane_y = {"disc": 112.0, "languages": 134.0, "copy": 152.0}
        lane_h = {"disc": 14.0, "languages": 10.0, "copy": 10.0}
        axis_y = H - 4.0
        t0, t1 = self._range()

        def X(t: float) -> float:
            return L + (t - t0) / max(1e-6, t1 - t0) * (R - L)

        reads = [s for s in self.spans if s.kind == "read"]
        drawn = {id(s): self._points(s) for s in reads}
        rates = [r for pts in drawn.values() for t, r in pts if t0 <= t <= t1]
        top_v = _scale_top(rates + ([self.est.rate / 1e6] if self.est else []))
        step = _nice(top_v)
        vmax = step * math.ceil(top_v * 1.1 / step)

        def Y(v: float) -> float:
            return bottom - min(v, vmax) / vmax * (bottom - top)

        p.save()
        p.setClipRect(QRectF(L, 0, R - L + 6, H))
        # The future, behind everything
        if self.now < t1:
            p.fillRect(QRectF(X(self.now), top, R - X(self.now), lane_y["copy"] + 10 - top), QColor("#181B1E"))
        p.restore()

        # Grid and its labels
        p.setFont(mono)
        v = 0.0
        while v <= vmax + 1e-9:
            y = Y(v)
            p.setPen(QPen(QColor(FIELD if v == 0 else ROWLINE), 1))
            p.drawLine(QPointF(L, y), QPointF(R, y))
            p.setPen(QColor(MUTED))
            txt = f"{v:g}" + (" MB/s" if v + step > vmax + 1e-9 else "")
            p.drawText(QRectF(0, y - 7, L - 6, 14), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, txt)
            v += step
        p.setFont(sans)
        p.setPen(QColor(SOFT))
        for key, text in LANES:
            p.drawText(QRectF(0, lane_y[key] - 2, L - 6, lane_h[key] + 4),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

        p.save()
        p.setClipRect(QRectF(L, 0, R - L + 6, H))
        hatch = QBrush(QColor(FIELD), Qt.BrushStyle.BDiagPattern)
        self.boxes = {}

        def band(a: float, b: float, y: float, h: float, brush, pen=None, rounded: float = 3.0):
            xa, xb = X(a), X(b)
            w = max(3.0, xb - xa)
            p.setPen(pen or Qt.PenStyle.NoPen)
            p.setBrush(brush)
            p.drawRoundedRect(QRectF(xa, y, w, h), rounded, rounded)

        # Opening the disc: hatched across the speed area and in the disc row.
        for s in self.spans:
            if s.kind != "open":
                continue
            end = s.end if s.end is not None else self.now
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(hatch)
            p.drawRect(QRectF(X(s.start), top, max(1.0, X(end) - X(s.start)), bottom - top))
            band(s.start, end, lane_y["disc"], lane_h["disc"], hatch, rounded=0)
            self._box(s.title_index, X(s.start), X(end))

        # Reading: the speed while reading, one piece per title, and the disc row.
        accent = QColor(ACCENT)
        fill = QColor(ACCENT)
        fill.setAlphaF(0.16)
        for s in reads:
            pts = drawn[id(s)]
            if len(pts) >= 2:
                path = QPainterPath(QPointF(X(pts[0][0]), Y(pts[0][1])))
                for t, r in pts[1:]:
                    path.lineTo(QPointF(X(t), Y(r)))
                area = QPainterPath(path)
                area.lineTo(QPointF(X(pts[-1][0]), bottom))
                area.lineTo(QPointF(X(pts[0][0]), bottom))
                area.closeSubpath()
                p.fillPath(area, fill)
                pen = QPen(accent, 2)
                pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawPath(path)
            band(s.start, end, lane_y["disc"], lane_h["disc"], accent)
            self._box(s.title_index, X(s.start), X(end))

        for s in self.spans:
            if s.kind in ("languages", "copy"):
                band(s.start, s.end if s.end is not None else self.now, lane_y[s.kind], lane_h[s.kind],
                     QColor(LANGS if s.kind == "languages" else COPY), rounded=2)

        # The estimate: dashed titles in the disc row, the expected speed as a dashed line.
        if self.est is not None:
            dash = QPen(accent, 1.5)
            dash.setDashPattern([3, 2])
            faint = QBrush(QColor(FIELD), Qt.BrushStyle.BDiagPattern)
            for k, pl in enumerate(self.est.planned):
                read_from = max(pl.read_start, self.now)
                if pl.read_start > self.now:
                    open_from = max(pl.open_start, self.now)
                    p.setOpacity(0.5)
                    band(open_from, pl.read_start, lane_y["disc"], lane_h["disc"], faint, rounded=0)
                    p.setOpacity(1.0)
                if pl.end > read_from:
                    band(read_from, pl.end, lane_y["disc"] + 0.75, lane_h["disc"] - 1.5, Qt.BrushStyle.NoBrush, dash)
                    line = QPen(accent, 1.5)
                    line.setDashPattern([4, 3])
                    p.setPen(line)
                    p.drawLine(QPointF(X(read_from), Y(self.est.rate / 1e6)), QPointF(X(pl.end), Y(self.est.rate / 1e6)))
                if k:   # the current title's box comes from its spans
                    self._box(pl.title_index, X(pl.open_start), X(pl.end))
                elif pl.title_index in self.boxes:
                    a, _b = self.boxes[pl.title_index]
                    self.boxes[pl.title_index] = (a, X(pl.end))
            end = self.est.planned[-1].end if self.est.planned else self.now
            if self.whole and end > self.now:
                pen = QPen(QColor(END), 1)
                pen.setDashPattern([3, 3])
                p.setPen(pen)
                p.drawLine(QPointF(X(end), num_y - 2), QPointF(X(end), lane_y["copy"] + 10))
                p.setFont(sans)
                p.setPen(QColor(END))
                txt = f"ripped ~{self.clock(end):%H:%M}"
                p.drawText(QRectF(X(end) - 160, top + 2, 154, 14), Qt.AlignmentFlag.AlignRight, txt)

        # Slow titles
        slow = ripstats.slow_titles(ripstats.timings(self.spans))
        for s in reads:
            if s.title_index in slow and s.end is not None:
                cx = (X(s.start) + X(s.end)) / 2
                tri = QPainterPath(QPointF(cx - 5, top + 1))
                tri.lineTo(QPointF(cx + 5, top + 1))
                tri.lineTo(QPointF(cx, top + 8))
                tri.closeSubpath()
                p.fillPath(tri, QColor(SLOW))

        # Title numbers, the selected one in white and bold
        p.setFont(mono)
        for ti, (a, b) in self.boxes.items():
            n = self.number_of.get(ti, ti)
            if b - a < QFontMetrics(mono).horizontalAdvance(str(n)) + 4:
                continue
            sel = ti == self.selected
            f = QFont(mono)
            f.setBold(sel)
            p.setFont(f)
            planned = ti not in {s.title_index for s in self.spans}
            p.setPen(QColor(TEXT if sel else DIM if planned else MUTED))
            p.drawText(QRectF(a, 0, b - a, num_y + 4), Qt.AlignmentFlag.AlignCenter, str(n))
        if self.selected in self.boxes:
            a, b = self.boxes[self.selected]
            p.setPen(QPen(QColor(TEXT), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(QRectF(a - 2, top - 2, b - a + 4, lane_y["copy"] + 14 - top), 3, 3)

        # Now
        if t0 <= self.now <= t1:
            pen = QPen(QColor(TEXT), 1)
            pen.setDashPattern([2, 3])
            p.setPen(pen)
            p.drawLine(QPointF(X(self.now), top - 4), QPointF(X(self.now), lane_y["copy"] + 10))
            last = next((x for x in reversed(self.samples) if x.kind == "rip"), None)
            reading = any(s.end is None for s in reads)
            if last is not None and reading:
                p.setPen(QPen(QColor(SURFACE), 2))
                p.setBrush(accent)
                p.drawEllipse(QPointF(X(self.now), Y(last.rate / 1e6)), 4, 4)
        p.restore()

        # Times along the bottom
        p.setFont(mono)
        p.setPen(QColor(MUTED))
        span = t1 - t0
        step_s = next(s for s in (60, 120, 300, 600, 900, 1800, 3600, 7200) if span / s <= 7)
        first = self.clock(t0)
        secs = first.hour * 3600 + first.minute * 60 + first.second
        t = t0 + (step_s - secs % step_s) % step_s
        nowx = X(self.now)
        while t <= t1:
            x = X(t)
            if L + 16 < x < R - 16 and abs(x - nowx) > 30:
                p.drawText(QRectF(x - 30, axis_y - 14, 60, 14), Qt.AlignmentFlag.AlignCenter, f"{self.clock(t):%H:%M}")
            t += step_s
        if t0 <= self.now <= t1:
            f = QFont(mono)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(nowx - 30, axis_y - 14, 60, 14), Qt.AlignmentFlag.AlignCenter, "now")
        p.end()

    def _points(self, s: Span) -> list[tuple[float, float]]:
        """The reading speed in MB/s during one read span. Copy samples share the list and are left out."""
        end = s.end if s.end is not None else self.now
        return [(x.t, x.rate / 1e6) for x in self.samples
                if x.kind == "rip" and s.start <= x.t <= end + 0.5]

    def _box(self, ti: int, a: float, b: float) -> None:
        old = self.boxes.get(ti)
        self.boxes[ti] = (min(a, old[0]), max(b, old[1])) if old else (a, b)

    # -- mouse ----------------------------------------------------------------
    def title_at(self, x: float) -> int:
        for ti, (a, b) in self.boxes.items():
            if a - 1 <= x <= b + 1:
                return ti
        return NONE

    def mouseMoveEvent(self, ev):
        ti = self.title_at(ev.position().x())
        tip = title_text(ti, self.number_of.get(ti, ti), self.spans, self.est, self.now, self.wall)[1] \
            if ti != NONE else ""
        if tip:
            QToolTip.showText(ev.globalPosition().toPoint(), tip, self)
        else:
            QToolTip.hideText()
        self.setCursor(Qt.CursorShape.PointingHandCursor if ti != NONE else Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.picked.emit(self.title_at(ev.position().x()))


# -- words ----------------------------------------------------------------------

def readout(spans: list[Span], samples: list[Sample], order: list[int]) -> str:
    """The big line above the timeline: the speed while reading, or what the drive is doing."""
    reading = next((s for s in spans if s.kind == "read" and s.end is None), None)
    opening = next((s for s in spans if s.kind == "open" and s.end is None), None)
    rips = [s for s in samples if s.kind == "rip"]
    if reading is not None and rips and rips[-1].t >= reading.start and rips[-1].rate > 0:
        r = rips[-1].rate
        return f"{r / 1e6:.1f} MB/s · {r / BLURAY_1X:.1f}×"
    if opening is not None:
        n = order.index(opening.title_index) + 1 if opening.title_index in order else opening.title_index
        return f"Opening the disc for title {n}"
    if reading is not None:
        return "Reading"
    return ""


def summary(spans: list[Span], est: ripstats.Estimate | None, now: float, ripped: int) -> str:
    """'13.7 GB ripped in 37 min · 8.1 MB/s while reading · 28% of the time opening the disc'."""
    start = min((s.start for s in spans), default=now)
    parts = [f"{fmt_mb(ripped)} ripped in {max(1, round((now - start) / 60))} min"]
    if est is not None:
        parts.append(f"{est.rate / 1e6:.1f} MB/s while reading")
    opened = sum((s.end if s.end is not None else now) - s.start for s in spans if s.kind == "open")
    if now - start > 60 and opened > 0:
        parts.append(f"{round(opened / (now - start) * 100)}% of the time opening the disc")
    return " · ".join(parts)


def estimate_text(est: ripstats.Estimate | None, wall: _dt.datetime) -> tuple[str, str]:
    """(heading, how it's worked out) for the line under the timeline."""
    if est is None:
        return "", ""
    mins = max(1, round(est.seconds / 60))
    done = wall + _dt.timedelta(seconds=est.seconds)
    head = f"Ripping done in about {mins} min, around {done:%H:%M}"
    titles = f"{est.titles_left} title{'s' if est.titles_left != 1 else ''} left"
    size = f"about {fmt_mb(est.per_title)} each"
    if abs(est.ratio - 1) > 0.1:
        size += f" ({round(est.ratio * 100)}% of MakeMKV's estimate, as on this disc so far)"
    speed = (f"{est.rate / 1e6:.1f} MB/s while reading, over the last {est.recent} title{'s' if est.recent != 1 else ''}"
             if est.recent else f"{est.rate / 1e6:.1f} MB/s while reading so far")
    how = f"{titles}, {round(est.open_s)} s to open the disc before each, {size}, at {speed}."
    lo, hi = max(1, round(est.low / 60)), max(1, round(est.high / 60))
    if hi - lo >= 2:
        how += f" At the speed of the fastest and slowest recent titles, {lo} to {hi} min."
    return head, how


def title_text(ti: int, n: int, spans: list[Span], est: ripstats.Estimate | None, now: float,
               wall: _dt.datetime) -> tuple[str, str]:
    """(heading, details) for one title: what happened to it, or what's expected."""
    tm = ripstats.timings(spans).get(ti)
    mine = {s.kind: s for s in spans if s.title_index == ti}

    def at(t: float) -> str:
        return f"{wall - _dt.timedelta(seconds=now - t):%H:%M:%S}"

    if tm is not None and tm.read_end is not None:
        rate = tm.rate
        head = f"Title {n}, {at(tm.open_start if tm.open_start is not None else tm.read_start)} to {at(tm.read_end)}"
        text = (f"MakeMKV took {fmt_dur(tm.open_s)} to open the disc, then read " if tm.open_s is not None else "Read ")
        text += f"{fmt_mb(tm.bytes)} in {fmt_dur(tm.read_s)}, at {rate / 1e6:.1f} MB/s."
        slow = ti in ripstats.slow_titles(ripstats.timings(spans))
        if slow:
            text += " That's slower than the titles around it."
        lang, copy = mine.get("languages"), mine.get("copy")
        if lang is not None and lang.end is not None:
            text += f" Kept the chosen languages in {fmt_dur(lang.end - lang.start)}."
        if copy is not None:
            text += (f" Copied to storage and checked in {fmt_dur(copy.end - copy.start)}." if copy.end is not None
                     else " Copying to storage now.")
        return head, text
    if tm is not None and tm.read_start is not None:
        return f"Title {n}, reading now", f"Reading since {at(tm.read_start)}" + (
            f", after {fmt_dur(tm.open_s)} opening the disc." if tm.open_s is not None else ".")
    if tm is not None and tm.open_start is not None:
        return f"Title {n}, opening the disc", f"MakeMKV started opening the disc at {at(tm.open_start)}."
    pl = next((p for p in (est.planned if est else []) if p.title_index == ti), None)
    if pl is not None:
        return (f"Title {n}, estimated",
                f"Opens at about {at(pl.open_start)[:5]} and should be about {fmt_mb(pl.bytes)}, done around "
                f"{at(pl.end)[:5]}.")
    return f"Title {n}", "Not ripped yet."
