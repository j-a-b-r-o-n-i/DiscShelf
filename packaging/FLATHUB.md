# Releasing on Flathub

Flathub builds Disc Shelf from its own repository, `github.com/flathub/io.github.j_a_b_r_o_n_i.DiscShelf`,
which holds the Flatpak manifest and the files it points to. The first submission goes
through a pull request to `github.com/flathub/flathub`; later releases are pull requests to
the app's own Flathub repository.

## Before the first submission

- This repository must be **public**. Flathub's linter checks that the app id's address,
  `github.com/j-a-b-r-o-n-i/DiscShelf`, is reachable, and the metainfo's screenshots are
  loaded from it.
- The icon must be final: `packaging/disc-shelf.svg`.
- `appstreamcli validate --no-net packaging/disc-shelf.metainfo.xml` and the linter runs below
  must come back clean, apart from the exception below.

## Each release

1. Update `version` in `pyproject.toml`, add a `<release>` to `packaging/disc-shelf.metainfo.xml`
   and an entry to `CHANGELOG.md`.
2. Take new screenshots if the window changed: `python3 scripts/screenshots.py`. The
   metainfo loads them from the release tag, so commit them before tagging.
3. Commit, tag and push: `git tag v0.1.0 && git push origin main v0.1.0`.
4. Make the Flathub files: `python3 scripts/flathub_bundle.py v0.1.0`. They're written to
   `.flatpak-build/flathub/io.github.j_a_b_r_o_n_i.DiscShelf/`, with Disc Shelf built from
   the tag's commit.
5. Build and lint them:

   ```bash
   cd .flatpak-build/flathub/io.github.j_a_b_r_o_n_i.DiscShelf
   flatpak run org.flatpak.Builder --user --force-clean --repo=repo --install build io.github.j_a_b_r_o_n_i.DiscShelf.yml
   flatpak run --command=flatpak-builder-lint org.flatpak.Builder manifest io.github.j_a_b_r_o_n_i.DiscShelf.yml
   flatpak run --command=flatpak-builder-lint org.flatpak.Builder repo repo
   ```

6. First release: fork `flathub/flathub`, make a branch from its `new-pr` branch, add the
   files, and open a pull request against `new-pr` titled "Add io.github.j_a_b_r_o_n_i.DiscShelf".
   Later releases: open a pull request with the new files on the app's Flathub repository.

## The exception to ask for

The linter warns `finish-args-flatpak-spawn-access`, because the manifest has
`--talk-name=org.freedesktop.Flatpak`. Flathub only allows it with an exception, so the
first pull request should explain it. Suggested text:

> Disc Shelf is a front end for MakeMKV, which reads the discs. MakeMKV is proprietary and
> its licence doesn't allow bundling, so Disc Shelf runs the MakeMKV Flatpak
> (`com.makemkv.MakeMKV`) the user installs from Flathub, with
> `flatpak-spawn --host flatpak run --command=makemkvcon com.makemkv.MakeMKV`. The first-run
> setup also uses it to offer installing that Flatpak, and to load the `sg` kernel module
> MakeMKV needs on distributions that don't load it (through pkexec, so the user approves
> it). Nothing else is run on the host.

If Flathub refuses the exception, the alternative is for Disc Shelf to fetch MakeMKV's own
Linux build at install time with Flatpak's `extra-data`, so nothing runs outside the sandbox.
That needs MakeMKV's licence to allow it and more packaging work, so it's not done yet.

## Other permissions reviewers ask about

- `--device=all`: the optical drive (`/dev/sr*`, `/dev/sg*`) and `/dev/nvidia*` for the
  AV1 encoder; `--device=dri` covers neither.
- `--filesystem=home`, `/mnt`, `/media`, `/run/media`: rips go to `~/rips` by default, and
  the user picks the folders their media server reads, usually a NAS share mounted under
  one of those.
- `--share=network`: TVmaze, TheDiscDB and Plex.
