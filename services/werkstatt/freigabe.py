"""Freigabe der Werkstatt: Schluessel, unter denen Aufgaben in LearnHouse und im Katalog stehen."""

from __future__ import annotations

from typing import Any


def werkstatt_schluessel(aufgabe: dict[str, Any]) -> tuple[str, str]:
    """`(aufgaben_key, lektion_key)`: die Herkunft aus der Fabrik, sonst ein Werkstatt-Schluessel."""
    task_id = aufgabe["task_id"]
    return (
        aufgabe.get("herkunft_key") or f"werkstatt:{task_id}",
        aufgabe.get("lektion_herkunft_key") or f"werkstatt:{task_id}:lektion",
    )
