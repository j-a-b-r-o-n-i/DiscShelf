"""Guess what each title on a disc is (episode, film, play-all, extra, ...) and which streams to keep.

The guesses only pre-tick boxes in the UI; the user can change any of them.
"""
from __future__ import annotations

from statistics import median

from .models import Stream, Title

FEATURE_MIN_S = 3600        # a film is at least an hour
SIMILAR = 0.25              # episodes are within 25% of the run's median length
PLAY_ALL_SLACK = 0.10       # a play-all runs 90-110% of the episodes it strings together
SHORT_S = 300               # under 5 minutes is a menu clip, logo or trailer
SHORT_RUN = 3               # episodes shorter than min_episode_seconds (11-minute cartoons) need a run of 3+

_COMMENTARY_WORDS = ("commentary", "descriptive", "audio description", "visually impaired")


def _similar_run(cands: list[Title]) -> list[Title]:
    """The largest group of 2+ titles with similar lengths, or [] when there is none."""
    best: list[Title] = []
    for c in cands:
        group = [o for o in cands if abs(o.duration_s - c.duration_s) <= SIMILAR * c.duration_s]
        if len(group) > len(best) or (len(group) == len(best) and
                                      sum(o.duration_s for o in group) > sum(o.duration_s for o in best)):
            best = group
    if len(best) < 2:
        return []
    # Re-centre on the median so one outlier at the edge of the first group doesn't decide it
    med = median(o.duration_s for o in best)
    best = [o for o in cands if abs(o.duration_s - med) <= SIMILAR * med]
    return best if len(best) >= 2 else []


def segments_comparable(titles: list[Title]) -> bool:
    """Blu-ray segment numbers are shared across the disc; DVD ones restart at 1 in every
    title (each 11-minute episode reports "1-3"). Two titles with the same segments but
    different lengths give the DVD kind away."""
    seen: dict[tuple, int] = {}
    for t in titles:
        if not t.segments:
            continue
        key = tuple(t.segments)
        if key in seen and abs(seen[key] - t.duration_s) > 2:
            return False
        seen.setdefault(key, t.duration_s)
    return True


def classify_titles(titles: list[Title], min_episode_seconds: int = 900, min_title_seconds: int = SHORT_S) -> list[Title]:
    """Set kind and selected on every title, in place, and return the list.

    Titles from min_title_seconds up are considered. A run of similar titles shorter than
    min_episode_seconds still counts as episodes when there are SHORT_RUN or more of them."""
    by_segments = segments_comparable([t for t in titles if t.duration_s >= min_title_seconds])
    kept: list[Title] = []
    for t in titles:
        t.selected = False
        if t.duration_s < min_title_seconds:
            t.kind = "short"
        elif by_segments and t.segments and any(k.segments == t.segments for k in kept):
            t.kind = "duplicate"
        elif not by_segments and t.size_bytes and any(
                abs(k.duration_s - t.duration_s) <= 1 and k.size_bytes == t.size_bytes for k in kept):
            t.kind = "duplicate"
        else:
            t.kind = "unknown"
            kept.append(t)

    # A play-all is a playlist made of the segments of two or more other titles.
    for t in kept:
        if not by_segments or not t.segments:
            continue
        mine = set(t.segments)
        inside = [o for o in kept if o is not t and o.segments
                  and set(o.segments) <= mine and len(set(o.segments)) < len(mine)]
        if len(inside) >= 2:
            t.kind = "play_all"
    rest = [t for t in kept if t.kind != "play_all"]

    def episode_run(cands: list[Title]) -> list[Title]:
        run = _similar_run(cands)
        if run and median(o.duration_s for o in run) < min_episode_seconds and len(run) < SHORT_RUN:
            return []    # two short titles are featurettes, not a TV disc
        return run

    # Without segment overlap, fall back to length: about the sum of a run of similar titles.
    for t in list(rest):
        run = episode_run([o for o in rest if o is not t])
        total = sum(o.duration_s for o in run)
        if run and (1 - PLAY_ALL_SLACK) * total <= t.duration_s <= (1 + PLAY_ALL_SLACK) * total:
            t.kind = "play_all"
            rest.remove(t)

    run = episode_run(rest)
    by_length = sorted(rest, key=lambda t: t.duration_s, reverse=True)
    longest = by_length[0] if by_length else None
    nxt = by_length[1] if len(by_length) > 1 else None
    is_feature = (longest is not None and longest.duration_s >= FEATURE_MIN_S
                  and (nxt is None or longest.duration_s >= 2 * nxt.duration_s)
                  # a film disc can carry two similar featurettes; they don't make it a TV disc
                  and sum(o.duration_s for o in run) < longest.duration_s)

    for t in rest:
        if is_feature and t is longest:
            t.kind, t.selected = "feature", True
        elif not is_feature and t in run:
            t.kind, t.selected = "episode", True
        else:
            t.kind = "extra"

    # A film with one long extra (a 100-minute film and a 55-minute documentary) fails the
    # 2x test; when nothing at all was picked, the longest title over an hour is the film.
    if longest is not None and not any(t.selected for t in titles) and longest.duration_s >= FEATURE_MIN_S:
        longest.kind, longest.selected = "feature", True
    return titles


def _is_commentary(s: Stream) -> bool:
    text = f"{s.name} {s.flags}".lower()
    return any(w in text for w in _COMMENTARY_WORDS)


def _is_core(s: Stream) -> bool:
    # the DTS / TrueHD core MakeMKV lists next to the HD track it came from
    return "core" in s.flags.split(",")


def pick_streams(title: Title, audio_langs: list[str], subtitle_langs: list[str],
                 include_commentary: bool = False) -> dict:
    """{"audio": [stream indexes], "subtitle": [...]}, in the order of the language lists.

    Audio leaves out commentary and audio-description tracks unless include_commentary is
    set, and then puts them after the main tracks of that language. Split-out cores of an
    HD audio track are left out, as they repeat the HD track. If no audio track matches a
    listed language, the first audio track is kept so the file is never silent.
    """
    audio = [s for s in title.streams if s.kind == "audio"]
    subs = [s for s in title.streams if s.kind == "subtitle"]
    picked_audio: list[int] = []
    for lang in (l.lower() for l in audio_langs):
        of_lang = [s for s in audio if s.lang.lower() == lang and not _is_core(s)]
        main = [s.index for s in of_lang if not _is_commentary(s)]
        extra = [s.index for s in of_lang if _is_commentary(s)] if include_commentary else []
        picked_audio += [i for i in main + extra if i not in picked_audio]
    if not picked_audio and audio:
        first = next((s for s in audio if not _is_core(s)), audio[0])
        picked_audio = [first.index]

    picked_subs: list[int] = []
    for lang in (l.lower() for l in subtitle_langs):
        picked_subs += [s.index for s in subs if s.lang.lower() == lang and s.index not in picked_subs]
    return {"audio": picked_audio, "subtitle": picked_subs}
