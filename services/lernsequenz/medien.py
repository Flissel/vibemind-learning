"""Images of sequences (drawn terminal windows) in the media folder the player loads from.

Named by content: the same window is stored once, and the name tells nothing about the task.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from uuid import uuid4


def media_root() -> Path:
    root = os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip()
    if not root:
        raise RuntimeError("LEARNING_ARTIFACT_ROOT is not set")
    return Path(root) / "lernsequenzen" / "medien"


def save_media(png: bytes) -> str:
    """Store ``png`` as ``term-<sha256[:16]>.png`` (atomically, only once); returns its player URL."""
    name = f"term-{hashlib.sha256(png).hexdigest()[:16]}.png"
    folder = media_root()
    path = folder / name
    if not path.is_file():
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / f".{name}.{uuid4().hex}.part"
        partial.write_bytes(png)
        os.replace(partial, path)
    return f"/lernsequenz-medien/{name}"
