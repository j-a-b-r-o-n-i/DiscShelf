#!/usr/bin/env python3
"""
Tests for a disc that holds more than one film, such as a holiday disc with two TV specials:
a title matched to another film is named from its match and gets its own folder, the disc's
extras stay with its first film, the names typed off the case go to the titles their running
times on TheDiscDB fit, title cards name the specials with no typing when the running times
agree, a job saved before the film fields existed still loads, and a
second film's year is looked up on TheDiscDB by its title.

Run:  python3 discshelf/test_specials.py    (also works under pytest)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import discdb, naming, pipeline  # noqa: E402
from discshelf.config import Config, Destination  # noqa: E402
from discshelf.jobs import JobStore  # noqa: E402
from discshelf.models import Job, Match, Title, job_from_dict, to_dict  # noqa: E402

# The box set TheDiscDB has: not this disc, but it gives each special's running time.
BOX = [{"title": "Ultimate Collection", "discs": [{"index": 1, "name": "D1", "format": "Blu-ray", "titles": [
    {"index": 0, "sourceFile": "00003.mpls", "duration": "0:25:18", "size": 1, "itemType": "MainMovie",
     "item": {"title": "It's the Great Pumpkin, Charlie Brown", "type": "MainMovie"}},
    {"index": 1, "sourceFile": "00004.mpls", "duration": "0:24:24", "size": 1, "itemType": "MainMovie",
     "item": {"title": "It's Magic, Charlie Brown", "type": "MainMovie"}},
]}]}]
DISCDB = json.dumps({"data": {"mediaItems": {"nodes": [
    {"title": "It's the Great Pumpkin, Charlie Brown", "year": 1966, "type": "Movie", "releases": BOX},
    {"title": "It's Magic, Charlie Brown", "year": 1981, "type": "Movie", "releases": BOX},
    {"title": "You're Not Elected, Charlie Brown", "year": 1972, "type": "Movie", "releases": []},
    {"title": "Peanuts", "year": 2014, "type": "Series", "releases": []},
]}}}).encode()


class FakeTvMaze:
    def search(self, q):
        return []

    def episodes(self, show_id, season):
        return []


def holiday_job(nas):
    titles = [Title(index=0, source="00071.mpls", duration_s=1516, kind="episode", selected=True),
              Title(index=2, source="00072.mpls", duration_s=1459, kind="episode", selected=True),
              Title(index=4, source="00026.m2ts", duration_s=838, kind="extra", selected=True)]
    matches = [Match(0, "feature", confidence="manual"),
               Match(2, "feature", confidence="manual", film="You're Not Elected, Charlie Brown", film_year=1972),
               Match(4, "extra", extra_name="Making Of", extra_folder="Featurettes", confidence="manual")]
    return Job(id="holiday", media_kind="movie", show="It's the Great Pumpkin, Charlie Brown", year=1966,
               destination=nas, titles=titles, matches=matches)


def test_specials():
    with tempfile.TemporaryDirectory() as root:
        nas = os.path.join(root, "Movies")
        os.makedirs(nas)
        cfg = Config(destinations=[Destination("movie", "Movies", nas, "", "movie")],
                     rip_dir=os.path.join(root, "rips"), read_title_cards=False)
        ctl = pipeline.Controller(cfg, JobStore(os.path.join(root, "jobs")), tvmaze=FakeTvMaze(),
                                  discdb_client=discdb.TheDiscDb(fetch=lambda _u, _b: DISCDB))
        job = holiday_job(nas)
        got = dict(ctl.preview_paths(job))
        assert got == {
            0: "It's the Great Pumpkin, Charlie Brown (1966)/It's the Great Pumpkin, Charlie Brown (1966).mkv",
            2: "You're Not Elected, Charlie Brown (1972)/You're Not Elected, Charlie Brown (1972).mkv",
            4: "It's the Great Pumpkin, Charlie Brown (1966)/Featurettes/Making Of.mkv",
        }, got

        # A disc's label stands for the one film whose title has all its words.
        assert pipeline._film_words("CHARLIE_BROWN_XMAS") == pipeline._film_words("A Charlie Brown Christmas")
        assert ctl.find_film("ITS THE GREAT PUMPKIN") == ("It's the Great Pumpkin, Charlie Brown", 1966)
        assert ctl.find_film("Charlie Brown") is None            # two films have those words
        assert ctl.find_film("Peanuts") is None                  # a series, not a film

        # A year left empty stays out of the name; TheDiscDB fills it in by the film's title.
        assert ctl.film_year("You're not elected - Charlie Brown") == 1972
        assert ctl.film_year("Peanuts") is None              # a series, not a film
        assert ctl.film_year("Not On TheDiscDB") is None
        job.matches[1].film_year = None
        assert dict(ctl.preview_paths(job))[2] == \
            "You're Not Elected, Charlie Brown/You're Not Elected, Charlie Brown.mkv"

        # The list from the case, bonus special first: each special still goes to its own title.
        job = holiday_job(nas)
        job.matches = [Match(0, "feature", confidence="check"), Match(2, "extra", extra_name="Title 02"),
                       Match(4, "extra", extra_name="Making Of", confidence="manual")]
        ctl.match_films_from_list(job, "Bonus Episode: It's Magic, Charlie Brown\n"
                                       "It's the Great Pumpkin, Charlie Brown")
        got = [(m.title_index, m.target, m.film, m.film_year, m.confidence, m.source) for m in job.matches]
        assert got == [(0, "feature", "", None, "close", "list"),
                       (2, "feature", "It's Magic, Charlie Brown", 1981, "close", "list"),
                       (4, "extra", "", None, "manual", "")], got
        assert dict(ctl.preview_paths(job))[2] == "It's Magic, Charlie Brown (1981)/It's Magic, Charlie Brown (1981).mkv"

        # Title cards name the specials. "Charlie Brown" is on both cards and in many specials'
        # names, so a reading counts because each title runs as long as TheDiscDB's record of it.
        from discshelf.titlecard import CardLine, TitleReading
        ctl.cfg.read_title_cards = True
        job = holiday_job(nas)
        job.matches = [Match(0, "feature", confidence="check"), Match(2, "extra", extra_name="Title 02", confidence="check"),
                       Match(4, "extra", extra_name="Making Of", confidence="manual")]
        assert ctl.cards_possible(job)
        assert sorted(ctl.film_choices(job)) == ["It's Magic, Charlie Brown", "It's the Great Pumpkin, Charlie Brown"]

        def card(*texts):
            return TitleReading(frame_h=480, frames=30, lines=[CardLine(tx, 0.9, 135.0, 200, 100 + 40 * i, 400, 36)
                                                               for i, tx in enumerate(texts)])

        ctl._cards[job.id] = {0: card("IT'S", "THE GREAT", "PUMPKIN", "CHARLIE RROWN"),
                              2: card("IT'S MAGIC", "CHARLIE BROWN")}
        assert ctl.apply_cards(job) == 2
        got = [(m.title_index, m.target, m.film, m.film_year, m.source) for m in job.matches]
        assert got == [(0, "feature", "", None, "card"), (2, "feature", "It's Magic, Charlie Brown", 1981, "card"),
                       (4, "extra", "", None, "")], got

        # A title whose running time doesn't fit the film its card names is flagged for a check.
        job = holiday_job(nas)
        job.titles[1].duration_s = 1800
        job.matches = [Match(0, "feature", confidence="check"), Match(2, "extra", extra_name="Title 02", confidence="check")]
        ctl._cards[job.id] = {0: card("IT'S", "THE GREAT", "PUMPKIN", "CHARLIE RROWN"),
                              2: card("IT'S MAGIC", "CHARLIE BROWN")}
        ctl.apply_cards(job)
        m = job.matches[1]
        assert (m.target, m.confidence) == ("feature", "check") and "30 min" in m.note, m

        # A disc with one film has no cards to read.
        one = holiday_job(nas)
        one.titles[1].kind = "feature"
        assert not ctl.cards_possible(one)

    # The film fields survive a save, and a job saved before they existed loads with none.
    back = job_from_dict(json.loads(json.dumps(to_dict(holiday_job("/x")))))
    assert (back.matches[1].film, back.matches[1].film_year) == ("You're Not Elected, Charlie Brown", 1972)
    old = to_dict(holiday_job("/x"))
    for m in old["matches"]:
        m.pop("film"), m.pop("film_year")
    back = job_from_dict(old)
    assert all(m.film == "" and m.film_year is None for m in back.matches)
    assert naming.relpath_for(back, back.matches[1], "movie") == \
        "It's the Great Pumpkin, Charlie Brown (1966)/It's the Great Pumpkin, Charlie Brown (1966).mkv"


if __name__ == "__main__":
    test_specials()
    print("specials: ok")
