"""Small shared helpers with no dependencies on the rest of the app."""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current time as a *naive* UTC datetime.

    We store naive UTC everywhere so behaviour is identical on SQLite (which
    has no real timezone type) and Postgres. API consumers should read all
    timestamps as UTC.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def slugify(value: str, max_len: int = 60) -> str:
    """Turn arbitrary text into a safe, ASCII-only filename fragment."""
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (value or "certificate")[:max_len]
