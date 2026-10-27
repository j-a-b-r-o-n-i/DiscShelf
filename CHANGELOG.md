# Changelog

## Unreleased

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
