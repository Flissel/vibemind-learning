"""Drawing tasks in the claw: one secret per opened canvas, strokes to canvas objects.

The person draws on a local page and submits there with a button; the model never
sees the secret and has no tool that submits a drawing (like the approval page).
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

_TTL_SECONDS = 60 * 60
_MAX_STROKES = 2_000
_MAX_POINTS = 20_000


class ZeichnungAbgelehnt(Exception):
    def __init__(self, grund: str) -> None:
        super().__init__(grund)
        self.grund = grund


@dataclass
class Zeichnung:
    session_id: str
    activity_id: str
    session_ref: str
    aufgabe: str
    erstellt: float = field(default_factory=time.monotonic)


class ZeichnungStore:
    """Open drawing tasks by secret; a secret is used up when the drawing is submitted."""

    def __init__(self, ttl_seconds: float = _TTL_SECONDS) -> None:
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._offen: dict[str, Zeichnung] = {}
        self._refs: dict[str, str] = {}

    def merken(self, zeichnung: Zeichnung) -> str:
        geheimnis = secrets.token_urlsafe(24)
        with self._lock:
            self._aufraeumen()
            self._offen[geheimnis] = zeichnung
            self._refs[zeichnung.session_id] = zeichnung.session_ref
        return geheimnis

    def holen(self, geheimnis: str) -> Zeichnung:
        with self._lock:
            self._aufraeumen()
            zeichnung = self._offen.get(geheimnis)
        if zeichnung is None:
            raise ZeichnungAbgelehnt("Diese Zeichenaufgabe ist abgelaufen oder schon abgegeben.")
        return zeichnung

    def verbrauchen(self, geheimnis: str) -> None:
        with self._lock:
            self._offen.pop(geheimnis, None)

    def session_ref(self, session_id: str) -> str | None:
        """The canvas reference of a session, for hints and reviews after opening."""
        with self._lock:
            return self._refs.get(session_id)

    def _aufraeumen(self) -> None:
        jetzt = time.monotonic()
        for geheimnis, zeichnung in list(self._offen.items()):
            if jetzt - zeichnung.erstellt > self._ttl:
                del self._offen[geheimnis]


def objekte_aus_strichen(striche: object) -> list[dict[str, Any]]:
    """Canvas path objects (bounding boxes) from the page's strokes.

    The rendered PNG is what the model judges; the objects give the structured
    artifact its shape and let a hint point at a stroke.
    """
    if not isinstance(striche, list) or len(striche) > _MAX_STROKES:
        raise ZeichnungAbgelehnt("Die Zeichnung ist ungueltig oder zu gross.")
    objekte: list[dict[str, Any]] = []
    punkte_gesamt = 0
    for index, strich in enumerate(striche):
        punkte = strich.get("punkte") if isinstance(strich, dict) else None
        if not isinstance(punkte, list) or not punkte:
            continue
        punkte_gesamt += len(punkte)
        if punkte_gesamt > _MAX_POINTS:
            raise ZeichnungAbgelehnt("Die Zeichnung hat zu viele Punkte.")
        try:
            xs = [float(p[0]) for p in punkte]
            ys = [float(p[1]) for p in punkte]
        except (TypeError, ValueError, IndexError) as error:
            raise ZeichnungAbgelehnt("Die Zeichnung ist ungueltig.") from error
        if not all(-1e6 <= v <= 1e6 for v in xs + ys):
            raise ZeichnungAbgelehnt("Die Zeichnung ist ungueltig.")
        objekte.append(
            {
                "object_id": f"strich-{index + 1}",
                "object_type": "path",
                "x": min(xs),
                "y": min(ys),
                "width": max(xs) - min(xs),
                "height": max(ys) - min(ys),
                "rotation": 0,
            }
        )
    return objekte
