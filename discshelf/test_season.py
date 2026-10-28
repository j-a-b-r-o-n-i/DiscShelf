#!/usr/bin/env python3
"""
Tests for the season checks (season.py) and how the controller uses them.

What matters here: two titles of one disc matched to the same episode are both held back
("check", with a note naming the other title) whatever made the match, even a "close" one,
so neither is copied as the title is ripped; a two-part title overlapping another clashes; a
title whose episode is already on another disc of the season, or already a file in the
season folder, is held too; a re-rip of a disc replaces its earlier job instead of clashing;
a title kept as a second version doesn't clash and its file gets " - Short cut"; settling a
problem gives back the earlier confidence and note; and the three fixes do what they say.
Discs without a number are numbered by rip order and named by their label. A match that needs
a check becomes its own problem. Two files already on the NAS for one episode are found from
another disc or from their own disc, the French-only one is renamed as the French version (with
its subtitles) through library.apply, the job that copied it is updated and saved, and Undo
puts back both the files and the record.

The jobs are made up here, a two-disc season of 7-minute episodes; nothing reads real job
files or a real share. The controller test uses a temporary job folder and a fake TVmaze.

Run:  python3 discshelf/test_season.py    (also works under pytest)
"""
import copy
import os
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from discshelf import discdb, library, nasfix, naming, pipeline, season  # noqa: E402
from discshelf.config import Config, Destination  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import Episode, Job, Match, RippedFile, Stream, Title, TitleProgress  # noqa: E402

NO_DISCDB = '{"data": {"mediaItems": {"nodes": []}}}'.encode()
NAMES = ["Kites", "Puddles", "The Shed", "Lanterns", "Ferry", "Marbles", "Postbox", "Rockpool", "Tadpoles", "Bunting"]
EPS = {n: Episode(1, n, NAMES[n - 1], 7) for n in range(1, 11)}


class TvMaze:
    def search(self, q):
        return []

    def episodes(self, show_id, season):
        return [copy.deepcopy(e) for e in EPS.values() if season in (None, 1)]


def disc(job_id, label, number, eps, lengths=None, state="ripped", created="2026-01-01T10:00:00", dest="/nas/Shows"):
    """A ripped disc whose title i (from 1) is matched to episode eps[i-1] (None: skipped)."""
    lengths = lengths or {}
    job = Job(id=job_id, disc_label=label, media_kind="tv", show="Kites and Things", show_id=77, year=2019,
              season=1, disc_number=number, destination=dest, created=created, stage="match")
    for i, n in enumerate(eps, start=1):
        secs = lengths.get(i, 441)
        job.titles.append(Title(i, f"{i:05d}.m2ts", secs, secs * 1000, 3, [i], kind="episode", selected=True))
        job.files.append(RippedFile(i, f"/rips/{label}/t{i:02d}.mkv", secs * 1000, state=state,
                                    dest_rel=f"Kites and Things (2019)/Season 01/Kites and Things (2019) - S01E{n:02d}.mkv"
                                    if n and state == "verified" else ""))
        job.titles_progress.append(TitleProgress(i, "verified" if state == "verified" else "ripped"))
        if n:
            job.matches.append(Match(i, "episode", copy.deepcopy(EPS[n]), confidence="close", source="discdb"))
        else:
            job.matches.append(Match(i, "skip"))
    return job


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    by = lambda job, ti: next(m for m in job.matches if m.title_index == ti)  # noqa: E731

    # --- one disc: two titles on one episode -----------------------------------
    d1 = disc("d1", "KITES_S1_D1", 1, [1, 2, 3, 4, 5, 3], lengths={6: 431})
    mine = season.title_holders(d1)
    probs = season.find_problems(mine, [], {(1, 3): "The Shed"})
    check(len(probs) == 1 and probs[0].titles == [3, 6], f"duplicate on one disc: {[(p.titles, p.text) for p in probs]}")
    check(probs and probs[0].text == "Titles 3 and 6 are both S01E03 “The Shed”.", f"problem text: {probs and probs[0].text}")
    notes = season.held_notes(probs)
    check(notes.get(6) == "Title 3 is also S01E03 “The Shed”.", f"title 6's note: {notes.get(6)!r}")
    check(notes.get(3) == "Title 6 is also S01E03 “The Shed”.", f"title 3's note: {notes.get(3)!r}")
    by(d1, 6).note = "TheDiscDB matched it by length only."
    check(season.apply_holds(d1.matches, notes), "holding changed nothing")
    m3, m6 = by(d1, 3), by(d1, 6)
    check(m3.confidence == "check" and not m3.ready and m6.confidence == "check" and not m6.ready,
          "a close duplicate is still ready to copy")
    check(m6.note == "Title 3 is also S01E03 “The Shed”. TheDiscDB matched it by length only.", f"held note: {m6.note!r}")
    check(by(d1, 1).ready, "a title with its own episode was held")
    m3.confirmed = True
    check(not m3.ready, "confirming a held duplicate made it ready")
    check(not season.apply_holds(d1.matches, notes), "holding again changed something")
    # Settled: title 6 becomes episode 6, so both go back to what they were.
    m6.episode = copy.deepcopy(EPS[6])
    probs = season.find_problems(season.title_holders(d1), [])
    season.apply_holds(d1.matches, season.held_notes(probs))
    check(not probs and m3.confidence == "close" and m3.note == "" and m3.ready, f"title 3 after: {m3}")
    check(m6.confidence == "close" and m6.note == "TheDiscDB matched it by length only." and not m6.held,
          f"title 6 after: {m6.confidence} {m6.note!r}")
    # Locked (already copied) titles are never held, but the problem is still reported.
    d1b = disc("d1b", "KITES_S1_D1", 1, [1, 1])
    probs = season.find_problems(season.title_holders(d1b), [])
    season.apply_holds(d1b.matches, season.held_notes(probs), locked={1})
    check(not by(d1b, 1).held and by(d1b, 2).held and probs and probs[0].titles == [1, 2], "a copied title was held")

    # --- a two-part title overlapping another --------------------------------------
    d2 = disc("dx", "KITES_S1_DX", 9, [7, 8])
    by(d2, 1).episode_end = 8
    probs = season.find_problems(season.title_holders(d2), [], {(1, 8): "Rockpool"})
    check(len(probs) == 1 and probs[0].titles == [1, 2] and probs[0].numbers == [8], f"overlap: {probs}")

    # --- a second version doesn't clash, and its file is named for it ------------
    d3 = disc("d3", "KITES_S1_D3", 3, [9, 9], lengths={2: 430})
    p = season.find_problems(season.title_holders(d3), [], {(1, 9): "Tadpoles"})[0]
    check(season.version_name(d3, 2, p) == "Short cut", f"version for the shorter: {season.version_name(d3, 2, p)}")
    check(season.version_name(d3, 1, p) == "Version 2", f"version for the longer: {season.version_name(d3, 1, p)}")
    d3.matches = season.fix_version(d3.matches, d3, 2, p)
    check(by(d3, 2).version == "Short cut" and by(d3, 2).confirmed, "fix_version didn't set the version")
    check(not season.find_problems(season.title_holders(d3), []), "a second version still clashes")
    rel = naming.relpath_for(d3, by(d3, 2), "tv")
    check(rel.endswith("S01E09 - Tadpoles - Short cut.mkv"), f"second version's file name: {rel}")

    # --- a title dubbed in another language is named for its language, not its length ---
    dd = disc("dd", "KITES_S1_D5", 5, [10, 10], lengths={2: 434})
    dd.audio_langs = ["eng", "fra"]
    dd.titles[0].streams = [Stream(1, "audio", lang="eng"), Stream(2, "audio", lang="eng")]
    dd.titles[1].streams = [Stream(1, "audio", lang="fra"), Stream(2, "audio", lang="fra")]
    p = season.find_problems(season.title_holders(dd), [], {(1, 10): NAMES[9]})[0]
    check(season.version_name(dd, 2, p) == "French", f"dub's version: {season.version_name(dd, 2, p)}")
    check(season.version_name(dd, 1, p) != "English", f"the original was named as a dub: {season.version_name(dd, 1, p)}")
    check(season.extra_name(dd, 2, p) == f"{NAMES[9]} (French)", f"dub's extra name: {season.extra_name(dd, 2, p)}")
    hint = season.compare_hint(dd, p, {})
    check("no English audio, only French" in hint and "dubbed in French" in hint, f"dub hint: {hint}")
    dd.titles[1].streams.append(Stream(3, "audio", lang="eng"))
    check(season.version_name(dd, 2, p) == "Short cut", "a title with the main language too was named as a dub")

    # Skipping the dub settles it: it isn't copied, and the other keeps the episode.
    dd.matches = season.fix_skip(dd.matches, 2)
    check(by(dd, 2).target == "skip" and not by(dd, 2).ready, "skip didn't skip the title")
    check(not season.find_problems(season.title_holders(dd), []), "a skipped title still clashes")

    # --- the fixes ------------------------------------------------------------------
    d4 = disc("d4", "KITES_S1_D4", 4, [10, 10], lengths={2: 434})
    p = season.find_problems(season.title_holders(d4), [], {(1, 10): "Bunting"})[0]
    kept = season.fix_keep(d4.matches, 1, p)
    check(by(SimpleNamespace(matches=kept), 1).confirmed and by(SimpleNamespace(matches=kept), 2).target == "skip",
          "fix_keep: the other title isn't skipped")
    check("Pick this title's episode" in by(SimpleNamespace(matches=kept), 2).note, "fix_keep: no note on the other")
    extra = by(SimpleNamespace(matches=season.fix_extra(d4.matches, d4, 2, p)), 2)
    check(extra.target == "extra" and extra.extra_name == "Bunting (short cut)" and extra.extra_folder == "Featurettes",
          f"fix_extra: {extra.target} {extra.extra_name!r}")
    check(d4.matches[1].target == "episode", "a fix changed the matches it was given")
    cards = {1: SimpleNamespace(state="read", text="BUNTING", sure=True),
             2: SimpleNamespace(state="read", text="Bunting", sure=False)}
    hint = season.compare_hint(d4, p, cards)
    check("Both title cards read" in hint and "Title 2 is 7 s shorter" in hint, f"hint: {hint}")
    check("read their title cards" in season.compare_hint(d4, p, {}), f"hint, unread: {season.compare_hint(d4, p, {})}")

    # --- across discs: another disc, a re-rip, and the library -----------------------
    first = disc("a1", "KITES_S1_D1", 1, [1, 2, 3, 4, 5], state="verified", created="2026-01-01T10:00:00")
    again = disc("a1b", "KITES_S1_D1", 1, [1, 2, 3, 4, 5], created="2026-01-02T10:00:00")      # re-rip of disc 1
    second = disc("a2", "KITES_S1_D2", 2, [6, 7, 5, 8], created="2026-01-03T10:00:00")
    other_show = disc("zz", "OTHER_S1_D1", 1, [6, 7])
    other_show.show_id, other_show.show = 99, "Another Show"
    never = disc("nv", "KITES_S1_D3", 3, [9])
    never.files, never.titles_progress = [], []
    jobs = [first, again, second, other_show, never]
    mates = season.season_jobs(second, jobs)
    check([j.id for j in mates] == ["a1b"], f"season discs for disc 2: {[j.id for j in mates]}")
    check([j.id for j in season.season_jobs(again, jobs)] == ["a2"], "a re-rip counted the disc it replaces")
    others = [h for j in mates for h in season.title_holders(j, "disc")]
    probs = season.find_problems(season.title_holders(second), others, {(1, 5): "Ferry"})
    check(len(probs) == 1 and probs[0].titles == [3], f"cross-disc problem: {probs}")
    check(probs and probs[0].text == "Title 3 is S01E05 “Ferry”, which is already on disc 1 title 5.",
          f"cross-disc text: {probs and probs[0].text}")
    check(season.held_notes(probs).get(3) == "S01E05 “Ferry” is already on disc 1 title 5.",
          f"cross-disc note: {season.held_notes(probs)}")
    # Without a number, a disc is named by its label.
    unnumbered = copy.deepcopy(again)
    unnumbered.disc_number = None
    check(season.title_holders(unnumbered, "disc")[0].phrase() == "the disc “KITES_S1_D1” title 1",
          season.title_holders(unnumbered, "disc")[0].phrase())

    # The library: files no job copied, by episode code; the jobs' own files don't count twice.
    tree = {"/nas/Shows": ["Kites and Things (2019)", "Kites and Things (2007)", "Other"],
            "/nas/Shows/Kites and Things (2019)": ["Season 01", "Season 02", "Featurettes"],
            "/nas/Shows/Kites and Things (2019)/Season 01": [
                "Kites and Things (2019) - S01E01.mkv", "Kites and Things (2019) - S01E08.mkv",
                "Kites and Things (2019) - s01e09-e10 - Tadpoles.mkv", "notes.txt", ".hidden.mkv",
                "Kites and Things (2019) - S01E07 - 2160p.mkv"]}
    rels = season.library_files("/nas/Shows", "Kites and Things", 2019, 1, listdir=lambda d: tree[d],
                                isdir=lambda d: d in tree)
    check(len(rels) == 4 and all(r.startswith("Kites and Things (2019)/Season 01/") for r in rels), f"library: {rels}")
    known = {f.dest_rel for f in first.files}
    lib = season.library_holders(rels, known, 1, "/nas/Shows")
    check(sorted((h.number, h.version) for h in lib) == [(7, "2160p"), (8, ""), (9, "Tadpoles"), (10, "Tadpoles")],
          f"library holders: {[(h.number, h.version) for h in lib]}")
    check(season.parse_episode_file("Show - S02E03-E04.mkv") == (2, 3, 4), "double-episode file name")
    check(season.parse_episode_file("Show - Season Finale.mkv") is None, "a name with no code")
    probs = season.find_problems(season.title_holders(second), lib, {(1, 8): "Rockpool"})
    check(any(p.titles == [4] and p.others[0].where == "library" for p in probs), f"library problem: {probs}")
    check(any(p.text == "Title 4 is S01E08 “Rockpool”, which is already in the library." for p in probs),
          f"library text: {[p.text for p in probs]}")
    # A second version on the NAS (" - 2160p") doesn't clash with a plain copy of episode 7.
    check(not any(7 in p.numbers for p in probs), "a library file with a version clashed")

    # The grid and the picker's taken list.
    st = season.season_status(second, season.title_holders(second), others + lib, list(EPS.values()),
                              season.find_problems(season.title_holders(second), others + lib))
    cells = {c.number: c for c in st.cells}
    check(cells[5].state == "duplicate" and cells[5].mark == "×2", f"cell 5: {cells[5]}")
    check(cells[6].state == "here" and cells[6].mark == "T1", f"cell 6: {cells[6]}")
    check(cells[1].state == "disc" and cells[1].mark == "D1", f"cell 1: {cells[1]}")
    check(cells[9].state == "library" and cells[9].mark == "Lib", f"cell 9: {cells[9]}")
    check(all(c.tip for c in st.cells), "a cell has no tooltip")
    check((None, "Disc 1, title 5") in st.taken[(1, 5)] and (3, "Title 3, this disc") in st.taken[(1, 5)],
          f"taken: {st.taken[(1, 5)]}")
    check("on more than one title" in st.summary, st.summary)

    # --- the controller: matches set by any source are checked, and held ones aren't copied --
    with tempfile.TemporaryDirectory() as root:
        dest = os.path.join(root, "nas")
        os.makedirs(os.path.join(dest, "Kites and Things (2019)", "Season 01"))
        with open(os.path.join(dest, "Kites and Things (2019)", "Season 01", "Kites and Things (2019) - S01E08.mkv"),
                  "w") as fh:
            fh.write("x")
        cfg = Config(rip_dir=os.path.join(root, "rips"), destinations=[Destination("tv", "Shows", dest, "", "tv")])
        store = JobStore(os.path.join(root, "jobs"))
        ctl = pipeline.Controller(cfg, store, tvmaze=TvMaze(),
                                  discdb_client=discdb.TheDiscDb(fetch=lambda _u, _b: NO_DISCDB))
        a = disc("c1", "KITES_S1_D1", 1, [1, 2, 3, 4, 5], state="verified", dest=dest)
        store.save(a)
        b = disc("c2", "KITES_S1_D2", 2, [6, 7, 6, 8], lengths={3: 430}, dest=dest)
        store.save(b)
        st = ctl.season_status(b, refresh=True)
        texts = [p.text for p in st.problems]
        check("Titles 1 and 3 are both S01E06 “Marbles”." in texts, f"controller problems: {texts}")
        check("Title 4 is S01E08 “Rockpool”, which is already in the library." in texts, f"library: {texts}")
        check(st.discs == 2 and st.library_files == 1, f"counted {st.discs} discs, {st.library_files} files")
        check(by(b, 1).held and by(b, 3).held and by(b, 4).held and not by(b, 2).held, "held titles")
        saved = store.load("c2")
        check(saved is not None and by(saved, 3).held, "the hold wasn't saved")
        # A manual pick is checked the moment it's made: title 2 picks episode 7's twin... episode 6.
        new = copy.deepcopy(b.matches)
        by(SimpleNamespace(matches=new), 2).episode = copy.deepcopy(EPS[6])
        by(SimpleNamespace(matches=new), 2).source, by(SimpleNamespace(matches=new), 2).confidence = "manual", "manual"
        ctl.update_matches(b, new)
        check(by(b, 2).held.startswith("Titles 1 and 3 are also"), f"a manual pick wasn't checked: {by(b, 2).held!r}")
        check(by(b, 2).confidence == "check", "a held manual pick isn't marked check")
        # Copy as ripped: a held title waits, a free one goes.
        run_ = SimpleNamespace(card_pending=set())
        check(not pipeline.Controller._ready(run_, 1, by(b, 1)), "copy as ripped would copy a held title")
        tp = TitleProgress(1, "ripped")
        pipeline._mark_waiting(tp, by(b, 1))
        check("It waits until that's settled on the Match page." in tp.note, f"waiting note: {tp.note}")
        # Copying the whole disc refuses while a title is held.
        try:
            ctl.copy(b, lambda f, t: None, __import__("threading").Event())
            check(False, "copy() copied a held title")
        except ValueError as e:
            check("Match page" in str(e), f"copy() error: {e}")
        # The Queue's card says so in a sentence, and the held rows point to the Match page.
        q = ctl.queue([b, a], set())
        item = next(i for i in q.items if i.job.id == "c2")
        check("so they wait. Compare them on the Match page." in item.reason or "wait. Compare them" in item.reason,
              f"queue reason: {item.reason}")
        check(any(r.held for r in item.checks), "no held row on the Queue card")
        # Settling it: title 3 as a second version, title 2 back to episode 7.
        p = next(p for p in ctl.season_status(b).problems if 6 in p.numbers)
        new = season.fix_version(b.matches, b, 3, p)
        by(SimpleNamespace(matches=new), 2).episode = copy.deepcopy(EPS[7])
        ctl.update_matches(b, new)
        check(not by(b, 1).held and not by(b, 3).held and by(b, 1).ready, "settling didn't release the titles")
        check(by(b, 3).version == "Short cut", "the version wasn't kept")
        paths = dict(ctl.preview_paths(b))
        check(paths.get(3, "").endswith("S01E06 - Marbles - Short cut.mkv") and paths.get(1, "").endswith("S01E06 - Marbles.mkv"),
              f"paths: {paths.get(1)} / {paths.get(3)}")

    # --- disc numbers by rip order, and the label in sentences ---------------------------
    one = disc("n1", "Kites and Things S1 First Half", None, [1], created="2026-01-01T10:00:00")
    two = disc("n2", "Kites and Things S1 Second Half", None, [2], created="2026-01-02T10:00:00")
    numbered = disc("n3", "KITES_S1_D3", 3, [3])
    check(season.disc_numbers([two, one, numbered]) == {"n1": 1, "n2": 2, "n3": 3},
          f"disc numbers: {season.disc_numbers([two, one, numbered])}")
    check(season.label_words(one) == "First Half" and season.label_words(numbered) == "",
          f"label words: {season.label_words(one)!r} {season.label_words(numbered)!r}")
    h = season.title_holders(one, "disc", number=1)[0]
    check(h.phrase() == "disc 1, First Half, title 1" and h.disc_no == 1, f"phrase: {h.phrase()}")

    # --- a check match ----------------------------------------------------------------
    d5 = disc("d5", "KITES_S1_D5", 5, [7, 8])
    by(d5, 2).confidence, by(d5, 2).source = "check", "runtime"
    checks = season.find_checks(d5)
    check([(p.key, p.text) for p in checks] == [("check:2", "Title 2 was matched by its running time only")],
          f"checks: {[(p.key, p.text) for p in checks]}")
    check(not season.find_checks(d5, locked={2}), "a copied title was offered for a check")
    by(d5, 2).confirmed = True
    check(not season.find_checks(d5), "a confirmed match is still a check")
    check(season.card_agrees("ROCKPOOL", "Rockpool") and season.card_agrees("R0CKP00L", "Rockpool")
          and not season.card_agrees("TADPOLES", "Rockpool"), "card_agrees")

    # --- two files on the NAS for one episode: rename the French one, update the job, undo ----
    def french(job, ti):
        t = next(t for t in job.titles if t.index == ti)
        t.streams = [Stream(0, "video"), Stream(1, "audio", lang="fra")]
        for o in job.titles:
            if o.index != ti:
                o.streams = [Stream(0, "video"), Stream(1, "audio", lang="eng")]

    with tempfile.TemporaryDirectory() as root:
        dest = os.path.join(root, "nas")
        cfg = Config(rip_dir=os.path.join(root, "rips"),
                     destinations=[Destination("tv", "Shows", dest, "", "tv")])
        store = JobStore(os.path.join(root, "jobs"))
        ctl = pipeline.Controller(cfg, store, tvmaze=TvMaze(),
                                  discdb_client=discdb.TheDiscDb(fetch=lambda _u, _b: NO_DISCDB))
        first = disc("f1", "Kites and Things S1 First Half", None, [1, 2, 3, 2], lengths={4: 431}, state="verified",
                     created="2026-01-01T10:00:00", dest=dest)
        french(first, 4)
        f4 = next(f for f in first.files if f.title_index == 4)
        f4.dest_rel = f4.dest_rel.replace(".mkv", " (2).mkv")
        store.save(first)
        second = disc("f2", "Kites and Things S1 Second Half", None, [4, 5], created="2026-01-02T10:00:00", dest=dest)
        store.save(second)
        sdir = os.path.join(dest, "Kites and Things (2019)", "Season 01")
        os.makedirs(sdir)
        for f in first.files:
            open(os.path.join(dest, f.dest_rel), "w").close()
        open(os.path.join(sdir, "Kites and Things (2019) - S01E02 (2).en.srt"), "w").close()
        st = ctl.season_status(second, refresh=True)
        nas = [p for p in st.problems if p.kind == "nas"]
        check(len(nas) == 1 and nas[0].text == "Two files in storage are S01E02 “Puddles”." and not nas[0].titles,
              f"NAS duplicate from another disc: {[(p.kind, p.text) for p in st.problems]}")
        cell2 = next(c for c in st.cells if c.number == 2)
        check(cell2.state == "duplicate" and cell2.problem == "nas:1:2", f"cell 2: {cell2.state} {cell2.problem}")
        check(next(c for c in st.cells if c.number == 1).mark == "D1", "disc 1 isn't D1 by rip order")
        p = nas[0]
        odd, kept = nasfix.odd_one(p.sides)
        check(odd.title_index == 4 and kept.title_index == 2, f"odd one: {odd.title_index}")
        opts = nasfix.options(p)
        check(opts[0][0] == "version" and opts[0][3] and opts[0][1] == "Keep disc 1, First Half, title 4 as the "
              "French version", f"options: {opts[0]}")
        old = "Kites and Things (2019)/Season 01/Kites and Things (2019) - S01E02 (2).mkv"
        new = "Kites and Things (2019)/Season 01/Kites and Things (2019) - S01E02 - Puddles - French.mkv"
        plan = nasfix.plan(p, "version", naming.Scheme())
        check(plan.moves == [(old, new)], f"plan: {plan.moves}")
        swap = nasfix.plan(p, "swap", naming.Scheme())
        check([d for _s, d in swap.moves] == ["Kites and Things (2019)/Season 01/Kites and Things (2019) - S01E02 - "
                                              "Puddles.mkv", "Kites and Things (2019)/Season 01/Kites and Things "
                                              "(2019) - S01E02 - Puddles - English.mkv"], f"swap: {swap.moves}")
        aside = nasfix.plan(p, "aside", naming.Scheme())
        check(aside.moves[0][1] == "Kites and Things (2019)/Set aside/Kites and Things (2019) - S01E02 (2).mkv"
              and aside.extra_files[0][0].endswith("Set aside/.plexignore"), f"aside: {aside.moves}")
        extra = nasfix.plan(p, "extra", naming.Scheme())
        check(extra.moves[0][1] == "Kites and Things (2019)/Featurettes/Puddles (French).mkv", f"extra: {extra.moves}")

        done = ctl.apply_nas_fix(second, plan, rescan=False)
        names = sorted(os.listdir(sdir))
        check("Kites and Things (2019) - S01E02 - Puddles - French.mkv" in names
              and "Kites and Things (2019) - S01E02 - Puddles - French.en.srt" in names
              and "Kites and Things (2019) - S01E02 (2).mkv" not in names, f"after the rename: {names}")
        check(os.path.isfile(done.log) and os.path.dirname(done.log) == ctl.fix_undo_dir(), "no undo log")
        saved = store.load("f1")
        m4, f4 = by(saved, 4), next(f for f in saved.files if f.title_index == 4)
        check(m4.version == "French" and f4.dest_rel == new, f"disc 1's record: {m4.version!r} {f4.dest_rel}")
        st = ctl.season_status(second, refresh=True)
        check(not [p for p in st.problems if p.kind == "nas"], "the renamed pair is still flagged")
        text = ctl.undo_nas_fix(second, done, rescan=False)
        names = sorted(os.listdir(sdir))
        check("Kites and Things (2019) - S01E02 (2).mkv" in names and
              "Kites and Things (2019) - S01E02 (2).en.srt" in names, f"undo: {text} {names}")
        saved = store.load("f1")
        check(by(saved, 4).version == "" and next(f for f in saved.files if f.title_index == 4).dest_rel == old,
              "undo didn't restore disc 1's record")
        st = ctl.season_status(second, refresh=True)
        check([p.key for p in st.problems if p.kind == "nas"] == ["nas:1:2"], "the pair isn't flagged after undo")

        # The same pair seen from disc 1 itself: its titles are copied, so it's a NAS duplicate there too.
        live = store.load("f1")
        st = ctl.season_status(live, refresh=True)
        check([(p.kind, p.titles) for p in st.problems] == [("nas", [2, 4])], f"from disc 1: {st.problems}")
        p = st.problems[0]
        done = ctl.apply_nas_fix(live, nasfix.plan(p, "extra", naming.Scheme()), rescan=False)
        check(by(live, 4).target == "extra" and by(live, 4).extra_name == "Puddles (French)"
              and next(f for f in live.files if f.title_index == 4).dest_rel.endswith("Featurettes/Puddles (French).mkv"),
              "a locked title on this disc wasn't updated")
        check(os.path.isfile(os.path.join(dest, "Kites and Things (2019)", "Featurettes", "Puddles (French).mkv")),
              "the extra wasn't moved")
        ctl.undo_nas_fix(live, done, rescan=False)
        check(by(live, 4).target == "episode" and os.path.isfile(os.path.join(dest, old)), "undo from disc 1")
        check(library.last_log(ctl.fix_undo_dir()) is None, "an undone log is still offered")

        # Deleting the French file takes its subtitles with it, and disc 1 records the title as skipped.
        delete = nasfix.plan(p, "delete", naming.Scheme())
        check(delete.moves == [] and delete.deletes == [old], f"delete: {delete.deletes}")
        done = ctl.apply_nas_fix(live, delete, rescan=False)
        names = sorted(os.listdir(sdir))
        check(done.log == "" and "Kites and Things (2019) - S01E02 (2).mkv" not in names
              and "Kites and Things (2019) - S01E02 (2).en.srt" not in names
              and "Kites and Things (2019) - S01E02.mkv" in names, f"after the delete: {names}")
        check(by(live, 4).target == "skip" and next(f for f in live.files if f.title_index == 4).dest_rel == "",
              "disc 1's record of the deleted title")
        st = ctl.season_status(live, refresh=True)
        check(not [p for p in st.problems if p.kind == "nas"], "the deleted pair is still flagged")
        try:
            nasfix.delete_files(dest, [old])
            check(False, "deleting a missing file didn't raise")
        except library.RenameError:
            pass

    for f in failures:
        print("FAIL:", f)
    print(f"season: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_season():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
