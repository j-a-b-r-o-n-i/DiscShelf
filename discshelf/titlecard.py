"""Read the episode's name off its title card and match it to an episode.

Many shows, cartoons especially, put the episode's name on screen near the start. Frames
from the opening minutes are read with OCR (ocr.py), and every line of text is scored
against the episode names with match.score_name.

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
             ffmpeg: str, run, keyframes: bool = True, grey: bool = False) -> list[tuple[float, str]]:
    """Frames every step_s seconds between start_s and end_s, as (time, png path), scaled to
    FRAME_W at the display aspect ratio.

    With keyframes, only keyframes are decoded (every half second or so on DVDs and
    Blu-rays), which keeps a long stretch of a 4K title cheap; a card is held on screen
    for a second or more, so one lands on a keyframe."""
    skip = ["-skip_frame", "nokey"] if keyframes else []
    cmd = [ffmpeg, "-v", "error", "-nostdin", "-threads", "1", *skip,
           "-ss", f"{start_s:.3f}", "-i", mkv_path, "-t", f"{end_s - start_s:.3f}", "-map", "0:v:0",
           "-vf", f"select='isnan(prev_selected_t)+gte(t-prev_selected_t,{step_s - 0.05:.2f})',"
                  f"scale='trunc(ih*dar/2)*2':ih,setsar=1,scale={FRAME_W}:-2" + (",hue=s=0" if grey else ""),
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


def _windows(times: list[float], start_s: float, end_s: float) -> list[tuple[float, float]]:
    """DENSE_S either side of each time, overlapping windows merged."""
    out: list[tuple[float, float]] = []
    for t in sorted(times):
        a, b = max(start_s, t - DENSE_S), min(end_s, t + DENSE_S)
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
# screen. A reading further than this from it is more likely a sign or a caption in the
# episode, and is marked down by OFF_CARD.
CARD_WINDOW_S = 10.0
OFF_CARD = 0.1
# Two titles reading one episode's name this close in time, both at least this well, are
# reading the show's own text.
ALIGN_S = 3.0
ALIGNED_SCORE = 0.8


class _Shared:
    """Whether a line of one title's text shows on another title too: the show's own text,
    such as a theme song's captions, a logo or "presents"."""

    def __init__(self, readings: dict[int, TitleReading]):
        # Runs as well as lines: a caption set as "JAKE" over "THE DOG" is shared as a whole.
        self.keys = {i: {k for k in (_key(_text(run)) for _, run in _all_runs(r)) if len(k) >= SHARED_MIN}
                     for i, r in readings.items()}
        self.cache: dict[tuple[int, str], bool] = {}

    def __call__(self, i: int, key: str) -> bool:
        if len(key) < SHARED_MIN:
            return False
        if (i, key) not in self.cache:
            # Part of the other title's text counts: OCR catches a caption half drawn
            # ("JAKE THE" for "JAKE THE DOG") on one title and whole on another.
            self.cache[(i, key)] = any(key in ks or any(key in k or _same_text(key, k) for k in ks)
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


def _best(hits: list[_Hit], card_t: float | None) -> list[_Hit]:
    """The best reading of each episode, best first, marking down readings away from the card time."""
    best: dict[tuple[int, int], _Hit] = {}
    for h in hits:
        off = card_t is not None and abs(h.t - card_t) > CARD_WINDOW_S
        rank = h.score - OFF_CARD if off else h.score
        k = (h.episode.season, h.episode.number)
        if k not in best or rank > best[k].rank:
            best[k] = _Hit(h.episode, h.score, rank, h.text, h.t)
    return sorted(best.values(), key=lambda h: (-h.rank, h.episode.season, h.episode.number))


def _quote_eps(eps: list[Episode]) -> str:
    names = [f"“{e.title}”" for e in eps]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def _judge(i: int, hs: list[_Hit], n_read: int, sure_score: float, margin: float,
           card_t: float | None) -> CardMatch:
    if not hs:
        return CardMatch(title_index=i, note="No title card was read: no episode's name is on screen in the "
                                             "title's opening minutes.")
    best, rest = hs[0], hs[1:]
    cm = CardMatch(title_index=i, episode=best.episode, score=best.score, text=best.text, t=best.t,
                   others=[(h.episode, h.score) for h in rest[:3]])
    close = [h.episode for h in rest if h.rank > best.rank - margin]
    if close:
        cm.note = f"The title card reads “{best.text}”, which could be {_quote_eps([best.episode] + close[:2])}."
    elif best.rank < sure_score:
        cm.note = f"The title card reads “{best.text}”, which is closest to “{best.episode.title}”."
    elif n_read < 2:
        cm.note = (f"The title card reads “{best.text}”. Only one title has been read, so the show's own "
                   "on-screen text can't be told apart from episode names yet.")
    elif card_t is not None and abs(best.t - card_t) > CARD_WINDOW_S:
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
    episode is within margin of it, at least two titles have been read, it was on screen
    when the disc's other cards were, and no other title reads the same name. Otherwise
    the best guess is kept with a note saying why it's unsure; a title whose text names
    no episode gets episode None."""
    shared = _Shared(readings)
    score = _Scorer(episodes)
    hits = _drop_shown({i: _title_hits(i, r, shared, score) for i, r in readings.items()})

    def judge_all(card_t):
        out = {}
        for i in sorted(readings):
            if readings[i].error and not readings[i].lines:
                out[i] = CardMatch(title_index=i, note=readings[i].error)
            else:
                out[i] = _judge(i, _best(hits[i], card_t), len(readings), sure_score, margin, card_t)
        return out

    out = judge_all(None)
    times = sorted(cm.t for cm in out.values() if cm.sure)
    if len(times) >= 2:
        out = judge_all(times[len(times) // 2])

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
