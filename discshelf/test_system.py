#!/usr/bin/env python3
"""
Tests for system.py on a fake computer. What matters: MakeMKV is found on the host or as
the MakeMKV Flatpak, and through flatpak-spawn from inside Disc Shelf's own Flatpak, without
makemkvcon ever being started; the key is read from settings.conf; a drive with no sg
device offers to load the module; missing tools, OCR and NVENC give the right sentence;
and each fix runs the right command (pkexec and flatpak are never really run).

Run:  python3 discshelf/test_system.py    (also works under pytest)
"""
import fnmatch
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import ocr, system  # noqa: E402
from discshelf.config import Config  # noqa: E402

HOME = "/home/u"
ENCODERS = " V..... libsvtav1  SVT-AV1\n V....D av1_nvenc  NVIDIA NVENC av1 encoder (codec av1)\n"
FLATPAK_INFO = "\nMakeMKV - DVD and Blu-ray to MKV\n\n          ID: com.makemkv.MakeMKV\n     Version: 1.18.1\n"


class FakeHost(system.Host):
    """A computer described by sets and dicts. Every command is recorded and answered by
    `answer`, which returns (exit code, stdout, stderr) or None for "not installed"."""

    def __init__(self, sandboxed=False, host_progs=("makemkvcon", "flatpak", "pkexec"),
                 tools=("ffmpeg", "ffprobe", "mkvmerge", "rsync", "dovi_tool", "tesseract", "fpcalc", "flatpak",
                        "xdg-open"),
                 paths=("/dev/sr0", "/sys/block/sr0/device/scsi_generic/sg3", "/dev/sg3", "/dev/nvidia0"),
                 noaccess=(), files=None, answer=None):
        self.sandboxed, self.home = sandboxed, HOME
        self.host_progs, self.tools = set(host_progs), set(tools)
        self.paths, self.noaccess = set(paths), set(noaccess)
        self.files = {f"{HOME}/.MakeMKV/settings.conf": 'app_Key = "T-abc123"\n'} if files is None else files
        self.answer = answer or (lambda cmd: None)
        self.calls, self.spawned = [], []

    def run(self, cmd, timeout=30):
        self.calls.append(list(cmd))
        bare = cmd[3:] if cmd[:3] == system.HOST else cmd
        got = self.answer(bare)
        if got is None:
            if bare[:2] == ["flatpak", "info"]:
                got = (0, FLATPAK_INFO, "") if "flatpak:makemkv" in self.host_progs else (1, "", "not installed")
            elif bare[:2] == ["sh", "-c"]:
                name = bare[2].split('"')[1]
                got = (0 if name in self.host_progs else 1, "", "")
            elif bare[1:3] == ["-hide_banner", "-encoders"]:
                got = (0, ENCODERS, "")
            else:
                got = (0, "", "")
        return subprocess.CompletedProcess(cmd, *got)

    def spawn(self, cmd):
        self.spawned.append(list(cmd))
        return "xdg-open" in self.tools

    def host_has(self, name):
        if self.sandboxed:
            return super().host_has(name)
        return name in self.host_progs

    def which(self, name):
        return f"/usr/bin/{name}" if name in self.tools else None

    def glob(self, pattern):
        return sorted(p for p in self.paths if fnmatch.fnmatch(p, pattern))

    def access(self, path, mode):
        return path in self.paths and path not in self.noaccess

    def read(self, path):
        return self.files.get(path)


def started_makemkvcon(calls):
    """Any command that would run makemkvcon itself (a lookup with command -v doesn't)."""
    out = []
    for cmd in calls:
        bare = cmd[3:] if cmd[:3] == system.HOST else cmd
        if bare and (os.path.basename(bare[0]) == "makemkvcon" or "--command=makemkvcon" in bare):
            out.append(cmd)
    return out


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    def eq(got, want, what):
        check(got == want, f"{what}: got {got!r}, want {want!r}")

    cfg = Config()
    flatpak_run = ["flatpak", "run", "--command=makemkvcon", "com.makemkv.MakeMKV"]
    saved_engine = ocr.engine
    ocr.engine = lambda: "rapidocr"
    try:
        # --- finding makemkvcon
        system.forget()
        eq(system.find_makemkvcon(FakeHost()), ["makemkvcon"], "host makemkvcon")
        h = FakeHost(host_progs=())
        eq(system.find_makemkvcon(h), ["makemkvcon"], "a find is remembered")
        eq(h.calls, [], "nothing run for a remembered find")
        system.forget()
        h = FakeHost(host_progs=("flatpak:makemkv",))
        eq(system.find_makemkvcon(h), flatpak_run, "the MakeMKV Flatpak")
        eq(h.calls, [["flatpak", "info", "com.makemkv.MakeMKV"]], "found with flatpak info only")
        system.forget()
        h = FakeHost(sandboxed=True)
        eq(system.find_makemkvcon(h), system.HOST + ["makemkvcon"], "host makemkvcon from the sandbox")
        eq(h.calls, [system.HOST + ["sh", "-c", 'command -v "makemkvcon"']], "looked up with command -v")
        system.forget()
        h = FakeHost(sandboxed=True, host_progs=("flatpak:makemkv",))
        eq(system.find_makemkvcon(h), system.HOST + flatpak_run, "MakeMKV Flatpak from the sandbox")
        system.forget()
        h = FakeHost(host_progs=(), tools=())
        eq(system.find_makemkvcon(h), None, "not installed")
        eq(h.calls, [], "no flatpak on the PATH: nothing run")
        h.host_progs.add("makemkvcon")
        eq(system.find_makemkvcon(h), ["makemkvcon"], "'not installed' isn't remembered")
        system.forget()

        # --- everything in place
        h = FakeHost(host_progs=("flatpak:makemkv", "flatpak", "pkexec"))
        got = system.checks(cfg, h)
        eq([c.key for c in got], ["makemkv", "makemkv_key", "drive", "drive_access", "tools", "ocr", "gpu"], "keys")
        eq([c.key for c in got if not c.ok], [], "all ok")
        eq([c.fix for c in got if c.fix], [], "no fix when ok")
        eq(got[0].detail, "MakeMKV 1.18.1 is installed from Flathub.", "MakeMKV detail")
        check("beta key" in got[1].detail, f"beta key detail: {got[1].detail}")
        eq(got[2].detail, "Found 1 optical drive (/dev/sr0).", "drive detail")
        eq(got[4].detail, "ffmpeg, ffprobe, mkvmerge, rsync, dovi_tool, tesseract and fpcalc are available.",
           "tools detail")
        check("much faster" in got[6].detail, f"gpu detail: {got[6].detail}")
        eq(started_makemkvcon(h.calls), [], "checks never start makemkvcon")
        check(not any("pkexec" in c or "install" in c for c in h.calls), f"checks changed something: {h.calls}")
        for c in got:
            check(c.detail.endswith(".") and "\n" not in c.detail, f"{c.key} detail is not one sentence: {c.detail!r}")
        system.forget()

        # --- MakeMKV missing: offer to install it; the key can't be fixed yet
        got = {c.key: c for c in system.checks(cfg, FakeHost(host_progs=(), files={}))}
        eq((got["makemkv"].ok, got["makemkv"].action), (False, "install_makemkv"), "install offered")
        eq((got["makemkv_key"].ok, got["makemkv_key"].action), (False, None), "key waits for MakeMKV")
        got = {c.key: c for c in system.checks(cfg, FakeHost(host_progs=(), tools=("ffmpeg", "ffprobe", "mkvmerge", "rsync"),
                                                             files={}))}
        eq(got["makemkv"].action, None, "no flatpak: nothing to offer")
        check("makemkv.com" in got["makemkv"].fix, f"manual install fix: {got['makemkv'].fix}")
        system.forget()

        # --- the key
        for text, ok, action, word in [('app_Key = "M-xyz"\n', True, None, "registered"),
                                       ('app_DestinationType = "2"\n', False, "open_makemkv_key", "30 days"),
                                       (None, False, "open_makemkv_key", "30 days")]:
            files = {} if text is None else {f"{HOME}/.MakeMKV/settings.conf": text}
            c = system.checks(cfg, FakeHost(files=files))[1]
            eq((c.ok, c.action), (ok, action), f"key from {text!r}")
            check(word in c.detail, f"key detail for {text!r}: {c.detail}")
        system.forget()

        # --- drives
        got = {c.key: c for c in system.checks(cfg, FakeHost(paths=()))}
        eq((got["drive"].ok, got["drive_access"].ok, got["drive_access"].action), (False, False, None), "no drive")
        got = {c.key: c for c in system.checks(cfg, FakeHost(paths=("/dev/sr0", "/dev/sr0p1")))}
        eq((got["drive"].ok, got["drive_access"].action), (True, "load_sg"), "no sg device: load it")
        check("password" in got["drive_access"].fix, f"sg fix: {got['drive_access'].fix}")
        got = {c.key: c for c in system.checks(cfg, FakeHost(noaccess=("/dev/sg3",)))}
        eq((got["drive_access"].ok, got["drive_access"].action), (False, None), "sg not usable")
        check("/dev/sg3" in got["drive_access"].detail and "optical group" in got["drive_access"].fix,
              f"sg access: {got['drive_access']}")
        got = {c.key: c for c in system.checks(cfg, FakeHost(noaccess=("/dev/sr0",)))}
        check(not got["drive_access"].ok and "/dev/sr0" in got["drive_access"].detail, "sr0 unreadable")
        system.forget()

        # --- tools
        got = {c.key: c for c in system.checks(cfg, FakeHost(tools=("ffmpeg", "ffprobe", "mkvmerge", "rsync")))}
        eq(got["tools"].ok, True, "optional tools missing is still ok")
        check("dovi_tool, tesseract and fpcalc are missing" in got["tools"].detail,
              f"optional note: {got['tools'].detail}")
        got = {c.key: c for c in system.checks(cfg, FakeHost(sandboxed=True, tools=("ffmpeg",)))}
        eq(got["tools"].ok, False, "required tool missing")
        check(got["tools"].detail.startswith("ffprobe, mkvmerge and rsync are not") and "Reinstall" in got["tools"].fix,
              f"sandboxed tools: {got['tools']}")
        no_svt = FakeHost(answer=lambda cmd: (0, " V....D av1_nvenc x\n", "") if "-encoders" in cmd else None)
        check("no SVT-AV1" in system.checks(cfg, no_svt)[4].detail, "missing SVT-AV1 is noted")
        system.forget()

        # --- OCR
        for engine, ok, word in [("rapidocr", True, "RapidOCR"), ("tesseract", True, "Tesseract"), ("", False, "No")]:
            ocr.engine = lambda e=engine: e
            c = system.checks(cfg, FakeHost())[5]
            check(c.ok == ok and c.detail.startswith(word), f"ocr {engine!r}: {c}")
        check(system.checks(cfg, FakeHost(sandboxed=True))[5].fix.startswith("Reinstall"), "sandboxed OCR fix")
        ocr.engine = lambda: "rapidocr"
        system.forget()

        # --- NVENC is optional: always ok, and the sentence says why not
        for host, word in [(FakeHost(answer=lambda c: (0, " V..... libsvtav1 x\n", "") if "-encoders" in c else None),
                            "no NVIDIA AV1"),
                           (FakeHost(paths=("/dev/sr0",)), "No NVIDIA graphics card"),
                           (FakeHost(answer=lambda c: (1, "", "OpenEncodeSessionEx failed") if "av1_nvenc" in c
                                     else None), "RTX 40")]:
            c = system.checks(cfg, host)[6]
            check(c.ok and word in c.detail and c.fix == "", f"gpu {word}: {c}")
        system.forget()

        # --- a check that breaks doesn't hide the others
        class Broken(FakeHost):
            def glob(self, pattern):
                raise PermissionError("sysfs said no")
        got = system.checks(cfg, Broken())
        eq(len(got), 7, "every check reported")
        check(not got[2].ok and "sysfs said no" in got[2].detail, f"broken check: {got[2]}")
        system.forget()

        # --- fixes
        h = FakeHost(sandboxed=True)
        ok, msg = system.run_action("install_makemkv", h)
        eq(ok, True, "install ok")
        eq(h.calls[-1], system.HOST + ["flatpak", "install", "--user", "-y", "--noninteractive", "flathub",
                                       "com.makemkv.MakeMKV"], "install command")
        check(h.calls[0][3:6] == ["flatpak", "remote-add", "--user"], f"flathub added first: {h.calls[0]}")
        h = FakeHost(answer=lambda c: (1, "", "error: No remote refs found") if "install" in c else None)
        ok, msg = system.run_action("install_makemkv", h)
        check(not ok and "No remote refs found" in msg, f"install failure: {msg}")
        eq(h.calls[-1][:2], ["flatpak", "install"], "no flatpak-spawn outside the sandbox")

        h = FakeHost(sandboxed=True, host_progs=("pkexec",))
        ok, msg = system.run_action("load_sg", h)
        check(ok and "every start-up" in msg, f"load_sg: {msg}")
        cmd = h.calls[-1]
        eq(cmd[:6], system.HOST + ["pkexec", "/bin/sh", "-c"], "pkexec on the host")
        check("modprobe sg" in cmd[-1] and "/etc/modules-load.d/sg.conf" in cmd[-1], f"sg script: {cmd[-1]}")
        for code, word in [(126, "closed"), (127, "isn't allowed"), (1, "failed")]:
            ok, msg = system.run_action("load_sg", FakeHost(answer=lambda c, rc=code: (rc, "", "modprobe: oops")
                                                            if "pkexec" in c else None))
            check(not ok and word in msg and (code == 126 or "sudo modprobe sg" in msg), f"load_sg exit {code}: {msg}")
        ok, msg = system.run_action("load_sg", FakeHost(host_progs=()))
        check(not ok and "no pkexec" in msg, f"no pkexec: {msg}")

        h = FakeHost()
        ok, msg = system.run_action("open_makemkv_key", h)
        eq(h.spawned, [["xdg-open", "https://forum.makemkv.com/forum/viewtopic.php?t=1053"]], "key page opened")
        check(ok and "Help, Register" in msg, f"key page message: {msg}")
        eq(h.calls, [], "the key page isn't fetched")
        ok, msg = system.run_action("open_makemkv_key", FakeHost(tools=()))
        check(not ok and "viewtopic.php?t=1053" in msg, f"no browser: {msg}")
        ok, msg = system.run_action("format_disk", FakeHost())
        check(not ok and msg.endswith("."), f"unknown action: {msg}")
    finally:
        ocr.engine = saved_engine
        system.forget()

    # The real Host only looks: in_flatpak is a file test, and a missing program is exit 127.
    check(system.in_flatpak() == os.path.exists("/.flatpak-info"), "in_flatpak")
    saved = os.environ.pop("FLATPAK_ID", None)
    try:
        eq(system.desktop_id(), "disc-shelf", "desktop id outside the Flatpak")
        os.environ["FLATPAK_ID"] = "io.example.App"
        eq(system.desktop_id(), "io.example.App", "desktop id inside")
    finally:
        os.environ.pop("FLATPAK_ID", None)
        if saved is not None:
            os.environ["FLATPAK_ID"] = saved
    eq(system.Host(sandboxed=False).run(["/nonexistent/disc-shelf-test"]).returncode, 127, "missing program")

    for f in failures:
        print("FAIL:", f)
    print(f"system: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_system():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
