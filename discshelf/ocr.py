"""Read text lines off images, for the back of a case and for title cards.

RapidOCR is the main engine. It runs as a helper process (ocr_helper.py), one process per
batch, because loading its models takes about a second, and because onnxruntime's thread
pool is best kept out of the app's own process. The helper's Python is, in order:

  1. the app's own Python (sys.executable), when rapidocr_onnxruntime is importable here.
     That is the case in the Flatpak, which bundles it, and after `pip install disc-shelf[ocr]`;
  2. the private venv OCR_VENV that scripts/install_disc_shelf.sh makes, for a distro
     install whose system Python has no onnxruntime.

The three ONNX models RapidOCR needs (text detection, angle classification, recognition,
about 14 MB) ship inside the rapidocr_onnxruntime 1.2.3 wheel under
rapidocr_onnxruntime/models/, so nothing is downloaded on first use.

When neither is there, tesseract is used instead; it is weaker on stylised case fonts but
is a system package.

Every external program goes through an injectable `run` (subprocess.run), so the tests never
start an OCR engine.
"""
from __future__ import annotations

import csv
import glob
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

OCR_VENV = os.path.expanduser("~/.local/share/disc-shelf/ocr")
HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ocr_helper.py")


class OcrError(Exception):
    """No OCR engine is available or the engine failed. The message is one sentence."""


@dataclass
class OcrLine:
    text: str
    conf: float      # 0 to 1
    x: int           # box in pixels, top-left origin
    y: int
    w: int
    h: int


def venv_dir() -> str:
    return os.environ.get("DISC_SHELF_OCR_VENV") or OCR_VENV


def _venv_python() -> str | None:
    venv = venv_dir()
    py = os.path.join(venv, "bin", "python")
    # Checking the package directory is enough; importing it here would cost the model load.
    if os.path.exists(py) and glob.glob(os.path.join(venv, "lib", "python*", "site-packages", "rapidocr_onnxruntime")):
        return py
    return None


def in_process_python() -> str | None:
    """sys.executable when rapidocr_onnxruntime can be imported by the app's own Python.
    find_spec only looks for the package; it doesn't load the models."""
    try:
        found = importlib.util.find_spec("rapidocr_onnxruntime") is not None
    except (ImportError, ValueError):
        found = False
    return sys.executable if found and sys.executable else None


def _helper_python() -> tuple[str, list[str]] | None:
    """The Python that runs the helper and the flags it needs, or None.

    The venv's Python gets -I (isolated): nothing from the app's environment leaks in. The
    app's own Python gets -P instead, which still keeps the helper's folder (discshelf/) off
    sys.path but leaves PYTHONPATH and the user's site-packages alone, since that is where
    a pip install --user put RapidOCR."""
    py = in_process_python()
    if py:
        return py, ["-P"]
    py = _venv_python()
    if py:
        return py, ["-I"]
    return None


def engine() -> str:
    """"rapidocr", "tesseract", or "" when neither is installed."""
    if _helper_python():
        return "rapidocr"
    if shutil.which("tesseract"):
        return "tesseract"
    return ""


def _nice() -> list[str]:
    # OCR shares the machine with a rip and a copy; it can wait.
    nice = shutil.which("nice")
    return [nice, "-n", "10"] if nice else []


def _rapidocr(py: str, flags: list[str], paths: list[str], run) -> list[list[OcrLine]]:
    cmd = _nice() + [py, *flags, HELPER] + list(paths)
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=60 + 15 * len(paths))
    except (OSError, subprocess.TimeoutExpired) as e:
        raise OcrError(f"RapidOCR did not finish ({e}).") from e
    if proc.returncode != 0:
        last = (proc.stderr or "").strip().splitlines()[-1:] or ["no message"]
        raise OcrError(f"RapidOCR failed (exit {proc.returncode}): {last[0]}")
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        raise OcrError("RapidOCR printed something that is not JSON.") from e
    return [[OcrLine(str(t), float(c), int(x), int(y), int(w), int(h)) for t, c, x, y, w, h in img] for img in data]


def parse_tesseract_tsv(tsv: str) -> list[OcrLine]:
    """Tesseract's TSV has one row per word; words sharing block, paragraph and line numbers
    are one line. The line's box covers its words and its confidence is their mean.

    Tesseract often runs two columns of a case together into one line, so a line is split
    wherever the gap between words is wider than the text is tall; RapidOCR splits there too."""
    groups: dict[tuple, list[dict]] = {}
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE):
        text = (row.get("text") or "").strip()
        if row.get("level") != "5" or not text:
            continue
        key = (int(row["page_num"]), int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
        groups.setdefault(key, []).append(row)
    runs = []
    for words in groups.values():
        words.sort(key=lambda r: int(r["word_num"]))
        height = max(int(r["height"]) for r in words)
        run_ = [words[0]]
        for prev, w in zip(words, words[1:]):
            if int(w["left"]) - (int(prev["left"]) + int(prev["width"])) > height:
                runs.append(run_)
                run_ = []
            run_.append(w)
        runs.append(run_)
    out = []
    for words in runs:
        x0 = min(int(r["left"]) for r in words)
        y0 = min(int(r["top"]) for r in words)
        x1 = max(int(r["left"]) + int(r["width"]) for r in words)
        y1 = max(int(r["top"]) + int(r["height"]) for r in words)
        confs = [max(float(r["conf"]), 0.0) for r in words]
        out.append(OcrLine(" ".join(r["text"].strip() for r in words), round(sum(confs) / len(confs) / 100, 3),
                           x0, y0, x1 - x0, y1 - y0))
    return out


def _tesseract(paths: list[str], run) -> list[list[OcrLine]]:
    out = []
    for path in paths:
        cmd = _nice() + ["tesseract", path, "-", "tsv"]
        try:
            proc = run(cmd, capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise OcrError(f"Tesseract did not finish ({e}).") from e
        if proc.returncode != 0:
            last = (proc.stderr or "").strip().splitlines()[-1:] or ["no message"]
            raise OcrError(f"Tesseract could not read {os.path.basename(path)}: {last[0]}")
        out.append(parse_tesseract_tsv(proc.stdout))
    return out


def read_images(paths: list[str], run=subprocess.run) -> list[list[OcrLine]]:
    """The text lines on each image, one list per image in the order given."""
    if not paths:
        return []
    helper = _helper_python()
    if helper:
        try:
            return _rapidocr(helper[0], helper[1], paths, run)
        except OcrError:
            # A broken venv shouldn't stop the user when tesseract can do the job.
            if not shutil.which("tesseract"):
                raise
    elif not shutil.which("tesseract"):
        if os.path.exists("/.flatpak-info"):
            raise OcrError("This copy of Disc Shelf is missing its text reader; reinstall it from Flathub.")
        raise OcrError("No OCR engine is installed; run make install-disc-shelf to add RapidOCR and tesseract.")
    return _tesseract(paths, run)
