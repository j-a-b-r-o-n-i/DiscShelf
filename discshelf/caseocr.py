"""Read the episode list off a photo of the back of a DVD or Blu-ray case.

The result is a list of names in disc order for the Match page's "Enter episode list" box,
which match.match_list then pairs with the disc's titles. Nothing here decides an episode
on its own: a name that could be several episodes is given as printed, so match_list flags it.

What a case looks like to OCR: the list is often in two or more columns, a long name wraps
onto a second line, printed numbers are misread ("1" as "I", "12" as "2"), RapidOCR drops the
spaces, and the photo also catches the logo, an ISBN, a barcode and the legal text. So lines
are read column by column, the leading number is taken off, a wrapped name is joined back,
and only lines that look like an episode on TVmaze are kept. The printed numbers decide the
order only when they are distinct and run without gaps.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field

from . import ocr
from .match import score_name
from .models import Episode
from .ocr import OcrLine

# A line with a printed number in front is very likely a list entry, so it needs less
# similarity to count than a stray line (a logo reading "ADVENTURE" scores 0.71).
NUMBERED_MIN = 0.7
UNNUMBERED_MIN = 0.8
# The best episode has to beat the next one by this much to be used by its TVmaze name.
# "Wizard" scores 0.775 for "Wizard Battle" and 0.762 for "Finn the Wizard": not clear.
CLEAR_MARGIN = 0.05

# A leading list number as OCR sees it: digits, or I l | O [ ] standing in for them, then a
# separator. "Episode 3:" and "Ep. 3 -" are taken off the same way.
_NUM = re.compile(r"^\s*(?:(?:episode|ep\.?)\s*)?([0-9IilLO|\[\]!]{1,3})\s*([.):\-]|\s)\s*", re.I)
_NUM_TIGHT = re.compile(r"^\s*([0-9IilLO|\[\]]{0,2}[0-9][0-9IilLO|\[\]]{0,2})(?=[A-Za-z])")
# Running times and other trailing notes: "(22 min)", "22 mins", "- 11:00".
_TRAIL = re.compile(r"\s*[-–(]*\s*(?:\d{1,3}\s*min(?:ute)?s?\.?|\d{1,2}:\d{2}(?::\d{2})?)\s*\)?\s*$", re.I)
_DIGIT = str.maketrans({"I": "1", "i": "1", "l": "1", "L": "1", "|": "1", "[": "1", "]": "1", "!": "1",
                        "O": "0", "o": "0"})


@dataclass
class CaseEntry:
    name: str                    # what goes in the episode list box
    text: str                    # the OCR text it came from, number taken off, wrapped lines joined
    number: int | None           # the printed number as read, None when there was none
    score: float                 # score_name's score for the best episode
    episode: Episode | None      # the TVmaze episode when the best one is clear, else None
    candidates: list[Episode] = field(default_factory=list)   # the closest episodes, best first


@dataclass
class SkippedLine:
    text: str
    reason: str                  # one sentence for the user


@dataclass
class CaseList:
    names: list[str] = field(default_factory=list)            # in disc order, one per line in the box
    entries: list[CaseEntry] = field(default_factory=list)    # same order as names
    skipped: list[SkippedLine] = field(default_factory=list)
    ordered_by_number: bool = False   # True when the printed numbers set the order
    engine: str = ""                  # "rapidocr" or "tesseract"; "" for lines passed in directly
    no_text: bool = False             # OCR read nothing at all from the photo

    def summary(self) -> str:
        """One sentence for the Match page."""
        if not self.names:
            if self.no_text:
                return "No text could be read from the photo. Try a sharper, straighter photo of the list."
            return "No episode names were found in the photo."
        s = f"Read {len(self.names)} episode name{'s' if len(self.names) != 1 else ''} from the photo"
        unsure = sum(1 for e in self.entries if e.episode is None)
        if unsure:
            s += f"; {unsure} could be more than one episode and {'is' if unsure == 1 else 'are'} left as printed"
        return s + "."


def split_number(text: str) -> tuple[int | None, str]:
    """The printed list number and the rest of the line. ("I.ITCAMEFROM" gives 1 and "ITCAMEFROM".)"""
    text = _TRAIL.sub("", text).strip()
    m = _NUM.match(text)
    if not m:
        # RapidOCR drops the separator too: "2RAINYDAYDAYDREAM", "I4THEMONSTER". Needs a real digit.
        m = _NUM_TIGHT.match(text)
    if not m:
        return None, text
    token = m.group(1)
    rest = text[m.end():].strip()
    if not rest:
        return None, text
    digits = token.translate(_DIGIT)
    return (int(digits) if digits.isdigit() else None), rest


# A list number in the middle of a line: "…PANICI5.HITMAN" is two items OCR ran together.
_MID_NUM = re.compile(r"(?<=[A-Za-z!?'’)])(?=[0-9IilLO|]{1,2}[.)])")


def _split_merged(lines: list[OcrLine], episodes: list[Episode]) -> list[OcrLine]:
    """Split lines where OCR ran two columns together. A line is split at a list number in its
    middle only when both halves match an episode better than the whole line does; with the
    number's first character as ambiguous as "PANICI5." ("PANIC" + "I5." or "PANICI" + "5."),
    the split whose halves score highest wins. The right half is placed at the column edge
    nearest its share of the line, so it sorts into its own column."""
    out = []
    for l in lines:
        best, best_score = None, _top(_best(split_number(l.text)[1], episodes))
        for m in _MID_NUM.finditer(l.text):
            left, right = l.text[:m.start()], l.text[m.start():]
            sl, sr = _top(_best(split_number(left)[1], episodes)), _top(_best(split_number(right)[1], episodes))
            if min(sl, sr) >= NUMBERED_MIN and sl + sr > 2 * best_score:
                best, best_score = m.start(), (sl + sr) / 2
        if best is None:
            out.append(l)
            continue
        lw = int(l.w * best / max(len(l.text), 1))
        # Letters aren't all the same width, so snap the estimate to the nearest left edge of
        # another numbered line to the right of this one: that's the column it came from.
        # (Only numbered lines: an ISBN or barcode further right is not a column of the list.)
        guess = l.x + lw
        edges = [o.x for o in lines if o.x > l.x + lw // 2 and split_number(o.text)[0] is not None]
        rx = min(edges, key=lambda x: abs(x - guess)) if edges else guess
        out.append(OcrLine(l.text[:best], l.conf, l.x, l.y, lw, l.h))
        out.append(OcrLine(l.text[best:], l.conf, rx, l.y, max(1, l.x + l.w - rx), l.h))
    return out


def _columns(lines: list[OcrLine]) -> list[list[OcrLine]]:
    """Lines grouped into columns by their left edge, columns left to right, lines top to bottom."""
    if not lines:
        return []
    heights = sorted(l.h for l in lines)
    gap = 3 * heights[len(heights) // 2]
    cols: list[list[OcrLine]] = []
    for l in sorted(lines, key=lambda l: l.x):
        if cols and l.x - sum(c.x for c in cols[-1]) / len(cols[-1]) <= gap:
            cols[-1].append(l)
        else:
            cols.append([l])
    return [sorted(c, key=lambda l: l.y) for c in cols]


def _best(text: str, episodes: list[Episode]) -> list[tuple[Episode, float]]:
    return score_name(text, episodes, min_score=0.5)


def _top(scored: list[tuple[Episode, float]]) -> float:
    return scored[0][1] if scored else 0.0


def _as_printed(text: str, ep: Episode | None) -> str:
    """The OCR text as a name: spelled and spaced like the episode's name when it is part of
    it ("WIZARD" in "Wizard Battle" gives "Wizard"), else in title case."""
    key = re.sub(r"[^a-z0-9]", "", text.lower())
    if ep is not None and key:
        pos = [i for i, ch in enumerate(ep.title) if ch.isalnum()]
        flat = "".join(ep.title[i].lower() for i in pos)
        at = flat.find(key)
        if at >= 0:
            return ep.title[pos[at]:pos[at + len(key) - 1] + 1]
    words = re.sub(r"\s+", " ", text).strip().split(" ")
    return " ".join(w[:1].upper() + w[1:].lower() for w in words)


def episode_list_from_lines(lines: list[OcrLine], episodes: list[Episode]) -> CaseList:
    """The episode list in OCR lines from one photo. See the module docstring for the steps."""
    out = CaseList()
    found: list[tuple[CaseEntry, bool]] = []      # entry, had a printed number
    for col in _columns(_split_merged(lines, episodes)):
        parsed = [split_number(l.text) for l in col]
        i = 0
        while i < len(col):
            number, text = parsed[i]
            raw = col[i].text.strip()
            scored = _best(text, episodes)
            # A name that wraps: the next line in the column, close below and unnumbered,
            # belongs to this one if the two together match better than either alone.
            if i + 1 < len(col) and parsed[i + 1][0] is None and col[i + 1].y - col[i].y < 2 * max(col[i].h, 1):
                nxt = parsed[i + 1][1]
                joined = _best(text + " " + nxt, episodes)
                if _top(joined) > max(_top(scored), _top(_best(nxt, episodes))):
                    text, raw, scored = text + " " + nxt, raw + " " + col[i + 1].text.strip(), joined
                    i += 1
            i += 1
            # A line that is itself a name ("I Am a Sword") must not lose its first word.
            if number is not None:
                whole = _best(raw, episodes)
                if _top(whole) > _top(scored):
                    number, text, scored = None, raw, whole
            need = NUMBERED_MIN if number is not None else UNNUMBERED_MIN
            if _top(scored) < need:
                why = ("It has a list number but no episode on TVmaze is close to it." if number is not None
                       else "It does not look like an episode name.")
                out.skipped.append(SkippedLine(raw, why))
                continue
            best, score = scored[0]
            clear = len(scored) == 1 or score - scored[1][1] >= CLEAR_MARGIN
            entry = CaseEntry(name=best.title if clear else _as_printed(text, best), text=text, number=number,
                              score=score, episode=best if clear else None,
                              candidates=[e for e, s in scored[:3] if s >= need])
            found.append((entry, number is not None))

    # The same episode twice is the list plus a "featuring" banner; the numbered one is the list.
    chosen: dict[object, int] = {}
    for i, (entry, numbered) in enumerate(found):
        key = (entry.episode.season, entry.episode.number) if entry.episode else entry.name.lower()
        if key not in chosen or (numbered and not found[chosen[key]][1]):
            chosen[key] = i
    keep = set(chosen.values())
    for i, (entry, _) in enumerate(found):
        if i not in keep:
            out.skipped.append(SkippedLine(entry.text, f"“{entry.name}” is already in the list."))
    kept = [found[i][0] for i in sorted(keep)]

    nums = [e.number for e in kept]
    if (len(kept) > 1 and None not in nums and len(set(nums)) == len(nums)
            and max(nums) - min(nums) + 1 == len(nums) and nums != sorted(nums)):
        kept.sort(key=lambda e: e.number)
        out.ordered_by_number = True
    out.entries = kept
    out.names = [e.name for e in kept]
    return out


# Formats OCR can't open (OpenCV and Tesseract read neither), as phones save them.
CONVERT = (".heic", ".heif", ".avif")


def readable_copy(path: str, tmpdir: str, run=subprocess.run) -> str:
    """path itself, or a JPEG copy in tmpdir of a photo OCR can't open (an iPhone's HEIC).
    Tries libheif's heif-dec, then ImageMagick, then ffmpeg. Raises ocr.OcrError when none works."""
    if not path.lower().endswith(CONVERT):
        return path
    out = os.path.join(tmpdir, os.path.splitext(os.path.basename(path))[0] + ".jpg")
    # ffmpeg last: it's inside the Flatpak, where neither of the others is.
    for cmd in (["heif-dec", path, out], ["magick", path, out],
                ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", path, "-frames:v", "1", "-q:v", "2", out]):
        try:
            done = run(cmd, capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode == 0 and os.path.isfile(out):
            return out
    raise ocr.OcrError("Couldn't open the photo. Install libheif, ImageMagick or ffmpeg to read HEIC photos, "
                       "or save it as a JPEG.")


def episode_list_from_photo(path: str, episodes: list[Episode], run=subprocess.run) -> CaseList:
    """OCR one photo of a case and read its episode list. Raises ocr.OcrError when no OCR
    engine is installed, the photo can't be opened, or OCR fails."""
    with tempfile.TemporaryDirectory(prefix="disc-shelf-photo-") as tmp:
        lines = ocr.read_images([readable_copy(path, tmp, run)], run=run)
    out = episode_list_from_lines(lines[0] if lines else [], episodes)
    out.engine = ocr.engine()
    out.no_text = not (lines and lines[0])
    return out
