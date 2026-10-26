# Design

- `brand-proposals/` is the source of the "Disc Shelf brand proposals" design canvas: four
  app-icon concepts, the colours and type, and banners for GitHub, the README and Flathub.
  Each `.dc.html` file is one artboard; `Icon.dc.html` draws the icons the others use.
- `icons/` has each icon concept as a standalone 128 × 128 SVG:
  A `a-shelf.svg`, B `b-sleeve.svg`, C `c-matched.svg`, D `d-tile.svg`.
  D, the tile, was chosen: it's `packaging/disc-shelf.svg`, which the Flatpak installs.
- `banner.png` (the README's header) and `social-preview.png` (for GitHub → Settings →
  Social preview) are drawn from the icon by `scripts/banners.py`.
