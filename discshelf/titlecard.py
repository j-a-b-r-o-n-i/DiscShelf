"""Read the episode's name off its title card and match it to an episode.

Many shows, cartoons especially, put the episode's name on screen near the start. Frames
from the opening minutes are read with OCR (ocr.py), and every line of text is scored
against the episode names with match.score_name. scan_title, which the pipeline uses,
skips the theme song by its sound (theme.py) and stops at the first frame that names an
episode; read_title reads a fixed stretch.

Text that shows on several titles of the same disc is the show's, not the episode's: a
theme song's captions ("JAKE THE DOG"), the show's logo, "presents". Some of it is word
for word a real episode's name, so pick_episodes looks at every title read so far and
discounts text that more than one title shows. A reading only counts as sure when the
name is read near enough in full and no other episode comes close; a missed reading is
better than a confident wrong one.
"""
from __future__ import annotations

import difflib
import glob
import os
import re
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .match import _fits, _mins, _non_episode, _words, score_name
from .models import Episode, Match, Title

# Frames are scaled to this width before OCR, at the display aspect ratio: the display
# width of a 16:9 DVD. Bigger frames read the cards no better.
FRAME_W = 854
# The first pass reads a frame a second. Wherever a frame shows big text (a line at least
# BIG_TEXT of the frame's height), a second pass reads DENSE_STEP_S apart for DENSE_S
# either side, as a card is on screen for only a second or two and OCR misreads some
# frames of it.
STEP_S = 1.0
BIG_TEXT = 0.08
DENSE_S = 1.0
DENSE_STEP_S = 0.25


class TitleCardError(RuntimeError):
    """ffmpeg could not read the title's frames."""


@dataclass
class CardLine:
    """One line of text OCR read on one frame."""
    text: str
    conf: float
    t: float                     # seconds into the title
    x: int = 0                   # box in the frame, pixels, frame scaled to FRAME_W wide
    y: int = 0
    w: int = 0
    h: int = 0


@dataclass
class TitleReading:
    path: str = ""
    start_s: float = 0.0
    end_s: float = 0.0
    frame_w: int = FRAME_W
    frame_h: int = 0
    frames: int = 0              # frames read with OCR
    lines: list[CardLine] = field(default_factory=list)
    seconds: float = 0.0         # wall time taken
    error: str = ""              # why nothing was read, in a sentence
    # Set by scan_title:
    theme_start_s: float | None = None   # where the theme song plays, when its sound was recognised
    theme_end_s: float | None = None
    card_t: float | None = None  # when the card that stopped the scan was on screen
    stopped_early: bool = False  # the scan stopped at a card before its limit
    fingerprint: list[int] = field(default_factory=list, repr=False)   # the opening's sound, theme.py


@dataclass
class CardMatch:
    title_index: int
    episode: Episode | None = None
    score: float = 0.0
    text: str = ""               # the card text the match rests on, as read
    t: float = 0.0               # when it was on screen, seconds into the title
    sure: bool = False           # read near enough in full, nothing else close
    others: list[tuple[Episode, float]] = field(default_factory=list)   # runners-up, best first
    note: str = ""               # why it isn't sure, in a sentence


def _extract(mkv_path: str, out_dir: str, start_s: float, end_s: float, step_s: float, tag: str,
             ffmpeg: str, run, keyframes: bool = True, grey: bool = False,
             width: int = FRAME_W) -> list[tuple[float, str]]:
    """Frames every step_s seconds between start_s and end_s, as (time, png path), scaled to
    width at the display aspect ratio.

    With keyframes, only keyframes are decoded (every half second or so on DVDs and
    Blu-rays), which keeps a long stretch of a 4K title cheap; a card is held on screen
    for a second or more, so one lands on a keyframe."""
    skip = ["-skip_frame", "nokey"] if keyframes else []
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-threads", "1", *skip,
           "-ss", f"{start_s:.3f}", "-i", mkv_path, "-t", f"{end_s - start_s:.3f}", "-map", "0:v:0",
           "-vf", f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{step_s - 0.05:.2f})',"
                  f"scale='trunc(ih*dar/2)*2':ih,setsar=1,scale={width}:-2" + (",hue=s=0" if grey else ""),
           "-fps_mode", "passthrough", "-frame_pts", "1", "-enc_time_base", "0.001",
           os.path.join(out_dir, f"{tag}%d.png")]
    proc = run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise TitleCardError(f"ffmpeg could not read frames: {(proc.stderr or '').strip()[-500:]}")
    out = []
    for p in glob.glob(os.path.join(out_dir, f"{tag}*.png")):
        m = re.fullmatch(re.escape(tag) + r"(\d+)\.png", os.path.basename(p))
        if m:
            out.append((round(start_s + int(m.group(1)) / 1000, 2), p))
    return sorted(out)


def _png_height(path: str) -> int:
    with open(path, "rb") as f:
        head = f.read(24)
    return int.from_bytes(head[20:24], "big") if head[:8] == b"\x89PNG\r\n\x1a\n" else 0


def _windows(times: list[float], start_s: float, end_s: float,
             half: float = DENSE_S) -> list[tuple[float, float]]:
    """half either side of each time, overlapping windows merged."""
    out: list[tuple[float, float]] = []
    for t in sorted(times):
        a, b = max(start_s, t - half), min(end_s, t + half)
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def read_title(mkv_path: str, start_s: float = 0.0, end_s: float = 90.0, *, ffmpeg: str = "ffmpeg",
               run=subprocess.run, read_images=None) -> TitleReading:
    """The text on screen in a title's opening, for pick_episodes.

    Two passes, each one batch of OCR: a grey frame a second from keyframes, then four
    colour frames a second around frames that show big text. The default 90 s covers a
    theme song and the card after it; pass a later end_s for a show with a long cold open.
    On the Adventure Time DVD that was 110 to 155 frames a title, 30 to 42 s of CPU. ffmpeg failing raises TitleCardError; OCR failing raises ocr.OcrError."""
    if read_images is None:
        from .ocr import read_images
    started = time.monotonic()
    r = TitleReading(path=mkv_path, start_s=start_s, end_s=end_s)
    with tempfile.TemporaryDirectory(prefix="discshelf-card-") as d:
        # The two passes read differently on purpose. OCR misses some lettering on a busy
        # coloured card that it reads in grey, and the other way round; on the Adventure
        # Time DVD grey then colour read 12 of 16 cards for sure, either one alone 10 or 11.
        frames = _extract(mkv_path, d, start_s, end_s, STEP_S, "a", ffmpeg, run, grey=True)
        if not frames:
            r.error = "ffmpeg found no video frames in the opening minutes."
            return r
        r.frame_h = _png_height(frames[0][1])
        found = read_images([p for _, p in frames])
        big = [t for (t, _), lines in zip(frames, found) if any(ln.h >= BIG_TEXT * r.frame_h for ln in lines)]
        dense = []
        for n, (a, b) in enumerate(_windows(big, start_s, end_s)):
            dense += [(t, p) for t, p in _extract(mkv_path, d, a, b, DENSE_STEP_S, f"b{n}_", ffmpeg, run, False)
                      if all(abs(t - t1) > DENSE_STEP_S / 2 for t1, _ in frames)]
        if dense:
            frames += dense
            found += read_images([p for _, p in dense])
    for (t, _), lines in zip(frames, found):
        r.lines += [CardLine(ln.text, round(float(ln.conf), 3), t, int(ln.x), int(ln.y), int(ln.w), int(ln.h))
                    for ln in lines]
    r.frames = len(frames)
    r.lines.sort(key=lambda ln: (ln.t, ln.y, ln.x))
    r.seconds = round(time.monotonic() - started, 1)
    return r


# scan_title reads a small grey frame every SCAN_STEP_S, SCAN_CHUNK frames to an OCR batch,
# and stops at the first frame that names an episode at least STOP_SCORE well, once frames
# read in colour around it agree (CONFIRM_BEFORE_S to CONFIRM_AFTER_S). Small grey frames
# read most cards as well as big ones and cost a third as much; where one shows big text
# that names nothing, BIG_REREAD_S after it is read again in full-size colour, as small
# grey frames lose some lettering.
SCAN_W = 640
SCAN_STEP_S = 2.0
SCAN_CHUNK = 8
STOP_SCORE = 0.8
CONFIRM_BEFORE_S = 0.5
CONFIRM_AFTER_S = 1.5
BIG_REREAD_S = 1.0


class _Scan:
    """One title's frames read so far, and the reading of them."""

    def __init__(self, r: TitleReading, episodes: list[Episode], out_dir: str, ffmpeg: str, run, read_images):
        self.r, self.dir, self.ffmpeg, self.run, self.read = r, out_dir, ffmpeg, run, read_images
        self.score = _Scorer(episodes) if episodes else None
        self.n = 0
        self.by_t: dict[float, list[CardLine]] = {}
        self.colour: list[float] = []     # times read in full-size colour
        self.big: list[float] = []        # frames with big text, when there are no names to look for

    def _grab(self, a: float, b: float, step: float, small: bool) -> list[tuple[float, list[CardLine]]]:
        """Frames between a and b read with OCR, as (time, lines), lines scaled to FRAME_W."""
        self.n += 1
        if small:
            frames = _extract(self.r.path, self.dir, a, b, step, f"s{self.n}_", self.ffmpeg, self.run,
                              grey=True, width=SCAN_W)
        else:
            frames = [(t, p) for t, p in _extract(self.r.path, self.dir, a, b, step, f"c{self.n}_", self.ffmpeg,
                                                  self.run, keyframes=False)
                      if all(abs(t - t1) > DENSE_STEP_S / 2 for t1 in self.colour)]
            self.colour += [t for t, _ in frames]
        if not frames:
            return []
        k = FRAME_W / SCAN_W if small else 1.0
        if not self.r.frame_h:
            self.r.frame_h = round(_png_height(frames[0][1]) * k)
        out = []
        for (t, p), lines in zip(frames, self.read([p for _, p in frames])):
            # A colour frame can land on a grey one's time; kept apart, they aren't read as one frame.
            while t in self.by_t:
                t = round(t + 0.01, 2)
            got = [CardLine(ln.text, round(float(ln.conf), 3), t, round(ln.x * k), round(ln.y * k),
                            round(ln.w * k), round(ln.h * k)) for ln in lines]
            self.by_t[t] = got
            self.r.lines += got
            out.append((t, got))
            os.remove(p)
        self.r.frames += len(frames)
        self.r.end_s = max(self.r.end_s, b)
        return out

    def _named(self, lines: list[CardLine]) -> dict[tuple[int, int], tuple[Episode, float]]:
        """The episodes this frame's text names at least STOP_SCORE well, with the best score."""
        out: dict[tuple[int, int], tuple[Episode, float]] = {}
        for run in _runs([ln for ln in lines if _key(ln.text)]):
            for ep, sc in self.score(_text(run)):
                k = (ep.season, ep.number)
                if sc >= STOP_SCORE and (k not in out or sc > out[k][1]):
                    out[k] = (ep, sc)
        return out

    def _big(self, lines: list[CardLine]) -> bool:
        return any(ln.h >= BIG_TEXT * self.r.frame_h for ln in lines)

    def _confirm(self, t: float) -> bool:
        """Whether the episode a frame at t names is the card: frames read densely in colour
        around it, and any other frames there, name one episode on at least two of them.
        A card being drawn on can read as another name for a frame ("MumanDaD" for "Mums and
        Dads" on the way to "Mount Mumandad"), so the best-read name of those wins."""
        a, b = max(0.0, t - CONFIRM_BEFORE_S), t + CONFIRM_AFTER_S
        self._grab(a, b, DENSE_STEP_S, small=False)
        moments: dict[tuple[int, int], set[int]] = defaultdict(set)
        best: dict[tuple[int, int], float] = {}
        first: dict[tuple[int, int], float] = {}
        for t1 in sorted(t1 for t1 in self.by_t if a <= t1 <= b):
            for k, (_ep, sc) in self._named(self.by_t[t1]).items():
                # A grey and a colour frame of the same moment are one reading, not two.
                moments[k].add(round(t1 / DENSE_STEP_S))
                best[k] = max(best.get(k, 0.0), sc)
                first.setdefault(k, t1)
        count = {k: len(m) for k, m in moments.items()}
        agreed = [k for k in count if count[k] >= 2]
        if not agreed:
            return False
        k = max(agreed, key=lambda k: (best[k], count[k]))
        self.r.card_t, self.r.stopped_early = first[k], True
        return True

    def scan(self, a: float, b: float) -> bool:
        """Read from a to b until a card is confirmed. True when one was."""
        pos = a
        while pos < b - 0.01:
            end = min(b, pos + SCAN_CHUNK * SCAN_STEP_S)
            frames = self._grab(pos, end, SCAN_STEP_S, small=True)
            if not frames:
                break                     # past the end of the title
            big = []
            for t, lines in frames:
                if self.score and self._named(lines):
                    if self._confirm(t):
                        return True
                elif self._big(lines):
                    big.append(t)
            if self.score:
                for wa, wb in _windows([t + BIG_REREAD_S / 2 for t in big], 0.0, end + BIG_REREAD_S,
                                       BIG_REREAD_S / 2):
                    for t, lines in self._grab(wa, wb, DENSE_STEP_S * 2, small=False):
                        if self._named(lines) and self._confirm(t):
                            return True
            else:
                self.big += big
            pos = end
        return False


def scan_title(mkv_path: str, episodes: list[Episode], *, limit_s: float = 300.0, themes=None,
               ffmpeg: str = "ffmpeg", fpcalc: str = "fpcalc", run=subprocess.run,
               read_images=None) -> TitleReading:
    """The text on screen from a title's start until its card, for pick_episodes.

    With themes (a theme.ThemeBook for the show), the title's opening is fingerprinted and
    reading starts where the theme song ends; a title whose theme isn't known yet is read
    from the start. Reading stops at the first frame that names one of the episodes, once
    frames around it agree, or at limit_s. When nothing is found after the theme, the part
    skipped is read too, so a wrong theme costs time rather than the card. With no episodes
    to look for, the whole stretch is read, densely around big text as read_title does.

    ffmpeg failing raises TitleCardError; OCR failing raises ocr.OcrError. fpcalc missing or
    failing only means reading from the start."""
    from . import theme
    started = time.monotonic()
    r = TitleReading(path=mkv_path, start_s=0.0, end_s=0.0)
    if themes is not None:
        listen = min(limit_s, theme.LISTEN_S)
        fp = theme.fingerprint(mkv_path, min(listen, theme.FIRST_LISTEN_S), fpcalc, run)
        p = themes.place_early(fp) if fp else None
        if fp and p is None:
            if listen > theme.FIRST_LISTEN_S:
                fp = theme.fingerprint(mkv_path, listen, fpcalc, run) or fp
            p = themes.place_or_learn(fp)
        if fp:
            r.fingerprint = fp
        if p:
            r.theme_start_s, r.theme_end_s = p.start_s, p.end_s
    session = None
    if read_images is None:
        from .ocr import Session
        session = Session()
        read_images = session.read
    try:
        with tempfile.TemporaryDirectory(prefix="discshelf-card-") as d:
            sc = _Scan(r, episodes, d, ffmpeg, run, read_images)
            start = min(r.theme_end_s or 0.0, limit_s)
            r.start_s = start
            if not sc.scan(start, limit_s) and start > 0:
                # Nothing after the theme: read the opening before it, then the theme itself,
                # whose captions can be an episode's name ("Jake the Dog").
                r.start_s = 0.0
                ts = min(r.theme_start_s or 0.0, start)
                if not (ts > 0 and sc.scan(0.0, ts)):
                    sc.scan(ts, start)
            if not sc.score and sc.big:
                for a, b in _windows(sc.big, 0.0, r.end_s):
                    sc._grab(a, b, DENSE_STEP_S, small=False)
    finally:
        if session is not None:
            session.close()
    if not r.frames:
        r.error = "ffmpeg found no video frames in the opening minutes."
    r.lines.sort(key=lambda ln: (ln.t, ln.y, ln.x))
    r.seconds = round(time.monotonic() - started, 1)
    return r


def place_theme(r: TitleReading, themes) -> bool:
    """Look again for the theme in a title read before its theme was learned (the first title
    of a show, read from the start). True when the card the scan stopped at plays during
    the theme, so it is likely the show's own caption and the title is worth reading again."""
    if r.theme_end_s is not None or not r.fingerprint:
        return False
    p = themes.place(r.fingerprint)
    if p is None:
        return False
    r.theme_start_s, r.theme_end_s = p.start_s, p.end_s
    return r.card_t is not None and p.start_s <= r.card_t < p.end_s


def _key(text: str) -> str:
    return "".join(_words(text))


def _same_text(a: str, b: str) -> bool:
    """Two OCR readings of the same words, allowing a misread letter or two."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4 or abs(len(a) - len(b)) > max(len(a), len(b)) * 0.3:
        return False
    sm = difflib.SequenceMatcher(None, a, b)
    return sm.quick_ratio() >= 0.8 and sm.ratio() >= 0.8


# Shorter text ("THE", "OF A") is part of many episode names, so it is never taken for
# the show's own text.
SHARED_MIN = 5
# Text shorter than this only counts when it is an episode's whole name.
SHORT = 5
# Once two or more titles are sure, their cards show when this disc puts the card on
# screen, counted from the end of the theme song when its sound was recognised. A reading
# further than this from it is more likely a sign or a caption in the episode, and is
# marked down by OFF_CARD. Some shows put the card anywhere from 5 to 40 s after the theme
# (Bluey); when fewer than CARD_AGREE of the sure titles are within the window, the titles
# don't agree on a time and none is marked down.
CARD_WINDOW_S = 10.0
OFF_CARD = 0.1
CARD_AGREE = 0.75
# Readings this far from the card scan_title stopped at are left out, unless it stopped
# during the theme song.
CARD_NEAR_S = CONFIRM_BEFORE_S + CONFIRM_AFTER_S
# A reading during the theme song is likely its caption ("FINN THE HUMAN"), so any other
# reading comes first; it still counts when there is nothing else.
IN_THEME = 0.5
# Two titles reading one episode's name this close in time, both at least this well, are
# reading the show's own text.
ALIGN_S = 3.0
ALIGNED_SCORE = 0.8


class _Shared:
    """Whether a line of one title's text shows on another title too: the show's own text,
    such as a theme song's captions, a logo or "presents".

    The show's own text names the same episode on every title, if any. Two cards of
    episodes with alike names ("Mums and Dads", "Mount Mumandad") read alike too, but each
    names its own episode, so they aren't taken for the show's text. A line names what
    the best-read run it is part of names: "MUmanDaD" alone is closest to "Mums and
    Dads", but under "MOUnT" it is "Mount Mumandad"."""

    def __init__(self, readings: dict[int, TitleReading], score: "_Scorer"):
        # Runs as well as lines: a caption set as "JAKE" over "THE DOG" is shared as a whole.
        self.keys: dict[int, set[str]] = {}
        self.names: dict[int, dict[str, tuple[float, tuple[int, int]]]] = {}
        for i, r in readings.items():
            keys, names = set(), {}
            for _, run in _all_runs(r):
                k = _key(_text(run))
                if len(k) >= SHARED_MIN:
                    keys.add(k)
                found = score(_text(run))
                if found:
                    ep, sc = found[0]
                    for sub in {k} | {_key(ln.text) for ln in run}:
                        if sub not in names or sc > names[sub][0]:
                            names[sub] = (sc, (ep.season, ep.number))
            self.keys[i], self.names[i] = keys, names
        self.cache: dict[tuple[int, str], bool] = {}

    def _alike(self, i: int, key: str, j: int, k: str) -> bool:
        # Part of the other title's text counts: OCR catches a caption half drawn
        # ("JAKE THE" for "JAKE THE DOG") on one title and whole on another.
        if not (key in k or _same_text(key, k)):
            return False
        a, b = self.names[i].get(key), self.names[j].get(k)
        return a is None or b is None or a[1] == b[1]

    def __call__(self, i: int, key: str) -> bool:
        if len(key) < SHARED_MIN:
            return False
        if (i, key) not in self.cache:
            self.cache[(i, key)] = any(any(self._alike(i, key, j, k) for k in ks)
                                       for j, ks in self.keys.items() if j != i)
        return self.cache[(i, key)]


def _reading_order(lines: list[CardLine]) -> list[list[CardLine]]:
    """Lines in rows, top to bottom, each row left to right. Lines share a row when they
    overlap by half the height of the smaller."""
    rows: list[list[CardLine]] = []
    for ln in sorted(lines, key=lambda ln: ln.y + ln.h / 2):
        for row in rows:
            r = row[-1]
            if min(r.y + r.h, ln.y + ln.h) - max(r.y, ln.y) >= 0.5 * min(r.h, ln.h):
                row.append(ln)
                break
        else:
            rows.append([ln])
    return [sorted(row, key=lambda ln: ln.x) for row in sorted(rows, key=lambda row: min(ln.y for ln in row))]


def _next_to(a: CardLine, b: CardLine, same_row: bool) -> bool:
    size = max(a.h, b.h)
    if same_row:
        return b.x - (a.x + a.w) <= size
    overlap = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
    return b.y - (a.y + a.h) <= size and overlap > 0


def _runs(lines: list[CardLine]) -> list[list[CardLine]]:
    """Each line on one frame, and each run of up to 3 lines that read on from one another:
    a name is often split across lines ("Rainy Day" over "Daydream") or, when OCR breaks
    it, across one row ("emoRy" and "ofa MemoRy")."""
    order: list[tuple[CardLine, int]] = []
    for r, row in enumerate(_reading_order(lines)):
        order += [(ln, r) for ln in row]
    out = []
    for i in range(len(order)):
        run = [order[i][0]]
        out.append(run)
        for k in range(i + 1, min(i + 3, len(order))):
            (prev, pr), (nxt, nr) = order[k - 1], order[k]
            if not _next_to(prev, nxt, pr == nr):
                break
            run = run + [nxt]
            out.append(run)
    return out


def _text(run: list[CardLine]) -> str:
    return " ".join(ln.text for ln in run)


def _all_runs(r: TitleReading):
    """(frame time, run) for every run on every frame of a title."""
    by_t: dict[float, list[CardLine]] = defaultdict(list)
    for ln in r.lines:
        if _key(ln.text):
            by_t[ln.t].append(ln)
    for t in sorted(by_t):
        for run in _runs(by_t[t]):
            yield t, run


@dataclass
class _Hit:
    episode: Episode
    score: float                 # as score_name gave it
    rank: float                  # score, less OFF_CARD when read away from the disc's card time
    text: str
    t: float
    show: bool = False           # the show's own text: another title shows it too


class _Scorer:
    """score_name, remembered per text, and skipping episodes whose names can't reach 0.6.

    A title's opening has hundreds of different lines and runs, and a show can have
    hundreds of episodes. Two strings can be no more similar than the letters they have in
    common allow, which is quick to count, so most names are ruled out before difflib."""

    def __init__(self, episodes: list[Episode]):
        self.names = [(e, _key(e.title), Counter(_key(e.title))) for e in episodes]
        self.done: dict[str, list[tuple[Episode, float]]] = {}

    def __call__(self, text: str) -> list[tuple[Episode, float]]:
        k = _key(text)
        if k not in self.done:
            c = Counter(k)
            maybe = [e for e, n, cn in self.names
                     if n and (k in n or n in k or 2 * sum((c & cn).values()) >= 0.6 * (len(k) + len(n)))]
            # A few letters are close to too many names; only a whole short name counts.
            self.done[k] = [(e, sc) for e, sc in score_name(text, maybe) if len(k) >= SHORT or sc == 1.0]
        return self.done[k]


def _title_hits(i: int, r: TitleReading, shared: _Shared, score: _Scorer) -> list[_Hit]:
    """Every reading of an episode name in this title's text, with the show's own text marked."""
    out = []
    for t, run in _all_runs(r):
        text = _text(run)
        found = score(text)
        if found:
            show = shared(i, _key(text)) or any(shared(i, _key(ln.text)) for ln in run)
            out += [_Hit(ep, sc, sc, text, t, show) for ep, sc in found]
    return out


def _drop_shown(hits: dict[int, list[_Hit]]) -> dict[int, list[_Hit]]:
    """Leave out the show's own text, and readings of an episode that another title reads
    well at about the same time.

    OCR reads the show's own text a little differently on each title ("JAKE THE DOG",
    "JAKE THE D0G"), so comparing text misses some of it, but it names the same episode
    at the same point in every title. Only a good reading on the other title counts: a
    card at the same point that merely resembles the name ("Crystals Have Power" for
    "Jelly Beans Have Power") must not hide the title that really is that episode."""
    strong: dict[tuple[int, int], list[tuple[int, float]]] = defaultdict(list)
    for i, hs in hits.items():
        for h in hs:
            if h.score >= ALIGNED_SCORE:
                strong[(h.episode.season, h.episode.number)].append((i, h.t))
    return {i: [h for h in hs if not h.show and not any(j != i and abs(t - h.t) <= ALIGN_S
                                                        for j, t in strong[(h.episode.season, h.episode.number)])]
            for i, hs in hits.items()}


def _after_theme(r: TitleReading, t: float) -> float:
    """t counted from the end of the title's theme song, or from its start when that isn't known."""
    return t - (r.theme_end_s or 0.0)


def _in_theme(r: TitleReading, t: float) -> bool:
    return r.theme_end_s is not None and (r.theme_start_s or 0.0) <= t < r.theme_end_s


def _best(hits: list[_Hit], card_t: float | None, r: TitleReading) -> list[_Hit]:
    """The best reading of each episode, best first, marking down readings away from the card time."""
    best: dict[tuple[int, int], _Hit] = {}
    for h in hits:
        off = card_t is not None and abs(_after_theme(r, h.t) - card_t) > CARD_WINDOW_S
        rank = h.score - (IN_THEME if _in_theme(r, h.t) else OFF_CARD if off else 0.0)
        k = (h.episode.season, h.episode.number)
        if k not in best or rank > best[k].rank:
            best[k] = _Hit(h.episode, h.score, rank, h.text, h.t)
    return sorted(best.values(), key=lambda h: (-h.rank, h.episode.season, h.episode.number))


def _quote_eps(eps: list[Episode]) -> str:
    names = [f"“{e.title}”" for e in eps]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _judge(i: int, hs: list[_Hit], n_read: int, sure_score: float, margin: float,
           card_t: float | None, r: TitleReading) -> CardMatch:
    if not hs:
        return CardMatch(title_index=i, note="No title card was read: no episode's name is on screen in the "
                                             "title's opening minutes.")
    best, rest = hs[0], hs[1:]
    cm = CardMatch(title_index=i, episode=best.episode, score=best.score, text=best.text, t=best.t,
                   others=[(h.episode, h.score) for h in rest[:3]])
    close = [h.episode for h in rest if h.rank > best.rank - margin]
    if _in_theme(r, best.t):
        cm.note = (f"“{best.text}” is on screen during the theme song, so it may be the show's own text "
                   "rather than this title's card.")
    elif close:
        cm.note = f"The title card reads “{best.text}”, which could be {_quote_eps([best.episode] + close[:2])}."
    elif best.rank < sure_score:
        cm.note = f"The title card reads “{best.text}”, which is closest to “{best.episode.title}”."
    elif n_read < 2 and r.theme_end_s is None:
        # With the theme recognised by its sound and skipped, its captions aren't in the reading.
        cm.note = (f"The title card reads “{best.text}”. Only one title has been read, so the show's own "
                   "on-screen text can't be told apart from episode names yet.")
    elif card_t is not None and abs(_after_theme(r, best.t) - card_t) > CARD_WINDOW_S:
        if r.theme_end_s is not None:
            cm.note = (f"“{best.text}” is on screen {_after_theme(r, best.t):.0f} s after the theme song, but "
                       f"other titles show their card about {card_t:.0f} s after it, so it may be text in the "
                       "episode.")
        else:
            cm.note = (f"“{best.text}” is on screen at {best.t:.0f} s, but other titles show their card at "
                       f"about {card_t:.0f} s, so it may be text in the episode.")
    else:
        cm.sure = True
    return cm


def pick_episodes(readings: dict[int, TitleReading], episodes: list[Episode],
                  sure_score: float = 0.85, margin: float = 0.15) -> dict[int, CardMatch]:
    """The episode each title's card names, for every title read so far (title index to
    CardMatch). Run it again as more titles are read: text that two titles both show is
    the show's own, and that only shows once a second title is in.

    A match is sure when the name is read with a score of at least sure_score, no other
    episode is within margin of it, at least two titles have been read (or the theme song
    was recognised and skipped), it isn't on screen during the theme song, it was on screen
    when the disc's other cards were (when they agree on a time), and no other title reads
    the same name. Otherwise
    the best guess is kept with a note saying why it's unsure; a title whose text names
    no episode gets episode None."""
    score = _Scorer(episodes)
    shared = _Shared(readings, score)
    hits = _drop_shown({i: _title_hits(i, r, shared, score) for i, r in readings.items()})

    def judge_all(card_t):
        out = {}
        for i in sorted(readings):
            if readings[i].error and not readings[i].lines:
                out[i] = CardMatch(title_index=i, note=readings[i].error)
            else:
                r = readings[i]
                # scan_title stopped at a card it confirmed; a name read once elsewhere wasn't.
                near = r.card_t is not None and not _in_theme(r, r.card_t)
                hs = [h for h in hits[i] if not near or abs(h.t - r.card_t) <= CARD_NEAR_S]
                out[i] = _judge(i, _best(hs, card_t, r), len(readings), sure_score, margin, card_t, r)
        return out

    out = judge_all(None)
    times = sorted(_after_theme(readings[i], cm.t) for i, cm in out.items() if cm.sure)
    if len(times) >= 2:
        mid = times[len(times) // 2]
        if sum(abs(t - mid) <= CARD_WINDOW_S for t in times) >= CARD_AGREE * len(times):
            out = judge_all(mid)

    _flag_repeats(out)
    return out


def _flag_repeats(picks: dict[int, CardMatch]) -> None:
    """Two titles naming one episode means at least one of them read something else, so
    neither is sure. (A title and its exact duplicate show the same text, which is taken
    for the show's own, so they end up with no reading rather than here.)"""
    by_ep: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, cm in picks.items():
        if cm.episode is not None:
            by_ep[(cm.episode.season, cm.episode.number)].append(i)
    for idx in by_ep.values():
        for i in idx if len(idx) > 1 else []:
            cm = picks[i]
            others = [str(j) for j in idx if j != i]
            said = (f"Title {others[0]} also reads" if len(others) == 1 else
                    f"Titles {', '.join(others[:-1])} and {others[-1]} also read")
            cm.sure = False
            cm.note = (cm.note + " " if cm.note else "") + f"{said} as “{cm.episode.title}”."


def card_matches(picks: dict[int, CardMatch], titles: list[Title], episodes: list[Episode],
                 tolerance: float = 0.12) -> list[Match]:
    """One Match per title, in title-index order, as match.match_titles gives them.

    A sure card reading whose episode's running time fits is "close"; any other reading is
    "check" with the reason. An episode title with no reading is skipped with a "check"
    note, and other titles go the way match_titles sends them (extras or skipped)."""
    out = []
    for t in sorted(titles, key=lambda t: t.index):
        if not (t.selected and t.kind == "episode"):
            out.append(_non_episode(t))
            continue
        cm = picks.get(t.index)
        if cm is None:
            out.append(Match(title_index=t.index, target="skip", confidence="check",
                             note="This title's card hasn't been read yet."))
            continue
        if cm.episode is None:
            out.append(Match(title_index=t.index, target="skip", confidence="check",
                             note=cm.note or "No title card was read, so this title is not matched."))
            continue
        m = Match(title_index=t.index, target="episode", episode=cm.episode,
                  confidence="close" if cm.sure else "check", note=cm.note)
        rt = cm.episode.runtime_min
        if rt and not _fits(_mins(t), rt, tolerance):
            m.confidence = "check"
            m.note = (m.note + " " if m.note else "") + \
                f"Title is {_mins(t)} min but the episode is listed as {rt} min."
        out.append(m)
    return out
