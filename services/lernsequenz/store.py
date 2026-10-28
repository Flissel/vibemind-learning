"""Sequences and learner sketches as JSON files under the Learning artifact root."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from spaces.learning.contracts.lernsequenz import DrawnExplanation, LernsequenzV1

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_POINTS = 20_000


class SequenceStore:
    def __init__(self, artifact_root: Path) -> None:
        self._root = artifact_root.resolve() / "lernsequenzen"

    def _path(self, sequence_id: str) -> Path:
        if not _ID.fullmatch(sequence_id) or ".." in sequence_id:
            raise LookupError("sequence is unknown")
        return self._root / f"{sequence_id}.json"

    def save(self, sequence: LernsequenzV1) -> Path:
        path = self._path(sequence.sequence_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(sequence.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)
        return path

    def sequence_ids(self) -> list[str]:
        """Ids of the stored sequences (not sketches or media), for maintenance runs."""
        if not self._root.is_dir():
            return []
        return sorted(p.stem for p in self._root.glob("*.json") if _ID.fullmatch(p.stem))

    def load(self, sequence_id: str) -> LernsequenzV1:
        path = self._path(sequence_id)
        if not path.is_file():
            raise LookupError("sequence is unknown")
        return LernsequenzV1.model_validate_json(path.read_text(encoding="utf-8"))

    def save_sketch(self, sequence: LernsequenzV1, checkpoint_id: str, strokes: object) -> str:
        """Keep a learner's own drawing for a person to look at; nothing grades it here."""
        if not isinstance(sequence.checkpoint(checkpoint_id), DrawnExplanation):
            raise ValueError("this checkpoint takes no drawing")
        if not isinstance(strokes, list) or not strokes or len(strokes) > 200:
            raise ValueError("drawing is empty or too large")
        points = 0
        for stroke in strokes:
            if not isinstance(stroke, list):
                raise ValueError("drawing is malformed")
            for point in stroke:
                if (
                    not isinstance(point, list) or len(point) != 2
                    or not all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in point)
                ):
                    raise ValueError("drawing is malformed")
                points += 1
        if points > _MAX_POINTS:
            raise ValueError("drawing is too large")
        sketch_id = str(uuid4())
        folder = self._root / "skizzen" / sequence.sequence_id / checkpoint_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{sketch_id}.json").write_text(
            json.dumps({
                "sketch_id": sketch_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "strokes": strokes,
            }),
            encoding="utf-8",
        )
        return sketch_id
