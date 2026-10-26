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
   the first 90 seconds of each title are read with OCR as soon as it's ripped. Text that
   appears on several titles, such as a theme song's captions, is ignored. A running-time
   guess waits for its title's card before it's copied. For a disc ripped earlier,
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
- An episode another title already uses says which title.
- The mouse wheel never changes a pick.

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

## Queue

The Queue lists discs by what they need rather than by date.

- **Needs you**:
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

## Command line

```bash
disc-shelf              # the window
disc-shelf --fake       # the window with sample discs, no drive needed
disc-shelf drives       # drives MakeMKV can see
disc-shelf scan 0       # titles on the disc in drive 0
disc-shelf cleanup      # local rips the clean-up setting says are due (--apply deletes them)
```
