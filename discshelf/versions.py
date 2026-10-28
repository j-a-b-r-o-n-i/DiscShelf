"""Titles on one disc that play the same film or episode, and what differs between them.

A Blu-ray title is a playlist: a list of video pieces (m2ts files) to play, and the audio and
subtitle tracks to offer with them. A disc often has more than one title for the same film:

- "languages": the same pieces with different tracks. A disc sold in several countries has
  one playlist per region, and the player picks one from its region setting.
- "same": the same pieces and the same languages, for example a second playlist the menu uses.
- "cut": a film and a longer cut of it. The longer one plays every piece of the shorter one,
  plus scenes only it has.
- "order": the same pieces in different orders. Some studios add dozens of these so copy
  programs pick the wrong one; only one tells the story in order.

find_groups() groups such titles and recommends one of each group. classify.py rips only the
recommended one; the Rip page shows the comparison so the choice can be changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Title

FEATURE_MIN_S = 3600          # a cut is only looked for between titles of an hour or more
CUT_MAX_EXTRA = 0.25          # a longer cut runs at most 25% longer than the shorter one
FOLD_AT = 3                   # languages that only some titles have fold into one row from this many

LANG_NAMES = {"eng": "English", "fra": "French", "fre": "French", "spa": "Spanish", "deu": "German",
              "ger": "German", "ita": "Italian", "jpn": "Japanese", "por": "Portuguese", "nld": "Dutch",
              "dut": "Dutch", "kor": "Korean", "zho": "Chinese", "chi": "Chinese", "swe": "Swedish",
              "dan": "Danish", "nor": "Norwegian", "fin": "Finnish", "pol": "Polish", "rus": "Russian",
              "cat": "Catalan", "ces": "Czech", "cze": "Czech", "hun": "Hungarian", "tha": "Thai",
              "tur": "Turkish", "heb": "Hebrew", "ara": "Arabic", "hin": "Hindi", "gre": "Greek", "ell": "Greek"}

_CODEC = {"DTS-HD MA": "DTS-HD MA", "DTS-HD HR": "DTS-HD HR", "TrueHD": "Dolby TrueHD", "DD": "Dolby Digital",
          "DD+": "Dolby Digital Plus", "AC3": "Dolby Digital", "E-AC3": "Dolby Digital Plus", "DTS": "DTS",
          "LPCM": "LPCM"}


@dataclass
class LangRow:
    name: str                     # "English", or "7 more languages" for a folded row
    yours: bool                   # one of your audio or subtitle languages
    cells: list[str]              # per title in Group.titles: "DTS-HD MA, Dolby Digital · 1 subtitle track" or "—"


@dataclass
class Strip:
    title: Title
    pieces: list[tuple[int, bool]]   # (segment number, marked): only in this title (cut) or out of place (order)
    note: str                        # "+10:53 in 4 scenes", "6 pieces out of place", "In order"


@dataclass
class Group:
    kind: str                     # "languages" | "same" | "cut" | "order"
    titles: list[Title]           # the recommended title first, then disc order
    head: str                     # "The film, twice: same video, different languages"
    why: str                      # one or two sentences saying what differs
    pick_why: str                 # why the first title is recommended
    same: list[str] = field(default_factory=list)       # what every title shares, as short tags
    langs: list[LangRow] = field(default_factory=list)  # "languages" and "same" groups
    strips: list[Strip] = field(default_factory=list)   # "cut" and "order" groups
    only: dict[int, str] = field(default_factory=dict)  # title index -> what only it has, e.g. "Japanese audio"

    @property
    def pick(self) -> Title:
        return self.titles[0]


def lang_name(code: str) -> str:
    return LANG_NAMES.get(code, code.upper() if code else "Unknown language")


def _is_core(s) -> bool:
    return "core" in s.flags.split(",")


def _langs(t: Title, kind: str) -> list[str]:
    out: list[str] = []
    for s in t.streams:
        if s.kind == kind and s.lang and s.lang not in out and not (kind == "audio" and _is_core(s)):
            out.append(s.lang)
    return out


def _clock(s: int) -> str:
    h, rem = divmod(max(0, s), 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def _names(codes: list[str]) -> str:
    names = [lang_name(c) for c in codes]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


# --------------------------------------------------------------------------- grouping


def _related(a: Title, b: Title, by_segments: bool) -> str:
    """How two titles relate: "same" (same pieces in the same order), "order", "cut", or ""."""
    if by_segments and a.segments and b.segments:
        if a.segments == b.segments:
            return "same"
        if (len(a.segments) >= 2 and sorted(a.segments) == sorted(b.segments)
                and abs(a.duration_s - b.duration_s) <= 2):
            return "order"
        short, long_ = (a, b) if a.duration_s <= b.duration_s else (b, a)
        if (short.duration_s >= FEATURE_MIN_S and set(short.segments) < set(long_.segments)
                and long_.duration_s - short.duration_s <= CUT_MAX_EXTRA * short.duration_s):
            return "cut"
        return ""
    # DVDs number their pieces again in every title, so only length and size can tell.
    if a.size_bytes and abs(a.duration_s - b.duration_s) <= 1 and a.size_bytes == b.size_bytes:
        return "same"
    return ""


def _score(t: Title, audio_langs: list[str], subtitle_langs: list[str]) -> tuple:
    """Higher is better: how many of your audio languages it has, then your subtitle languages,
    then how many audio tracks and subtitle tracks it has in them."""
    al, sl = _langs(t, "audio"), _langs(t, "subtitle")
    audio = sum(1 for s in t.streams if s.kind == "audio" and s.lang in audio_langs and not _is_core(s))
    subs = sum(1 for s in t.streams if s.kind == "subtitle" and s.lang in subtitle_langs)
    return (sum(1 for l in audio_langs if l in al), sum(1 for l in subtitle_langs if l in sl), audio, subs)


def find_groups(titles: list[Title], audio_langs: list[str], subtitle_langs: list[str],
                by_segments: bool = True, min_seconds: int = 0) -> list[Group]:
    """Groups of 2 or more titles that play the same film or episode, each with the
    recommended title first. Titles shorter than min_seconds are left out."""
    cands = [t for t in titles if t.duration_s >= min_seconds]
    parent = {t.index: t.index for t in cands}
    rel: dict[int, set[str]] = {}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, a in enumerate(cands):
        for b in cands[i + 1:]:
            r = _related(a, b, by_segments)
            if r:
                ra, rb = root(a.index), root(b.index)
                kinds = rel.pop(rb, set()) | {r}
                parent[rb] = ra
                rel.setdefault(ra, set()).update(kinds)
    groups: dict[int, list[Title]] = {}
    for t in cands:
        groups.setdefault(root(t.index), []).append(t)
    out = []
    for r, members in groups.items():
        if len(members) < 2:
            continue
        kinds = rel.get(r, {"same"})
        kind = "cut" if "cut" in kinds else "order" if "order" in kinds else "same"
        out.append(_describe(kind, members, audio_langs, subtitle_langs))
    return out


# --------------------------------------------------------------------------- describing


def _what(titles: list[Title]) -> str:
    return "The film" if min(t.duration_s for t in titles) >= FEATURE_MIN_S else "The same title"


def _times(n: int) -> str:
    return {2: "twice", 3: "3 times"}.get(n, f"{n} times")


def _describe(kind: str, members: list[Title], audio_langs: list[str], subtitle_langs: list[str]) -> Group:
    if kind == "same":
        sigs = {(tuple(sorted(_langs(t, "audio"))), tuple(sorted(_langs(t, "subtitle")))) for t in members}
        if len(sigs) > 1:
            return _by_languages(members, audio_langs, subtitle_langs)
        pick = members[0]
        rest = members[1:]
        g = Group("same", [pick] + rest, f"{_what(members)}, {_times(len(members))}: the same video and tracks",
                  "These titles play the same video pieces with the same languages. The disc's menus use more "
                  "than one, so it lists them all.",
                  f"Ripping {pick.source or 'the first one'} gives the same file as any of the others.")
        g.same = _same_tags(members, langs_same=True)
        g.langs = _lang_rows(g.titles, audio_langs, subtitle_langs)
        g.only = {t.index: "Same tracks" for t in rest}
        return g
    if kind == "cut":
        return _by_cut(members, audio_langs, subtitle_langs)
    return _by_order(members)


def _same_tags(titles: list[Title], langs_same: bool = False) -> list[str]:
    t0 = titles[0]
    tags = []
    if all(t.segments == t0.segments for t in titles) and t0.segments:
        tags.append("The same video pieces" if len(t0.segments) > 1 else f"Video piece {t0.segments[0]:05d}.m2ts")
    if all(t.size_bytes == t0.size_bytes for t in titles) and t0.size_bytes:
        tags.append(f"{t0.size_bytes / 1e9:.1f} GB")
    if all(abs(t.duration_s - t0.duration_s) <= 2 for t in titles):
        tags.append(_clock(t0.duration_s))
    if all(t.chapters == t0.chapters for t in titles) and t0.chapters:
        tags.append(f"{t0.chapters} chapters")
    if langs_same:
        tags.append("Languages and subtitles")
    else:
        for kind, word in (("audio", "audio"), ("subtitle", "subtitles")):
            shared = [l for l in _langs(t0, kind) if all(l in _langs(t, kind) for t in titles)]
            for l in shared[:2]:
                tags.append(f"{lang_name(l)} {word}")
    return tags


def _cell(t: Title, codes: list[str]) -> str:
    """What a title has in these languages: "DTS-HD MA, Dolby Digital · 1 subtitle track", or "—"."""
    audio = [s for s in t.streams if s.kind == "audio" and s.lang in codes and not _is_core(s)]
    subs = [s for s in t.streams if s.kind == "subtitle" and s.lang in codes]
    if not audio and not subs:
        return "—"
    if len(codes) > 1:
        return _names([c for c in codes if c in _langs(t, "audio") + _langs(t, "subtitle")])
    names: list[str] = []
    for s in audio:
        n = _CODEC.get(s.codec, s.codec or "Audio")
        names.append(n)
    counted = []
    for n in names:
        k = names.count(n)
        label = f"{k} × {n}" if k > 1 else n
        if label not in counted:
            counted.append(label)
    parts = [", ".join(counted)] if counted else []
    if subs:
        parts.append(f"{len(subs)} subtitle track{'' if len(subs) == 1 else 's'}")
    return " · ".join(parts)


def _lang_rows(titles: list[Title], audio_langs: list[str], subtitle_langs: list[str]) -> list[LangRow]:
    order: list[str] = []
    for l in list(audio_langs) + list(subtitle_langs):
        if l not in order and any(l in _langs(t, "audio") + _langs(t, "subtitle") for t in titles):
            order.append(l)
    for t in titles:
        for l in _langs(t, "audio") + _langs(t, "subtitle"):
            if l not in order:
                order.append(l)
    mine = set(audio_langs) | set(subtitle_langs)
    rows: list[LangRow] = []
    # Languages that aren't yours fold together when the same titles have them, so a disc
    # with 8 European dubs on one title doesn't fill the screen.
    by_sig: dict[tuple, list[str]] = {}
    for l in order:
        if l in mine:
            continue
        sig = tuple(l in _langs(t, "audio") + _langs(t, "subtitle") for t in titles)
        by_sig.setdefault(sig, []).append(l)
    folded = {l for ls in by_sig.values() if len(ls) >= FOLD_AT for l in ls}
    done: set[tuple] = set()
    for l in order:
        if l in folded:
            sig = tuple(l in _langs(t, "audio") + _langs(t, "subtitle") for t in titles)
            if sig in done:
                continue
            done.add(sig)
            ls = by_sig[sig]
            rows.append(LangRow(f"{len(ls)} more languages", False, [_cell(t, ls) for t in titles]))
        else:
            rows.append(LangRow(lang_name(l), l in mine, [_cell(t, [l]) for t in titles]))
    return rows


def _by_languages(members: list[Title], audio_langs: list[str], subtitle_langs: list[str]) -> Group:
    best = max(members, key=lambda t: _score(t, audio_langs, subtitle_langs))
    titles = [best] + [t for t in members if t is not best]
    only: dict[int, str] = {}
    for t in titles:
        others = [o for o in titles if o is not t]
        extra_a = [l for l in _langs(t, "audio") if not any(l in _langs(o, "audio") for o in others)]
        extra_s = [l for l in _langs(t, "subtitle") if not any(l in _langs(o, "subtitle") for o in others)
                   and l not in extra_a]
        if extra_a:
            only[t.index] = f"{_names(extra_a)} audio" if len(extra_a) <= 2 else f"{len(extra_a)} more languages"
        elif extra_s:
            only[t.index] = f"{_names(extra_s)} subtitles" if len(extra_s) <= 2 else f"{len(extra_s)} more subtitles"
        else:
            only[t.index] = "Fewer languages"
    pick = titles[0]
    rest = titles[1:]
    same_video = all(t.segments == pick.segments for t in titles) and pick.segments
    what = "same video" if same_video else "same length"
    if len(rest) == 1:
        o = rest[0]
        mine_extra = [l for l in _langs(o, "audio") if l not in _langs(pick, "audio")]
        theirs = [l for l in _langs(pick, "audio") if l not in _langs(o, "audio")]
        if mine_extra and theirs:
            swap = (f"{_names(mine_extra)} audio" if len(mine_extra) <= 2 else f"{len(mine_extra)} other languages")
            gone = (_names(theirs) if len(theirs) <= 2 else f"{len(theirs)} other languages")
            why = f"{o.source or 'The other title'} has {swap} in place of {gone}."
        elif mine_extra:
            why = f"{o.source or 'The other title'} also has {_names(mine_extra)} audio."
        else:
            why = f"{o.source or 'The other title'} has fewer languages."
    else:
        why = "Each title offers a different set of languages."
    if same_video:
        why = "Both titles play the same video file. " + why if len(titles) == 2 else \
              "All of them play the same video file. " + why
    if any(l in _langs(pick, "audio") for l in audio_langs):
        subs = _names(subtitle_langs) + " subtitles" if subtitle_langs else "no subtitles"
        mine, best_other = _score(pick, audio_langs, subtitle_langs), max(
            _score(t, audio_langs, subtitle_langs) for t in rest)
        step = next((k for k in range(4) if mine[k] != best_other[k]), None)
        name = pick.source or "This title"
        pick_why = f"Your languages are {_names(audio_langs)} audio and {subs}. " + (
            f"{name} has more of your audio languages." if step == 0 else
            f"{name} has more of your subtitle languages." if step == 1 else
            f"Both have them, and {name} has more audio tracks in them." if step == 2 and len(titles) == 2 else
            f"{name} has the most audio tracks in them." if step == 2 else
            f"Both have them, and {name} has more subtitle tracks in them." if step == 3 and len(titles) == 2 else
            f"{name} has the most subtitle tracks in them." if step == 3 else
            "The titles have the same tracks in them, so the first one the disc lists is picked.")
    else:
        pick_why = "None of the titles has audio in your languages, so the first one the disc lists is picked."
    g = Group("languages", titles, f"{_what(titles)}, {_times(len(titles))}: {what}, different languages", why,
              pick_why)
    g.same = _same_tags(titles)
    g.langs = _lang_rows(titles, audio_langs, subtitle_langs)
    g.only = {k: v for k, v in only.items() if k != pick.index}
    return g


def _by_cut(members: list[Title], audio_langs: list[str], subtitle_langs: list[str]) -> Group:
    # MakeMKV lists the title the disc's "Play" button starts first, so the first one stays the pick.
    pick = members[0]
    rest = members[1:]
    titles = [pick] + rest
    every = set().union(*(set(t.segments) for t in titles))
    shared = set.intersection(*(set(t.segments) for t in titles))
    strips, only = [], {}
    for t in titles:
        extra = [s for s in t.segments if s not in shared]
        diff = t.duration_s - min(o.duration_s for o in titles)
        if extra and diff > 0:
            note = f"+{_clock(diff)} in {len(extra)} scene{'' if len(extra) == 1 else 's'}"
        else:
            note = f"Ends at {_clock(t.duration_s)}"
        strips.append(Strip(t, [(s, s not in shared) for s in t.segments], note))
        if t is not pick:
            only[t.index] = "Longer cut" if t.duration_s > pick.duration_s else "Shorter cut"
    longest = max(titles, key=lambda t: t.duration_s)
    diff = longest.duration_s - min(t.duration_s for t in titles)
    n_extra = len([s for s in longest.segments if s not in shared])
    head_n = "twice" if len(titles) == 2 else f"{len(titles)} times"
    why = (f"{longest.source or 'One title'} is {_clock(diff)} longer. It plays the same pieces as the shorter "
           f"one, plus {n_extra} scene{'' if n_extra == 1 else 's'} only it has. It's probably an extended cut.")
    g = Group("cut", titles, f"The film, {head_n}: {'two' if len(titles) == 2 else len(titles)} cuts", why,
              f"MakeMKV lists {pick.source or 'it'} first, which is usually the title the disc's Play button starts.")
    g.same = ["The same " + ("pieces" if len(shared) > 1 else "piece") + f" ({len(shared)} of {len(every)})"]
    if len({(tuple(_langs(t, 'audio')), tuple(_langs(t, 'subtitle'))) for t in titles}) == 1:
        g.same.append("Languages and subtitles")
    g.strips, g.only = strips, only
    return g


def _by_order(members: list[Title]) -> Group:
    pick = members[0]
    titles = list(members)
    strips, only = [], {}
    ref = sorted(pick.segments)
    ascending = [t for t in titles if t.segments == sorted(t.segments)]
    if len(ascending) == 1:
        pick = ascending[0]
        titles = [pick] + [t for t in titles if t is not pick]
        pick_why = (f"{pick.source or 'It'} is the only title that plays its pieces in the order they're stored "
                    f"on the disc. Watch the start of it to be sure.")
    else:
        pick_why = (f"Disc Shelf can't tell which of these plays the film in order. MakeMKV lists "
                    f"{pick.source or 'this one'} first; watch a few minutes of it before ripping.")
    for t in titles:
        marks = [(s, s != pick.segments[i] if i < len(pick.segments) else True) for i, s in enumerate(t.segments)]
        n = sum(1 for _s, m in marks if m)
        strips.append(Strip(t, marks, "In order" if t is pick and len(ascending) == 1 else
                            ("The pick" if t is pick else f"{n} piece{'' if n == 1 else 's'} in a different place")))
        if t is not pick:
            only[t.index] = "Different order"
    g = Group("order", titles, f"{_what(titles)}, {_times(len(titles))}: the same pieces in different orders",
              f"All {len(titles)} titles are {_clock(pick.duration_s)} and play the same {len(ref)} pieces, each in "
              f"its own order. Some studios add these so copy programs pick the wrong one.", pick_why)
    g.same = [f"Length {_clock(pick.duration_s)}", f"The same {len(ref)} pieces"]
    g.strips, g.only = strips, only
    return g
