"""A Controller with sample discs and simulated progress, for screenshots and for running the window without a drive.

    python3 -m discshelf.gui --fake

The drive holds one of three sample discs; Eject moves on to the next one (a 4K UHD
Severance season 1 disc, The Office season 3 disc 2, and the film Pitch Perfect).
Nothing touches the disk, the network or Plex.

The queue also has two 4K films with AV1 versions: Dune: Part Two is encoding (it moves
on whenever the queue is read, unless delay is 0 or hold is set), and Blade Runner 2049
failed its quality check. A 4K title copied in a simulated rip or copy queues one too.
"""
from __future__ import annotations

import atexit
import copy
import datetime as dt
import math
import os
import shutil
import struct
import tempfile
import threading
import zlib
import time
from types import SimpleNamespace

from . import av1, season
from .play import Player
from .config import Config, sample_destinations
from .discinfo import AudioInfo, DiscReport, SubtitleInfo, TitleInfo, VideoInfo
from .match import feature_match, match_titles, match_list, parse_episode_list
from .models import Disc, Drive, EncodeJob, Episode, HdrInfo, Job, Match, RippedFile, Sample, Stream, Title, TitleProgress
from .naming import EXTRAS_FOLDERS, Scheme, dedupe, relpath_for, season_folder, show_folder
from .pipeline import (ENCODE_ACTIVE, CardStatus, EncodeStatus, QueueView, QueueViews, _enc, _encode_holds,
                       _video_width)

MODEL = "BD-RE PIONEER BD-RW BDR-XD08U 1.01"


def _now(minutes_ago: int = 0) -> str:
    t = dt.datetime.now().astimezone() - dt.timedelta(minutes=minutes_ago)
    return t.isoformat(timespec="seconds")


def _secs(text: str) -> int:
    parts = [int(p) for p in text.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, s = parts
    return h * 3600 + m * 60 + s


def _uhd_streams() -> list[Stream]:
    return [
        Stream(0, "video", "MpegH", "HEVC (H.265)", resolution="3840x2160"),
        Stream(1, "audio", "TrueHD", "Dolby TrueHD Atmos", "eng", "English", "Surround 7.1", "7.1"),
        Stream(2, "audio", "AC3", "Dolby Digital", "eng", "English", "Surround 5.1", "5.1"),
        Stream(3, "audio", "EAC3", "Dolby Digital Plus", "spa", "Spanish", "Surround 5.1", "5.1"),
        Stream(4, "audio", "AC3", "Dolby Digital", "eng", "English", "Stereo", "2.0", flags="descriptive"),
        Stream(5, "subtitle", "PGS", "HDMV PGS", "eng", "English"),
        Stream(6, "subtitle", "PGS", "HDMV PGS", "eng", "English", "SDH", flags="sdh"),
        Stream(7, "subtitle", "PGS", "HDMV PGS", "spa", "Spanish"),
    ]


def _bd_streams() -> list[Stream]:
    return [
        Stream(0, "video", "Mpeg4", "AVC (H.264)", resolution="1920x1080"),
        Stream(1, "audio", "DTSHDMA", "DTS-HD Master Audio", "eng", "English", "Surround 5.1", "5.1"),
        Stream(2, "audio", "AC3", "Dolby Digital", "eng", "English", "Stereo", "2.0", flags="commentary"),
        Stream(3, "audio", "AC3", "Dolby Digital", "fra", "French", "Stereo", "2.0"),
        Stream(4, "subtitle", "PGS", "HDMV PGS", "eng", "English"),
        Stream(5, "subtitle", "PGS", "HDMV PGS", "fra", "French"),
    ]


def _titles(rows, streams) -> list[Title]:
    out = []
    for i, (src, length, gb, ch, kind, sel) in enumerate(rows):
        out.append(Title(index=i, source=src, duration_s=_secs(length), size_bytes=int(gb * 1e9), chapters=ch,
                         segments=list(range(ch)), streams=copy.deepcopy(streams), kind=kind, selected=sel))
    return out


def _sample_discs(drive: Drive) -> list[tuple[Disc, dict]]:
    sev = Disc(drive, "SEVERANCE_S1_D1", "Severance", "Blu-ray disc", uhd=True, libredrive=True,
               size_bytes=int(81.7e9), titles=_titles([
                   ("00800.mpls", "57:04", 17.6, 8, "episode", True),
                   ("00801.mpls", "54:31", 16.8, 8, "episode", True),
                   ("00802.mpls", "41:12", 12.7, 7, "episode", True),
                   ("00803.mpls", "54:56", 16.9, 8, "episode", True),
                   ("00315.mpls", "3:27:43", 64.0, 31, "play_all", False),
                   ("00024.m2ts", "18:20", 3.1, 1, "extra", False),
               ], _uhd_streams()))
    office = Disc(drive, "THE_OFFICE_S3_D2", "The Office", "Blu-ray disc", libredrive=True,
                  size_bytes=int(23.9e9), titles=_titles([
                      ("00012.mpls", "21:44", 4.1, 6, "episode", True),
                      ("00013.mpls", "21:58", 4.2, 6, "episode", True),
                      ("00011.mpls", "21:30", 4.0, 6, "episode", True),
                      ("00014.mpls", "43:10", 7.9, 11, "episode", True),
                      ("00030.m2ts", "16:02", 2.4, 1, "extra", True),
                  ], _bd_streams()))
    pitch = Disc(drive, "PITCH_PERFECT", "Pitch Perfect", "Blu-ray disc", libredrive=True,
                 size_bytes=int(31.2e9), titles=_titles([
                     ("00001.mpls", "1:52:16", 29.4, 24, "feature", True),
                     ("00045.m2ts", "12:30", 1.4, 1, "extra", False),
                     ("00046.m2ts", "0:31", 0.1, 1, "short", False),
                 ], _bd_streams()))
    return [
        (sev, dict(media_kind="tv", show="Severance", season=1, disc_number=1)),
        (office, dict(media_kind="tv", show="The Office (US)", season=3, disc_number=2)),
        (pitch, dict(media_kind="movie", show="Pitch Perfect", year=2012)),
    ]


SHOWS = [
    SimpleNamespace(id=44933, name="Severance", year=2022, network="Apple TV+"),
    SimpleNamespace(id=526, name="The Office (US)", year=2005, network="NBC"),
    SimpleNamespace(id=527, name="The Office", year=2001, network="BBC Two"),
    SimpleNamespace(id=41821, name="Bluey", year=2018, network="ABC Kids"),
]

# A season of 7-minute cartoons on two discs, each with one title more than it has episodes:
# the extra title is an episode dubbed in French, a little shorter, which another title also
# holds in English, so two titles end up matched to the same episode. (episode number, playlist, length) per title.
BLUEY_D1 = [(25, "00029", 441), (24, "00028", 441), (23, "00027", 441), (22, "00026", 441), (21, "00025", 441),
            (20, "00024", 441), (19, "00023", 441), (17, "00021", 441), (16, "00019", 441), (15, "00018", 441),
            (14, "00017", 441), (13, "00016", 441), (12, "00015", 441), (11, "00014", 441), (10, "00013", 441),
            (9, "00012", 441), (8, "00011", 441), (7, "00009", 441), (18, "00022", 441), (6, "00008", 441),
            (5, "00007", 441), (4, "00006", 440), (3, "00004", 441), (2, "00003", 441), (1, "00002", 441),
            (20, "00034", 431), (26, "00032", 441)]
BLUEY_D2 = [(51, "00029", 441), (52, "00032", 441), (50, "00028", 441), (49, "00027", 441), (48, "00026", 441),
            (47, "00025", 441), (46, "00024", 441), (45, "00023", 441), (43, "00021", 441), (42, "00019", 441),
            (41, "00018", 441), (40, "00017", 441), (39, "00016", 441), (38, "00015", 441), (37, "00014", 441),
            (36, "00013", 441), (35, "00012", 441), (34, "00011", 441), (33, "00009", 441), (44, "00022", 441),
            (32, "00008", 441), (31, "00007", 441), (30, "00006", 441), (29, "00004", 441), (28, "00003", 441),
            (27, "00002", 441), (51, "00034", 434)]
# Where each title's card is on screen, seconds in, when the sample reads it.
BLUEY_CARD_AT = {1: 74, 27: 52}

EPISODES = {
    (44933, 1): [(1, "Good News About Hell", 57), (2, "Half Loop", 54), (3, "In Perpetuity", 52),
                 (4, "The You You Are", 55), (5, "The Grim Barbarity of Optics and Design", 50),
                 (6, "Hide and Seek", 47), (7, "Defiant Jazz", 51), (8, "What's for Dinner?", 50),
                 (9, "The We We Are", 40)],
    (41821, 1): [(n + 1, name, 7) for n, name in enumerate((
        "Magic Xylophone", "Hospital", "Keepy Uppy", "Daddy Robot", "Shadowlands", "The Weekend", "BBQ",
        "Fruitbat", "Horsey Ride", "Hotel", "Bike", "Bob Bilby", "Spygame", "Takeaway", "Butterflies",
        "Yoga Ball", "Calypso", "The Doctor", "The Claw", "Markets", "Blue Mountains", "The Pool", "Shops",
        "Wagon Ride", "Taxi", "The Beach", "Pirates", "Grannies", "The Creek", "Fairies", "Work",
        "Bumpy and the Wise Old Wolfhound", "Trampoline", "The Dump", "Zoo", "Backpackers", "The Adventure",
        "Copycat", "The Sleepover", "Early Baby", "Mums and Dads", "Hide and Seek", "Camping",
        "Mount Mumandad", "Kids", "Chickenrat", "Neighbours", "Teasing", "Asparagus", "Shaun",
        "Daddy Putdown", "Verandah Santa"))],
    (526, 3): [(1, "Gay Witch Hunt", 22), (2, "The Convention", 22), (3, "The Coup", 22),
               (4, "Grief Counseling", 22), (5, "Initiation", 22), (6, "Diwali", 22), (7, "Branch Closing", 22),
               (8, "The Merger", 22), (9, "The Convict", 27), (10, "A Benihana Christmas", 42),
               (11, "Back from Vacation", 22), (12, "Traveling Salesmen", 22)],
}
BLUEY_S1 = [name for _, name, _ in EPISODES[(41821, 1)]]


def _sample_library(root: str) -> None:
    """Empty files standing in for a TV library that needs tidying, for the Library page:
    Bluey named the old way (no episode names) with a second Markets from the "(2)" dedupe,
    a stray clip and a misplaced episode; Severance already named the Plex way; and a
    release folder of The Office."""
    files = [f"Bluey (2018)/Season 01/Bluey (2018) - S01E{n:02d}.mkv" for n in range(1, 27) if n != 24]
    files += ["Bluey (2018)/Season 01/Bluey (2018) - S01E20 (2).mkv",
              "Bluey (2018)/Season 01/Bluey (2018) - S01E01.en.srt",
              "Bluey (2018)/Season 02/Bluey (2018) - S01E24.mkv",
              "Bluey (2018)/Season 01/bonus clip.mkv",
              "Bluey (2018)/Featurettes/Making Bluey.mkv"]
    files += [f"Severance (2022)/Season 01/Severance (2022) - S01E{n:02d} - {t}.mkv"
              for n, t, _rt in EPISODES[(44933, 1)]]
    files += [f"The.Office.S03.720p.WEB/The.Office.S03E{n:02d}.720p.WEB.x264.mkv" for n in range(1, 13)]
    for rel in files:
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"\0" * (900 if "(2)" in rel else 1000))   # the second Markets is the shorter one


RIP_TICK = 10.0      # simulated seconds per step while ripping (one sample per step)
ENCODE_STEPS = 400   # a whole simulated encode takes this many delays (24 s at the window's 0.06)
COPY_TICK = 2.0      # simulated seconds per step during a copy-only run
TAIL = 512_000_000   # a film copied while it rips stays this far behind the rip


def _png(path: str, w: int, h: int, top: tuple, bottom: tuple, band: tuple) -> None:
    """Write a plain RGB PNG: a vertical gradient with a lighter band, standing in for cover art."""
    rows = []
    for y in range(h):
        f = y / max(1, h - 1)
        c = bytes(int(a + (b - a) * f) for a, b in zip(top, bottom))
        line = bytearray()
        for x in range(w):
            inside = 0.55 * h < y < 0.68 * h and 0.12 * w < x < 0.88 * w
            line += bytes(band) if inside else c
        rows.append(b"\x00" + bytes(line))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                 + chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


# label: (width, height, top colour, bottom colour, band colour, source). A disc not listed has no cover.
COVERS = {
    "SEVERANCE_S1_D1": (320, 180, (22, 58, 70), (8, 20, 26), (214, 226, 230), "disc"),      # the disc's own artwork
    "THE_OFFICE_S3_D2": (210, 300, (196, 150, 92), (70, 52, 34), (240, 236, 226), "TVmaze"),  # a show poster
}


def _left(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


class FakeController(QueueViews, season.SeasonChecks):
    def __init__(self, cfg: Config | None = None, delay: float = 0.06):
        self.cfg = cfg or Config()
        if not self.cfg.destinations:
            # Sample mode needs somewhere to "copy" to; these folders are never created.
            self.cfg.destinations = sample_destinations("/srv/media")
        if not self.cfg.plex_url:
            self.cfg.plex_url = "http://plex.local:32400"
        self.delay = delay
        self._lock = threading.Lock()
        self.job_lock = threading.RLock()
        self._cover_dir: str | None = None
        self.hold = threading.Event()       # for screenshots and tests: freeze the simulated rip and copy
        self._drive = Drive(0, "/dev/sr0", MODEL)
        self._discs = _sample_discs(self._drive)
        self._slot = 0                      # which sample disc is in the drive
        self._jobs: dict[str, Job] = {}
        self._n = 0
        self._signed_in = True
        self._polls = 0
        self.saved_config: Config | None = None
        self._card_state: dict[str, dict[int, CardStatus]] = {}
        self._pretend: set[str] = set()     # sample jobs the queue shows as ripping, though nothing runs
        self.ejected = 0
        self.played: list[tuple[str, float]] = []   # what Watch opened, for tests
        self._enc_phase: dict[tuple[str, int], float] = {}   # (job id, title) -> when its check or copy began
        self._enc_tick = time.monotonic()
        self._library_root: str | None = None
        self.plex_scans: list[tuple[str, str]] = []     # (library, folder) the Library page asked Plex to scan
        self._seed()

    # -- sample history -----------------------------------------------------
    def _seed(self):
        """The queue's sample: The Office S3 disc 2 waits for 2 checks, disc 1 has a failed copy,
        Severance S1 disc 1 is "ripping", and Pitch Perfect reached Plex 2 minutes ago."""
        office_disc, office_meta = self._discs[1]
        office = self._job_for(office_disc, office_meta, created=_now(95))
        office.show_id = 526
        for t in office.titles:
            office.files.append(RippedFile(t.index, f"{self.cfg.rip_path}/The Office S3D2/title_t{t.index:02d}.mkv",
                                           t.size_bytes, state="ripped"))
        office.matches = self.suggest_matches(office, 526)
        eps = {n: Episode(3, n, title, rt) for n, title, rt in EPISODES[(526, 3)]}
        for m in office.matches:
            if m.target == "extra":
                m.extra_name, m.extra_folder = "Deleted Scenes", "Featurettes"
            elif m.title_index == 1:
                m.episode, m.confidence, m.source = eps[8], "check", "card"
                m.note = "The title card reads “THE MERGFR”, which is closest to “The Merger”."
            elif m.title_index == 2:
                m.episode, m.confidence, m.source = eps[9], "check", "runtime"
                m.note = ("Title is 21 min but the episode is listed as 27 min. Order unclear: the running "
                          "time also fits episode 11, “Back from Vacation”.")
            else:
                m.confidence = "close"
        self._card_state[office.id] = {1: CardStatus("read", text="THE MERGFR", seconds=14.2),
                                       2: CardStatus("read", text="CONVICT", seconds=15.0)}
        office.stage, office.progress = "match", 1.0
        office.status = "Ripped. 2 titles wait for you to check their matches."

        pitch_disc, pitch_meta = self._discs[2]
        pitch = self._job_for(pitch_disc, pitch_meta, created=_now(300))
        pitch.files = [RippedFile(0, f"{self.cfg.rip_path}/Pitch Perfect/title_t00.mkv",
                                  pitch.titles[0].size_bytes, sha256="0" * 64, state="verified")]
        pitch.matches = feature_match(pitch.titles)
        self.plan_paths(pitch)
        for f in pitch.files:
            f.state = "verified"
        pitch.stage, pitch.progress = "done", 1.0
        pitch.status = "Copied to Movies and checked. Plex added it 2 min ago."
        pitch.copied_at = pitch.updated = _now(2)

        failed = Job(id=self._new_id(), disc_label="THE_OFFICE_S3_D1", media_kind="tv", show="The Office (US)",
                     show_id=526, year=2005, season=3, disc_number=1, destination=self.cfg.destination("tv").path,
                     library="TV Shows", stage="failed", created=_now(2900), updated=_now(2880),
                     status="Copy failed: the NAS stopped answering at 62%. The other 5 episodes are in Plex.")
        for i in range(6):
            ok = i < 5
            failed.files.append(RippedFile(
                i, f"{self.cfg.rip_path}/The Office S3D1/title_t{i:02d}.mkv", int(4.1e9),
                sha256="0" * 64 if ok else "", state="verified" if ok else "failed",
                dest_rel=f"The Office (US) (2005)/Season 03/The Office (US) (2005) - S03E0{i + 1}.mkv"))
            failed.matches.append(Match(i, "episode", eps[i + 1], confidence="close", source="runtime"))
            failed.titles_progress.append(TitleProgress(
                i, "verified" if ok else "failed", int(4.1e9), int(4.1e9), int(4.1e9) if ok else int(2.5e9),
                int(4.1e9) if ok else 0, note="" if ok else "Copy failed: the NAS stopped answering at 62%."))

        sev_disc, sev_meta = self._discs[0]
        sev = self._job_for(sev_disc, sev_meta, created=_now(25))
        sev.show_id, sev.year, sev.copy_mode, sev.stage = 44933, 2022, "per_title", "rip"
        sev.status = "Ripping title 2 of 4 · 29.0 MB/s · about 14 min left"
        sev.progress = 0.47
        picked = [t for t in sev.titles if t.selected]
        sev_eps = {n: Episode(1, n, title, rt) for n, title, rt in EPISODES[(44933, 1)]}
        for k, t in enumerate(picked):
            sev.matches.append(Match(t.index, "episode", sev_eps[k + 1], confidence="close", source="runtime"))
            tp = TitleProgress(t.index, total_bytes=t.size_bytes)
            if k == 0:
                tp.state = "verified"
                tp.ripped_bytes = tp.copied_bytes = tp.checked_bytes = t.size_bytes
                sev.files.append(RippedFile(t.index, f"{sev.rip_dir}/title_t{t.index:02d}.mkv", t.size_bytes,
                                            sha256="0" * 64, state="verified",
                                            dest_rel=f"Severance (2022)/Season 01/Severance (2022) - S01E01.mkv"))
            elif k == 1:
                tp.state, tp.ripped_bytes, tp.rip_rate = "ripping", int(0.41 * t.size_bytes), 29.0e6
            sev.titles_progress.append(tp)
        self._pretend.add(sev.id)
        for j in (failed, pitch, office, sev):
            self._jobs[j.id] = j
        self._seed_av1()
        self._seed_bluey()

    def _seed_bluey(self):
        """Bluey season 1 on two discs. Disc 1 went to the NAS 3 days ago with titles 6 and 26 both
        as “Markets” (so the NAS has a “S01E20 (2)”); disc 2 is ripped and matched, and titles 1
        and 27 are both “Daddy Putdown”, which holds both back."""
        tv = self.cfg.destination("tv")
        eps = {n: Episode(1, n, name, rt) for n, name, rt in EPISODES[(41821, 1)]}
        for disc, rows, created in ((1, BLUEY_D1, 4400), (2, BLUEY_D2, 180)):
            label = f"BLUEY_S1_D{disc}"
            job = Job(id=self._new_id(), disc_label=label, media_kind="tv", show="Bluey", show_id=41821,
                      year=2018, season=1, disc_number=disc, destination=tv.path if tv else "",
                      library=tv.library if tv else "", rip_dir=f"{self.cfg.rip_path}/{label}",
                      created=_now(created), updated=_now(created - 30))
            for k, (n, src, secs) in enumerate(rows):
                ti = k + 1
                size = int(secs * 2.4e6)
                # 00034 is the episode dubbed in French, with no English audio.
                streams = _bd_streams() if src != "00034" else \
                    [st for st in _bd_streams() if st.kind != "audio" or st.lang == "fra"]
                job.titles.append(Title(ti, f"{src}.m2ts", secs, size, 3, [int(src)], streams, "episode", True))
                done = disc == 1
                job.files.append(RippedFile(ti, f"{job.rip_dir}/title_t{ti:02d}.mkv", size,
                                            sha256="0" * 64 if done else "", state="verified" if done else "ripped",
                                            local_deleted=done))
                job.titles_progress.append(TitleProgress(ti, "verified" if done else "ripped", size, size,
                                                         size if done else 0, size if done else 0))
                job.matches.append(Match(ti, "episode", copy.deepcopy(eps[n]), confidence="close", source="discdb"))
            if disc == 1:
                self.plan_paths(job)
                job.stage, job.progress, job.copied_at = "done", 1.0, _now(created - 40)
                job.status = "Copied to TV Shows and checked. Plex added it."
            else:
                job.stage, job.progress = "match", 1.0
                job.status = "Ripped 27 titles. Match them to episodes next."
            self._jobs[job.id] = job
        with self.job_lock:
            self.apply_season(job)
    def _seed_av1(self):
        """Dune: Part Two's remux is in Plex and its AV1 version is 37% encoded; Blade Runner 2049's
        AV1 version scored 89 on the quality check, so it wasn't copied."""
        movies = self.cfg.destination("movie")
        for label, name, year, length, gb, created in (("DUNE_PART_TWO", "Dune: Part Two", 2024, "2:46:00", 61.8, 80),
                                                       ("BLADE_RUNNER_2049", "Blade Runner 2049", 2017, "2:43:47", 58.3, 600)):
            title = Title(0, "00800.mpls", _secs(length), int(gb * 1e9), 40, [0], _uhd_streams(), "feature", True)
            job = Job(id=self._new_id(), disc_label=label, media_kind="movie", show=name, year=year, version="2160p",
                      destination=movies.path if movies else "", library=movies.library if movies else "",
                      rip_dir=f"{self.cfg.rip_path}/{label}", titles=[title], stage="done", progress=1.0,
                      created=_now(created), updated=_now(created - 60), copied_at=_now(created - 70),
                      status="Copied to Movies and checked. Plex added it.")
            job.files = [RippedFile(0, f"{job.rip_dir}/title_t00.mkv", title.size_bytes, sha256="0" * 64,
                                    state="verified", hdr=HdrInfo("3840x2160", "hevc", hdr10=True, dolby_vision=True,
                                                                  dv_profile=7, dv_layer_kind="FEL"))]
            job.matches = feature_match(job.titles)
            self.plan_paths(job)
            f = job.files[0]
            rel = av1.av1_name(f.dest_rel)
            e = EncodeJob(0, encoder="cpu", quality=2, grain=True, src=f.path,
                          path=f"{job.rip_dir}/{os.path.basename(rel)}", dest_rel=rel, width=3840,
                          started=_now(created - 70), queued=_now(created - 70), projected_bytes=int(15e9))
            if label == "DUNE_PART_TWO":
                e.state, e.fraction, e.fps = "encoding", 0.37, 14.6
                e.size_bytes, e.eta_seconds = int(15e9 * 0.37), 0.63 * 9960 * 24 / 14.6
            else:
                e.state, e.vmaf, e.finished, e.encoder = "failed", 89.4, _now(created - 400), "gpu"
                e.grain, e.fraction, e.size_bytes = False, 1.0, int(19.6e9)
                e.note = "The AV1 version scored 89 on the quality check (needs 93), so it wasn't copied."
            job.encodes = [e]
            self._jobs[job.id] = job

    def _new_id(self) -> str:
        self._n += 1
        return f"fake-{self._n:03d}"

    def _job_for(self, disc: Disc, meta: dict, created: str = "") -> Job:
        dest = self.cfg.destination(meta["media_kind"])
        return Job(id=self._new_id(), disc_label=disc.label, media_kind=meta["media_kind"], show=meta.get("show", ""),
                   year=meta.get("year"), season=meta.get("season"), disc_number=meta.get("disc_number"),
                   destination=dest.path if dest else "", library=dest.library if dest else "",
                   rip_dir=f"{self.cfg.rip_path}/{disc.label}", titles=copy.deepcopy(disc.titles),
                   hdr_policy=copy.deepcopy(self.cfg.hdr), created=created or _now(), updated=created or _now())

    def _sleep(self, cancel: threading.Event | None = None, steps: int = 1) -> bool:
        """Wait a few steps; True when cancelled. While self.hold is set, a rip or copy stands still."""
        for _ in range(steps):
            if cancel is not None and cancel.is_set():
                return True
            time.sleep(self.delay)
            while cancel is not None and self.hold.is_set() and not cancel.is_set():
                time.sleep(0.01)
        return bool(cancel is not None and cancel.is_set())

    # -- Controller API -----------------------------------------------------
    def drives(self) -> list[Drive]:
        self._sleep()
        d = copy.copy(self._drive)
        disc = self._discs[self._slot][0] if self._slot is not None else None
        d.has_disc = disc is not None
        d.disc_label = disc.label if disc else ""
        return [d]

    def scan(self, drive_index: int) -> Disc:
        if drive_index != 0:
            raise RuntimeError(f"There is no drive {drive_index}.")
        self._sleep(steps=8)
        if self._slot is None:
            raise RuntimeError("The drive is empty. Put a disc in and scan again.")
        disc = copy.deepcopy(self._discs[self._slot][0])
        disc.drive = self.drives()[0]
        return disc

    def disc_info(self, drive_index: int) -> DiscReport:
        rep = self._disc_info(drive_index)
        rep.cover_path, rep.cover_source = self.cover(drive_index)
        return rep

    def _disc_info(self, drive_index: int) -> DiscReport:
        disc = self.scan(drive_index)
        langs = {"eng": "English", "spa": "Spanish", "fra": "French", "por": "Portuguese"}
        if disc.uhd:
            video = VideoInfo("HEVC (H.265)", "3840x2160", "23.976", "16:9", ["HDR10", "Dolby Vision"],
                              "4K · HEVC · 23.976 fps · HDR10 · Dolby Vision")
            audio = [AudioInfo("English", "Dolby TrueHD Atmos", "7.1", "48 kHz", "4.6 Mb/s"),
                     AudioInfo("English", "Dolby Digital", "5.1", "48 kHz", "640 kb/s"),
                     AudioInfo("Spanish", "Dolby Digital Plus", "5.1", "48 kHz", "1.0 Mb/s"),
                     AudioInfo("French", "Dolby Digital Plus", "5.1", "48 kHz", "1.0 Mb/s"),
                     AudioInfo("English", "Dolby Digital", "2.0", "48 kHz", "192 kb/s", "Audio description"),
                     AudioInfo("English", "Dolby Digital", "2.0", "48 kHz", "192 kb/s", "Commentary")]
            subs = [SubtitleInfo("English", "PGS"), SubtitleInfo("English SDH", "PGS"), SubtitleInfo("Spanish", "PGS"),
                    SubtitleInfo("French", "PGS"), SubtitleInfo("Portuguese", "PGS")]
            rows = [("00800.mpls", "0:57:04", "17.6 GB", 8, "episode"), ("00801.mpls", "0:54:31", "16.8 GB", 8, "episode"),
                    ("00802.mpls", "0:41:12", "12.7 GB", 7, "episode"), ("00803.mpls", "0:54:56", "16.9 GB", 8, "episode"),
                    ("00315.mpls", "3:27:43", "64.0 GB", 31, "play_all"), ("00024.m2ts", "0:18:20", "3.1 GB", 1, "extra"),
                    ("00025.m2ts", "0:09:48", "1.6 GB", 1, "extra"), ("00026.m2ts", "0:01:12", "210 MB", 1, "short"),
                    ("00027.m2ts", "0:00:24", "64 MB", 1, "short")]
            titles = []
            for i, (src, dur, size, ch, kind) in enumerate(rows):
                short = kind in ("short",) or src.endswith(".m2ts")
                titles.append(TitleInfo(i, src, dur, size, ch, ch, kind, copy.deepcopy(video),
                                        audio=copy.deepcopy(audio[:2] if short else audio),
                                        subtitles=copy.deepcopy(subs[:2] if short else subs)))
            return DiscReport(
                title="Severance · Season 1 · Disc 1", label=disc.label, disc_type="4K UHD Blu-ray",
                capacity="81.7 GB of video", layers="3 layers (100 GB disc)", drive=MODEL, libredrive=True,
                cover_path="",
                highlights=["4K UHD", "HDR10", "Dolby Vision", "Dolby Atmos", "3 audio languages",
                            "5 subtitle languages"],
                audio_languages=["English", "Spanish", "French"],
                subtitle_languages=["English", "English SDH", "Spanish", "French", "Portuguese"],
                titles=titles,
                notes=["A play-all title repeats episodes that are also listed on their own, so it is skipped.",
                       "Dolby Vision is read from the disc's playlist. The profile is checked on the ripped file."])
        main = disc.titles[0]
        vid = next(s for s in main.streams if s.kind == "video")
        video = VideoInfo(vid.codec_long, vid.resolution, "23.976", "16:9", [], "1080p · AVC · 23.976 fps")
        audio = [AudioInfo(langs.get(s.lang, s.lang), s.codec_long, s.channels, "48 kHz", "",
                           "Commentary" if "commentary" in s.flags else "") for s in main.streams if s.kind == "audio"]
        subs = [SubtitleInfo(langs.get(s.lang, s.lang), s.codec) for s in main.streams if s.kind == "subtitle"]
        titles = [TitleInfo(t.index, t.source, time.strftime("%H:%M:%S", time.gmtime(t.duration_s)).lstrip("0") or "0",
                            f"{t.size_bytes / 1e9:.1f} GB", t.chapters, len(t.segments), t.kind, copy.deepcopy(video),
                            copy.deepcopy(audio), copy.deepcopy(subs)) for t in disc.titles]
        return DiscReport(title=disc.name, label=disc.label, disc_type="Blu-ray",
                          capacity=f"{disc.size_bytes / 1e9:.1f} GB of video", layers="2 layers (50 GB disc)",
                          drive=MODEL, libredrive=True,
                          highlights=["Full HD 1080p", "DTS-HD Master Audio", "2 audio languages",
                                      "2 subtitle languages"],
                          audio_languages=["English", "French"], subtitle_languages=["English", "French"],
                          titles=titles)

    def new_job(self, disc: Disc) -> Job:
        meta = next((m for d, m in self._discs if d.label == disc.label), {"media_kind": "tv"})
        job = self._job_for(disc, meta)
        if meta["media_kind"] == "tv":
            self._next_disc_prefill(job, disc.label)
        return job

    def save_job(self, job: Job) -> None:
        with self._lock:
            job.updated = _now()
            if not job.created:
                job.created = job.updated
            self._jobs[job.id] = job

    def jobs(self) -> list[Job]:
        self._tick_encodes()
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def _layout(self, job: Job) -> str:
        d = self.cfg.destination(job.media_kind)
        return d.layout if d else "tv"

    def _show_id(self, job: Job) -> int | None:
        return next((s.id for s in SHOWS if s.name == job.show), None)

    @staticmethod
    def _rip_rate(t: float, span: float) -> float:
        """About 27 MB/s (6x), with a dip a little before halfway, as a scratched layer change would give."""
        v = 27.5e6 + 1.6e6 * math.sin(t / 120) + 0.6e6 * math.sin(t / 14)
        if 0.40 * span < t < 0.46 * span:
            v -= 11e6
        return v

    @staticmethod
    def _copy_rate(t: float, span: float, lo: float = 0.58, hi: float = 0.62) -> float:
        """About 110 MB/s over the network, with a short drop when it slows."""
        v = 110e6 + 5e6 * math.sin(t / 30) + 2e6 * math.sin(t * 2.3)
        if lo * span < t < hi * span:
            v = 9e6
        return v

    def stream_possible(self, job: Job) -> tuple[bool, str]:
        if self._layout(job) != "movie":
            return False, "Only a film can be copied while it rips. A show's titles are copied one at a time as each is ripped."
        sel = [t for t in job.titles if t.selected]
        if len(sel) != 1:
            return False, (f"{len(sel)} titles are selected, so the film is copied after the rip. "
                           "Only a single title can be copied while it rips.")
        if not job.hdr_policy.is_raw:
            return False, ("The HDR setting isn't RAW, so the file is rewritten after the rip. "
                           "The film is copied once that's done.")
        return True, ""

    def preview_paths(self, job: Job) -> list[tuple[int, str]]:
        layout = self._layout(job)
        ripped = {f.title_index for f in job.files}
        keep = ripped | {t.index for t in job.titles if t.selected}
        with self.job_lock:
            ms = [m for m in job.matches if m.title_index in keep and m.target != "skip"]
            paths = dedupe([relpath_for(job, m, layout, Scheme.from_config(self.cfg)) for m in ms])
        return [(m.title_index, p) for m, p in zip(ms, paths) if p]

    def update_matches(self, job: Job, matches: list[Match]) -> None:
        with self.job_lock:
            locked = {p.title_index for p in job.titles_progress if p.state in ("copying", "checking", "verified")}
            old = {m.title_index: m for m in job.matches}
            job.matches = [old[m.title_index] if m.title_index in locked and m.title_index in old else m
                           for m in matches]
            self.apply_season(job)

    def confirm_matches(self, job: Job) -> None:
        with self.job_lock:
            for m in job.matches:
                if m.target != "skip":
                    m.confirmed = True
        self.save_job(job)

    def _new_file(self, job: Job, t: Title) -> RippedFile:
        uhd = any(s.resolution.startswith("3840") for s in t.streams if s.kind == "video")
        hdr = None
        if uhd:
            dv = job.hdr_policy.keep == "all"
            hdr = HdrInfo("3840x2160", "hevc", hdr10=True, hdr10plus=False, dolby_vision=dv,
                          dv_profile=(8 if job.hdr_policy.convert_p81 else 7) if dv else None,
                          dv_enhancement_layer=dv and not job.hdr_policy.convert_p81,
                          dv_layer_kind="FEL" if dv and not job.hdr_policy.convert_p81 else None)
        return RippedFile(t.index, f"{job.rip_dir}/title_t{t.index:02d}.mkv", t.size_bytes,
                          state="converted" if job.hdr_policy.convert_p81 else "ripped", hdr=hdr)

    def rip(self, job: Job, progress, cancel: threading.Event, samples=None) -> None:
        emit = samples or (lambda _s: None)
        layout = self._layout(job)
        sel = [t for t in job.titles if t.selected]
        by_index = {t.index: t for t in sel}
        with self.job_lock:
            if not job.matches:
                job.matches = self.suggest_matches(job, self._show_id(job))
            ok, why = self.stream_possible(job) if layout == "movie" else (False, "")
            if layout == "movie" and self.cfg.stream_films and ok:
                job.copy_mode, job.copy_mode_note = "stream", ""
            else:
                job.copy_mode = "per_title" if self.cfg.copy_as_ripped else "after"
                job.copy_mode_note = why if layout == "movie" and self.cfg.stream_films else ""
            done = {f.title_index for f in job.files}
            old = {p.title_index: p for p in job.titles_progress}
            job.titles_progress = [old.get(t.index) or TitleProgress(t.index, total_bytes=t.size_bytes) for t in sel]
            for p in job.titles_progress:
                if p.title_index in done and p.state in ("waiting", "ripping"):
                    p.state, p.ripped_bytes = "ripped", p.total_bytes
                elif p.state == "ripping":
                    p.state = "waiting"
        tp = job.titles_progress
        total = sum(p.total_bytes for p in tp) or 1
        span = total / 27.5e6
        mode = job.copy_mode
        t = 0.0
        ejected = False

        def ready(p: TitleProgress) -> bool:
            m = next((m for m in job.matches if m.title_index == p.title_index), None)
            return bool(m and m.ready)

        while True:
            if self._sleep(cancel or threading.Event()):
                with self.job_lock:
                    for p in tp:
                        if p.state == "ripping":
                            p.state, p.rip_rate = "waiting", 0.0
                        elif p.state in ("copying", "checking"):
                            p.state, p.copy_rate = "ripped", 0.0
                    n = sum(1 for p in tp if p.state not in ("waiting",))
                    job.status = f"Rip stopped after {n} of {len(tp)} titles."
                self.save_job(job)
                return
            t += RIP_TICK
            with self.job_lock:
                cur = next((p for p in tp if p.state in ("waiting", "ripping")), None)
                rate = 0.0
                if cur is not None:
                    rate = self._rip_rate(t, span)
                    cur.state, cur.rip_rate = "ripping", rate
                    cur.ripped_bytes = min(cur.total_bytes, cur.ripped_bytes + int(rate * RIP_TICK))
                    if cur.ripped_bytes >= cur.total_bytes:
                        cur.state, cur.rip_rate = "ripped", 0.0
                        job.files.append(self._new_file(job, by_index[cur.title_index]))
                    emit(Sample(t, "rip", rate, sum(p.ripped_bytes for p in tp)))
                # What each ripped title does next depends on its match.
                for p in tp:
                    if p.state in ("ripped", "waiting_match") and mode != "after":
                        m = next((m for m in job.matches if m.title_index == p.title_index), None)
                        if m is None or m.target == "skip":
                            p.state, p.note = "skipped", "Skipped on the Match screen."
                        elif not m.ready:
                            p.state, p.note = "waiting_match", "Waits for you to check its match."
                        else:
                            p.state, p.note = "ripped", ""
                # One title copies at a time; a film may copy while it is still ripping.
                active = next((p for p in tp if p.state in ("copying", "checking")), None)
                if active is None and mode != "after":
                    active = next((p for p in tp if p.state == "ripped" and ready(p)), None)
                    if active is None and mode == "stream":
                        active = next((p for p in tp if p.state == "ripping" and ready(p)
                                       and p.ripped_bytes > TAIL), None)
                if active is not None:
                    if active.state in ("ripped", "copying") or mode == "stream":
                        if active.state == "ripped":
                            active.state = "copying"
                            with_dest = self.plan_paths(job)
                            for f in with_dest:
                                if f.title_index == active.title_index:
                                    f.state = "copying"
                    crate = self._copy_rate(t, span)
                    limit = active.ripped_bytes - (TAIL if active.state == "ripping" else 0)
                    moved = int(max(0, min(limit - active.copied_bytes, crate * RIP_TICK)))
                    if active.state == "ripping" and active.copied_bytes == 0:
                        self.plan_paths(job)
                    active.copied_bytes += moved
                    active.copy_rate = moved / RIP_TICK
                    if mode == "stream":
                        active.checked_bytes = max(active.checked_bytes, active.copied_bytes - TAIL)
                    if active.state == "copying" and active.copied_bytes >= active.total_bytes:
                        active.state = "checking"
                    elif active.state == "checking":
                        active.checked_bytes = min(active.total_bytes, active.checked_bytes + int(400e6 * RIP_TICK))
                        if active.checked_bytes >= active.total_bytes:
                            active.state, active.copy_rate = "verified", 0.0
                            for f in job.files:
                                if f.title_index == active.title_index:
                                    f.state, f.sha256 = "verified", "0" * 64
                                    self._queue_encode(job, f)
                    emit(Sample(t, "copy", active.copy_rate, sum(p.copied_bytes for p in tp)))
                ripped = sum(p.ripped_bytes for p in tp)
                copied = sum(p.copied_bytes for p in tp)
                rip_left = any(p.state in ("waiting", "ripping") for p in tp)
                busy = any(p.state in ("copying", "checking") or (p.state == "ripped" and ready(p)) for p in tp)
            if mode == "after":
                frac = ripped / total
            else:
                frac = (ripped + copied) / (2 * total)
            if cur is not None:
                k = tp.index(cur) + 1
                text = f"Ripping title {k} of {len(tp)} · {rate / 1e6:.1f} MB/s"
            else:
                text = "Ripped. Copying the last title to the NAS."
            progress(frac, text)
            if not rip_left and not ejected and self.cfg.eject_after_rip:
                ejected = True          # the tray opens once the last title is ripped; the drive reads empty
                self._slot = None
                self.ejected += 1
            if not rip_left and (mode == "after" or not busy):
                break

        with self.job_lock:
            waiting = [p for p in tp if p.state == "waiting_match"]
            all_done = mode != "after" and all(p.state in ("verified", "skipped") for p in tp) and \
                any(p.state == "verified" for p in tp)
        if all_done:
            job.stage, job.progress = "plex", 1.0
            job.status = f"Asking Plex to scan {job.library or 'nothing'}."
            progress(1.0, job.status)
            self._sleep(steps=6)
            d = self.cfg.destination(job.media_kind)
            job.stage = "done"
            job.copied_at = _now()
            job.status = f"Copied to {d.label if d else 'the NAS'} and checked. Plex added it just now."
        else:
            job.stage, job.progress = "match", 1.0
            if waiting:
                job.status = (f"Ripped. {len(waiting)} title waits for you to check its match." if len(waiting) == 1
                              else f"Ripped. {len(waiting)} titles wait for you to check their matches.")
            else:
                job.status = f"Ripped. {len(sel)} titles are ready to match."
        self.save_job(job)

    def search_show(self, query: str) -> list:
        self._sleep(steps=3)
        q = query.strip().lower()
        hits = [s for s in SHOWS if q and (q in s.name.lower() or s.name.lower().startswith(q))]
        return [copy.copy(h) for h in hits]

    def episodes(self, show_id: int, season: int | None) -> list[Episode]:
        """One season's episodes, or every season's when season is None."""
        self._sleep(steps=2)
        return [Episode(s, n, title, rt) for (sid, s), eps in sorted(EPISODES.items())
                if sid == show_id and season in (None, s) for n, title, rt in eps]

    def episode_list_from_photo(self, path: str, show_id: int):
        self._sleep(steps=2)
        names = [e.title for e in self.episodes(show_id, None)][:3]
        return SimpleNamespace(names=names, summary=lambda: f"Read {len(names)} episode names from the photo.")

    def card_status(self, job: Job) -> dict:
        with self.job_lock:
            return {ti: copy.copy(st) for ti, st in self._card_state.get(job.id, {}).items()}

    def read_cards(self, job: Job, progress=None, cancel=None, only=None) -> int:
        """Pretends each ripped title's card names the next episode of the show; the first is clear,
        the rest need a check. Reading only some titles (two that hold one episode) pretends their
        cards show the episode they're matched to, and changes no match."""
        if only is not None:
            return self._read_some(job, set(only), progress, cancel)
        eps = self.episodes(job.show_id, None) if job.show_id else []
        files = [f.title_index for f in job.files]
        if not files or not eps:
            return 0
        with self.job_lock:
            self._card_state[job.id] = {ti: CardStatus() for ti in files}
        n = 0
        for k, ti in enumerate(files):
            if cancel is not None and cancel.is_set():
                break
            with self.job_lock:
                st = self._card_state[job.id][ti]
                st.state, st.started = "reading", time.monotonic()
            self._sleep(steps=2)
            e = eps[k % len(eps)]
            with self.job_lock:
                st.state, st.text, st.sure, st.seconds = "read", e.title.upper(), k == 0, 0.1
                job.matches = [Match(ti, "episode", e, confidence="close" if k == 0 else "check", source="card",
                                     note=f"The title card reads “{e.title.upper()}”.") if m.title_index == ti else m
                               for m in job.matches]
            n += 1
            if progress:
                progress((k + 1) / len(files), f"Reading title cards · {k + 1} of {len(files)}")
        with self.job_lock:
            for st in self._card_state[job.id].values():
                if st.state in ("waiting", "reading"):
                    st.state = "stopped"
        return n

    def _read_some(self, job: Job, only: set[int], progress=None, cancel=None) -> int:
        tis = [f.title_index for f in job.files if f.title_index in only]
        if not tis:
            raise ValueError("No ripped titles are left on this computer to read.")
        with self.job_lock:
            states = self._card_state.setdefault(job.id, {})
            for ti in tis:
                states[ti] = CardStatus()
        for k, ti in enumerate(tis):
            if cancel is not None and cancel.is_set():
                break
            with self.job_lock:
                st = states[ti]
                st.state, st.started = "reading", time.monotonic()
            self._sleep(steps=8)
            m = next((m for m in job.matches if m.title_index == ti), None)
            with self.job_lock:
                name = m.episode.title.upper() if m is not None and m.episode is not None else ""
                st.state, st.text, st.sure, st.seconds = "read" if name else "none", name, False, 0.5
                st.t = float(BLUEY_CARD_AT.get(ti, 40 + ti)) if name else None
            if progress:
                progress((k + 1) / len(tis), f"Reading title cards · {k + 1} of {len(tis)}")
        with self.job_lock:
            for st in states.values():
                if st.state in ("waiting", "reading"):
                    st.state = "stopped"
        return 0

    def card_reads(self, job: Job, titles: list[int]) -> dict[int, tuple[str, float]]:
        with self.job_lock:
            return {ti: (st.text, st.t or 0.0) for ti, st in self._card_state.get(job.id, {}).items()
                    if ti in titles and st.state == "read" and st.text}

    # -- watching a title ---------------------------------------------------
    def playable(self, job: Job, ti: int) -> str:
        """The sample's files aren't on disk, so a title plays once it's ripped, as a real one would."""
        tp = next((p for p in job.titles_progress if p.title_index == ti), None)
        if tp is not None and tp.state in ("waiting", "ripping"):
            return ""
        f = next((f for f in job.files if f.title_index == ti), None)
        if f is None:
            return ""
        if not f.local_deleted:
            return f.path
        return os.path.join(job.destination, f.dest_rel) if f.state == "verified" and f.dest_rel else ""

    def player(self) -> Player | None:
        return Player("mpv", ["mpv"], True)

    def play(self, path: str, start_s: float = 0.0) -> str:
        self.played.append((path, start_s))
        return ""

    def _library_rels(self, job: Job) -> list[str]:
        return []

    def _save(self, job: Job) -> None:
        self.save_job(job)

    def match_from_list(self, job: Job, show_id: int, text: str) -> list[Match]:
        names = parse_episode_list(text)
        if not names:
            raise ValueError("Type the episode names from the case, one per line.")
        return match_list([copy.copy(t) for t in job.titles], self.episodes(show_id, None), names)

    def suggest_matches(self, job: Job, show_id: int | None) -> list[Match]:
        layout = (self.cfg.destination(job.media_kind).layout if self.cfg.destination(job.media_kind) else "tv")
        ripped = {f.title_index for f in job.files}
        titles = [copy.copy(t) for t in job.titles]
        for t in titles:
            t.selected = t.index in ripped if ripped else t.selected
        if layout == "movie":
            return feature_match(titles)
        eps = [Episode(job.season or 0, n, title, rt) for n, title, rt in EPISODES.get((show_id, job.season), [])]
        # Earlier discs of the season hold the episodes before this one's.
        start = 1
        if job.disc_number and job.disc_number > 1:
            per_disc = sum(1 for t in titles if t.selected and t.kind == "episode")
            start = 1 + (job.disc_number - 1) * (per_disc + 2)
        return match_titles(titles, eps, start_number=start)

    def plan_paths(self, job: Job) -> list[RippedFile]:
        d = self.cfg.destination(job.media_kind)
        layout = d.layout if d else "tv"
        by_title = {m.title_index: m for m in job.matches}
        files = [f for f in job.files if f.title_index in by_title]
        scheme = Scheme.from_config(self.cfg)
        paths = dedupe([relpath_for(job, by_title[f.title_index], layout, scheme) for f in files])
        out = []
        for f, p in zip(files, paths):
            f.dest_rel = p or ""
            if p:
                out.append(f)
        return out

    def copy(self, job: Job, progress, cancel: threading.Event, samples=None) -> None:
        emit = samples or (lambda _s: None)
        with self.job_lock:
            held = [m for m in job.matches if m.held and any(f.title_index == m.title_index and f.state != "verified"
                                                              for f in job.files)]
            if held:
                raise ValueError(f"{held[0].held} Settle it at the top of the Match page first: compare the "
                                 "titles, then keep one, keep both as versions, or save one as an extra.")
            files = self.plan_paths(job)
            job.stage = "copy"
            prog = {p.title_index: p for p in job.titles_progress}
            for f in files:
                if f.title_index not in prog:
                    p = TitleProgress(f.title_index, state="ripped", total_bytes=f.size_bytes,
                                      ripped_bytes=f.size_bytes)
                    job.titles_progress.append(p)
                    prog[f.title_index] = p
        total = sum(f.size_bytes for f in files) or 1
        done = sum(f.size_bytes for f in files if f.state == "verified")
        span = max(1.0, (total - done) / 110e6)
        t = 0.0
        for f in files:
            if f.state == "verified":
                continue
            p = prog[f.title_index]
            with self.job_lock:
                f.state, p.state = "copying", "copying"
                if p.copied_bytes >= f.size_bytes:
                    p.copied_bytes = 0
            while p.copied_bytes < f.size_bytes:
                if self._sleep(cancel):
                    with self.job_lock:
                        f.state, p.state, p.copy_rate = "ripped", "ripped", 0.0   # rsync resumes the partial file
                    self.save_job(job)
                    return
                t += COPY_TICK
                rate = self._copy_rate(t, span, 0.40, 0.47)
                with self.job_lock:
                    moved = int(min(f.size_bytes - p.copied_bytes, rate * COPY_TICK))
                    p.copied_bytes += moved
                    p.copy_rate = rate
                copied = done + sum(prog[g.title_index].copied_bytes for g in files if g.state != "verified")
                emit(Sample(t, "copy", rate, int(copied)))
                progress(copied / total, f"{rate / 1e6:.0f} MB/s · {_left((total - copied) / rate)} left")
            with self.job_lock:
                f.state, p.state = "copied", "checking"
            self._sleep(steps=2)
            with self.job_lock:
                f.state, p.state, p.copy_rate = "verified", "verified", 0.0
                p.checked_bytes = f.size_bytes
                f.sha256 = "0" * 64
                self._queue_encode(job, f)
            done += f.size_bytes
        job.stage, job.progress = "plex", 1.0
        job.status = f"Asking Plex to scan {job.library or 'nothing'}."
        progress(1.0, job.status)
        self._sleep(steps=6)
        label = self.cfg.destination(job.media_kind).label if self.cfg.destination(job.media_kind) else "the NAS"
        job.stage = "done"
        job.copied_at = _now()
        job.status = f"Copied to {label} and checked. Plex added it just now."
        self.save_job(job)

    def cleanup(self, apply: bool = False) -> list[str]:
        return []

    # -- the Queue page -------------------------------------------------------
    # queue() itself is the real one (QueueViews); these stand in for the disk and TVmaze.
    def queue(self, jobs: list[Job], running: set[str]) -> QueueView:
        self._tick_encodes()
        pretend = {j for j in self._pretend if j in self._jobs and self._jobs[j].stage == "rip"}
        return QueueViews.queue(self, jobs, set(running) | pretend)

    def _locked(self, job: Job) -> set[int]:
        out = {p.title_index for p in job.titles_progress if p.state in ("copying", "checking", "verified")}
        return out | {f.title_index for f in job.files if f.state in ("copying", "verified")}

    def _card_text(self, job: Job, ti: int) -> str:
        st = self._card_state.get(job.id, {}).get(ti)
        return st.text if st is not None and st.text else ""

    def _show_episodes(self, show_id: int) -> list[Episode] | None:
        return [Episode(s, n, title, rt) for (sid, s), eps in sorted(EPISODES.items()) if sid == show_id
                for n, title, rt in eps] or None

    @staticmethod
    def _local_size(f: RippedFile) -> int:
        return 0 if f.local_deleted or not f.path else f.size_bytes

    def _rip_folder_bytes(self) -> int:
        # The sample files plus about 4 GB the jobs don't know about, as a real rip folder has.
        return sum(self._local_size(f) for j in self._jobs.values() for f in j.files) + int(4.2e9)

    def _safe_local(self, job: Job, f: RippedFile, check_nas: bool = False) -> bool:
        return (f.state == "verified" and not f.local_deleted and bool(f.path) and bool(f.dest_rel)
                and not _encode_holds(job, f.title_index))

    @staticmethod
    def _av1_size(e: EncodeJob) -> int:
        return 0 if e.local_deleted or not e.path or not (e.encoded or e.state == "verified") else e.size_bytes

    def _safe_av1(self, job: Job, e: EncodeJob, check_nas: bool = False) -> bool:
        return e.state == "verified" and not e.local_deleted and bool(e.path) and bool(e.dest_rel)

    def confirm_title(self, job: Job, title_index: int, episode: Episode) -> None:
        with self.job_lock:
            matches = copy.deepcopy(job.matches)
            m = next((m for m in matches if m.title_index == title_index), None)
            if m is None:
                m = Match(title_index, "episode", source="manual")
                matches.append(m)
            changed = m.episode is None or (m.episode.season, m.episode.number) != (episode.season, episode.number)
            m.target, m.episode, m.confirmed = "episode", copy.deepcopy(episode), True
            if changed:
                m.source, m.confidence, m.episode_end, m.note = "manual", "manual", None, ""
            self.update_matches(job, matches)
        self.save_job(job)

    def copy_ready(self, job: Job, progress, cancel: threading.Event, samples=None) -> int:
        with self.job_lock:
            self.plan_paths(job)
            by = {m.title_index: m for m in job.matches}
            todo = [f for f in job.files if f.state != "verified" and f.dest_rel and by.get(f.title_index)
                    and by[f.title_index].target != "skip" and (by[f.title_index].ready or f.state == "failed")
                    and not f.local_deleted]
            job.stage, job.dismissed = "copy", False
        for k, f in enumerate(todo):
            if self._sleep(cancel, steps=4):
                with self.job_lock:
                    job.status = "Copy paused. Start it again to carry on where it stopped."
                self.save_job(job)
                return k
            with self.job_lock:
                f.state, f.sha256 = "verified", "0" * 64
                self._queue_encode(job, f)
                tp = next((p for p in job.titles_progress if p.title_index == f.title_index), None)
                if tp is not None:
                    tp.state, tp.note = "verified", ""
                    tp.copied_bytes = tp.checked_bytes = f.size_bytes
            progress((k + 1) / len(todo), f"Copied {k + 1} of {len(todo)}")
        with self.job_lock:
            left = [f for f in job.files if f.state != "verified"
                    and not (by.get(f.title_index) and by[f.title_index].target == "skip")]
            if left:
                job.stage, job.progress = "match", 1.0
                job.status = (f"Copied {len(todo)} title{'s' if len(todo) != 1 else ''}. "
                              f"{len(left)} {'waits' if len(left) == 1 else 'wait'} for you.")
            else:
                d = self.cfg.destination(job.media_kind)
                job.stage, job.progress, job.copied_at = "done", 1.0, _now()
                job.status = f"Copied to {d.label if d else 'the NAS'} and checked. Plex added it just now."
        self.save_job(job)
        progress(1.0, job.status)
        return len(todo)

    def dismiss(self, job: Job) -> None:
        with self.job_lock:
            job.dismissed = True
            for e in job.encodes:
                if e.state == "failed":
                    e.state = "skipped"
                    e.note = (e.note + " It was given up, so the remux is kept as it is.").strip()
        self.save_job(job)

    def free_local(self, job: Job | None = None) -> int:
        freed = 0
        with self.job_lock:
            for j in [job] if job is not None else list(self._jobs.values()):
                if j.id in self._pretend and j.stage == "rip":
                    continue
                for f in j.files:
                    if self._safe_local(j, f):
                        freed += f.size_bytes
                        f.local_deleted = True
                for e in j.encodes:
                    if self._safe_av1(j, e):
                        freed += e.size_bytes
                        e.local_deleted = True
        return freed

    def nas_folder(self, job: Job) -> str:
        rels = [f.dest_rel for f in job.files if f.dest_rel] or [p for _, p in self.preview_paths(job)]
        dirs = [os.path.dirname(r) for r in rels]
        dirs = [d for d in dirs if os.path.basename(d) not in EXTRAS_FOLDERS] or [os.path.dirname(d) for d in dirs]
        if dirs:
            return os.path.join(job.destination, max(set(dirs), key=dirs.count))
        scheme = Scheme.from_config(self.cfg)
        folder = show_folder(job.show, job.year, scheme)
        if self._layout(job) != "movie" and job.season is not None:
            folder += "/" + season_folder(job.season, scheme)
        return os.path.join(job.destination, folder)

    def reverify(self, job: Job, progress) -> list[str]:
        """Pretends to read each NAS copy back; every sample copy matches."""
        files = [f for f in job.files if f.state == "verified"]
        for k, _f in enumerate(files):
            self._sleep(steps=2)
            progress((k + 1) / max(1, len(files)), f"Checking {k + 1} of {len(files)}")
        job.status = f"Checked again: all {len(files)} NAS cop{'y' if len(files) == 1 else 'ies'} match the rip."
        return []

    # -- the AV1 second version -------------------------------------------------
    def _queue_encode(self, job: Job, f: RippedFile) -> None:
        """Queue a 4K title's AV1 version once its remux is verified, as the Controller does. Hold job_lock."""
        wanted = job.av1 != "off" and (job.av1 in ("cpu", "gpu") or self.cfg.av1_enabled)
        width = _video_width(job, f.title_index)
        if not wanted or not av1.is_4k(width) or not f.dest_rel or _enc(job, f.title_index) is not None:
            return
        enc = job.av1 if job.av1 in ("cpu", "gpu") else self.cfg.av1_encoder
        rel = av1.av1_name(f.dest_rel)
        est, _, _ = self.av1_estimate(av1.EncodeSettings(enc, self.cfg.av1_quality, self.cfg.av1_grain), width,
                                      next((t.duration_s for t in job.titles if t.index == f.title_index), 3600),
                                      f.size_bytes)
        job.encodes.append(EncodeJob(f.title_index, encoder=enc, quality=self.cfg.av1_quality,
                                     grain=self.cfg.av1_grain and enc == "cpu", src=f.path,
                                     path=f"{job.rip_dir}/{os.path.basename(rel)}", dest_rel=rel, width=width,
                                     queued=_now(), projected_bytes=est))

    def _order(self) -> list[tuple[Job, EncodeJob]]:
        pairs = [(j, e) for j in self._jobs.values() for e in j.encodes if e.state in ENCODE_ACTIVE]
        return sorted(pairs, key=lambda p: (p[1].queued, p[0].created, p[1].title_index))

    def _tick_encodes(self) -> None:
        """Move the encode at the head of the queue along by the time since the last look."""
        now = time.monotonic()
        dt_s, self._enc_tick = now - self._enc_tick, now
        if self.delay <= 0 or self.hold.is_set():
            return
        span = ENCODE_STEPS * self.delay
        with self.job_lock:
            order = self._order()
            cur = next(((j, e) for j, e in order if e.state in ("encoding", "paused", "checking", "copying")), None)
            if cur is None:
                cur = next(((j, e) for j, e in order if e.state == "waiting"), None)
            if cur is None:
                return
            job, e = cur
            key = (job.id, e.title_index)
            if e.state == "waiting":
                e.state, e.started, e.note = "encoding", _now(), ""
            if e.state == "encoding":
                e.fraction = min(1.0, e.fraction + dt_s / span)
                e.fps = (e.fps or 14.5) + 0.3 * math.sin(now)
                e.size_bytes = int(e.projected_bytes * e.fraction)
                film = next((t.duration_s for t in job.titles if t.index == e.title_index), 3600)
                e.eta_seconds = (1 - e.fraction) * film * 24 / max(1.0, e.fps)
                if e.fraction >= 1.0:
                    e.state, e.encoded, e.eta_seconds, e.fps = "checking", True, 0.0, 0.0
                    self._enc_phase[key] = now
            elif e.state == "checking" and now - self._enc_phase.get(key, now) >= span * 0.05:
                e.state, e.vmaf = "copying", 95.2
                self._enc_phase[key] = now
            elif e.state == "copying" and now - self._enc_phase.get(key, now) >= span * 0.08:
                e.state, e.finished, e.note = "verified", _now(), ""

    def pause_encode(self, job: Job) -> bool:
        with self.job_lock:
            for e in job.encodes:
                if e.state in ("encoding", "waiting") and not e.held:
                    e.state, e.held, e.fps, e.eta_seconds = "paused", True, 0.0, None
                    e.note = "Paused. Resume it to carry on where it stopped."
                    return True
        return False

    def resume_encode(self, job: Job) -> bool:
        changed = False
        with self.job_lock:
            for e in job.encodes:
                if e.held:
                    e.held, e.note, changed = False, "", True
                    e.state = "encoding" if e.fraction > 0 else "waiting"
        self._enc_tick = time.monotonic()
        return changed

    def stop_encode(self, job: Job) -> bool:
        changed = False
        with self.job_lock:
            for e in job.encodes:
                if e.state in ENCODE_ACTIVE or e.state == "failed":
                    e.state, e.held, e.encoded, e.finished = "skipped", False, False, _now()
                    e.note, changed = "Stopped. The remux stays in Plex as it is.", True
        return changed

    def retry_encode(self, job: Job) -> int:
        n = 0
        with self.job_lock:
            for e in job.encodes:
                if e.state == "failed":
                    e.state, e.note, e.finished, e.vmaf, e.queued = "waiting", "", "", None, _now()
                    e.fraction, e.size_bytes, e.encoded = 0.0, 0, False
                    n += 1
        return n

    def encode_status(self) -> EncodeStatus:
        self._tick_encodes()
        with self.job_lock:
            order = self._order()
            cur = next(((j, e) for j, e in order if e.state != "waiting"), None)
            out = EncodeStatus(waiting=sum(1 for _, e in order if e.state == "waiting"))
            if cur is not None:
                out.job_id, out.title_index, out.state = cur[0].id, cur[1].title_index, cur[1].state
        return out

    def av1_estimate(self, settings, width: int = 3840, duration_s: float = 9960,
                     source_bytes: float = 62e9) -> tuple[int, float, bool]:
        return av1.Stats(os.devnull).estimate(settings, width, duration_s, int(source_bytes))

    def recover_encodes(self) -> list:
        return []

    def plex_link(self, job: Job) -> str:
        root = f"{self.cfg.plex_url.rstrip('/')}/web/index.html"
        if job.stage != "done":
            return root
        return f"{root}#!/server/fake-server/details?key=%2Flibrary%2Fmetadata%2F{abs(hash(job.show)) % 90000 + 1000}"

    def cover(self, drive_index: int, season: int | None = None) -> tuple[str, str]:
        """(image path, source) for the disc in the drive, or ("", "") when it has none."""
        self._sleep(steps=4)
        if self._slot is None:
            return "", ""
        label = self._discs[self._slot][0].label
        spec = COVERS.get(label)
        if spec is None:
            return "", ""
        w, h, top, bottom, band, source = spec
        if self._cover_dir is None:
            self._cover_dir = tempfile.mkdtemp(prefix="disc-shelf-covers-")
            atexit.register(shutil.rmtree, self._cover_dir, True)
        path = os.path.join(self._cover_dir, f"{label}.png")
        if not os.path.exists(path):
            _png(path, w, h, top, bottom, band)
        return path, source

    def disc_present(self, device: str) -> bool | None:
        return self._slot is not None

    def remove_disc(self) -> None:
        """For tests: empty the drive as if the disc was taken out."""
        self._slot = None

    def insert_disc(self, slot: int = 0) -> None:
        self._slot = slot

    def eject(self, drive_index: int) -> None:
        self._sleep(steps=6)
        self.ejected += 1
        self._slot = (self._slot + 1) % len(self._discs) if self._slot is not None else 0

    def plex_sign_in_start(self) -> tuple[int, str, str]:
        self._sleep(steps=2)
        self._polls = 0
        return 4242, "DS7Q", "https://app.plex.tv/auth#?code=DS7Q"

    def plex_sign_in_poll(self, pin_id: int) -> bool:
        self._polls += 1
        if self._polls >= 2:
            self._signed_in = True
        return self._signed_in and self._polls >= 2

    # -- extras the window uses when present ----------------------------------
    # -- the Library page ----------------------------------------------------
    def _library(self) -> str:
        if self._library_root is None:
            self._library_root = tempfile.mkdtemp(prefix="disc-shelf-library-")
            atexit.register(shutil.rmtree, self._library_root, True)
            for d in self.cfg.destinations:
                os.makedirs(os.path.join(self._library_root, d.kind), exist_ok=True)
            tv = self.cfg.destination("tv")
            if tv:
                _sample_library(os.path.join(self._library_root, tv.kind))
            os.makedirs(os.path.join(self._library_root, ".undo"), exist_ok=True)
        return self._library_root

    def library_path(self, dest) -> str:
        """Where the destination is on this computer: here, a temporary folder of sample shows."""
        return os.path.join(self._library(), dest.kind)

    def library_undo_dir(self) -> str:
        return os.path.join(self._library(), ".undo")

    def library_jobs(self) -> list[Job]:
        """The jobs whose title-card readings the Library page shows, with a sample Bluey disc
        whose card for the second Markets was read. It isn't in the queue."""
        tv = self.cfg.destination("tv")
        markets = Episode(1, 20, "Markets", 7)
        read = [(n, BLUEY_S1[n - 1].upper()) for n in range(1, 13)]     # the first disc's cards, as read
        bluey = Job(id="bluey-sample", show="Bluey", year=2018, season=1, disc_number=1, show_id=41821,
                    destination=tv.path if tv else "", stage="done",
                    titles=[Title(20, duration_s=441), Title(26, duration_s=431)],
                    matches=[Match(20, "episode", markets, confidence="close", source="card",
                                   note="The title card reads “MaRKeTS”, which is closest to “Markets”."),
                             Match(26, "episode", markets, confidence="close", source="card",
                                   note="The title card reads “MaRKeTS”, which is closest to “Markets”.",
                                   version="Short cut")],
                    files=[RippedFile(20, "", dest_rel="Bluey (2018)/Season 01/Bluey (2018) - S01E20.mkv",
                                      state="verified"),
                           RippedFile(26, "", dest_rel="Bluey (2018)/Season 01/Bluey (2018) - S01E20 (2).mkv",
                                      state="verified")])
        for n, text in read:
            bluey.titles.append(Title(100 + n, duration_s=441))
            bluey.matches.append(Match(100 + n, "episode", Episode(1, n, BLUEY_S1[n - 1], 7), confidence="close",
                                       source="card", note=f"The title card reads “{text}”."))
            bluey.files.append(RippedFile(100 + n, "", state="verified",
                                          dest_rel=f"Bluey (2018)/Season 01/Bluey (2018) - S01E{n:02d}.mkv"))
        return [bluey, *self.jobs()]

    def plex_scan_folder(self, library: str, rel: str) -> str:
        self.plex_scans.append((library, rel))
        return f"Plex is scanning {rel}."

    def plex_signed_in(self) -> bool:
        return self._signed_in

    def plex_libraries(self) -> list[str]:
        return ["Movies", "TV Shows", "Anime"]

    def free_bytes(self, path: str) -> int | None:
        return int(1.5e12) if "rip" in path else int(8e12)

    def save_config(self, cfg: Config) -> None:
        self.saved_config = copy.deepcopy(cfg)
