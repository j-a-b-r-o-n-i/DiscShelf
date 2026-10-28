"""Fixing two files in storage that hold the same episode, from the Match page.

This is the case a season check finds after the fact: a disc copied before duplicates were
checked left "Show - S01E20.mkv" and "Show - S01E20 (2).mkv" side by side. One of them (the
odd one: a dub, a shorter cut, the " (2)" copy) is renamed or deleted, and the job that copied it is told,
so its records match the files and the season check stops flagging it.

plan() only works out names. The renames themselves go through library.apply(), which moves
sidecars (subtitles, .nfo) along, never overwrites a file, and writes an undo log;
library.undo() puts them back. The job records are changed and restored by apply_records()
and restore_records(); the controller saves the jobs.
"""
from __future__ import annotations

import copy as _copy
import os
import posixpath
from dataclasses import dataclass, field

from . import mediaserver, naming
from .library import JFIGNORE, PLEXIGNORE, SET_ASIDE
from .models import Job, Match
from .season import SHORT_CUT_S, Holder, Problem, lang_name

FIXES = ("version", "extra", "aside", "delete", "swap")


@dataclass
class Change:
    """What one job's record of one title becomes."""
    job_id: str
    title_index: int
    dest_rel: str
    target: str = "episode"      # "episode" | "extra" | "skip"
    version: str = ""
    extra_name: str = ""
    note: str = ""


@dataclass
class Plan:
    fix: str                     # one of FIXES
    moves: list[tuple[str, str]]             # (old, new) under the destination, videos only
    changes: list[Change]                    # job records to update (none for a file no job copied)
    extra_files: list[tuple[str, str]] = field(default_factory=list)   # e.g. Set aside/.plexignore
    deletes: list[str] = field(default_factory=list)   # videos to delete, for the delete fix (no undo)
    scan_folder: str = ""        # the season folder, for the media server to rescan
    result: str = ""             # one sentence once it's done


@dataclass
class Done:
    """An applied fix, for Undo."""
    plan: Plan
    log: str                     # library.apply's undo log, "" for a delete
    before: list[tuple[str, int, Match | None, str]]   # (job id, title, its match before, its dest_rel before)
    text: str


def odd_one(sides: list[Holder]) -> tuple[Holder, Holder]:
    """(the file to rename, the one that keeps the plain name): a dub (no audio in its disc's main
    language while the other has it) first, then the shorter, then a " (2)" copy, then the later title."""
    def score(h: Holder):
        dub = bool(h.main and h.audio and h.main not in h.audio)
        copy = posixpath.splitext(h.dest_rel)[0].endswith(")")
        longest = max((o.length_s for o in sides), default=0)
        short = bool(h.length_s and longest - h.length_s > SHORT_CUT_S)
        return (dub, short, copy, h.title_index or 0)
    ranked = sorted(sides, key=score, reverse=True)
    return ranked[0], ranked[1]


def dub_language(odd: Holder, kept: Holder) -> str:
    if odd.audio and odd.main and odd.main not in odd.audio and kept.main in kept.audio:
        return lang_name(odd.audio[0])
    return ""


def version_label(odd: Holder, kept: Holder) -> str:
    dub = dub_language(odd, kept)
    if dub:
        return dub
    if odd.length_s and kept.length_s and kept.length_s - odd.length_s > SHORT_CUT_S:
        return "Short cut"
    return "Version 2"


def describe(h: Holder) -> str:
    """"Disc 1, First Half, title 26", "This disc's title 26", "The file … (no job copied it)"."""
    if h.where == "here":
        return f"Title {h.title_index}"
    if h.where == "disc":
        return f"{h.disc[:1].upper()}{h.disc[1:]}{',' if ',' in h.disc else ''} title {h.title_index}"
    return f"“{h.name}”"


def options(problem: Problem) -> list[tuple[str, str, str, bool]]:
    """(fix, label, what happens, suggested) for each fix, the suggested one first."""
    odd, kept = odd_one(problem.sides)
    who = describe(odd)
    who_l = who[:1].lower() + who[1:] if odd.where != "library" else who
    dub = dub_language(odd, kept)
    label = version_label(odd, kept)
    name = problem.name or problem.code
    first = f"Keep {who_l} as the {dub} version" if dub else f"Keep {who_l} as a second version"
    out = [("version", first, f"It's renamed with “ - {label}” at the end, so media servers list it as a second "
                               f"version of {name}.", True),
           ("extra", f"Move {who_l} to Featurettes", f"It's moved to Featurettes as “{extra_name(name, odd, kept)}”. "
                                                     "Media servers list it with the show's extras.", False),
           ("aside", f"Set {who_l} aside", f"It's moved into “{SET_ASIDE}” inside the show's folder. "
                                           "Nothing is deleted.", False),
           ("delete", f"Delete {who_l}", "It's deleted from storage, with its subtitles. This can't be undone.",
            False)]
    if kept.dest_rel and odd.dest_rel:
        other = describe(kept)
        other_l = other[:1].lower() + other[1:] if kept.where != "library" else other
        swap_label = lang_name(kept.audio[0]) if dub and kept.audio else label
        out.append(("swap", f"{who} is the real {name}",
                    f"The two swap names, and {other_l} becomes the {swap_label} version. Pick this if you want "
                    f"{who_l} to be the one that plays first.", False))
    return out


def extra_name(name: str, odd: Holder, kept: Holder) -> str:
    dub = dub_language(odd, kept)
    if dub:
        return f"{name} ({dub})"
    label = version_label(odd, kept)
    return f"{name} (short cut)" if label == "Short cut" else f"{name} (another version)"


def _episode_name(problem: Problem, h: Holder, version: str, scheme: naming.Scheme) -> str:
    """The file's new path: the scheme's name for the episode with this version, in the folder it's in now."""
    folder = h.dest_rel.split("/", 1)[0]
    ext = posixpath.splitext(h.dest_rel)[1].lstrip(".") or "mkv"
    built = naming.episode_relpath(folder, None, problem.season, problem.numbers[0], None, version, ext,
                                   problem.name, "", "", scheme, show_dir=folder)
    return posixpath.join(posixpath.dirname(h.dest_rel), posixpath.basename(built))


def plan(problem: Problem, fix: str, scheme: naming.Scheme | None = None) -> Plan:
    """What the fix renames and which job records change. Raises ValueError for an unknown fix."""
    scheme = scheme or naming.DEFAULT
    odd, kept = odd_one(problem.sides)
    folder = odd.dest_rel.split("/", 1)[0]
    season_dir = posixpath.dirname(kept.dest_rel or odd.dest_rel)
    label = version_label(odd, kept)
    name = problem.name or problem.code
    who = describe(odd)

    def change(h: Holder, dest: str, **kw) -> list[Change]:
        return [Change(h.job_id, h.title_index, dest, **kw)] if h.job_id and h.title_index is not None else []

    if fix == "version":
        dst = _episode_name(problem, odd, label, scheme)
        return Plan(fix, [(odd.dest_rel, dst)], change(odd, dst, version=label), scan_folder=season_dir,
                    result=f"{who} is now the {label} version: “{posixpath.basename(dst)}”.")
    if fix == "extra":
        ext = posixpath.splitext(odd.dest_rel)[1].lstrip(".") or "mkv"
        dst = naming.extra_relpath(folder, extra_name(name, odd, kept), "Featurettes", ext, scheme)
        return Plan(fix, [(odd.dest_rel, dst)], change(odd, dst, target="extra", extra_name=extra_name(name, odd, kept)),
                    scan_folder=folder, result=f"{who} is now an extra: “Featurettes/{posixpath.basename(dst)}”.")
    if fix == "aside":
        dst = f"{folder}/{SET_ASIDE}/{posixpath.basename(odd.dest_rel)}"
        return Plan(fix, [(odd.dest_rel, dst)],
                    change(odd, dst, target="skip", note=f"Moved to the “{SET_ASIDE}” folder."),
                    extra_files=[(f"{folder}/{SET_ASIDE}/{n}", text) for n, text in (PLEXIGNORE, JFIGNORE)],
                    scan_folder=season_dir,
                    result=f"{who} is set aside in “{folder}/{SET_ASIDE}”.")
    if fix == "delete":
        return Plan(fix, [], change(odd, "", target="skip", note="Deleted from storage."), deletes=[odd.dest_rel],
                    scan_folder=season_dir, result=f"{who} is deleted from storage: “{posixpath.basename(odd.dest_rel)}”.")
    if fix == "swap":
        dub = dub_language(odd, kept)
        other_label = lang_name(kept.audio[0]) if dub and kept.audio else label
        plain = _episode_name(problem, odd, "", scheme)
        plain = posixpath.join(posixpath.dirname(kept.dest_rel), posixpath.basename(plain))
        second = _episode_name(problem, kept, other_label, scheme)
        return Plan(fix, [(odd.dest_rel, plain), (kept.dest_rel, second)],
                    change(odd, plain, version="") + change(kept, second, version=other_label), scan_folder=season_dir,
                    result=f"Swapped: {who[:1].lower() + who[1:] if odd.where != 'library' else who} is now "
                           f"“{posixpath.basename(plain)}”, and {describe(kept)[:1].lower() + describe(kept)[1:]} is "
                           f"the {other_label} version.")
    raise ValueError(f"Unknown fix {fix!r}.")


def with_sidecars(moves: list[tuple[str, str]], listing: dict[str, list[str]]) -> list[tuple[str, str]]:
    """The moves plus each video's sidecars (subtitles, .nfo, -thumb.jpg) found in listing,
    which maps a folder (under the destination) to its file names."""
    from .library import _SIDECAR_REST
    out = []
    for src, dst in moves:
        out.append((src, dst))
        d, name = posixpath.split(src)
        stem, new_stem = posixpath.splitext(src)[0], posixpath.splitext(dst)[0]
        base = posixpath.splitext(name)[0]
        for other in listing.get(d, []):
            if other != name and other.startswith(base) and _SIDECAR_REST.match(other[len(base):]):
                rel = posixpath.join(d, other)
                out.append((rel, new_stem + rel[len(stem):]))
    return out


def apply_records(jobs: dict[str, Job], p: Plan) -> list[tuple[str, int, Match | None, str]]:
    """Change the jobs' records as the plan says; returns what they were, for restore_records."""
    before = []
    for c in p.changes:
        job = jobs.get(c.job_id)
        if job is None:
            continue
        m = next((m for m in job.matches if m.title_index == c.title_index), None)
        f = next((f for f in job.files if f.title_index == c.title_index), None)
        before.append((c.job_id, c.title_index, _copy.deepcopy(m), f.dest_rel if f else ""))
        if f is not None:
            f.dest_rel = c.dest_rel
        if m is None:
            continue
        m.held, m.held_was, m.confirmed = "", "", True
        if c.target == "episode":
            m.version = c.version
        elif c.target == "extra":
            m.target, m.episode, m.episode_end, m.version = "extra", None, None, ""
            m.extra_name, m.extra_folder = c.extra_name, "Featurettes"
        else:
            m.target, m.version = "skip", ""
        m.source, m.confidence = "manual", "manual"
        if c.note:
            m.note = c.note
    return before


def restore_records(jobs: dict[str, Job], before: list[tuple[str, int, Match | None, str]]) -> None:
    for job_id, ti, m, rel in reversed(before):
        job = jobs.get(job_id)
        if job is None:
            continue
        f = next((f for f in job.files if f.title_index == ti), None)
        if f is not None:
            f.dest_rel = rel
        if m is not None:
            job.matches = [_copy.deepcopy(m) if x.title_index == ti else x for x in job.matches]


def delete_files(root: str, rels: list[str]) -> list[str]:
    """Delete each video and its sidecars under root; returns what was deleted. Raises
    library.RenameError, before deleting anything, when a video isn't there."""
    from .library import RenameError
    for rel in rels:
        if not os.path.isfile(os.path.join(root, rel)):
            raise RenameError(f"“{rel}” isn't there any more. Check the show again.")
    pairs = with_sidecars([(rel, rel) for rel in rels], listing_for(root, [(rel, rel) for rel in rels]))
    gone = []
    for rel, _same in pairs:
        os.remove(os.path.join(root, rel))
        gone.append(rel)
    return gone


def listing_for(root: str, moves: list[tuple[str, str]]) -> dict[str, list[str]]:
    """The file names in each folder the moves come from. One directory listing each."""
    out: dict[str, list[str]] = {}
    for src, _dst in moves:
        d = posixpath.dirname(src)
        if d not in out:
            try:
                out[d] = sorted(os.listdir(os.path.join(root, d)))
            except OSError:
                out[d] = []
    return out


class NasFixes:
    """The controllers' side: rename files in storage and update the jobs. Each controller supplies
    cfg, job_lock, jobs(), _save(job), and optionally library_path(dest), library_undo_dir()
    and server_scan_folder(library, rel)."""

    def fix_root(self, job: Job) -> str:
        """Where the job's destination is on this computer."""
        fn = getattr(self, "library_path", None)
        if fn is not None:
            d = next((d for d in self.cfg.destinations if d.path == job.destination), None) or \
                self.cfg.destination(job.media_kind)
            if d is not None:
                return fn(d)
        return job.destination

    def fix_undo_dir(self) -> str:
        """Its own folder, so the Library page's Undo last rename never undoes a fix without its records."""
        from . import config
        fn = getattr(self, "library_undo_dir", None)
        base = fn() if fn else os.path.join(config.state_dir(), "library-undo")
        return os.path.join(base, "match")

    def _fix_jobs(self, job: Job, p: Plan) -> dict[str, Job]:
        want = {c.job_id for c in p.changes} - {job.id}
        out = {job.id: job}
        if want:
            for j in self.jobs():
                if j.id in want:
                    out[j.id] = j
        return out

    def _plex_rescan(self, job: Job, folder: str) -> str:
        fn = getattr(self, "server_scan_folder", None)
        name = mediaserver.name(self.cfg)
        if fn is None or not name or not job.library or not folder:
            return ""
        try:
            fn(job.library, folder)
        except Exception as exc:  # noqa: BLE001 -- the renames are done either way
            return f" {name} couldn't be asked to rescan: {exc}"
        return f" {name} was asked to rescan {posixpath.basename(folder)}."

    def _after_fix(self, job: Job, jobs: dict[str, Job]) -> None:
        """Save the changed jobs, keep the copy-locked matches in step, and look at the season again."""
        frozen = getattr(self, "_frozen", None)
        for j in jobs.values():
            if frozen is not None and j.id in frozen:
                for m in j.matches:
                    if m.title_index in frozen[j.id]:
                        frozen[j.id][m.title_index] = _copy.deepcopy(m)
            self._save(j)
        getattr(self, "_season_others", {}).pop(job.id, None)

    def apply_nas_fix(self, job: Job, p: Plan, rescan: bool = True) -> Done:
        """Rename the files (with their sidecars) and update the jobs that copied them. Raises
        library.RenameError when a file is missing or a new name is taken; nothing changes then."""
        from . import library
        root = self.fix_root(job)
        if p.deletes:
            log = ""
            delete_files(root, p.deletes)
        else:
            moves = with_sidecars(p.moves, listing_for(root, p.moves))
            log = library.apply(root, moves, self.fix_undo_dir(), p.result, p.extra_files,
                                scan_folders=[p.scan_folder])
        jobs = self._fix_jobs(job, p)
        with self.job_lock:
            before = apply_records(jobs, p)
            self._after_fix(job, jobs)
        text = p.result + (self._plex_rescan(job, p.scan_folder) if rescan else "")
        return Done(p, log, before, text)

    def undo_nas_fix(self, job: Job, done: Done, rescan: bool = True) -> str:
        """Put the names and the job records back. Returns a sentence."""
        from . import library
        n, skipped = library.undo(done.log)
        jobs = self._fix_jobs(job, done.plan)
        with self.job_lock:
            restore_records(jobs, done.before)
            self._after_fix(job, jobs)
        text = f"Put back the old name{'s' if n != 1 else ''} of {n} file{'s' if n != 1 else ''}."
        if skipped:
            text += f" {skipped[0]}"
        return text + (self._plex_rescan(job, done.plan.scan_folder) if rescan else "")
