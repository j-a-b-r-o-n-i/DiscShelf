# Releasing

Disc Shelf is released as a Flatpak bundle attached to each GitHub release. It isn't on
Flathub yet; see the end of this file for why and what that would take.

## Each release

1. Update `version` in `pyproject.toml`, add a `<release>` to `packaging/disc-shelf.metainfo.xml`
   and an entry to `CHANGELOG.md`.
2. Take new screenshots if the window changed: `python3 scripts/screenshots.py`. The
   metainfo loads them from the release tag, so commit them before tagging.
3. Commit, tag and push: `git tag -a v0.1.0 -m "Disc Shelf 0.1.0" && git push origin main v0.1.0`.
4. Build the release from the tag, the way Flathub would, into a local Flatpak repository:

   ```bash
   python3 scripts/flathub_bundle.py v0.1.0
   cd .flatpak-build/flathub/io.github.j_a_b_r_o_n_i.DiscShelf
   flatpak run org.flatpak.Builder --user --force-clean --repo=repo build io.github.j_a_b_r_o_n_i.DiscShelf.yml
   ```

   Add `--state-dir=<repo root>/.flatpak-builder` to reuse the cached ffmpeg, mkvtoolnix and
   other modules from an earlier build; a build from nothing takes about an hour.
5. Make the bundle. It points at Flathub for the KDE runtime, so installing it fetches the
   runtime too:

   ```bash
   flatpak build-bundle --runtime-repo=https://flathub.org/repo/flathub.flatpakrepo \
       repo DiscShelf-0.1.0.flatpak io.github.j_a_b_r_o_n_i.DiscShelf master
   sha256sum DiscShelf-0.1.0.flatpak
   ```

6. Check it installs and starts: `flatpak install --user --reinstall --bundle DiscShelf-0.1.0.flatpak`,
   then open Disc Shelf.
7. Create the GitHub release for the tag with the changelog entry and the checksum, and
   attach the bundle.

## Flathub

Flathub would give automatic updates and a listing in every Linux software centre. The
packaging is ready for it: the app id, the metainfo, the screenshots and the manifest pass
Flathub's own linter, apart from one permission that needs an exception (below). But a
submission isn't possible yet, because of Flathub's
[requirements](https://docs.flathub.org/docs/for-app-authors/requirements):

- **Development history.** Flathub wants "a sustained commit history" and evidence of
  real-world use, and generally doesn't accept apps that have only existed for a short time.
- **Generative AI policy.** Much of Disc Shelf's code and packaging was written with an AI
  assistant, and that has to be disclosed; reviewers decide case by case. Two of the rules
  are absolute:
  - The Flathub manifest must not contain AI-generated or AI-assisted content, so a
    maintainer has to write the one submitted to Flathub themselves. The manifest in this
    folder can serve as a reference for what has to be built.
  - AI tools or agents must not open or automate the submission pull request or answer its
    review, so a maintainer has to do the submission personally.
- **The MakeMKV permission.** `--talk-name=org.freedesktop.Flatpak` lets Disc Shelf run the
  MakeMKV Flatpak, install it from the setup screen, and load the `sg` kernel module through
  pkexec. Flathub only allows it with an exception, which the submission has to justify. If
  it's refused, the alternative is to fetch MakeMKV's own Linux build at install time with
  Flatpak's `extra-data`, if MakeMKV's licence allows that.

Permissions reviewers ask about:

- `--device=all`: the optical drive (`/dev/sr*`, `/dev/sg*`) and `/dev/nvidia*` for the
  AV1 encoder; `--device=dri` covers neither.
- `--filesystem=home`, `/mnt`, `/media`, `/run/media`: rips go to `~/rips` by default, and
  the user picks the folders their media server reads, usually a NAS share mounted under
  one of those.
- `--share=network`: TVmaze, TheDiscDB and Plex.
