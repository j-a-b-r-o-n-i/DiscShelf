#!/usr/bin/env python3
"""
Tests for ocr.py with fake RapidOCR and tesseract runs. What matters: the app's own Python
runs the helper when it can import RapidOCR (the Flatpak), and is preferred to the venv; the
RapidOCR venv is used when it is there and run once for a whole batch at low priority, tesseract's words
become lines with boxes (and two columns run together are split again), tesseract takes
over when the venv is missing or broken, and with no engine the error is a plain sentence.
A Session keeps one helper running for many batches, and says why when it stops.

Run:  python3 discshelf/test_ocr.py    (also works under pytest)
"""
import json
import os
import queue
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import ocr  # noqa: E402
from discshelf.ocr import OcrError, OcrLine  # noqa: E402

HEAD = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"


def word(block, line, n, left, top, width, height, conf, text):
    return f"5\t1\t{block}\t1\t{line}\t{n}\t{left}\t{top}\t{width}\t{height}\t{conf}\t{text}"


# Trimmed from tesseract 5.5 on the Adventure Time case: one line holds both columns.
TSV = "\n".join([
    HEAD,
    "1\t1\t0\t0\t0\t0\t0\t0\t1512\t2016\t-1\t",
    word(1, 1, 1, 28, 1811, 269, 99, 95.0, " "),
    word(3, 1, 1, 201, 1052, 61, 30, 50.18, "I.IT"),
    word(3, 1, 2, 275, 1050, 120, 31, 89.18, "CAME"),
    word(3, 1, 3, 409, 1049, 124, 32, 89.71, "FROM"),
    word(3, 1, 4, 789, 1043, 30, 33, 91.0, "9."),
    word(3, 1, 5, 830, 1042, 200, 34, 92.0, "PRISONERS"),
    word(3, 2, 1, 199, 1103, 80, 34, 90.0, "THE"),
]) + "\n"


class Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class Env:
    """Point the module at a temporary venv (or none), a PATH with or without tesseract, and
    an app Python that can or can't import RapidOCR (whatever this machine really has)."""

    def __init__(self, venv: bool, tesseract: bool, in_process: str | None = None):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.venv = os.path.join(root, "venv")
        if venv:
            os.makedirs(os.path.join(self.venv, "bin"))
            os.makedirs(os.path.join(self.venv, "lib", "python3.14", "site-packages", "rapidocr_onnxruntime"))
            open(os.path.join(self.venv, "bin", "python"), "w").close()
        path = os.path.join(root, "path")
        os.makedirs(path)
        if tesseract:
            exe = os.path.join(path, "tesseract")
            with open(exe, "w") as f:
                f.write("#!/bin/sh\n")
            os.chmod(exe, 0o755)
        self.saved = {k: os.environ.get(k) for k in ("PATH", "DISC_SHELF_OCR_VENV")}
        os.environ["DISC_SHELF_OCR_VENV"] = self.venv
        # nice stays reachable; the fake run never starts anything anyway.
        os.environ["PATH"] = path
        self.saved_probe = ocr.in_process_python
        ocr.in_process_python = lambda: in_process

    def close(self):
        ocr.in_process_python = self.saved_probe
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    lines = ocr.parse_tesseract_tsv(TSV)
    check([l.text for l in lines] == ["I.IT CAME FROM", "9. PRISONERS", "THE"], f"tsv lines: {lines}")
    check(lines[0] == OcrLine("I.IT CAME FROM", 0.764, 201, 1049, 332, 33), f"tsv box: {lines[0]}")
    check(lines[1].x == 789 and lines[1].w == 241, f"second column box: {lines[1]}")

    # RapidOCR from the venv: one helper run for both images, niced, isolated, JSON back.
    env = Env(venv=True, tesseract=True)
    try:
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return Proc(json.dumps([[["9.PRISONERSOFLOVE", 0.876, 787, 1038, 482, 41]], []]))

        check(ocr.engine() == "rapidocr", f"engine with venv: {ocr.engine()}")
        got = ocr.read_images(["a.jpg", "b.jpg"], run=fake)
        check(len(calls) == 1, f"one helper run per batch: {calls}")
        cmd = calls[0]
        check(cmd[-3:] == [ocr.HELPER, "a.jpg", "b.jpg"] and "-I" in cmd
              and cmd[cmd.index("-I") - 1] == os.path.join(env.venv, "bin", "python"), f"helper cmd: {cmd}")
        check(got == [[OcrLine("9.PRISONERSOFLOVE", 0.876, 787, 1038, 482, 41)], []], f"helper lines: {got}")

        # A broken venv falls back to tesseract, once per image.
        calls.clear()

        def broken(cmd, **kw):
            calls.append(cmd)
            if ocr.HELPER in cmd:
                return Proc(stderr="Traceback ...\nImportError: libonnxruntime.so", returncode=1)
            return Proc(TSV)

        got = ocr.read_images(["a.jpg"], run=broken)
        check(len(calls) == 2 and calls[1][-4:] == ["tesseract", "a.jpg", "-", "tsv"], f"fallback cmds: {calls}")
        check([l.text for l in got[0]][:2] == ["I.IT CAME FROM", "9. PRISONERS"], f"fallback lines: {got}")
    finally:
        env.close()

    # No venv: tesseract.
    env = Env(venv=False, tesseract=True)
    try:
        check(ocr.engine() == "tesseract", f"engine without venv: {ocr.engine()}")
        got = ocr.read_images(["a.jpg"], run=lambda cmd, **kw: Proc(TSV))
        check(len(got) == 1 and got[0][0].text == "I.IT CAME FROM", f"tesseract lines: {got}")
        try:
            ocr.read_images(["a.jpg"], run=lambda cmd, **kw: Proc(stderr="Error in pixReadStream", returncode=1))
            check(False, "tesseract failure raises")
        except OcrError as e:
            check("a.jpg" in str(e), f"tesseract error: {e}")
    finally:
        env.close()

    # Neither: a sentence that says how to fix it, and no process started.
    env = Env(venv=False, tesseract=False)
    try:
        check(ocr.engine() == "", f"no engine: {ocr.engine()}")
        try:
            ocr.read_images(["a.jpg"], run=lambda cmd, **kw: failures.append(f"ran {cmd}"))
            check(False, "no engine raises")
        except OcrError as e:
            check("install-disc-shelf" in str(e), f"no engine message: {e}")
        check(ocr.read_images([]) == [], "no images, no error")
    finally:
        env.close()

    # RapidOCR importable by the app's own Python (the Flatpak): that Python runs the helper,
    # with -P rather than -I so a pip --user install stays visible, ahead of the venv.
    env = Env(venv=True, tesseract=False, in_process="/app/bin/python3")
    try:
        calls = []

        def fake(cmd, **kw):
            calls.append(cmd)
            return Proc(json.dumps([[["THE ENEMY WITHIN", 0.91, 10, 20, 300, 40]]]))

        check(ocr.engine() == "rapidocr", f"engine in process: {ocr.engine()}")
        got = ocr.read_images(["card.png"], run=fake)
        cmd = calls[0] if calls else []
        check(cmd[-2:] == [ocr.HELPER, "card.png"] and "/app/bin/python3" in cmd and "-P" in cmd
              and "-I" not in cmd and cmd.index("-P") == cmd.index("/app/bin/python3") + 1, f"in-process cmd: {cmd}")
        check(got == [[OcrLine("THE ENEMY WITHIN", 0.91, 10, 20, 300, 40)]], f"in-process lines: {got}")
    finally:
        env.close()

    # The real probe answers without importing RapidOCR (which would load onnxruntime).
    before = set(sys.modules)
    probe = ocr.in_process_python()
    check(probe in (None, sys.executable), f"probe: {probe}")
    check("onnxruntime" not in set(sys.modules) - before, "the probe imported onnxruntime")

    # The helper times out: the error names the engine.
    env = Env(venv=True, tesseract=False)
    try:
        def slow(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        try:
            ocr.read_images(["a.jpg"], run=slow)
            check(False, "timeout raises")
        except OcrError as e:
            check("RapidOCR" in str(e), f"timeout message: {e}")
    finally:
        env.close()

    failures += _test_session()

    for f in failures:
        print("FAIL:", f)
    print(f"ocr: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


class ServeProc:
    """ocr_helper.py --serve: answers each line of paths with one line of JSON. die_after
    batches in, it exits with an error on stderr instead."""

    def __init__(self, cmd, stderr, die_after=None, **kw):
        self.cmd, self.err, self.die_after = cmd, stderr, die_after
        self.out: queue.Queue = queue.Queue()
        self.batches = []
        self.stdout = iter(self.out.get, None)
        proc = self

        class Stdin:
            def write(self, line):
                paths = json.loads(line)
                proc.batches.append(paths)
                if proc.die_after is not None and len(proc.batches) > proc.die_after:
                    proc.err.write("Traceback ...\nMemoryError: out of memory\n")
                    proc.err.flush()
                    proc.out.put(None)
                    return
                proc.out.put("onnxruntime says hello\n")      # a library printing to stdout
                proc.out.put(json.dumps([[[os.path.basename(p), 0.9, 1, 2, 3, 4]] for p in paths]) + "\n")

            def flush(self):
                pass

            def close(self):
                proc.out.put(None)

        self.stdin = Stdin()

    def wait(self, timeout=None):
        return 0


def _test_session():
    failures = []
    env = Env(venv=True, tesseract=False)
    try:
        started = []

        def popen(cmd, **kw):
            started.append(ServeProc(cmd, **kw))
            return started[-1]
        with ocr.Session(popen=popen) as s:
            got = [s.read([f"/f/{i}.png", f"/f/{i}b.png"]) for i in range(3)]
        if len(started) != 1 or started[0].cmd[-2:] != [ocr.HELPER, "--serve"] or len(started[0].batches) != 3:
            failures.append(f"session: one helper for three batches: {[p.cmd for p in started]}")
        if got[2] != [[OcrLine("2.png", 0.9, 1, 2, 3, 4)], [OcrLine("2b.png", 0.9, 1, 2, 3, 4)]]:
            failures.append(f"session lines: {got[2]}")

        def dying(cmd, **kw):
            return ServeProc(cmd, die_after=1, **kw)
        with ocr.Session(popen=dying) as s:
            s.read(["/f/a.png"])
            try:
                s.read(["/f/b.png"])
                failures.append("session: a helper that stops raises")
            except OcrError as e:
                if "MemoryError" not in str(e):
                    failures.append(f"session: the error says why: {e}")
    finally:
        env.close()
    return failures


def test_ocr():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
