# How Disc Shelf works

This walks through each screen and the decisions Disc Shelf makes for you.

## Rip

- **A disc is scanned as soon as it's in the drive.** Disc Shelf asks the drive for its
  status every 3 seconds with the kernel's CD-ROM status request, which doesn't start
  MakeMKV or spin the disc up.
- **Short titles are left out.** MakeMKV scans at a 2-minute minimum title length, and the
  rip uses the same value, because MakeMKV numbers titles after dropping short ones.
- **Each title gets a type**: episode, film, play-all, duplicate, extra, or short (under
  5 minutes). Episodes or the film are ticked for ripping.
  - Episodes can be short: a run of 3 or more similar titles over 5 minutes counts, so
    11-minute cartoons work.
  - DVDs number each title's segments from 1, so on a DVD, play-alls and duplicates are
    found by length and size instead.
- **Audio and subtitles** default to English; change the defaults in Settings.
- **HDR** defaults to RAW, which keeps every HDR format and converts nothing. The "Not Raw"
  badge lists each setting that would change the HDR from the disc.
- **The next disc of a season starts filled in.** When a disc's show name (or its label
  before `_S` or `_D`) matches exactly one season in progress, the new disc gets that
  season's show, year, library and the next disc number. Whatever the label itself says
  about season or disc wins.
- **After ripping**, mkvmerge keeps only the chosen languages. It matches them by
  language, since MakeMKV's track numbers don't match the disc's. ffprobe reads the HDR
  formats, and on a Dolby Vision profile 7 file dovi_tool works out whether it has a full
  or minimal enhancement layer. Only a "Not Raw" setting runs dovi_tool to change the file.
- **Ejecting.** With "Eject the disc when its rip finishes" on (the default), the tray
  opens as soon as MakeMKV is done, while the last titles still copy. A stopped or failed
  rip doesn't eject.

## Match episodes

Disc Shelf looks the show up on [TVmaze](https://www.tvmaze.com), which is free and needs
no key. Each title's match comes from the first of these sources that has one. A later
source never replaces an earlier one, a match you picked, or one you confirmed.

1. **The episode list from the case.** Choose **Enter episode list…** and type or paste the
   names from the back of the case, one per line. Or choose **Read a photo of the case…**,
   or drop a photo on the box, and the names are read with OCR for you to check. iPhone
   HEIC photos work. The names are matched to titles in disc order, across every season,
   so volume DVDs that mix seasons work. A shortened name ("Wizard" for "Finn the Wizard")
   is flagged with the other episodes it could be.
2. **[TheDiscDB](https://thediscdb.com)**, a community database of what each title on a
   disc is. A disc counts as found only when its titles' playlist (or DVD title number),
   length and exact size all match the scan, so another pressing never matches. Episodes
   are tied to TVmaze by name, because TheDiscDB numbers them the way TMDB does. Coverage
   is still small, so most discs fall through to the next two sources.
3. **Title cards.** With "Read title cards to match episodes" on (Settings → Matching),
   each title is read with OCR as soon as it's ripped. Chromaprint's fpcalc fingerprints
   the opening's sound, and the theme song is found by comparing two titles of the show:
   the longest stretch that sounds the same in both. Reading then starts where the theme
   ends, one small grey frame every 2 seconds, and stops at the first frame that names one
   of the show's episodes, once the frames around it, read again in full-size colour,
   agree. It reads up to "Look for the title card up to" (5 minutes unless changed), so a
   card after a long opening scene is found too. Each version of a show's theme is kept,
   so a special or a season with a new theme song is learned as well. A title whose theme
   isn't recognised is read from its start, and when nothing is found after the theme the
   skipped part is read too, so the theme only ever saves time. Text that appears on
   several titles, such as a logo, is ignored, as is text during the theme song unless
   there is nothing else. A running-time guess waits for its title's card before it's
   copied. For a disc ripped earlier,
   **Read title cards** on the Match page reads them from the files, several titles at
   once. A bar shows each title's progress, and the **Title card** column shows exactly
   what each card read.
4. **Running time**, carrying on from the episodes earlier discs of the same season
   covered. A disc that names no season is guessed from season 1 and always marked "check".

Uncertain matches are marked "check" with the reason. Matching works straight after the
scan, while the disc rips.

**Picking an episode by hand.** Each title's episode is a button that opens a search box
over every episode of the show.

- Each typed word must appear in the name.
- "s3e8", "3x8" or "8" find an episode by number.
- The season buttons narrow the list.
- The title's current match and any episode its note names come first, with the reason.
- The disc's own season is listed as **Free** first, then **Taken**, each taken episode
  saying where it is: another title of this disc, another disc of the season, or the
  library. A taken episode can still be picked; it then shows as a problem straight away.
- The mouse wheel never changes a pick.

**The same episode twice.** After any match (TheDiscDB, the case, title cards, running
time or a pick by hand), Disc Shelf looks for an episode held by two titles of the disc,
including a two-part title that overlaps another. It also looks across the season:

- the other discs of the same show and season (by TVmaze show, or by name and year), from
  the jobs in `~/.local/state/disc-shelf/jobs/`. A re-rip of a disc (the same volume label,
  or the same disc number) replaces the earlier rip rather than clashing with it;
- the files already in the season folder of the library, by the `S01E20` in their names.
  The folder is only listed, never changed.

Each title involved is marked "check" with a sentence naming the other ("Title 26 is also
S01E20 “Markets”."), and it isn't copied, even during a rip, until the problem is settled.
A title already on the NAS keeps its match. The red box at the top of the Match page lists
each problem; **Compare** shows the titles side by side with their length, playlist, size,
what their title cards read and where they are now, and offers:

- **This is …**: the title keeps the episode, and the other is skipped until you pick its
  episode.
- **Keep as a second version**: both keep the episode, and this file gets " - Short cut"
  (when it's more than 5 seconds shorter) or " - Version 2" in its name, so Plex offers the
  two under Play Version.
- **Save as an extra**: it goes to `Featurettes` as "<episode> (short cut)".
- **Pick another episode** opens the title's picker.

**Read both title cards** reads just those titles. A card is judged on its own here,
since two titles showing the same name would otherwise be taken for the show's own text.
When both read the same name and one is shorter, the panel says the shorter one is
probably a second cut.

**The season grid** below the matches has a cell per episode of the season: on this disc
(T and the title number), on another disc (D and its number), in the library (Lib), on more
than one title (×2), or no title yet (a dashed outline). Hover a cell to see where it is;
click a ×2 cell to compare its titles.

**Watch.** The ▶ button on each title opens it in a video player: the ripped file, or its
checked copy on the NAS once the local file is gone. It's available once the title is
ripped. When the title card was read, you choose between starting at the card or from the
beginning. mpv and VLC can start at the card; Celluloid and the desktop's default player
start from the beginning. Inside the Flatpak, mpv or VLC on the host is started with
`flatpak-spawn --host`; without either, the desktop's default player opens the file.

## Copy to your library

- **Each title is copied as soon as it's ripped**, with "Copy each title as soon as it's
  ripped" on (the default), as long as its match is ready. A "check" match waits for you,
  and a title already copied has its match locked.
- **A film is copied while it rips.** A film is one title, so with "Copy films while they
  rip" on, it's copied as MakeMKV writes it, in 64 MB chunks that are read back and checked
  as they land. That needs the HDR setting at RAW and MakeMKV choosing the languages
  during the rip, through a per-run profile file; your own MakeMKV settings aren't touched.
- **Copies are checked.** Copies use rsync, which resumes after a network drop. Each file
  is read back through the mount and its checksum compared with the original. Then Plex is
  asked to scan just that folder.
- **Speed and time left** show in the progress text, with graphs on the Rip and Copy
  screens and progress on the task-manager icon.
- **Plex sign-in** uses Plex's PIN flow. The token is stored in
  `~/.config/disc-shelf/secrets.json`, readable only by you.

## File names

Settings → File names. Every choice is a name Plex reads, because Plex matches an episode
by the show folder, the season and the `S01E01` code, and ignores the rest of the name.

- **Episodes**: `Show (Year)/Season 01/Show (Year) - S01E01 - Episode name.mkv` by default.
  The other choices are the code alone (`… - S01E01.mkv`), or the name followed by the
  quality of the video (`… - S01E01 - Episode name [1080p].mkv`).
- **Show and season folders**: the year can be left out of the show folder, and the season
  folder can be `Season 1` instead of `Season 01`.
- **Two episodes in one title** are `S01E01-E02`. The name is the shared name of a two-part
  episode ("Encounter at Farpoint" for parts 1 and 2), or both names joined with "&".
- **A second cut of an episode** keeps the episode's code and name and adds the version,
  for example `Bluey (2018) - S01E20 - Markets - Short cut.mkv`. Plex lists the two files
  as one episode and offers both under Play Version.
- **Films**: `Aliens (1986)/Aliens (1986).mkv`, or with an edition the way Plex reads it,
  `Aliens (1986) {edition-Special Edition}.mkv`, optionally followed by the quality
  (`[2160p]`). Every edition and version of a film sits in the one folder.
- **Characters a Windows share can't store** are replaced by default: a colon becomes
  " - ", and `? * " < > |` are removed. Turned off, only `/` is removed.

A show that already has a folder in the destination keeps it: a new rip of "Bluey" goes
into an existing `Bluey` or `bluey (2018)` folder, named `Bluey - S01E01 - …` to match,
rather than starting a second `Bluey (2018)` folder.

Two titles matched to the same episode with no version get " (2)" added to the second
name, so neither is overwritten. That's rare now that versions are named.

## Library

The Library page checks the files already in a TV destination and renames them to the
file-name scheme. Films aren't checked yet.

- **Checking a show** lists its folder (subfolders too, so release folders work) and reads
  each video's episode code from its name: `S01E02`, `S01 E02`, `S01E02-E03`, `1x02`, and a
  bare `E02` inside a season folder. A file with no code is matched by its name when exactly
  one episode on TVmaze has that name, which covers libraries numbered 001, 002 and so on.
  The show is found on TVmaze from the folder's name and year; when that's ambiguous, the
  page says so and lets you pick the show.
- **It's read-only.** Nothing changes until you choose Rename. On a network share only the
  folders are listed; sizes are read only for files that share an episode.
- **What it reports**: each episode of the seasons present as a square (a title card read
  for it, no card read, two files, no file, or not on TVmaze); files that hold the same
  episode; files with no episode code or one TVmaze doesn't know; and files in the wrong
  season folder. When Disc Shelf ripped a file and read its title card, the page says what
  the card read.
- **Two files of one episode**: keep the second as another version (the default), move it
  to the show's `Featurettes` folder as an extra, or set it aside in a `Set aside` folder in
  the show's folder, with a `.plexignore` so Plex skips it. Nothing is deleted.
- **Show folders keep their names** unless you tick "Rename show folders too", because a
  new folder name makes Plex match the whole show again and leaves files that aren't
  videos behind. The folder's name starts each file name inside it; a release folder's
  files take the show's name and year from TVmaze instead. Season folders and file names
  always follow the scheme.
- **What changes** lists every old name and its new one. The scheme can be changed for one
  rename without changing Settings, and the rename can cover one show or every show in the
  destination. Shows whose TVmaze match needs a decision wait.
- **Renaming** moves files within the share (no copying), takes subtitles, `.nfo` and
  `-thumb.jpg` files with the same name along, makes folders as needed and removes folders
  left empty. It refuses to start if a new name is already taken by another file.
- **Undo last rename** puts the old names back. Each rename is logged in
  `~/.local/state/disc-shelf/library-undo/`.
- **Plex** is asked to rescan the show's folder afterwards when you're signed in. Watched
  status belongs to the episode, so it survives the new names.

## Queue

The Queue lists discs by what they need rather than by date.

- **Needs you**:
  - Titles that share an episode with another title, with a button to compare them on the
    Match page.
  - A title whose match needs a check. What its title card read and the likely episodes
    are shown, and confirming one copies it straight away, even mid-rip.
  - Titles matched but not copied yet.
  - A failed copy, with what happened and a Retry button.
- **Working now**: what's ripping, copying or encoding.
- **Seasons in progress**: for a season ripped across several discs, each episode's state
  and which disc to insert next.
- **Done**: finished discs, for 24 hours. They then live only in History.

The footer shows how much of the local rip folder is already safely copied, with a button
to delete it now.

## AV1 versions of 4K discs

Settings → AV1 versions.

- **When it runs.** Once a 4K title's remux has been copied and checked, Disc Shelf
  encodes a smaller AV1 copy from the local remux, at low priority.
- **Two encoders.** SVT-AV1 runs on the processor and gives the best quality; NVENC runs
  on an Nvidia graphics card and is fast.
- **A quality check.** The AV1 copy is scored against the remux on 6 clips with VMAF. A
  file scoring under 93 isn't copied, and the disc shows under "Needs you" with the score.
- **Where it goes.** It's copied next to the remux as `… - 2160p AV1.mkv`, so Plex lists
  the two as versions of one film or episode. The remux is never changed and reaches Plex
  first.
- **HDR.** HDR10 is kept in the AV1 copy; Dolby Vision stays in the remux only.
- **One at a time.** One encode runs at a time, in the order queued. "When to encode" can
  hold encodes back while a disc rips, or until 1 am to 7 am.
- **The local remux is kept** until its AV1 version is copied, stopped or given up.
- **After a restart**, an encode that was running starts again from the beginning; a file
  that was already encoded is only checked and copied.
- **Estimates learn.** Encode times and sizes are recorded, and the estimates in Settings
  use them.

Many TV boxes, including the Nvidia Shield and Apple TVs before 2026, can't decode AV1.
Plex keeps playing them the remux.

## Disc info

- **What it shows.** The disc's formats: its own title and cover art from `BDMV/META`,
  video, audio and subtitle details per title, and highlights such as 4K UHD, HDR10 and
  Dolby Atmos.
- **How it reads the disc.** The disc is mounted read-only with udisksctl and unmounted
  again.
- **DVDs aren't mounted.** They carry no artwork, and some USB drives hang reading a DVD's
  file system. A TV disc without artwork gets the show's (or season's) poster from TVmaze.

## Where things live

- **Settings**: `~/.config/disc-shelf/config.json`.
- **Jobs**: one JSON file each in `~/.local/state/disc-shelf/jobs/`, so a crash loses
  nothing.
- **Local rips**: `~/rips` by default.
- **Clean-up**: the local clean-up setting runs each time the app opens. It deletes a local
  rip only once its copy was checked and is still there at the same size.
- **Lost titles**: when the app opens, it puts back any ripped title a job lost track of.
- **Cover art**: cached in `~/.local/state/disc-shelf/covers/`.
- **AV1 encode measurements**: `~/.local/state/disc-shelf/av1_stats.json`.
- **Library rename logs**: `~/.local/state/disc-shelf/library-undo/`, one per rename.

## Command line

```bash
disc-shelf              # the window
disc-shelf --fake       # the window with sample discs, no drive needed
disc-shelf drives       # drives MakeMKV can see
disc-shelf scan 0       # titles on the disc in drive 0
disc-shelf cleanup      # local rips the clean-up setting says are due (--apply deletes them)
```
