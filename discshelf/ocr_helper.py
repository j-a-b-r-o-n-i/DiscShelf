#!/usr/bin/env python3
"""Run RapidOCR on images and print the text lines as JSON. Started by ocr.py, with the
app's own Python when it can import rapidocr_onnxruntime (the Flatpak, a pip install) and
with the private OCR venv's Python otherwise.

    <python> -I discshelf/ocr_helper.py a.jpg b.png

Prints one JSON list per image, in argument order, each a list of
[text, confidence, x, y, w, h] with the box in pixels from the top-left corner.
An image that cannot be read gives an empty list. Nothing from discshelf is imported.
"""
import json
import sys


def _quiet_threads() -> None:
    """onnxruntime's default thread pool busy-waits on every core (125 s of CPU for 18 frames
    on a 32-thread machine). RapidOCR 1.2.3 has no setting for it, so its session options
    are swapped for ones with two threads that sleep when idle (about 0.2 s of CPU a frame)."""
    import onnxruntime
    base = onnxruntime.SessionOptions

    def options():
        o = base()
        o.intra_op_num_threads, o.inter_op_num_threads = 2, 1
        o.add_session_config_entry("session.intra_op.allow_spinning", "0")
        return o

    onnxruntime.SessionOptions = options
    try:
        import rapidocr_onnxruntime.utils as utils
        utils.SessionOptions = options
    except (ImportError, AttributeError):
        pass


def main(paths: list[str]) -> int:
    _quiet_threads()
    from rapidocr_onnxruntime import RapidOCR

    # One engine for the whole batch: loading the models takes about a second.
    engine = RapidOCR()
    out = []
    for path in paths:
        lines = []
        try:
            result, _elapse = engine(path)
        except Exception as e:  # a corrupt or unsupported image; keep the rest of the batch
            print(f"ocr_helper: {path}: {e}", file=sys.stderr)
            result = None
        for box, text, score in result or []:
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            x, y = int(min(xs)), int(min(ys))
            lines.append([text, round(float(score), 3), x, y, int(max(xs)) - x, int(max(ys)) - y])
        out.append(lines)
    json.dump(out, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
