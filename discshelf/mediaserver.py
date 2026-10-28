"""Which media server, if any, gets told about new rips: Plex, Jellyfin or none.

Both clients (plex.Plex, jellyfin.Jellyfin) offer the same calls the rest of Disc Shelf uses:
sections(), section(title), refresh(key, path), server_path(section, rel), find_key(key,
title, year), season_key(show key, number), home_url() and link(key). connect() builds the
one the settings name, signed in, or says in a sentence why it can't.
"""
from __future__ import annotations

import re

NAMES = {"plex": "Plex", "jellyfin": "Jellyfin", "none": ""}


class ServerError(RuntimeError):
    """The media server answered with an error, or could not be reached."""


def norm_title(title: str) -> str:
    """A title compared without case, punctuation or spacing: "Severance" == "severance"."""
    return re.sub(r"[^0-9a-z]+", "", title.casefold())


def name(cfg) -> str:
    """"Plex", "Jellyfin", or "" with no media server."""
    return NAMES.get(cfg.media_server, "")


def server_url(cfg) -> str:
    return {"plex": cfg.plex_url, "jellyfin": cfg.jellyfin_url}.get(cfg.media_server, "")


def secret_name(cfg) -> str:
    return {"plex": "plex_token", "jellyfin": "jellyfin_key"}.get(cfg.media_server, "")


def signed_in(cfg) -> bool:
    from . import config
    key = secret_name(cfg)
    return bool(key and config.load_secret(key))


def connect(cfg, fetch=None):
    """(client, "") when the chosen server is set up and signed in, else (None, the reason as a
    sentence for the user). With no media server the reason is ""."""
    from . import config
    which = name(cfg)
    if not which:
        return None, ""
    url = server_url(cfg)
    if not url:
        return None, f"Add your {which} server in Settings so new rips are scanned automatically."
    secret = config.load_secret(secret_name(cfg))
    if cfg.media_server == "plex":
        if not secret:
            return None, "Sign in to Plex in Settings so new rips are scanned automatically."
        from .plex import Plex
        return Plex(url, secret, fetch=fetch), ""
    if not secret:
        return None, "Add a Jellyfin API key in Settings so new rips are scanned automatically."
    from .jellyfin import Jellyfin
    return Jellyfin(url, secret, fetch=fetch), ""


def home_url(cfg) -> str:
    """The server's web app, for a link when nothing more exact is known."""
    url = server_url(cfg).rstrip("/")
    if cfg.media_server == "plex":
        return f"{url}/web/index.html" if url else "https://app.plex.tv/desktop"
    if cfg.media_server == "jellyfin" and url:
        return f"{url}/web/"
    return ""
