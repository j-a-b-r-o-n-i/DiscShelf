#!/usr/bin/env python3
"""
Make the files for Flathub's repository from this repo's Flatpak manifest, for a release tag.

    python3 scripts/flathub_bundle.py v0.1.0 [out_dir]

Flathub builds an app from its own repository (github.com/flathub/<app id>), which holds
the manifest and the files it points to. The manifest here builds Disc Shelf from this
checkout (`type: dir`); the bundle's copy builds it from the release tag and its commit
instead, so Flathub builds exactly what was released. The tag must exist on GitHub.
Nothing is pushed or submitted.
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "packaging")
REPO = "https://github.com/j-a-b-r-o-n-i/DiscShelf.git"
# Files the manifest refers to by a path next to it.
COPY = ["python3-ocr-deps.json", "dovi_tool-cargo-sources.json", "build-aux/xxd.py"]

DIR_SOURCE = re.compile(r"( *)- type: dir\n\1  path: \.\.\n(?:\1  skip:\n(?:\1    - .*\n)+)?")


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    tag = argv[1]
    manifest = next(f for f in os.listdir(PKG) if re.fullmatch(r"io\.github\..+\.yml", f))
    out = argv[2] if len(argv) > 2 else os.path.join(ROOT, ".flatpak-build", "flathub", manifest[:-4])
    commit = subprocess.run(["git", "-C", ROOT, "rev-list", "-n", "1", tag], capture_output=True, text=True)
    if commit.returncode != 0:
        print(f"There's no tag {tag} in this repo. Tag the release first: git tag {tag}")
        return 1
    commit = commit.stdout.strip()
    with open(os.path.join(PKG, manifest)) as fh:
        text = fh.read()
    m = DIR_SOURCE.search(text)
    if not m:
        print("The manifest's `type: dir` source for Disc Shelf wasn't found; update this script.")
        return 1
    pad = m.group(1)
    text = text[:m.start()] + (f"{pad}- type: git\n{pad}  url: {REPO}\n{pad}  tag: {tag}\n"
                               f"{pad}  commit: {commit}\n") + text[m.end():]
    text = text.replace("      # For Flathub this becomes a `type: git` source with a tag and commit.\n", "")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, manifest), "w") as fh:
        fh.write(text)
    for rel in COPY:
        os.makedirs(os.path.dirname(os.path.join(out, rel)) or out, exist_ok=True)
        shutil.copy2(os.path.join(PKG, rel), os.path.join(out, rel))
    print(f"Wrote {out} for {tag} ({commit[:12]}): {manifest} and {', '.join(COPY)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
