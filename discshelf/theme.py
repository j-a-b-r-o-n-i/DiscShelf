"""Find a show's theme song in a title by its sound, so the title card reader can skip it.

Chromaprint's fpcalc turns audio into one 32-bit value per ITEM_S seconds; the same sound
gives values a few bits apart, even from a different encode. Two episodes of a show share
the theme song, so the longest stretch where their openings sound the same is the theme,
and where it ends in each title is where the card reader starts.

A show can have more than one theme: a special, or a season whose theme changes part way
through. Each one learned is kept as a version, in state_dir()/themes/<show>.json. A title
that matches no version is kept as an unplaced opening until a second title shares a
stretch with it, and that stretch becomes a new version.

The theme only says where to start reading. titlecard.py reads the skipped part too when
nothing is found after it, so a wrong or missing theme costs time, never a card.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass

from .config import state_dir

ITEM_S = 0.1238              # seconds of audio per fingerprint value
MAX_BITS = 10                # values this many bits apart or fewer are the same sound
GAP_S = 1.0                  # a run of the same sound can break this long (a sound effect over the theme)
MIN_THEME_S = 15.0           # shorter shared stretches are stings, logos and silence
# Longer is not a theme: two titles of the same episode share their whole opening.
MAX_THEME_S = 150.0
LISTEN_S = 300.0             # how much of each title's opening is fingerprinted to learn a theme
# A known theme is looked for in this much first, as fpcalc takes about 0.6 s a minute.
FIRST_LISTEN_S = 120.0
KEEP_UNPLACED = 6            # openings kept while waiting for a second title with the same theme
KEEP_VERSIONS = 8


@dataclass
class Placement:
    """Where a theme version plays in a title, in seconds."""
    start_s: float
    end_s: float
    version: int                 # index into the show's versions
    learned: bool = False        # the version was learned from this title


def fingerprint(path: str, seconds: float = LISTEN_S, fpcalc: str = "fpcalc",
                run=subprocess.run) -> list[int] | None:
    """The fingerprint of a title's first seconds of audio, or None when fpcalc is missing or
    fails; the reader then starts from the beginning."""
    cmd = [fpcalc, "-raw", "-length", str(max(1, int(seconds))), "-json", path]
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        values = json.loads(proc.stdout)["fingerprint"]
        return [int(v) & 0xFFFFFFFF for v in values]
    except (ValueError, KeyError, TypeError):
        return None


@dataclass
class _Run:
    a_start: int                 # items, end exclusive
    a_end: int
    b_start: int
    b_end: int

    @property
    def items(self) -> int:
        return self.a_end - self.a_start


def common_run(a: list[int], b: list[int], min_s: float = MIN_THEME_S,
               max_s: float = MAX_THEME_S) -> _Run | None:
    """The longest stretch, between min_s and max_s long, where a and b sound the same at
    some shift of one against the other, or None."""
    min_items, max_items, gap = int(min_s / ITEM_S), int(max_s / ITEM_S), int(GAP_S / ITEM_S)
    best: _Run | None = None
    for shift in range(-(len(a) - min_items), len(b) - min_items + 1):   # b index = a index + shift
        i0, i1 = max(0, -shift), min(len(a), len(b) - shift)
        if i1 - i0 < min_items:
            continue
        ok = [(x ^ y).bit_count() <= MAX_BITS for x, y in zip(a[i0:i1], b[i0 + shift:i1 + shift])]
        # Unrelated audio matches about 1 value in 40, so most shifts end here.
        if ok.count(True) < min_items // 2:
            continue
        idx = [k for k, v in enumerate(ok) if v]
        s = p = idx[0]
        hits = 1
        for k in idx[1:] + [len(ok) + gap + 1]:
            if k - p > gap:
                n = p - s + 1
                if (min_items <= n <= max_items and hits >= n * 0.4
                        and (best is None or n > best.items)):
                    best = _Run(i0 + s, i0 + p + 1, i0 + s + shift, i0 + p + 1 + shift)
                s, hits = k, 0
            p = k
            hits += 1
    return best


_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _lock(path: str) -> threading.Lock:
    with _locks_lock:
        return _locks.setdefault(path, threading.Lock())


class ThemeBook:
    """The theme versions learned for one show, kept on disk. Safe to use from the several
    threads that read a disc's titles at once."""

    def __init__(self, show_key: str, folder: str | None = None):
        name = re.sub(r"[^A-Za-z0-9_-]+", "-", str(show_key)).strip("-") or "show"
        self.path = os.path.join(folder or os.path.join(state_dir(), "themes"), name + ".json")

    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {"versions": list(data.get("versions") or []), "unplaced": list(data.get("unplaced") or [])}
        except (OSError, ValueError):
            pass
        return {"versions": [], "unplaced": []}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def versions(self) -> list[dict]:
        """Each version learned: {"fingerprint": [...], "seconds": length}."""
        with _lock(self.path):
            return self._load()["versions"]

    @staticmethod
    def _find(fp: list[int], versions: list[dict]) -> Placement | None:
        best = None
        for n, v in enumerate(versions):
            seg = v.get("fingerprint") or []
            run = common_run(seg, fp, min_s=min(MIN_THEME_S, len(seg) * ITEM_S * 0.8))
            if run and (best is None or run.items > best[0].items):
                best = (run, n)
        if best is None:
            return None
        run, n = best
        # Where the matching sound ends in this title, not where the version would end: a
        # shortened variant of the theme ends early, and the card can follow straight on.
        return Placement(round(run.b_start * ITEM_S, 2), round(run.b_end * ITEM_S, 2), n)

    def place(self, fp: list[int]) -> Placement | None:
        """Where a known version plays in this fingerprint, without learning anything."""
        if not fp:
            return None
        return self._find(fp, self.versions())

    def place_early(self, fp: list[int]) -> Placement | None:
        """Where a known version plays in a short fingerprint, when it ends inside it; a
        theme cut off by the fingerprint's end is left for the longer one to place."""
        p = self.place(fp)
        return p if p and p.end_s < len(fp) * ITEM_S - 2 * GAP_S else None

    def place_or_learn(self, fp: list[int]) -> Placement | None:
        """Where a known version plays in this fingerprint. When none does, the stretch this
        opening shares with an earlier unplaced one is learned as a new version; when none
        is shared either, the opening is kept for a later title to compare with."""
        if not fp:
            return None
        with _lock(self.path):
            data = self._load()
            found = self._find(fp, data["versions"])
            if found:
                return found
            for k, other in enumerate(data["unplaced"]):
                run = common_run(fp, other)
                if run is None:
                    continue
                seg = fp[run.a_start:run.a_end]
                data["versions"] = (data["versions"] + [{"fingerprint": seg, "seconds": round(len(seg) * ITEM_S, 1)}]
                                    )[-KEEP_VERSIONS:]
                del data["unplaced"][k]
                self._save(data)
                return Placement(round(run.a_start * ITEM_S, 2), round(run.a_end * ITEM_S, 2),
                                 len(data["versions"]) - 1, learned=True)
            data["unplaced"] = (data["unplaced"] + [fp])[-KEEP_UNPLACED:]
            self._save(data)
            return None
