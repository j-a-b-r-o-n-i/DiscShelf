#!/usr/bin/env bash
# Install Disc Shelf on this machine: the tools it drives, the kernel module MakeMKV
# needs, and a launcher entry. Safe to re-run.
#
#   make install-disc-shelf                 (or: bash scripts/install_disc_shelf.sh)
#   SKIP_PACKAGES=1 make install-disc-shelf only the launcher, config, OCR venv and checks
#   bash scripts/install_disc_shelf.sh ocr  only the OCR venv (no sudo)
#
# What it does:
#   packages   Arch/CachyOS: mkvtoolnix-cli rsync ffmpeg pyside6 ttf-ibm-plex tesseract
#              tesseract-data-eng chromaprint from the repos and makemkv from the AUR (paru
#              or yay). Fedora: mkvtoolnix rsync ffmpeg-free python3-pyside6 tesseract
#              tesseract-langpack-eng chromaprint-tools and the IBM Plex fonts with dnf,
#              and MakeMKV as the
#              Flathub app com.makemkv.MakeMKV (the config is pointed at it).
#   OCR venv   ~/.local/share/disc-shelf/ocr with rapidocr_onnxruntime 1.2.3, for
#              reading episode lists off case photos. The app's own Python can't import it,
#              so discshelf/ocr.py runs it as a helper; without it, tesseract is used.
#   dovi_tool  release 2.3.4 from GitHub into ~/.local/bin, checked against its sha256.
#              Only needed for the Dolby Vision conversions; RAW rips never call it.
#   sg module  /etc/modules-load.d/sg.conf + modprobe, so MakeMKV gets /dev/sg* (sudo).
#   launcher   ~/.local/share/applications/disc-shelf.desktop, pointing at
#              scripts/disc_shelf.py in the main checkout, so `git pull` updates the app.
#   config     ~/.config/disc-shelf/config.json if there is none yet.
set -euo pipefail

DOVI_VERSION=2.3.4
DOVI_SHA256=1844258e13c26607b32224bf1fa82b595d3b35949f5467405fda560daad32b3f
# The newest release pip picks for this Python; keep in step with discshelf/ocr_helper.py,
# which reads its (result, elapse) return value.
RAPIDOCR_VERSION=1.2.3

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# From a worktree, resolve to the main checkout: that is the path that lasts.
common="$(git -C "$here" rev-parse --git-common-dir 2>/dev/null || true)"
if [ -n "$common" ]; then
  case "$common" in /*) ;; *) common="$here/$common" ;; esac
  repo="$(cd "$(dirname "$common")" && pwd)"
else
  repo="$(dirname "$here")"
fi
script="$repo/scripts/disc_shelf.py"
bin="$HOME/.local/bin"
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
cfgdir="${XDG_CONFIG_HOME:-$HOME/.config}/disc-shelf"
makemkv_cmd='["makemkvcon"]'

# Same default as OCR_VENV in discshelf/ocr.py.
ocr_venv="${DISC_SHELF_OCR_VENV:-$HOME/.local/share/disc-shelf/ocr}"

say() { printf '>> %s\n' "$*"; }

rapidocr_ok() {
  "$ocr_venv/bin/python" -c "import importlib.metadata as m, sys; sys.exit(m.version('rapidocr_onnxruntime') != '$RAPIDOCR_VERSION')" 2>/dev/null
}

install_ocr_venv() {
  if rapidocr_ok; then return 0; fi
  say "Installing RapidOCR $RAPIDOCR_VERSION into $ocr_venv (about 400 MB, most of it OpenCV and onnxruntime)"
  mkdir -p "$(dirname "$ocr_venv")"
  [ -x "$ocr_venv/bin/python" ] || python3 -m venv "$ocr_venv"
  "$ocr_venv/bin/python" -m pip install --quiet --disable-pip-version-check "rapidocr_onnxruntime==$RAPIDOCR_VERSION"
}

if [ "${1:-}" = ocr ]; then
  install_ocr_venv
  if rapidocr_ok; then say "  ok      RapidOCR in $ocr_venv"; exit 0; fi
  say "  MISSING RapidOCR in $ocr_venv"; exit 1
fi

if [ -z "${SKIP_PACKAGES:-}" ]; then
  if command -v pacman >/dev/null; then
    say "Installing packages with pacman (asks for your password)"
    sudo pacman -S --needed --noconfirm mkvtoolnix-cli rsync ffmpeg pyside6 ttf-ibm-plex tesseract tesseract-data-eng \
      chromaprint
    if ! command -v makemkvcon >/dev/null; then
      aur="$(command -v paru || command -v yay || true)"
      if [ -n "$aur" ]; then
        say "Installing makemkv from the AUR with $(basename "$aur")"
        "$aur" -S --needed makemkv
      else
        say "No AUR helper found. Install makemkv from the AUR yourself, then re-run this."
      fi
    fi
  elif command -v dnf >/dev/null; then
    say "Installing packages with dnf (asks for your password)"
    sudo dnf install -y mkvtoolnix rsync ffmpeg-free python3-pyside6 tesseract tesseract-langpack-eng \
      chromaprint-tools ibm-plex-sans-fonts ibm-plex-mono-fonts flatpak
    if ! command -v makemkvcon >/dev/null; then
      say "Installing MakeMKV from Flathub"
      flatpak remote-add --user --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
      flatpak install --user -y flathub com.makemkv.MakeMKV
      makemkv_cmd='["flatpak", "run", "--command=makemkvcon", "com.makemkv.MakeMKV"]'
    fi
  else
    say "Unknown distribution: install MakeMKV, mkvtoolnix, rsync, ffmpeg, PySide6, tesseract and chromaprint yourself."
  fi
fi

if ! command -v dovi_tool >/dev/null && [ "$(uname -m)" = x86_64 ]; then
  say "Installing dovi_tool $DOVI_VERSION into $bin"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  tarball="dovi_tool-$DOVI_VERSION-x86_64-unknown-linux-musl.tar.gz"
  curl -fsSL -o "$tmp/$tarball" "https://github.com/quietvoid/dovi_tool/releases/download/$DOVI_VERSION/$tarball"
  echo "$DOVI_SHA256  $tmp/$tarball" | sha256sum -c --quiet
  tar -xzf "$tmp/$tarball" -C "$tmp"
  mkdir -p "$bin"
  install -m 755 "$tmp/dovi_tool" "$bin/dovi_tool"
fi

# Not fatal: case photos fall back to tesseract, and typing the list still works.
install_ocr_venv || say "Could not install RapidOCR; reading case photos will use tesseract."

if [ ! -f /etc/modules-load.d/sg.conf ]; then
  say "Loading the sg kernel module now and at every boot (asks for your password)"
  echo sg | sudo tee /etc/modules-load.d/sg.conf >/dev/null
fi
lsmod | grep -qw sg || sudo modprobe sg

mkdir -p "$apps" "$cfgdir"
cat > "$apps/disc-shelf.desktop" <<EOF
[Desktop Entry]
Type=Application
Version=1.5
Name=Disc Shelf
GenericName=Blu-ray ripper
Comment=Rip Blu-ray and 4K UHD discs, name them for Plex and copy them to the NAS
Exec=$(command -v python3) $script
Icon=media-optical-blu-ray
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=disc-shelf
EOF
command -v update-desktop-database >/dev/null && update-desktop-database "$apps" || true

if [ ! -f "$cfgdir/config.json" ]; then
  say "Writing $cfgdir/config.json"
  printf '{\n  "makemkvcon": %s\n}\n' "$makemkv_cmd" > "$cfgdir/config.json"
fi
mkdir -p "$HOME/rips"

say "Checking what is installed"
missing=0
for tool in mkvmerge rsync ffprobe; do
  if command -v "$tool" >/dev/null; then say "  ok      $tool"; else say "  MISSING $tool"; missing=1; fi
done
if command -v makemkvcon >/dev/null || flatpak info com.makemkv.MakeMKV >/dev/null 2>&1; then say "  ok      MakeMKV"
else say "  MISSING MakeMKV"; missing=1; fi
if command -v dovi_tool >/dev/null || [ -x "$bin/dovi_tool" ]; then say "  ok      dovi_tool"
else say "  missing dovi_tool (only needed for Dolby Vision conversions)"; fi
if command -v tesseract >/dev/null && tesseract --list-langs 2>/dev/null | grep -qx eng; then say "  ok      tesseract (English)"
else say "  missing tesseract with English data (the fallback for reading case photos)"; fi
if command -v fpcalc >/dev/null; then say "  ok      fpcalc"
else say "  missing fpcalc from chromaprint (title cards are then read from each title's start, which is slower)"; fi
if rapidocr_ok; then say "  ok      RapidOCR in $ocr_venv"
else say "  missing RapidOCR in $ocr_venv (reading case photos falls back to tesseract)"; fi
if python3 -c 'import PySide6' 2>/dev/null; then say "  ok      PySide6"; else say "  MISSING PySide6"; missing=1; fi
if ls /dev/sg* >/dev/null 2>&1; then say "  ok      /dev/sg* devices"; else say "  no /dev/sg* yet: plug in the drive"; fi
# Where rips are copied is chosen in the app's first-run setup, so there are no folders to check here.

say "Disc Shelf is on the launcher. Start it from there, or run: python3 $script"
exit "$missing"
