#!/usr/bin/env python3
"""
Tests for config.adopt_host_data(): inside the Flatpak (FLATPAK_ID set, XDG folders under
~/.var/app), settings and jobs from a non-Flatpak install are copied over once; outside the
Flatpak, or when the Flatpak already has its own, nothing is copied.

Run:  python3 discshelf/test_config.py    (also works under pytest)
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from discshelf import config  # noqa: E402


def run():
    failures = []

    def check(cond, msg):
        if not cond:
            failures.append(msg)

    keys = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "FLATPAK_ID")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        with tempfile.TemporaryDirectory() as home:
            os.environ["HOME"] = home
            os.makedirs(os.path.join(home, ".config/disc-shelf"))
            os.makedirs(os.path.join(home, ".local/state/disc-shelf/jobs"))
            with open(os.path.join(home, ".config/disc-shelf/config.json"), "w") as fh:
                fh.write("{}")
            secret = os.path.join(home, ".config/disc-shelf/secrets.json")
            with open(secret, "w") as fh:
                fh.write("{}")
            os.chmod(secret, 0o600)
            with open(os.path.join(home, ".local/state/disc-shelf/jobs/a.json"), "w") as fh:
                fh.write("{}")
            app = os.path.join(home, ".var/app/io.example.DiscShelf")
            os.environ["XDG_CONFIG_HOME"] = os.path.join(app, "config")
            os.environ["XDG_STATE_HOME"] = os.path.join(app, ".local/state")
            os.environ.pop("FLATPAK_ID", None)
            check(config.adopt_host_data() == [], "copied outside the Flatpak")
            os.environ["FLATPAK_ID"] = "io.example.DiscShelf"
            got = config.adopt_host_data()
            check(len(got) == 2 and os.path.isfile(os.path.join(config.config_dir(), "config.json"))
                  and os.path.isfile(os.path.join(config.state_dir(), "jobs/a.json")), f"not copied: {got}")
            check(os.stat(os.path.join(config.config_dir(), "secrets.json")).st_mode & 0o077 == 0,
                  "the Plex sign-in became readable by others")
            check(config.adopt_host_data() == [], "copied a second time over the Flatpak's own data")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    for f in failures:
        print("FAIL:", f)
    print(f"config: {'ok' if not failures else f'{len(failures)} failure(s)'}")
    return 1 if failures else 0


def test_config():
    assert run() == 0


if __name__ == "__main__":
    sys.exit(run())
