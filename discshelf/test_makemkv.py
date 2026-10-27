#!/usr/bin/env python3
"""
Tests for makemkv.py. What matters: real makemkvcon 2.0 output parses (the drive listing
and a Pitch Perfect Blu-ray scan in fixtures/), quoted values with commas and escaped
quotes survive, empty drive slots are dropped, UHD and LibreDrive are spotted, a failed
scan raises with MakeMKV's own message, and a rip reports progress across all titles,
stops when cancelled and leaves no half-written file behind.

No drive is touched: every makemkvcon call goes to a fake run/popen.

Run:  python3 discshelf/test_makemkv.py    (also works under pytest)
"""
import os
import sys
import tempfile
import threading
import time
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import hdr  # noqa: E402
from discshelf import makemkv as mk  # noqa: E402
from discshelf.config import Config  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

# A hand-written 4K TV disc: a play-all, four episodes, a duplicate playlist, extras.
TV_DISC = r'''MSG:1005,0,1,"MakeMKV v2.0.0 linux(x64-release) started","%1 started","MakeMKV v2.0.0 linux(x64-release)"
DRV:0,2,999,12,"BD-RE PIONEER BD-RW  BDR-XD08U 1.01","SHOW_S1_D1","/dev/sr0"
DRV:1,256,999,0,"","",""
MSG:1011,0,1,"Using LibreDrive mode (v02.1 id=000000000000)","%1","Using LibreDrive mode (v02.1 id=000000000000)"
MSG:5011,0,0,"Operation successfully completed","Operation successfully completed"
TCOUNT:7
CINFO:1,6209,"Blu-ray disc"
CINFO:2,0,"Show, Season 1"
CINFO:32,0,"SHOW_S1_D1"
TINFO:0,9,0,"2:56:10"
TINFO:0,11,0,"80000000000"
TINFO:0,16,0,"00800.mpls"
TINFO:0,26,0,"1-4"
TINFO:0,27,0,"Show, Season 1_t00.mkv"
TINFO:1,8,0,"8"
TINFO:1,9,0,"0:44:02"
TINFO:1,11,0,"20000000000"
TINFO:1,16,0,"00801.mpls"
TINFO:1,26,0,"1"
TINFO:2,9,0,"0:43:30"
TINFO:2,16,0,"00802.mpls"
TINFO:2,26,0,"2"
TINFO:3,9,0,"0:46:11"
TINFO:3,16,0,"00803.mpls"
TINFO:3,26,0,"3"
TINFO:4,9,0,"0:42:27"
TINFO:4,16,0,"00804.mpls"
TINFO:4,26,0,"4"
TINFO:5,9,0,"0:44:02"
TINFO:5,16,0,"00901.mpls"
TINFO:5,26,0,"1"
TINFO:6,9,0,"0:21:15"
TINFO:6,16,0,"00010.m2ts"
TINFO:6,26,0,"10,12-13"
TINFO:7,9,0,"0:03:05"
TINFO:7,16,0,"00011.m2ts"
TINFO:7,26,0,"11"
SINFO:1,0,1,6201,"Video"
SINFO:1,0,6,0,"MpegH"
SINFO:1,0,7,0,"MpegH HEVC Main10@L5.1"
SINFO:1,0,19,0,"3840x2160"
SINFO:1,1,1,6202,"Audio"
SINFO:1,1,2,0,"Surround 7.1"
SINFO:1,1,3,0,"eng"
SINFO:1,1,4,0,"English"
SINFO:1,1,6,0,"TrueHD"
SINFO:1,1,14,0,"8"
SINFO:1,1,22,0,"1024"
SINFO:1,1,38,0,"d"
SINFO:1,1,40,0,"7.1"
SINFO:1,2,1,6202,"Audio"
SINFO:1,2,2,0,"Surround 5.1"
SINFO:1,2,3,0,"eng"
SINFO:1,2,6,0,"DD"
SINFO:1,2,22,0,"2304"
SINFO:1,3,1,6202,"Audio"
SINFO:1,3,2,0,"Director\"s Commentary, with cast"
SINFO:1,3,3,0,"eng"
SINFO:1,3,6,0,"DD"
SINFO:1,3,14,0,"2"
SINFO:1,3,22,0,"1"
SINFO:1,4,1,6202,"Audio"
SINFO:1,4,2,0,"Surround 5.1"
SINFO:1,4,3,0,"fra"
SINFO:1,4,6,0,"DD"
SINFO:1,5,1,6203,"Subtitles"
SINFO:1,5,3,0,"eng"
SINFO:1,5,6,0,"PGS"
SINFO:1,5,30,0,"PGS English"
SINFO:1,6,1,6203,"Subtitles"
SINFO:1,6,3,0,"eng"
SINFO:1,6,6,0,"PGS"
SINFO:1,6,22,0,"6144"
SINFO:1,7,1,6203,"Subtitles"
SINFO:1,7,3,0,"spa"
SINFO:1,7,6,0,"PGS"
'''

FAILED_SCAN = '''MSG:1005,0,1,"MakeMKV v2.0.0 linux(x64-release) started","%1 started","MakeMKV v2.0.0 linux(x64-release)"
DRV:0,0,999,0,"BD-RE PIONEER BD-RW  BDR-XD08U 1.01","","/dev/sr0"
MSG:5010,0,0,"Failed to open disc","Failed to open disc"
TCOUNT:0
'''


def fake_run(stdout, returncode=0, calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        return types.SimpleNamespace(stdout=stdout, returncode=returncode)
    return run


class FakeProc:
    """Stands in for a makemkvcon mkv run: writes its MKV, then prints the given lines."""

    def __init__(self, cmd, out_dir, lines, rc=0, block_after=None):
        self.cmd, self.lines, self.rc, self.block_after = cmd, lines, rc, block_after
        self.returncode = None
        self.terminated = threading.Event()
        title = int(cmd[-2])
        with open(os.path.join(out_dir, f"Show_t{title:02d}.mkv"), "w") as fh:
            fh.write("partial")
        self.stdout = self._out()

    def _out(self):
        for n, line in enumerate(self.lines):
            if self.block_after is not None and n == self.block_after:
                # MakeMKV goes quiet; only terminate() ends the run
                self.terminated.wait(5)
                return
            yield line + "\n"

    def poll(self):
        return self.returncode

    def wait(self):
        self.returncode = -15 if self.terminated.is_set() else self.rc
        return self.returncode

    def terminate(self):
        self.terminated.set()


RIP_LINES = [
    'MSG:1005,0,1,"MakeMKV v2.0.0 started","%1 started","MakeMKV v2.0.0"',
    'PRGT:5018,0,"Saving to MKV file"',
    'PRGV:0,0,65536',
    'PRGC:5017,0,"Saving to MKV file"',
    'PRGV:100,32768,65536',
    'PRGV:65536,65536,65536',
    'MSG:5036,0,2,"Copy complete. 1 titles saved.","Copy complete. %1 titles saved.","1"',
]


def missing_run(cmd, **kw):
    raise FileNotFoundError(cmd[0])


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def eq(got, want, what):
        check(got == want, f"{what}: got {got!r}, want {want!r}")

    cfg = Config()

    # Line parser: quoted commas and backslash-escaped quotes
    eq(mk.parse_line('SINFO:1,3,2,0,"Director\\"s Commentary, with cast"'),
       ("SINFO", ["1", "3", "2", "0", 'Director"s Commentary, with cast']), "escaped quote")
    eq(mk.parse_line("not robot output"), None, "a stray line")
    eq(mk.expand_segments("10-12,15"), [10, 11, 12, 15], "segment ranges")
    eq(mk.expand_segments("145,142,146"), [145, 142, 146], "segment order kept")
    eq(mk.parse_duration("1:52:07"), 6727, "duration")

    # Real drive listing: one Pioneer with a disc, fifteen empty slots
    with open(os.path.join(HERE, "fixtures", "drives.txt")) as fh:
        calls = []
        drives = mk.list_drives(cfg, run=fake_run(fh.read(), calls=calls))
    eq(len(drives), 1, "real drives kept")
    if drives:
        d = drives[0]
        eq((d.index, d.device, d.disc_label, d.has_disc), (0, "/dev/sr0", "PITCH_PERFECT_NA", True), "drive")
        check(d.model.startswith("BD-RE PIONEER"), f"drive model: {d.model!r}")
    eq(calls[0], ["makemkvcon", "-r", "--cache=1", "info", "disc:9999"], "drive listing command")

    # Real scan of a Pitch Perfect Blu-ray (1080p, LibreDrive on)
    with open(os.path.join(HERE, "fixtures", "pitch_perfect_info.txt")) as fh:
        calls = []
        disc = mk.scan_disc(cfg, 0, run=fake_run(fh.read(), calls=calls), min_seconds=120)
    eq(calls[0], ["makemkvcon", "-r", "--cache=1", "--minlength=120", "info", "disc:0"], "scan command")
    eq((disc.label, disc.name, disc.disc_type), ("PITCH_PERFECT_NA", "Pitch Perfect", "Blu-ray disc"), "disc info")
    eq((disc.uhd, disc.libredrive), (False, True), "uhd / libredrive")
    eq(len(disc.titles), 13, "title count")
    t0 = disc.titles[0]
    eq((t0.source, t0.duration_s, t0.size_bytes, t0.chapters, t0.segments),
       ("00800.mpls", 6727, 33799938048, 20, [23]), "feature title")
    eq(disc.titles[2].segments, [145, 142, 146, 143, 147, 151], "play-all segments")
    v, hd, core = t0.streams[0], t0.streams[1], t0.streams[2]
    eq((v.kind, v.codec, v.resolution), ("video", "Mpeg4", "1920x1080"), "video stream")
    eq((hd.kind, hd.codec, hd.lang, hd.name, hd.channels, hd.flags),
       ("audio", "DTS-HD MA", "eng", "Surround 5.1", "5.1(side)", "default"), "DTS-HD stream")
    eq(core.flags, "core", "DTS core split from the HD track")
    forced = t0.streams[8]
    eq((forced.kind, forced.name, forced.flags), ("subtitle", "PGS English  (forced only)", "default,forced"),
       "forced subtitle named from the tree info")

    # Hand-written 4K TV disc
    disc = mk.scan_disc(cfg, 0, run=fake_run(TV_DISC))
    eq((disc.uhd, disc.libredrive, disc.name), (True, True, "Show, Season 1"), "tv disc")
    eq(disc.titles[0].segments, [1, 2, 3, 4], "play-all range")
    eq(disc.titles[6].segments, [10, 12, 13], "mixed segments")
    s = {x.index: x for x in disc.titles[1].streams}
    eq(s[3].name, 'Director"s Commentary, with cast', "commentary name")
    eq(s[3].flags, "commentary", "commentary flag")
    eq(s[2].flags, "core", "core flag")
    eq(s[1].channels, "7.1", "layout preferred over count")
    eq(s[6].flags, "forced", "forced flag")

    # A disc MakeMKV can't open
    try:
        mk.scan_disc(cfg, 0, run=fake_run(FAILED_SCAN))
        failures.append("a failed scan did not raise")
    except RuntimeError as e:
        check("Failed to open disc" in str(e), f"failed scan message: {e}")
    try:
        mk.scan_disc(cfg, 0, run=fake_run("", returncode=1))
        failures.append("a non-zero exit did not raise")
    except RuntimeError as e:
        check("code 1" in str(e), f"exit code message: {e}")

    # A flatpak wrapper is used as given
    fcfg = Config(makemkvcon=["flatpak", "run", "--command=makemkvcon", "com.makemkv.MakeMKV"])
    calls = []
    mk.list_drives(fcfg, run=fake_run("", calls=calls))
    eq(calls[0][:4], ["flatpak", "run", "--command=makemkvcon", "com.makemkv.MakeMKV"], "wrapper command")

    # Ripping two titles: progress spans both, files come back in title order
    with tempfile.TemporaryDirectory() as out:
        with open(os.path.join(out, "Show_t09.mkv"), "w") as fh:
            fh.write("an older rip that must not be reported")
        seen, cmds = [], []

        def popen(cmd, **kw):
            cmds.append(cmd)
            return FakeProc(cmd, out, RIP_LINES)
        paths = mk.rip_titles(cfg, 0, [1, 3], out, lambda f, t: seen.append((f, t)), popen=popen, min_seconds=900)
        eq([os.path.basename(p) for p in paths], ["Show_t01.mkv", "Show_t03.mkv"], "ripped files")
        eq(cmds[0], ["makemkvcon", "-r", "--progress=-same", "--minlength=900", "mkv", "disc:0", "1", out],
           "rip command")
        fracs = [f for f, _ in seen]
        check(fracs == sorted(fracs), f"progress went backwards: {fracs}")
        check(0.25 in fracs and 0.75 in fracs and fracs[-1] == 1.0, f"overall fractions: {fracs}")
        check(any("Title 2 of 2: Saving to MKV file" in t for _, t in seen), f"status text: {seen}")

    # A failed title raises with MakeMKV's message and removes its partial file
    with tempfile.TemporaryDirectory() as out:
        bad = RIP_LINES[:3] + ['MSG:5036,0,2,"Copy complete. 0 titles saved, 1 failed.","x","0","1"']
        try:
            mk.rip_titles(cfg, 0, [2], out, lambda f, t: None, popen=lambda cmd, **kw: FakeProc(cmd, out, bad))
            failures.append("a failed rip did not raise")
        except RuntimeError as e:
            check("1 failed" in str(e), f"failed rip message: {e}")
        eq(os.listdir(out), [], "partial file after a failed rip")

    # Cancelling while MakeMKV is quiet terminates it and removes the partial file
    with tempfile.TemporaryDirectory() as out:
        cancel = threading.Event()
        procs = []

        def popen(cmd, **kw):
            procs.append(FakeProc(cmd, out, RIP_LINES, block_after=5))
            return procs[-1]

        def progress(f, t):
            if f >= 0.25:
                cancel.set()
        try:
            mk.rip_titles(cfg, 0, [1, 2], out, progress, cancel=cancel, popen=popen)
            failures.append("a cancelled rip did not raise")
        except mk.Cancelled:
            pass
        check(len(procs) == 1 and procs[0].terminated.is_set(), "cancel did not terminate makemkvcon")
        eq(os.listdir(out), [], "partial file after cancel")

    # Callbacks while ripping: start once the file exists, sizes while it grows, done after exit
    with tempfile.TemporaryDirectory() as out:
        events = []

        class GrowingProc(FakeProc):
            def __init__(self, cmd, out_dir, lines):
                self.path = os.path.join(out_dir, f"Show_t{int(cmd[-2]):02d}.mkv")
                super().__init__(cmd, out_dir, lines)

            def _out(self):
                time.sleep(0.05)     # MakeMKV opens the disc before the file appears
                for n, line in enumerate(self.lines):
                    with open(self.path, "a") as fh:
                        fh.write("x" * 1000)
                    time.sleep(0.04)
                    yield line + "\n"

        def popen(cmd, **kw):
            events.append(("popen", int(cmd[-2])))
            return GrowingProc(cmd, out, RIP_LINES)
        paths = mk.rip_titles(cfg, 0, [1, 3], out, lambda f, t: None, popen=popen,
                              on_title_start=lambda t, p: events.append(("start", t, os.path.basename(p))),
                              on_bytes=lambda t, p, n: events.append(("bytes", t, n)),
                              on_title_done=lambda t, p: events.append(("done", t, os.path.basename(p))),
                              bytes_interval=0.05)
        kinds = [(e[0], e[1]) for e in events]
        for t in (1, 3):
            mine = [k for k, tt in kinds if tt == t]
            check(mine[0] == "popen" and mine[1] == "start" and mine[-1] == "done"
                  and mine.count("start") == 1 and mine.count("done") == 1,
                  f"callback order for title {t}: {mine}")
            sizes = [e[2] for e in events if e[0] == "bytes" and e[1] == t]
            check(len(sizes) >= 3 and sizes == sorted(sizes), f"sizes for title {t}: {sizes}")
            check(sizes and sizes[-1] == os.path.getsize(os.path.join(out, f"Show_t{t:02d}.mkv")),
                  f"last size for title {t} is not the final size: {sizes}")
        check(kinds.index(("done", 1)) < kinds.index(("popen", 3)), f"title 1 not done before title 3: {kinds}")
        check(("start", 1, "Show_t01.mkv") in events, f"start path: {events[:3]}")
        eq([os.path.basename(p) for p in paths], ["Show_t01.mkv", "Show_t03.mkv"], "files with callbacks")

    # The previous title's file is rewritten (languages kept, HDR) while the next title's run
    # is still opening the disc. It must not be taken for the next title's file, and a
    # failure of the next title must not delete it. (Every second title of a DVD was lost.)
    for fail in (False, True):
        with tempfile.TemporaryDirectory() as out:
            events, rewrites = [], []

            class SlowOpenProc(FakeProc):
                def __init__(self, cmd, out_dir, lines):
                    self.cmd, self.lines, self.rc, self.block_after = cmd, lines, 0, None
                    self.returncode = None
                    self.terminated = threading.Event()
                    self.path = os.path.join(out_dir, f"Show_t{int(cmd[-2]):02d}.mkv")
                    self.stdout = self._out()

                def _out(self):
                    time.sleep(0.6)      # opening the disc; the previous file is rewritten meanwhile
                    with open(self.path, "w") as fh:
                        fh.write("x" * 1000)
                    for line in self.lines:
                        yield line + "\n"

            def rewrite_later(t, p):
                events.append(("done", t, os.path.basename(p)))

                def remux():
                    time.sleep(0.2)
                    tmp = hdr._temp_beside(p)     # the real temp name, which ends in .mkv
                    with open(tmp, "w") as fh:
                        fh.write("remuxed, a different size")
                    time.sleep(0.6)      # still being written when the next title's file appears
                    os.replace(tmp, p)
                rewrites.append(threading.Thread(target=remux))
                rewrites[-1].start()

            lines2 = RIP_LINES[:3] + ['MSG:5036,0,2,"Copy complete. 0 titles saved, 1 failed.","x","0","1"'] \
                if fail else RIP_LINES
            try:
                paths = mk.rip_titles(cfg, 0, [1, 2], out, lambda f, t: None,
                                      popen=lambda cmd, **kw: SlowOpenProc(cmd, out, lines2 if cmd[-2] == "2"
                                                                           else RIP_LINES),
                                      on_title_start=lambda t, p: events.append(("start", t, os.path.basename(p))),
                                      on_title_done=rewrite_later, bytes_interval=0.05)
                check(not fail, "the failed second title did not raise")
                eq([os.path.basename(p) for p in paths], ["Show_t01.mkv", "Show_t02.mkv"], "files with a rewrite")
                eq([e for e in events if e[1] == 2], [("start", 2, "Show_t02.mkv"), ("done", 2, "Show_t02.mkv")],
                   "title 2 took the previous title's file")
            except RuntimeError:
                check(fail, "the second title failed although MakeMKV succeeded")
                time.sleep(0.3)
                check(os.path.isfile(os.path.join(out, "Show_t01.mkv")),
                      "a failed title deleted the previous title's finished file")
            for th in rewrites:
                th.join()

    # A file that appears and finishes between two looks still gets start, bytes and done
    with tempfile.TemporaryDirectory() as out:
        events = []
        mk.rip_titles(cfg, 0, [2], out, lambda f, t: None, popen=lambda cmd, **kw: FakeProc(cmd, out, RIP_LINES),
                      on_title_start=lambda t, p: events.append("start"),
                      on_bytes=lambda t, p, n: events.append(("bytes", n)),
                      on_title_done=lambda t, p: events.append("done"), bytes_interval=10)
        eq(events, ["start", ("bytes", 7), "done"], "callbacks for a quick title")

    # Selection rules
    eq(mk.selection_for(["eng"], ["eng"]),
       "-sel:all,+sel:video,-sel:mvcvideo,+sel:(audio&(eng|nolang|single)),"
       "-sel:(audio&(special|core|havemulti)),+sel:(subtitle&(eng)),=100:all,"
       "-10:(audio&eng),-10:(subtitle&eng)", "selection for English")
    eq(mk.selection_for(["eng", "FRA", "eng"], []),
       "-sel:all,+sel:video,-sel:mvcvideo,+sel:(audio&(eng|fra|nolang|single)),"
       "-sel:(audio&(special|core|havemulti)),=100:all,-20:(audio&eng),-10:(audio&fra)",
       "two audio languages, no subtitles")
    check("+sel:audio," in mk.selection_for([], ["spa"]), "no audio languages keeps all audio")
    try:
        mk.selection_for(["english"], [])
        failures.append("a bad language code was accepted")
    except ValueError:
        pass
    xml = mk.profile_xml(mk.selection_for(["eng"], ["eng"]), "eng")
    import xml.etree.ElementTree as ET
    root = ET.fromstring(xml.encode())
    sels = [o.get("defaultSelection") for o in root.iter("output")]
    eq(len(sels), 3, "outputs in the profile")
    check(all(s == mk.selection_for(["eng"], ["eng"]) for s in sels), f"profile selections: {sels}")
    eq(root.find("profileSettings").get("app_DefaultSelectionString"), sels[0], "profileSettings rule")
    eq(root.find("profileSettings").get("app_PreferredLanguage"), "eng", "preferred language")

    # A selection is passed as a per-run --profile, and the profile is gone afterwards,
    # also when the rip fails; ~/.MakeMKV is never written.
    with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as out:
        mkdir = os.path.join(home, ".MakeMKV")
        os.makedirs(mkdir)
        original = b'app_Key = "T-secret"\r\napp_DefaultSelectionString = "+sel:all"\n'
        with open(os.path.join(mkdir, "settings.conf"), "wb") as fh:
            fh.write(original)
        saved_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            seen_profiles = []

            def popen(cmd, **kw):
                prof = [a for a in cmd if a.startswith("--profile=")]
                seen_profiles.append(prof)
                if prof:
                    p = prof[0].split("=", 1)[1]
                    check(os.path.dirname(p) == os.path.abspath(out) and os.path.exists(p), f"profile file: {p}")
                    check("+sel:(subtitle&amp;(eng))" in open(p).read(), "profile holds the rule")
                check(cmd.index(prof[0]) < cmd.index("mkv") if prof else True, "--profile after the command")
                return FakeProc(cmd, out, RIP_LINES)
            sel = mk.selection_for(["eng"], ["eng"])
            mk.rip_titles(cfg, 0, [1, 2], out, lambda f, t: None, popen=popen, selection=sel)
            check(len(seen_profiles) == 2 and all(len(p) == 1 for p in seen_profiles), f"profiles: {seen_profiles}")
            check(not os.path.exists(os.path.join(out, mk.PROFILE_NAME)), "profile left after a rip")

            def boom(cmd, **kw):
                raise RuntimeError("drive vanished")
            try:
                mk.rip_titles(cfg, 0, [1], out, lambda f, t: None, popen=boom, selection=sel)
                failures.append("a raising popen did not raise")
            except RuntimeError:
                pass
            check(not os.path.exists(os.path.join(out, mk.PROFILE_NAME)), "profile left after a failed rip")

            # MakeMKV rejecting the rule or loading another profile fails the title
            for msg in ('MSG:2003,0,1,"Profile parsing error: Invalid token \'x\'","%1","x"',
                        'MSG:5014,0,2,"Saving 1 titles into directory /r using profile \'Default\' '
                        'from file \'/usr/share/default.mmcp.xml\'","x","1"'):
                lines = RIP_LINES[:1] + [msg] + RIP_LINES[1:]
                with tempfile.TemporaryDirectory() as out2:
                    try:
                        mk.rip_titles(cfg, 0, [1], out2, lambda f, t: None, selection=sel,
                                      popen=lambda cmd, **kw: FakeProc(cmd, out2, lines))
                        failures.append(f"a rejected selection did not raise: {msg}")
                    except RuntimeError as e:
                        check("track selection" in str(e), f"rejected selection message: {e}")
                    eq(os.listdir(out2), [], "files left after a rejected selection")

            # No selection: no --profile
            cmds = []
            mk.rip_titles(cfg, 0, [1], out, lambda f, t: None,
                          popen=lambda cmd, **kw: (cmds.append(cmd), FakeProc(cmd, out, RIP_LINES))[1])
            check(not any(a.startswith("--profile") for a in cmds[0]), f"profile without a selection: {cmds[0]}")
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
        with open(os.path.join(mkdir, "settings.conf"), "rb") as fh:
            eq(fh.read(), original, "settings.conf after selection rips")
        eq(os.listdir(mkdir), ["settings.conf"], "files in ~/.MakeMKV")

    # selection_supported asks makemkvcon whether it knows --profile, once per command
    mk._supported.clear()
    usage = "Use: makemkvcon [switches] Command [Parameters]\n"
    calls = []
    eq(mk.selection_supported(cfg, run=fake_run(usage, 1, calls)), True, "--profile accepted")
    eq(mk.selection_supported(cfg, run=fake_run(usage, 1, calls)), True, "cached answer")
    eq(len(calls), 1, "makemkvcon asked once")
    eq(calls[0], ["makemkvcon", "--profile=/nonexistent.mmcp.xml"], "support check command")
    mk._supported.clear()
    eq(mk.selection_supported(cfg, run=fake_run("makemkvcon: unrecognized option '--profile=x'\n" + usage, 1)),
       False, "--profile unknown")
    mk._supported.clear()
    eq(mk.selection_supported(cfg, run=missing_run), False, "makemkvcon missing")
    mk._supported.clear()

    # The default command falls back to system.find_makemkvcon() only when no makemkvcon is
    # on the PATH, and a set command is never replaced.
    saved_find, saved_path = mk.system.find_makemkvcon, os.environ.get("PATH", "")
    flatpak_cmd = ["flatpak", "run", "--command=makemkvcon", "com.makemkv.MakeMKV"]
    try:
        with tempfile.TemporaryDirectory() as bindir:
            os.environ["PATH"] = bindir
            mk.system.find_makemkvcon = lambda: list(flatpak_cmd)
            eq(mk.command(cfg), flatpak_cmd, "no makemkvcon on PATH: the Flatpak")
            eq(mk.command(Config(makemkvcon=["/opt/mm/makemkvcon"])), ["/opt/mm/makemkvcon"], "a set command stays")
            mk.system.find_makemkvcon = lambda: None
            eq(mk.command(cfg), ["makemkvcon"], "nothing found: the default, which then reports not installed")
            exe = os.path.join(bindir, "makemkvcon")
            with open(exe, "w") as fh:
                fh.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
            mk.system.find_makemkvcon = lambda: failures.append("looked up with makemkvcon on PATH")
            eq(mk.command(cfg), ["makemkvcon"], "makemkvcon on PATH")
    finally:
        mk.system.find_makemkvcon, os.environ["PATH"] = saved_find, saved_path

    # A rip through the MakeMKV Flatpak is granted its output folder, which that Flatpak
    # can't otherwise see; also from inside Disc Shelf's own Flatpak. Other commands aren't.
    eq(mk.command(fcfg, "/home/u/rips/Show"), ["flatpak", "run", "--filesystem=/home/u/rips/Show",
                                               "--command=makemkvcon", "com.makemkv.MakeMKV"], "folder grant")
    spawn = Config(makemkvcon=["flatpak-spawn", "--host", "--watch-bus", *flatpak_cmd])
    eq(mk.command(spawn, "/home/u/rips/A: B")[:6], ["flatpak-spawn", "--host", "--watch-bus", "flatpak", "run",
                                                    "--filesystem=/home/u/rips/A: B:rw"], "grant from the sandbox")
    eq(mk._grant_folder(["makemkvcon"], "/x"), ["makemkvcon"], "no grant for a plain makemkvcon")
    eq(mk.command(Config(makemkvcon=["/usr/bin/run", "makemkvcon"]), "/x"), ["/usr/bin/run", "makemkvcon"],
       "no grant without flatpak run")
    with tempfile.TemporaryDirectory() as out:
        cmds = []

        def popen(cmd, **kw):
            cmds.append(cmd)
            return FakeProc(cmd, out, RIP_LINES)
        mk.rip_titles(fcfg, 0, [1], out, lambda f, t: None, popen=popen)
        check(cmds and f"--filesystem={out}" in cmds[0] and cmds[0][-1] == out, f"flatpak rip command: {cmds}")

    # Eject
    eq(mk.eject("/dev/sr0", run=fake_run("", 0)), True, "eject ok")
    eq(mk.eject("/dev/sr0", run=fake_run("", 1)), False, "eject failed")

    def missing(cmd, **kw):
        raise FileNotFoundError(cmd[0])
    eq(mk.eject("/dev/sr0", run=missing), False, "eject not installed")

    for f in failures:
        print("FAIL:", f)
    print(f"makemkv: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_makemkv():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
