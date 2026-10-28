# How Disc Shelf works

This walks through each screen and the decisions Disc Shelf makes for you.

## Rip

- **A disc is scanned as soon as it's in the drive.** Disc Shelf asks the drive for its
  status every 3 seconds with the kernel's CD-ROM status request, which doesn't start
  MakeMKV or spin the disc up.
- **While it scans**, the Rip page shows a clock and each step with its time: opening the
  disc, LibreDrive, getting the disc's keys from MakeMKV's server, and reading the titles.
  MakeMKV's output is read as it arrives.
  - A 4K disc's first scan can take several minutes. MakeMKV looks up the disc's keys on
    its own server, about one request a second, and the drive is idle meanwhile. Disc Shelf
    counts those requests by reading MakeMKV's connections from `/proc`, and draws them per
    10 seconds. Inside Disc Shelf's Flatpak, MakeMKV runs on the host and its connections
    can't be seen, so the key step isn't shown there.
  - There's no time limit. After 3 minutes with no output and no server requests (10
    minutes when the requests can't be seen), the panel asks whether to stop and scan
    again, keep waiting, or eject. Stop scan is there the whole time.
  - The disc's name gives a guess at the film or show, and the destination folder is checked for a folder
    with that name, while MakeMKV works.
  - "When the scan finishes" can rip a film's main title straight away: the one title over
    60 minutes, when there's exactly one. A desktop notification says the disc is ready,
    or that it's ripping, when the window isn't in front.
  - How long each kind of disc took is kept, and the clock shows the usual times.
- **Short titles are left out.** MakeMKV scans at a 2-minute minimum title length, and the
  rip uses the same value, because MakeMKV numbers titles after dropping short ones.
- **Each title gets a type**: episode, film, play-all, duplicate, extra, or short (under
  5 minutes). Episodes or the film are ticked for ripping.
  - Episodes can be short: a run of 3 or more similar titles over 5 minutes counts, so
    11-minute cartoons work.
  - DVDs number each title's segments from 1, so on a DVD, play-alls and duplicates are
    found by length and size instead.
- **Titles that play the same film are grouped**, and only one is ticked. A disc can hold:
  - the same video with different languages, one playlist per country it's sold in;
  - a longer cut that plays every piece of the shorter one, plus scenes only it has;
  - copies that play the same pieces in different orders, added to confuse copy programs.

  The one with the most tracks in your languages is ticked. The others are labelled by what
  only they have, for example "Japanese audio". Under the title list, a panel compares them
  (languages side by side, or each title's pieces as a bar) and says why one was picked.
  "Rip … instead" swaps the pick. For shuffled copies, the pick is the one that plays its
  pieces in the order they're stored on the disc, if exactly one does; that's a guess, so
  watch the start of it.
- **Video choices that don't apply are greyed out** with a tooltip saying why: no 4K video,
  no Dolby Vision once the rip has been checked, or the rip is running.
- **Audio and subtitles** default to English; change the defaults in Settings.
- **HDR** defaults to RAW, which keeps every HDR format and converts nothing. The main
  file's badge says RAW, Compressed (Lossless) or Compressed (Lossy), and lists each
  setting that would change the HDR from the disc. Converting Dolby Vision to profile 8.1
  is lossless on a disc whose enhancement layer is minimal (MEL) and lossy on one with a
  full layer (FEL); until a rip has shown which, it counts as lossy. Keeping HDR10 only is
  always lossy. When an AV1 version is made, it gets its own Compressed (Lossy) badge.
- **The next disc of a season starts filled in.** When a disc's show name (or its label
  before `_S` or `_D`) matches exactly one season in progress, the new disc gets that
  season's show, year, library and the next disc number. Whatever the label itself says
  about season or disc wins.
- **After ripping**, mkvmerge keeps only the chosen languages. It matches them by
  language, since MakeMKV's track numbers don't match the disc's. ffprobe reads the HDR
  formats, and on a Dolby Vision profile 7 file dovi_tool works out whether it has a full
  or minimal enhancement layer. Only a setting that isn't RAW runs dovi_tool to change the file.
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
A title already copied to storage keeps its match.

**Fixing what's flagged.** Anything marked red or amber on the Match page opens the fix
panel: a ×2 or ? cell in the season grid, a row's Check or duplicate badge, the line at the
top of the page ("Fix it", then "Next one"), and the Queue's needs-you card. The panel says
what's wrong in a sentence, shows each side (length, audio languages, what its title card
read, where it is now) with Watch, and lists the fixes as choices that each say what will
happen, the suggested one marked. The button names the action ("Rename 1 file", "Confirm
the match"). Afterwards a green line says what was done, with Undo, and the panel moves on
to the next problem; ‹ and › step between them. Clicking anything that isn't flagged opens
the same panel as what's known about it: where the episode is, how it was matched, and
Watch. In the grid the arrow keys move between cells and Enter opens one.

The fixes depend on what's wrong:

- **Two titles of this disc on one episode.** Keep one (the other is skipped until you pick
  its episode), keep both as versions (the file gets its language, " - French", when it has
  no audio in the disc's main language and the other does, else " - Short cut" when it's
  more than 5 seconds shorter, else " - Version 2"; Plex offers them under Play Version),
  save one as an extra in `Featurettes` as "<episode> (French)" or "<episode> (short
  cut)", skip it, read their title cards first, or pick another episode.
- **Two files already in storage for one episode**, from a disc copied before duplicates
  were checked (both from another disc of the season, or this disc's own copied titles):
  keep the odd one (a dub, then the shorter, then the " (2)" copy) as the French version or
  a second version, move it to `Featurettes`, set it aside in "<show>/Set aside/" (which
  holds a `.plexignore`, so Plex skips it; nothing is deleted), delete it with its subtitles
  (after a confirmation, with no Undo; the disc's records mark the title as skipped), or swap
  the two so the other becomes the version. The panel lists the old and new names, and can ask Plex to rescan the
  season folder. The renames go through the Library page's rename, so subtitles and other
  sidecars move with the file and nothing is overwritten. The job that copied the file is
  updated too (its file name, version, or extra), so the season check stops flagging it and
  History stays right. Undo puts back both the names and the job's records. These undo logs
  are kept apart from the Library page's, so its "Undo last rename" never undoes one of
  these without its records.
- **A match that needs a check** (a running-time guess, an unclear title card): confirm
  it, read its title card first, pick another episode, or skip the title. Reading reads
  just that title; when the card names the matched episode, Confirm says "(its card
  agrees)" and is chosen for you.

A card read for comparing is judged on its own, since two titles showing the same name
would otherwise be taken for the show's own text. The disc's main language is the first
audio language you keep, or else the one most titles play first.

**The season grid** below the matches has a cell per episode of the season: on this disc
(T and the title number), on another disc (D and its number), in the library (Lib), on more
than one title or file (×2), a match that needs a check (? and the title), or no title yet
(a dashed outline). Hover a cell to see where it is. A disc with no number of its own is
numbered by the order the season's discs were ripped, and named by its label in sentences
("disc 1, First Half").

**Watch.** The ▶ button on each title opens it in a video player: the ripped file, or its
checked copy in storage once the local file is gone. It's available once the title is
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
  is read back through the mount and its checksum compared with the original. Then the
  media server, if you chose one, is asked to scan just that folder.
- **Speed and time left** show in the progress text, with graphs on the Rip and Copy
  screens and progress on the task-manager icon.
- **One MakeMKV run for the whole disc.** Each time MakeMKV starts, it opens the disc
  before writing anything, which takes about 40 s on a Blu-ray. So when the titles you
  didn't pick are small (short clips, not a play-all title that repeats the whole disc),
  the disc is ripped in one run: MakeMKV writes every title at least as long as the scan's
  minimum, one after another, and the ones you didn't pick are deleted as each finishes.
  Each picked title is handed on for its languages, matching and copy as soon as MakeMKV
  starts the next one. When ripping the unpicked titles would take longer than opening the
  disc for every title, each title gets its own run instead. To always use one run per
  title, set `"rip_in_one_run": false` in `~/.config/disc-shelf/config.json`.
- **The rip timeline** on a disc's card shows each title in turn. Opening the disc is
  drawn as a hatched band with no speed line. The reading speed is averaged over 20 s,
  because MakeMKV writes in 32 MB blocks a few seconds apart. Below it, rows show when each
  title had its languages kept and when it was copied to storage. A title read below 75% of
  the speed of the titles around it gets a red marker. Click a title for its times.
- **The time left** is worked out per title from the disc's own titles so far: the average
  time to open the disc, the speed while reading over the last 5 titles, and the sizes
  MakeMKV actually writes, which on Blu-rays are often about half its own estimate. The
  titles still to rip are drawn dashed after "now", up to when the rip should end.
- **The media server** is Plex, Jellyfin or none (Settings → Media server). Plex sign-in
  uses Plex's PIN flow; Jellyfin uses an API key made under Dashboard → API Keys. The token
  or key is stored in `~/.config/disc-shelf/secrets.json`, readable only by you. Jellyfin is
  asked to scan one folder through `POST /Library/Media/Updated`. With no media server, a
  disc is done once its files are copied and checked, and the Library page can't ask for a
  rescan.

## File names

Settings → File names. Every choice is a name Plex and Jellyfin read, because both match an
episode by the show folder, the season and the `S01E01` code, and ignore the rest of the name.

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
  (`[2160p]`). The `{edition-…}` tag is Plex's own; Jellyfin would list two tagged files as
  two films. With Jellyfin, or no media server, an edition is named as a version instead,
  `Aliens (1986) - Special Edition.mkv`, which Jellyfin lists under the one film.
  Every edition and version of a film sits in the one folder.
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
- **The media server** is asked to rescan the show's folder afterwards when it's connected. Watched
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
- **Where it goes.** It's copied next to the remux as `… - 2160p AV1.mkv`, so Plex and
  Jellyfin list the two as versions of one film or episode. The remux is never changed and
  reaches the library first.
- **HDR.** HDR10 is kept in the AV1 copy; Dolby Vision stays in the remux only.
- **One at a time.** One encode runs at a time, in the order queued. "When to encode" can
  hold encodes back while a disc rips, or until 1 am to 7 am.
- **The local remux is kept** until its AV1 version is copied, stopped or given up.
- **After a restart**, an encode that was running starts again from the beginning; a file
  that was already encoded is only checked and copied.
- **Estimates learn.** Encode times and sizes are recorded, and the estimates in Settings
  use them.

Many TV boxes, including the Nvidia Shield and Apple TVs before 2026, can't decode AV1.
Plex keeps playing them the remux. Jellyfin lists both versions and the viewer picks one
when they start playing.

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
- **Scan times**: `~/.local/state/disc-shelf/scan-times.json`, the last 20 of each kind of disc.
- **Library rename logs**: `~/.local/state/disc-shelf/library-undo/`, one per rename.

## Command line

```bash
disc-shelf              # the window
disc-shelf --fake       # the window with sample discs, no drive needed
disc-shelf drives       # drives MakeMKV can see
disc-shelf scan 0       # titles on the disc in drive 0
disc-shelf cleanup      # local rips the clean-up setting says are due (--apply deletes them)
```
