# Contributing

Thanks for helping. Bug reports from other drives, discs and NAS setups are especially
useful, since the project has only been tried on a few.

## Reporting a problem

Open an issue with:

- what you did, what you expected, and what happened instead;
- the drive model and firmware (shown at the top of the window);
- the disc (title, Blu-ray, 4K or DVD, region);
- for a scan or rip problem, the output of `disc-shelf scan 0` with the drive's serial
  number removed.

## Changing the code

```bash
pip install -e '.[ocr]' pytest
QT_QPA_PLATFORM=offscreen pytest -q discshelf
```

- Each module has a `discshelf/test_<module>.py`. Each can also be run on its own with
  `python3 discshelf/test_<module>.py`.
- Tests never use a real drive. MakeMKV, ffmpeg and the other tools are faked, and tests
  that need a missing tool skip themselves.
- `disc-shelf --fake` runs the whole window on sample discs, which is the quickest way to
  see a GUI change.
- Text in the interface says what something does in plain words, and errors say what to do
  next. Keep new strings in the same style.

## Test fixtures

Fixtures captured from real discs are welcome, because they're what make matching better.
Remove the drive's serial number (the `DRV:` line) before adding one.
