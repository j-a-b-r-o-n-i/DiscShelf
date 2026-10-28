"""Command line for Disc Shelf. With no subcommand it opens the window."""
from __future__ import annotations

import argparse
import sys

from . import config


def _controller(fake: bool):
    if fake:
        from .fake import FakeController
        return FakeController(config.Config())
    from .jobs import JobStore
    from .pipeline import Controller
    return Controller(config.load(), JobStore())


def _minutes(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def main(argv: list[str] | None = None) -> int:
    """The `disc-shelf` command (pyproject.toml) and scripts/disc_shelf.py both end up here."""
    if argv is None:
        argv = sys.argv[1:]
    ap = argparse.ArgumentParser(prog="disc-shelf", description="Rip Blu-rays, 4K discs and DVDs and file the episodes for a media server.")
    ap.add_argument("--fake", action="store_true", help="use sample discs instead of a real drive")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("drives", help="list optical drives MakeMKV can see")
    sc = sub.add_parser("scan", help="list the titles on a disc")
    sc.add_argument("drive", nargs="?", type=int, default=0)
    cl = sub.add_parser("cleanup", help="delete local rips the clean-up setting says are due")
    cl.add_argument("--apply", action="store_true", help="delete them; without this it only lists them")
    args, rest = ap.parse_known_args(argv)

    if not args.fake:
        for path in config.adopt_host_data():
            print(f"Copied your existing Disc Shelf settings and jobs into {path}", file=sys.stderr)
    ctl = _controller(args.fake)
    if args.cmd is None:
        from .gui import run_gui
        if not args.fake:
            try:
                ctl.cleanup(apply=True)    # the clean-up setting ("after 7 days" etc.) applies each time the app opens
            except OSError as e:
                print(f"Clean-up skipped: {e}", file=sys.stderr)
            for job, found in ctl.recover_titles():
                print(f"Recovered {len(found)} ripped title(s) of {job.disc_label}: {found}", file=sys.stderr)
            # AV1 versions the last run left half-made start again; the encoder carries on with the queue.
            for job, back in ctl.recover_encodes():
                print(f"AV1 versions of {job.disc_label} queued again after a restart: {back}", file=sys.stderr)
        return run_gui(ctl, [sys.argv[0], *rest])
    if args.cmd == "drives":
        drives = ctl.drives()
        if not drives:
            print("No optical drive found. Plug one in and check that the sg kernel module is loaded.")
            return 1
        for d in drives:
            disc = f"disc: {d.disc_label}" if d.has_disc else "no disc"
            print(f"{d.index}  {d.device:10}  {d.model}  ({disc})")
        return 0
    if args.cmd == "scan":
        disc = ctl.scan(args.drive)
        job = ctl.new_job(disc)
        print(f"{disc.label} · {disc.disc_type}{' · 4K UHD' if disc.uhd else ''}"
              f"{' · LibreDrive on' if disc.libredrive else ''}")
        for t in job.titles:
            mark = "x" if t.selected else " "
            print(f"[{mark}] {t.index:3}  {t.source:12} {_minutes(t.duration_s):>8}  "
                  f"{t.size_bytes / 1e9:6.1f} GB  {t.kind}")
        return 0
    if args.cmd == "cleanup":
        due = ctl.cleanup(apply=args.apply)
        for p in due:
            print(("deleted " if args.apply else "due     ") + p)
        if not args.apply and due:
            print("Run again with --apply to delete them.")
        return 0
    return 2
