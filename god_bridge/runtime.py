"""Runtime locations and identities shared by installed and source workflows."""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path


def workspace_root() -> Path:
    """Keep mutable data out of the installed Python package."""
    return Path(os.environ.get("MINECRAFT_GOD_HOME", os.getcwd())).resolve()


def player_id(value: str, *, canonical_uuid: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("player identity must be a string")
    value = value.strip().lower()
    if canonical_uuid:
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError) as error:
            raise ValueError("player identity must be a Minecraft UUID") from error
    # Safe symbolic identities support deterministic offline fixtures as well.
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", value):
        raise ValueError("player identity must be a UUID or safe alphanumeric identifier")
    return value


def profile_path(directory: Path, identifier: str) -> Path:
    base = directory.resolve()
    destination = (base / player_id(identifier)).resolve()
    if not destination.is_relative_to(base):
        raise ValueError("player profile must remain inside the profile directory")
    return destination


def event_token() -> str:
    token = os.environ.get("MINECRAFT_EVENT_TOKEN", "")
    path = os.environ.get("MINECRAFT_EVENT_TOKEN_FILE", "")
    if not token and path:
        token = Path(path).read_text(encoding="utf-8").strip()
    return token
