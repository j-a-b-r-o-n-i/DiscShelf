# Changelog

## Unreleased

- The rip graph is now a timeline of each title: MakeMKV opening the disc, reading (with
  the speed averaged over 20 s), keeping languages and copying to the NAS, with slow titles
  marked and the rest of the rip drawn as an estimate. The time left is worked out from
  this disc's own titles, so it no longer swings between minutes and hours.
- Every second title of a disc was mistaken for the previous title's temporary file while
  that one had its languages kept. It showed spikes of about 100 MB/s on the rip graph, and
  the title wasn't matched or copied until the whole disc had ripped.
- Two titles matched to the same episode are flagged on the Match page instead of being
  copied as "S01E20 (2)". Both wait until you compare them and keep one, keep both as
  versions of the episode, or save one as an extra.
- Matches are checked against the season's other discs and the files already in its
  season folder, and the episode picker lists free episodes first and says where each taken
  one is.
- A title that's the same episode dubbed in another language, with no audio in the disc's
  main language, is recognised as a dub and kept as " - French" (or its language) rather
  than called a shorter cut, on the Match page and the Library page.
- A season grid on the Match page shows where every episode of the season is.
- Anything flagged on the Match page (a ×2 or ? cell, a row's badge, the line at the top, or
  the Queue's needs-you card) opens one fix panel: what's wrong, both sides with Watch, the
  fixes as choices that say what happens, and Undo. The rest opens what's known about it.
- Two files already on the NAS for one episode, from a disc copied before duplicates were
  checked, can be fixed from the Match page: keep one as the French (or second) version,
  move it to Featurettes, set it aside, delete it, or swap them. The files are renamed with the
  Library page's rename and the disc's records are updated; Undo reverses both. A delete asks
  first and can't be undone.
- A match that needs a check can have just its title card read; when the card agrees,
  confirming is one click.
- Discs without a disc number are numbered by the order they were ripped, and named by
  their label.
- Watch opens a ripped title in mpv or VLC, at its title card when the card was read.
- Title cards are found up to 20 minutes into a title: the theme song is recognised by its
  sound and skipped, and reading stops at the first frame that names an episode. Settings
  has a choice of how far to look.

## 0.1.0 (unreleased)

The first public release.

- Rip Blu-rays, 4K UHD discs and DVDs with MakeMKV, with episodes, films and extras picked
  out automatically.
- Match episodes from the case's episode list (typed or read from a photo), TheDiscDB,
  title cards read with OCR, and running times.
- Copy each title to your library as it's ripped, read back and checked, and ask Plex to
  scan it.
- File names with the episode name by default, and a choice of schemes in Settings → File
  names: with or without the episode name, quality, show year and edition, and padded or
  plain season folders. A second cut of an episode is named as a version of it.
- A Library page that checks the shows already in a TV destination, finds duplicate,
  misnamed and misplaced episodes, and renames them to the scheme, with undo.
- Keep HDR10, HDR10+ and Dolby Vision exactly as on the disc, or convert Dolby Vision
  profile 7 to 8.1.
- Optional AV1 versions of 4K discs, on the processor or an Nvidia graphics card, with a
  quality check.
- A Queue that puts what needs you first, tracks seasons across discs, and frees local
  disk space once copies are checked.
- First-run setup that checks the computer and installs MakeMKV.
