"""Is there a disc in the drive? Asked through the kernel's CD-ROM status request, which
answers at once without spinning the disc up or starting MakeMKV, so the window can
poll it every few seconds."""
from __future__ import annotations

import fcntl
import os

CDROM_DRIVE_STATUS = 0x5326          # linux/cdrom.h
CDS_NO_DISC, CDS_TRAY_OPEN, CDS_DRIVE_NOT_READY, CDS_DISC_OK = 1, 2, 3, 4


def disc_present(device: str, opener=os.open, ioctl=fcntl.ioctl) -> bool | None:
    """True: a disc is in and readable. False: empty or tray open.
    None: can't tell yet (the drive is spinning up) or the device can't be opened."""
    try:
        # O_NONBLOCK lets the open succeed with no disc in, instead of failing or waiting.
        fd = opener(device, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        status = ioctl(fd, CDROM_DRIVE_STATUS, 0)
    except OSError:
        return None
    finally:
        os.close(fd)
    if status == CDS_DISC_OK:
        return True
    if status in (CDS_NO_DISC, CDS_TRAY_OPEN):
        return False
    return None
