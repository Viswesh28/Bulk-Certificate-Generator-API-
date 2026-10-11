"""Certificate file storage.

Files are addressed by *keys* (relative paths like ``<job_id>/<recipient_id>.pdf``),
never absolute paths, and the database only stores the key. That keeps the
backend swappable: an ``S3Storage`` with the same four methods could replace
``LocalStorage`` without touching models, engine or API code.

``_resolve`` guards against path traversal (``..``) in keys.
"""
from __future__ import annotations

from pathlib import Path


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        if not key or key.startswith(("/", "\\")):
            raise ValueError(f"invalid storage key: {key!r}")
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError(f"invalid storage key (path traversal): {key!r}")
        return path

    def save(self, key: str, data: bytes) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename would be the atomic variant; a plain write is
        # fine here because keys are unique per recipient and never reused.
        path.write_bytes(data)

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def read(self, key: str) -> bytes:
        return self._resolve(key).read_bytes()

    def abspath(self, key: str) -> Path:
        """Absolute path (used for zero-copy FileResponse downloads)."""
        return self._resolve(key)

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)
