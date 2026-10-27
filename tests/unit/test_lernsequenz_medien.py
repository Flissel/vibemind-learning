"""Sequence images saved by content: one file per image, the name says nothing about the task."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from spaces.learning.services.lernsequenz.medien import save_media


def test_save_media_names_by_content_and_writes_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_ARTIFACT_ROOT", str(tmp_path))
    png = b"\x89PNG\r\n\x1a\n" + b"x" * 100
    url = save_media(png)
    name = f"term-{hashlib.sha256(png).hexdigest()[:16]}.png"
    assert url == f"/lernsequenz-medien/{name}"
    path = tmp_path / "lernsequenzen" / "medien" / name
    assert path.read_bytes() == png
    stamp = path.stat().st_mtime_ns
    assert save_media(png) == url and path.stat().st_mtime_ns == stamp
    assert not list(path.parent.glob("*.part"))


def test_save_media_needs_the_artifact_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEARNING_ARTIFACT_ROOT", raising=False)
    with pytest.raises(RuntimeError):
        save_media(b"\x89PNG")
