# Third-party data and software

Disc Shelf is GPL-3.0-or-later. It talks to these services and runs these programs; none of
them is part of Disc Shelf's source.

## Data services

- **TVmaze** (https://www.tvmaze.com): show and episode lists, posters. TVmaze data is
  licensed [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/). The test fixtures
  `discshelf/fixtures/adventure_time_episodes.json` and
  `discshelf/fixtures/tvmaze_office_severance_episodes.json` are trimmed copies of TVmaze
  episode lists and are shared under the same licence.
- **TheDiscDB** (https://thediscdb.com): which title on a disc is which episode or extra.
  The fixture `discshelf/fixtures/thediscdb_search.json` is a trimmed reply from its API.
  TheDiscDB's data repository (https://github.com/TheDiscDb/data) is under the MIT licence:

  > MIT License
  >
  > Copyright (c) 2021 Luke Foust
  >
  > Permission is hereby granted, free of charge, to any person obtaining a copy of this
  > software and associated documentation files (the "Software"), to deal in the Software
  > without restriction, including without limitation the rights to use, copy, modify, merge,
  > publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
  > to whom the Software is furnished to do so, subject to the following conditions:
  >
  > The above copyright notice and this permission notice shall be included in all copies or
  > substantial portions of the Software.
  >
  > THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
  > INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
  > PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
  > FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
  > OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
  > DEALINGS IN THE SOFTWARE.
- **Plex** (https://www.plex.tv): sign-in with Plex's PIN flow, library scans. Disc Shelf
  stores the Plex token in `~/.config/disc-shelf/secrets.json`, readable only by you.

## Programs Disc Shelf runs

Each runs as a separate program; Disc Shelf doesn't link to them.

| Program | Licence | Used for |
|---|---|---|
| MakeMKV (`makemkvcon`) | proprietary, installed separately | reading discs |
| ffmpeg / ffprobe | LGPL/GPL | reading formats, title-card frames, AV1 encodes, VMAF |
| SVT-AV1 | BSD-3-Clause-Clear | AV1 encodes on the processor |
| mkvmerge (MKVToolNix) | GPL-2.0 | keeping the chosen languages, HDR10 metadata |
| dovi_tool | MIT | Dolby Vision checks and conversion |
| Tesseract | Apache-2.0 | reading case photos (fallback) |
| RapidOCR (+ ONNX Runtime, OpenCV) | Apache-2.0 | reading title cards and case photos |
| rsync | GPL-3.0 | copying to the NAS |

The Python libraries are PySide6 (LGPL-3.0) and, for OCR, `rapidocr_onnxruntime` and its
dependencies.

## Test fixtures captured from discs

`discshelf/fixtures/` holds MakeMKV scan output and OCR text captured from a Pitch Perfect
Blu-ray and an Adventure Time DVD (drive serial numbers removed), plus the disc's own menu
metadata file. They exist so the tests run without a drive.
