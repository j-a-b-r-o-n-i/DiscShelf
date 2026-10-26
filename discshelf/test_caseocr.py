#!/usr/bin/env python3
"""
Tests for caseocr.py on the OCR lines both engines read off the back of the Adventure Time
"It Came from the Nightosphere" DVD (fixtures/adventure_time_case_*.json; the photo itself
is not in the repo) and on hand-made cases. What matters: two columns are read left column
first, a name that wraps onto a second line is joined, misread list numbers are taken off
but only decide the order when they are consistent, the logo, ISBN and special feature are
left out, and a name that could be several episodes ("Wizard") stays as printed.

Run:  python3 discshelf/test_caseocr.py    (also works under pytest)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf.caseocr import episode_list_from_lines, episode_list_from_photo, readable_copy, split_number  # noqa: E402
from discshelf.models import Episode  # noqa: E402
from discshelf.ocr import OcrError, OcrLine  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

NIGHTOSPHERE = ["It Came from the Nightosphere", "Rainy Day Daydream", "Wizard", "Power Animal",
                "The Enchiridion!", "Slumber Party Panic", "The Real You", "Memory of a Memory",
                "Prisoners of Love", "Crystals Have Power", "Business Time", "Mystery Train",
                "Guardians of Sunshine", "The Monster", "Hitman", "The Creeps"]


def at_episodes():
    # Every numbered Adventure Time episode on TVmaze (show 290), season, number, name, runtime.
    with open(os.path.join(FIX, "adventure_time_episodes.json")) as f:
        return [Episode(*row) for row in json.load(f)]


def case_lines(engine):
    with open(os.path.join(FIX, f"adventure_time_case_{engine}.json")) as f:
        return [OcrLine(**d) for d in json.load(f)]


def column(texts, x=100, y=100, step=50):
    return [OcrLine(t, 0.9, x, y + i * step, 400, 40) for i, t in enumerate(texts)]


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    check(split_number("I.ITCAMEFROM") == (1, "ITCAMEFROM"), f"I. as 1: {split_number('I.ITCAMEFROM')}")
    check(split_number("IO.CRYSTALSHAVEPOWER") == (10, "CRYSTALSHAVEPOWER"), "IO. as 10")
    check(split_number("2RAINYDAYDAYDREAM") == (2, "RAINYDAYDAYDREAM"), "no separator")
    check(split_number("I4THEMONSTER") == (14, "THEMONSTER"), "I4 as 14")
    check(split_number("[ BUSINESS TIME") == (1, "BUSINESS TIME"), "bracket as 1")
    check(split_number("|6. THE CREEPS") == (16, "THE CREEPS"), "bar as 1")
    check(split_number("Episode 3: Hitman (11 min)") == (3, "Hitman"), split_number("Episode 3: Hitman (11 min)"))
    check(split_number("THENIGHTOSPHERE") == (None, "THENIGHTOSPHERE"), "no number")
    check(split_number("ITCAMEFROM") == (None, "ITCAMEFROM"), "a leading I with no separator is a letter")

    eps = at_episodes()
    for engine in ("rapidocr", "tesseract"):
        got = episode_list_from_lines(case_lines(engine), eps)
        check(got.names == NIGHTOSPHERE, f"{engine} names: {got.names}")
        # 12 read as 2 (and 15 as 5 by tesseract), so the numbers must not reorder anything.
        check(not got.ordered_by_number, f"{engine}: misread numbers left the order alone")
        wiz = got.entries[2]
        check(wiz.episode is None and [e.title for e in wiz.candidates[:2]] == ["Wizard Battle", "Finn the Wizard"],
              f"{engine} Wizard is unclear: {wiz}")
        check(got.entries[0].episode and (got.entries[0].episode.season, got.entries[0].episode.number) == (2, 1)
              and got.entries[0].number == 1, f"{engine} wrapped name: {got.entries[0]}")
        skipped = {s.text for s in got.skipped}
        check({"SPECIALFEATURE", "SPECIAL FEATURE"} & skipped and {"LITTLEDIDYOUKNOW", "LITTLE DID YOU KNOW"} & skipped,
              f"{engine} special feature skipped: {skipped}")
        check("1 could be more than one episode" in got.summary(), f"{engine} summary: {got.summary()}")
    check({"ADVENTURE", "ISBN0-7806-6655-0", "CARTOON"} <= {s.text for s in episode_list_from_lines(
        case_lines("rapidocr"), eps).skipped}, "logo and ISBN skipped")

    # One column, "1. Name (22 min)", printed out of order but numbered consistently: sorted.
    got = episode_list_from_lines(column(["2. Hitman (11 min)", "1. The Creeps (11 min)", "3. Business Time 11:00"]), eps)
    check(got.names == ["The Creeps", "Hitman", "Business Time"] and got.ordered_by_number, f"by number: {got}")

    # "Episode N: Name", with a banner above repeating one of them unnumbered: listed once.
    lines = [OcrLine("FEATURING THE MONSTER", 0.9, 100, 20, 600, 40)] + column(
        ["Episode 1: The Monster", "Episode 2: Mystery Train"])
    got = episode_list_from_lines(lines, eps)
    check(got.names == ["The Monster", "Mystery Train"], f"banner duplicate: {got.names}")
    # "I Am a Sword" keeps its I.
    got = episode_list_from_lines(column(["I Am a Sword", "2. Hitman"]), eps)
    check(got.names == ["I Am a Sword", "Hitman"], f"leading I kept: {got.names}")
    # A numbered name TVmaze doesn't have is reported, not listed.
    got = episode_list_from_lines(column(["1. Hitman", "2. Totally Made Up Thing"]), eps)
    check(got.names == ["Hitman"] and "list number" in got.skipped[0].reason, f"unknown name: {got}")
    check(episode_list_from_lines([], eps).summary() == "No episode names were found in the photo.", "empty")

    # Two columns run together on one line (an iPhone photo of the same case): "6." and "15."
    # are split apart, and Hitman lands in the right-hand column, after The Monster.
    lines = case_lines("rapidocr")
    merged = []
    for l in lines:
        if "SLUMBERPARTYPANIC" in l.text:
            merged.append(OcrLine("6.SLUMBERPARTYPANICI5.HITMAN", l.conf, l.x, l.y, 1227, l.h))
        elif "HITMAN" not in l.text:
            merged.append(l)
    got = episode_list_from_lines(merged, eps)
    check(got.names == NIGHTOSPHERE, f"merged columns: {got.names}")
    check(episode_list_from_lines(column(["1. Slumber Party Panic Special"]), eps).names == ["Slumber Party Panic"],
          "a line with no second number was split")

    # A HEIC photo is converted first; heif-dec failing falls back to ImageMagick; neither is an error.
    with tempfile.TemporaryDirectory() as tmp:
        calls = []

        def conv(cmd, ok):
            calls.append(cmd[0])
            if cmd[0] in ok:
                open(cmd[-1], "w").close()
            return type("P", (), {"returncode": 0 if cmd[0] in ok else 1})()

        check(readable_copy("/x/IMG_1.jpg", tmp, run=lambda c, **k: conv(c, ())) == "/x/IMG_1.jpg", "a JPEG converted")
        out = readable_copy("/x/IMG_3018.HEIC", tmp, run=lambda c, **k: conv(c, ("magick",)))
        check(out == os.path.join(tmp, "IMG_3018.jpg") and calls == ["heif-dec", "magick"], f"HEIC: {out} {calls}")
        calls.clear()
        out = readable_copy("/x/IMG_4.heic", tmp, run=lambda c, **k: conv(c, ("ffmpeg",)))
        check(out == os.path.join(tmp, "IMG_4.jpg") and calls == ["heif-dec", "magick", "ffmpeg"],
              f"HEIC through ffmpeg (inside the Flatpak): {out} {calls}")
        try:
            readable_copy("/x/IMG_2.heic", tmp, run=lambda c, **k: conv(c, ()))
            check(False, "an unreadable HEIC didn't raise")
        except OcrError as e:
            check("HEIC" in str(e), f"HEIC error: {e}")

    # The photo path goes through ocr.read_images with the injected run.
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "5\t1\t1\t1\t1\t1\t100\t100\t40\t30\t90\t1.\n5\t1\t1\t1\t1\t2\t150\t100\t200\t30\t90\tHITMAN\n")

    class Proc:
        stdout, stderr, returncode = tsv, "", 0

    saved = os.environ.get("DISC_SHELF_OCR_VENV")
    os.environ["DISC_SHELF_OCR_VENV"] = "/nonexistent"
    try:
        got = episode_list_from_photo("case.jpg", eps, run=lambda cmd, **kw: Proc())
        check(got.names == ["Hitman"], f"photo path: {got}")
    except OcrError:
        pass  # no tesseract on this machine; test_ocr.py covers that path
    finally:
        if saved is None:
            os.environ.pop("DISC_SHELF_OCR_VENV", None)
        else:
            os.environ["DISC_SHELF_OCR_VENV"] = saved

    for f in failures:
        print("FAIL:", f)
    print(f"caseocr: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_caseocr():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
