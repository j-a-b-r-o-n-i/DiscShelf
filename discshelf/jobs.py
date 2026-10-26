"""Jobs kept as one JSON file each under ~/.local/state/disc-shelf/jobs/, so a crash or a reboot loses nothing."""
from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid

from .config import state_dir
from .models import Job, job_from_dict, to_dict


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class JobStore:
    def __init__(self, root: str | None = None):
        self.root = root or os.path.join(state_dir(), "jobs")
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    def new_id(self) -> str:
        return dt.datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]

    def _path(self, job_id: str) -> str:
        return os.path.join(self.root, f"{job_id}.json")

    def save(self, job: Job) -> None:
        job.updated = now_iso()
        if not job.created:
            job.created = job.updated
        with self._lock:
            tmp = self._path(job.id) + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(to_dict(job), fh, indent=1)
            os.replace(tmp, self._path(job.id))

    def load(self, job_id: str) -> Job | None:
        try:
            with open(self._path(job_id)) as fh:
                return job_from_dict(json.load(fh))
        except FileNotFoundError:
            return None

    def all(self) -> list[Job]:
        out = []
        for name in os.listdir(self.root):
            if name.endswith(".json"):
                try:
                    with open(os.path.join(self.root, name)) as fh:
                        out.append(job_from_dict(json.load(fh)))
                except (OSError, ValueError):
                    continue    # a half-written file from a crash; the .tmp rename makes this rare
        return sorted(out, key=lambda j: j.created, reverse=True)

    def delete(self, job_id: str) -> None:
        try:
            os.remove(self._path(job_id))
        except FileNotFoundError:
            pass
