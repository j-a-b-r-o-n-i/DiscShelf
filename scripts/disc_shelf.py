#!/usr/bin/env python3
"""Start Disc Shelf from the repo checkout: the window by default, or a CLI subcommand.

  disc_shelf.py                 open the window
  disc_shelf.py --fake          open the window with sample discs, no drive needed
  disc_shelf.py drives          list optical drives MakeMKV can see
  disc_shelf.py scan [N]        list the titles on the disc in drive N (default 0)
  disc_shelf.py cleanup         delete local rips the clean-up setting says are due
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from discshelf.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
