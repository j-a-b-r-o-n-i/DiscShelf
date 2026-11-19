#!/usr/bin/env python3
"""
Tests for a disc that holds more than one film, such as a holiday disc with two TV specials:
a title matched to another film is named from its match and gets its own folder, the disc's
extras stay with its first film, a job saved before the film fields existed still loads, and a
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

DISCDB = json.dumps({"data": {"mediaItems": {"nodes": [
    {"title": "It's the Great Pumpkin, Charlie Brown", "year": 1966, "type": "Movie", "releases": []},
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

        # A year left empty stays out of the name; TheDiscDB fills it in by the film's title.
        assert ctl.film_year("You're not elected - Charlie Brown") == 1972
        assert ctl.film_year("Peanuts") is None              # a series, not a film
        assert ctl.film_year("Not On TheDiscDB") is None
        job.matches[1].film_year = None
        assert dict(ctl.preview_paths(job))[2] == \
            "You're Not Elected, Charlie Brown/You're Not Elected, Charlie Brown.mkv"

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
