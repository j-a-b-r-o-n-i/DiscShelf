#!/usr/bin/env python3
"""
Draw the README banner and the GitHub social preview from the app icon, into design/:

    python3 scripts/banners.py

- design/banner.png         1280 × 320, the top of README.md
- design/social-preview.png 1280 × 640, for GitHub → Settings → Social preview

They follow the "Banners" board in design/brand-proposals/. Uses IBM Plex Sans when it's
installed, else the system's sans-serif.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtSvg import QSvgRenderer  # noqa: E402

ICON = os.path.join(ROOT, "packaging", "disc-shelf.svg")
OUT = os.path.join(ROOT, "design")


def sans() -> str:
    return "IBM Plex Sans" if "IBM Plex Sans" in QFontDatabase.families() else "sans-serif"


def font(px: int, weight=QFont.Weight.Normal) -> QFont:
    f = QFont(sans())
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def pill(p: QPainter, x: float, y: float, text: str, bg: str, fg: str) -> float:
    f = font(15, QFont.Weight.DemiBold)
    p.setFont(f)
    w = p.fontMetrics().horizontalAdvance(text) + 28
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(bg))
    p.drawRoundedRect(QRectF(x, y, w, 32), 16, 16)
    p.setPen(QColor(fg))
    p.drawText(QRectF(x, y, w, 32), Qt.AlignmentFlag.AlignCenter, text)
    return x + w + 10


def banner(path: str) -> None:
    img = QImage(1280, 320, QImage.Format.Format_ARGB32)
    img.fill(QColor("#F4F4F2"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    QSvgRenderer(ICON).render(p, QRectF(72, 72, 176, 176))
    p.setPen(QColor("#1A1D20"))
    p.setFont(font(64, QFont.Weight.Bold))
    p.drawText(QRectF(292, 70, 940, 76), Qt.AlignmentFlag.AlignVCenter, "Disc Shelf")
    p.setPen(QColor("#4A5056"))
    p.setFont(font(24))
    p.drawText(QRectF(292, 150, 940, 36), Qt.AlignmentFlag.AlignVCenter,
               "Blu-ray, 4K and DVD backups for Plex, with the episodes already named.")
    x = 292.0
    for text, bg, fg in (("Episodes matched for you", "#FBE3B8", "#3A2A12"),
                         ("Copied and checked", "#DCEAF6", "#12324F"),
                         ("HDR kept as on the disc", "#D7EFE2", "#0E3B24")):
        x = pill(p, x, 206, text, bg, fg)
    p.end()
    img.save(path)


def social(path: str) -> None:
    img = QImage(1280, 640, QImage.Format.Format_ARGB32)
    img.fill(QColor("#111315"))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    QSvgRenderer(ICON).render(p, QRectF(80, 96, 168, 168))
    p.setPen(QColor("#E9EBED"))
    p.setFont(font(88, QFont.Weight.Bold))
    p.drawText(QRectF(80, 290, 700, 100), Qt.AlignmentFlag.AlignVCenter, "Disc Shelf")
    p.setPen(QColor("#C3C8CD"))
    p.setFont(font(30))
    p.drawText(QRectF(80, 400, 700, 100), Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
               "Rip your discs, name the episodes, and put them straight into Plex.")
    rows = (("S02E01", "It Came from the Nightosphere", "In Plex", "#1F2A22", "#8FE0B0", "#2E3338"),
            ("S01E23", "Rainy Day Daydream", "In Plex", "#1F2A22", "#8FE0B0", "#2E3338"),
            ("S01E11", "Finn the Wizard", "Check", "#2B2416", "#F5C27A", "#8A6A33"),
            ("S02E07", "Power Animal", "Copying", "#15202B", "#A9D1F6", "#2E3338"),
            ("S01E05", "The Enchiridion!", "Ripping", "#15202B", "#A9D1F6", "#2E3338"))
    y = 150.0
    for code, name, state, tag_bg, tag_fg, border in rows:
        r = QRectF(800, y, 400, 60)
        p.setBrush(QColor("#1A1D20"))
        p.setPen(QColor(border))
        p.drawRoundedRect(r, 12, 12)
        p.setFont(font(17))
        p.setPen(QColor("#A2A9B0"))
        p.drawText(QRectF(818, y, 80, 60), Qt.AlignmentFlag.AlignVCenter, code)
        p.setPen(QColor("#E9EBED"))
        p.setFont(font(18))
        p.drawText(QRectF(898, y, 200, 60), Qt.AlignmentFlag.AlignVCenter,
                   p.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, 196))
        f = font(14, QFont.Weight.DemiBold)
        p.setFont(f)
        w = p.fontMetrics().horizontalAdvance(state) + 20
        tag = QRectF(1186 - w, y + 18, w, 24)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(tag_bg))
        p.drawRoundedRect(tag, 12, 12)
        p.setPen(QColor(tag_fg))
        p.drawText(tag, Qt.AlignmentFlag.AlignCenter, state)
        y += 72
    p.end()
    img.save(path)


def main() -> int:
    QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
    os.makedirs(OUT, exist_ok=True)
    banner(os.path.join(OUT, "banner.png"))
    social(os.path.join(OUT, "social-preview.png"))
    print(os.path.join(OUT, "banner.png"))
    print(os.path.join(OUT, "social-preview.png"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
