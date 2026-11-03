"""Aus einer falschen Antwort eine Notiz machen: was gefragt war und was dazu zu sagen ist."""

from __future__ import annotations

from typing import Any

from spaces.learning.services.adaptive_engine.session_service import PublicTask, TurnFeedback
from spaces.learning.services.werkstatt.formeltext import klartext

SELECTION_MAX = 2_000
SOURCES_MAX = 20
FESTER_TEXT = "Diese Antwort war noch nicht richtig. Sehen Sie sich die Aufgabe noch einmal in Ruhe an."


def notiz_aus_bewertung(task: PublicTask, feedback: TurnFeedback, session_id: str) -> dict[str, Any] | None:
    """Die `NoteStore.create`-Argumente (ohne actor_id/course_id) oder None, wenn keine Notiz nötig ist."""
    if feedback.state not in ("incorrect", "partial"):
        return None
    erklaerung = FESTER_TEXT
    gewertet = [k for k in feedback.criteria if k.feedback.strip()]
    if gewertet:
        erklaerung = min(gewertet, key=lambda k: k.score).feedback
    return {
        "sequence_id": session_id,
        "anchor": {"art": "aufgabe", "aufgabe": task.id},
        "selection": klartext(task.prompt)[:SELECTION_MAX],
        "explanation": erklaerung,
        "extra": "",
        "extra_sources": [s[:500] for s in feedback.source_refs if s][:SOURCES_MAX],
        "quelle": "bewertung",
    }