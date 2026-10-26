#!/usr/bin/env python3
"""
Tests for discinfo.py: the Disc info report built from a real MakeMKV scan of the
Pitch Perfect Blu-ray, a hand-written 4K UHD stream with HDR markers, and reading the
disc's own title and cover from BDMV/META without the real disc.

Run:  python3 discshelf/test_discinfo.py    (also works under pytest)
"""
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from discshelf import classify, discinfo, makemkv  # noqa: E402


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    with open(os.path.join(HERE, "fixtures", "pitch_perfect_info.txt")) as fh:
        parsed = makemkv.parse_robot(fh.read().splitlines())
    disc = makemkv.build_disc(parsed, 0)
    classify.classify_titles(disc.titles)
    rep = discinfo.build_report(parsed, disc, discinfo.DiscMeta(title="Pitch Perfect"))
    check(rep.title == "Pitch Perfect" and rep.disc_type == "Blu-ray", f"title/type: {rep.title} {rep.disc_type}")
    check(rep.layers.startswith("2 layers"), f"layers: {rep.layers}")
    check("Full HD 1080p" in rep.highlights and "DTS-HD Master Audio" in rep.highlights, f"highlights: {rep.highlights}")
    main = rep.titles[0]
    check(main.video.label == "1080p · AVC · 23.976 fps", f"video label: {main.video.label}")
    check(not any(a.codec == "DTS" and a.lang == "English" for a in main.audio),
          "the DTS core beside the DTS-HD track is listed as its own track")
    check(main.audio[0].channels == "5.1" and main.audio[0].sample_rate == "48 kHz", f"audio: {main.audio[0]}")
    check(rep.audio_languages == ["English", "French"], f"audio languages: {rep.audio_languages}")
    check(rep.libredrive and not any("LibreDrive is off" in n for n in rep.notes), "LibreDrive note")

    # A 4K stream whose MakeMKV description names HDR10 and Dolby Vision.
    uhd = makemkv.parse_robot([
        'CINFO:1,6209,"Blu-ray disc"', 'CINFO:2,0,"Severance"', 'CINFO:32,0,"SEVERANCE_S1_D1"',
        'TINFO:0,9,0,"0:57:04"', 'TINFO:0,11,0,"17600000000"', 'TINFO:0,16,0,"00800.mpls"',
        'TINFO:1,9,0,"0:54:31"', 'TINFO:1,11,0,"16800000000"', 'TINFO:1,16,0,"00801.mpls"',
        'SINFO:0,0,1,6201,"Video"', 'SINFO:0,0,6,0,"MpegH"', 'SINFO:0,0,7,0,"Mpeg-H HEVC Main10@L5.1"',
        'SINFO:0,0,19,0,"3840x2160"', 'SINFO:0,0,21,0,"23.976 (24000/1001)"',
        'SINFO:0,0,30,0,"Mpeg-H HEVC Main10@L5.1 HDR10"',
        'SINFO:0,1,1,6201,"Video"', 'SINFO:0,1,6,0,"MpegH"', 'SINFO:0,1,7,0,"Dolby Vision enhancement layer"',
        'SINFO:0,1,19,0,"1920x1080"',
        'SINFO:0,2,1,6202,"Audio"', 'SINFO:0,2,3,0,"eng"', 'SINFO:0,2,4,0,"English"',
        'SINFO:0,2,7,0,"Dolby TrueHD Atmos"', 'SINFO:0,2,14,0,"8"', 'SINFO:0,2,40,0,"7.1"',
        'SINFO:1,0,1,6201,"Video"', 'SINFO:1,0,6,0,"MpegH"', 'SINFO:1,0,19,0,"3840x2160"',
        'MSG:1011,0,1,"Using LibreDrive mode (v02.1 id=0)","%1","x"',
    ])
    d4 = makemkv.build_disc(uhd, 0)
    classify.classify_titles(d4.titles)
    r4 = discinfo.build_report(uhd, d4)
    check(r4.disc_type == "4K UHD Blu-ray", f"UHD type: {r4.disc_type}")
    check(r4.titles[0].video.hdr == ["Dolby Vision", "HDR10"], f"HDR markers: {r4.titles[0].video.hdr}")
    check(r4.highlights[:3] == ["4K UHD", "Dolby Vision", "HDR10"] and "Dolby Atmos" in r4.highlights,
          f"UHD highlights: {r4.highlights}")
    check(r4.titles[0].audio[0].channels == "7.1", f"Atmos channels: {r4.titles[0].audio[0]}")

    # BDMV/META on a mounted disc: the larger thumbnail is the cover, copied out before unmounting.
    with tempfile.TemporaryDirectory() as root:
        dl = os.path.join(root, "disc", "BDMV", "META", "DL")
        os.makedirs(dl)
        shutil.copy(os.path.join(HERE, "fixtures", "pitch_perfect_bdmt_eng.xml"), os.path.join(dl, "bdmt_eng.xml"))
        with open(os.path.join(dl, "PP_BDJ_Jacket_LRG.jpg"), "wb") as fh:
            fh.write(b"L" * 200)
        with open(os.path.join(dl, "PP_BDJ_Jacket_SML.jpg"), "wb") as fh:
            fh.write(b"S" * 20)
        meta = discinfo.read_meta(os.path.join(root, "disc"))
        check(meta.title == "Pitch Perfect" and meta.cover_path.endswith("LRG.jpg"), f"meta: {meta}")

        mounts = os.path.join(root, "mounts")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd[1])
            if cmd[1] == "mount":
                with open(mounts, "w") as fh:
                    fh.write(f"/dev/sr9 {os.path.join(root, 'disc').replace(' ', chr(92) + '040')} udf ro 0 0\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        orig = discinfo.mount_point
        discinfo.mount_point = lambda dev, mounts_file=mounts: orig(dev, mounts_file)
        try:
            got = discinfo.fetch_meta("/dev/sr9", os.path.join(root, "covers"), "PITCH_PERFECT_NA", run=fake_run)
        finally:
            discinfo.mount_point = orig
        check(calls == ["mount", "unmount"], f"mount calls: {calls}")
        check(got.cover_path == os.path.join(root, "covers", "PITCH_PERFECT_NA.jpg") and os.path.isfile(got.cover_path),
              f"cover not copied out: {got.cover_path}")

    check(discinfo.layers_for(81.7e9).startswith("3 layers") and discinfo.layers_for(0) == "", "layers_for")

    for f in failures:
        print("FAIL:", f)
    print(f"discinfo: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_discinfo():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
