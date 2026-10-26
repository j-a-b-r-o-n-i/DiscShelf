"""Pair a disc's titles with TVmaze episodes (or a film's feature) by disc order and running time.

Nothing here reorders titles on its own. When running times disagree with disc order,
the match is marked "check" with a sentence saying why, and the user decides.
"""
from __future__ import annotations

import difflib
import os
import re

from .models import Episode, Match, Title


def next_episode_number(season_episodes_done: set[int]) -> int:
    """The first episode number not yet ripped, so disc 2 carries on where disc 1 stopped."""
    n = 1
    while n in season_episodes_done:
        n += 1
    return n


def _fits(minutes: float, listed: float, tolerance: float) -> bool:
    if listed <= 0:
        return False
    if abs(minutes - listed) / listed <= tolerance:
        return True
    # TVmaze lists broadcast shows by their time slot, adverts included, so a 43 min
    # episode shows as 60 and an 11 min cartoon as 15. A title between 65% and 100% of a
    # whole quarter-hour slot fits.
    return listed % 15 == 0 and 0.65 * listed <= minutes <= listed


def _err(minutes: float, listed: int | None) -> float:
    return abs(minutes - listed) / listed if listed else float("inf")


def _mins(t: Title) -> int:
    return round(t.duration_s / 60)


def _ep_label(e: Episode) -> str:
    return f"episode {e.number}, “{e.title}”" if e.title else f"episode {e.number}"


def extra_name(t: Title) -> str:
    """A file name stem for an extra, from MakeMKV's source name when it has words in it."""
    stem = os.path.splitext(os.path.basename(t.source or ""))[0]
    words = re.sub(r"[_.\-]+", " ", stem).strip()
    if not words or not re.search(r"[A-Za-z]", words) or re.fullmatch(r"[A-Za-z]?\d+[A-Za-z]?", words.replace(" ", "")):
        # Playlist numbers such as 00800 mean nothing to a viewer.
        return f"Title {t.index:02d}"
    return " ".join(w if w.isupper() and len(w) > 1 else w.capitalize() for w in words.split())


def _non_episode(t: Title) -> Match:
    if t.selected and t.kind == "extra":
        return Match(title_index=t.index, target="extra", extra_name=extra_name(t), confidence="close")
    if t.selected:
        return Match(title_index=t.index, target="extra", extra_name=extra_name(t), confidence="check",
                     note="This title is not marked as an episode, so it will be saved as an extra.")
    return Match(title_index=t.index, target="skip", confidence="close")


def match_titles(titles: list[Title], episodes: list[Episode], start_number: int = 1,
                 tolerance: float = 0.12) -> list[Match]:
    """One Match per title, in title-index order.

    Selected "episode" titles take episodes in disc order from start_number. A title about
    as long as two episodes together becomes a double episode. Unselected titles are skipped
    and other selected titles become extras.
    """
    by_num = {e.number: e for e in episodes}
    ep_titles = sorted((t for t in titles if t.selected and t.kind == "episode"), key=lambda t: t.index)
    matches: dict[int, Match] = {}

    n = start_number
    for t in ep_titles:
        mins = _mins(t)
        ep = by_num.get(n)
        if ep is None:
            matches[t.index] = Match(title_index=t.index, target="skip", confidence="check",
                                     note=f"The season has no episode {n} on TVmaze, so this title is not "
                                          "matched to anything.")
            continue
        m = Match(title_index=t.index, target="episode", episode=ep)
        nxt = by_num.get(n + 1)
        rt = ep.runtime_min
        if rt is None:
            m.confidence = "check"
            m.note = f"TVmaze lists no running time for {_ep_label(ep)}, so the {mins} min title could not be compared."
            n += 1
        elif _fits(mins, rt, tolerance):
            m.confidence = "close"
            n += 1
        elif nxt is not None and nxt.runtime_min and _fits(mins, rt + nxt.runtime_min, tolerance):
            m.episode_end = nxt.number
            m.confidence = "check"
            m.note = (f"Title is {mins} min, about as long as episodes {ep.number} and {nxt.number} together, "
                      "so it is saved as a double episode.")
            n += 2
        else:
            m.confidence = "check"
            if mins > rt * 1.5:
                why = "so it may hold two episodes"
            elif mins < rt:
                why = "so it may be a different episode or an extra"
            else:
                why = "so it may be an extended cut or a different episode"
            m.note = f"Title is {mins} min but the episode is listed as {rt} min, {why}."
            n += 1
        matches[t.index] = m

    _flag_order(ep_titles, matches, episodes, start_number, tolerance)

    out = []
    for t in sorted(titles, key=lambda t: t.index):
        out.append(matches.get(t.index) or _non_episode(t))
    return out


def _flag_order(ep_titles: list[Title], matches: dict[int, Match], episodes: list[Episode],
                start_number: int, tolerance: float) -> None:
    """Mark a single-episode match "check" when its running time is wrong for its episode
    but right for another episode in the stretch this disc covers. Discs are sometimes
    authored out of broadcast order; the user decides, nothing is swapped here."""
    window = [e for e in episodes if start_number <= e.number < start_number + len(ep_titles) and e.runtime_min]
    for t in ep_titles:
        m = matches[t.index]
        if m.target != "episode" or m.episode is None or m.episode_end is not None:
            continue
        mins = _mins(t)
        own = m.episode.runtime_min
        if own and _fits(mins, own, tolerance):
            continue
        others = [e for e in window if e.number != m.episode.number and _fits(mins, e.runtime_min, tolerance)
                  and _err(mins, e.runtime_min) < _err(mins, own)]
        if not others:
            continue
        best = min(others, key=lambda e: (_err(mins, e.runtime_min), e.number))
        m.confidence = "check"
        # Replaces the length note: a likely swap is the more useful thing to say.
        m.note = f"Order unclear: the running time also fits {_ep_label(best)}."


def _words(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).split()


def parse_episode_list(text: str) -> list[str]:
    """Episode names from text typed or pasted off the case, one per line, in disc order.
    Leading numbers ("1.", "02 -", "3)") are dropped, and so are blank lines."""
    out = []
    for line in text.splitlines():
        name = re.sub(r"^\s*\d{1,3}\s*[.):\-]?\s+", "", line).strip()
        if name:
            out.append(name)
    return out


def _joined(s: str) -> str:
    return "".join(_words(s))


def score_name(text: str, episodes: list[Episode], min_score: float = 0.6) -> list[tuple[Episode, float]]:
    """Episodes whose name text could be, with a score from 0 to 1, best first.

    Made for OCR, which drops spaces ("THEENCHIRIDION!") and misreads letters ("PONERAN"
    for "Power Animal"), so names are compared with spaces and punctuation removed. A
    whole name scores 1. A shortened name the episode's name contains ("Wizard" in "Finn
    the Wizard") scores by how much of it is there, and anything else by similarity."""
    t = _joined(text)
    if len(t) < 3:
        return []
    out = []
    for e in episodes:
        n = _joined(e.title)
        if not n:
            continue
        if t == n:
            s = 1.0
        elif len(t) >= 5 and t in n:
            s = 0.6 + 0.35 * len(t) / len(n)
        elif len(n) >= 5 and n in t:
            s = 0.6 + 0.35 * len(n) / len(t)
        else:
            s = difflib.SequenceMatcher(None, t, n).ratio()
        if s >= min_score:
            out.append((e, round(s, 3)))
    out.sort(key=lambda es: (-es[1], es[0].season, es[0].number))
    return out


def find_episode(name: str, episodes: list[Episode]) -> list[Episode]:
    """Episodes that could be the one called name, best first; [] when none is close.

    A case often shortens a title ("Wizard" for "Finn the Wizard"), so after an exact match
    an episode whose name holds every word of name counts, then near spellings. Ties go to
    the earliest episode."""
    key = "".join(_words(name))
    if not key:
        return []
    order = sorted(episodes, key=lambda e: (e.season, e.number))
    exact = [e for e in order if "".join(_words(e.title)) == key]
    if exact:
        return exact
    words = set(_words(name)) - {"the", "a", "an"} or set(_words(name))
    holds = [e for e in order if words <= set(_words(e.title))]
    if holds:
        return holds
    joined = {}
    for e in order:
        joined.setdefault("".join(_words(e.title)), e)
    return [joined[k] for k in difflib.get_close_matches(key, list(joined), n=3, cutoff=0.8)]


def match_list(titles: list[Title], episodes: list[Episode], names: list[str], tolerance: float = 0.12) -> list[Match]:
    """One Match per title, pairing the selected episode titles in disc order with the names
    the user typed from the case. episodes may span several seasons."""
    ep_titles = sorted((t for t in titles if t.selected and t.kind == "episode"), key=lambda t: t.index)
    matches: dict[int, Match] = {}
    for i, t in enumerate(ep_titles):
        if i >= len(names):
            matches[t.index] = Match(title_index=t.index, target="skip", confidence="check",
                                     note=f"The episode list has {len(names)} names, so this title is not matched.")
            continue
        found = find_episode(names[i], episodes)
        if not found:
            matches[t.index] = Match(title_index=t.index, target="skip", confidence="check",
                                     note=f"TVmaze has no episode called “{names[i]}”. Pick it by hand.")
            continue
        ep = found[0]
        m = Match(title_index=t.index, target="episode", episode=ep, confidence="close")
        notes = []
        if len(found) > 1:
            others = ", ".join(f"S{e.season:02d}E{e.number:02d} “{e.title}”" for e in found[1:3])
            notes.append(f"“{names[i]}” could also be {others}.")
        elif "".join(_words(ep.title)) != "".join(_words(names[i])):
            notes.append(f"“{names[i]}” is taken to be “{ep.title}”.")
        if ep.runtime_min and not _fits(_mins(t), ep.runtime_min, tolerance):
            notes.append(f"Title is {_mins(t)} min but the episode is listed as {ep.runtime_min} min.")
        if notes:
            m.confidence, m.note = "check", " ".join(notes)
        matches[t.index] = m
    return [matches.get(t.index) or _non_episode(t) for t in sorted(titles, key=lambda t: t.index)]


def feature_match(titles: list[Title]) -> list[Match]:
    """For a film: the first selected "feature" title is the film, other selected titles are extras."""
    out = []
    have_feature = False
    for t in sorted(titles, key=lambda t: t.index):
        if t.selected and t.kind == "feature" and not have_feature:
            have_feature = True
            out.append(Match(title_index=t.index, target="feature", confidence="close"))
        elif t.selected and t.kind == "feature":
            # A second cut of the film, such as a theatrical and an extended version.
            out.append(Match(title_index=t.index, target="extra", extra_name=extra_name(t), confidence="check",
                             note="A second feature-length title is selected, so it is saved as an extra; "
                                  "rename it if it is another cut of the film."))
        elif t.selected:
            out.append(Match(title_index=t.index, target="extra", extra_name=extra_name(t), confidence="close"))
        else:
            out.append(Match(title_index=t.index, target="skip", confidence="close"))
    if not have_feature:
        # Fall back to the longest selected title so a film always gets a feature.
        sel = [m for m in out if m.target == "extra"]
        if sel:
            dur = {t.index: t.duration_s for t in titles}
            best = max(sel, key=lambda m: (dur.get(m.title_index, 0), -m.title_index))
            best.target, best.extra_name, best.confidence = "feature", "", "check"
            best.note = "No title is marked as the film, so the longest selected title is used."
    return out
