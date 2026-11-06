"""Reads what a learner handwrote on the canvas; grading stays deterministic."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from spaces.learning.contracts.beschriftung import (
    Beschriftung,
    antwort_aus_etiketten,
    beschriften,
)
from spaces.learning.services.course_factory.model_gateway import (
    ROLE_CONTRACTS,
    ModelGateway,
    ModelInvocation,
    png_bild,
)

PROMPT_VERSION = "handschrift-lesen-v1"

_FORMAT = {
    "single_choice": {"auswahl": "list with exactly one label"},
    "multiple_choice": {"auswahl": "list of labels"},
    "ordering": {"reihenfolge": "labels in the written order"},
    "matching": {"paare": "left label -> right label"},
    "numeric": {"wert": "the number as written"},
    "fill_in": {"text": "the text as written"},
}


class GelesenV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    lesbar: bool
    # Labels or the value as read; the system turns them into an answer.
    roh: dict[str, object]
    # A short rendering for the confirmation card, e.g. "x = -1,5".
    text: str = Field(max_length=200)


class _ModellLesungV1(BaseModel):
    """What the reading model returns; the gateway demands the schema version."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["handschrift-gelesen-v1"]
    lesbar: bool
    roh: dict[str, object]
    text: str = Field(max_length=200)


def _text_of(entry: object) -> str:
    if isinstance(entry, Mapping):
        for key in ("label", "text"):
            value = entry.get(key)
            if isinstance(value, str):
                return value[:500]
    return ""


def _entries(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


def _etiketten(beschriftung: Beschriftung, task: Mapping[str, object]) -> dict[str, object]:
    options = {_id: _text_of(o) for o in _entries(task.get("options")) if (_id := _id_of(o))}
    presentation = task.get("presentation")
    presentation = presentation if isinstance(presentation, Mapping) else {}
    left = {_id: _text_of(o) for o in _entries(presentation.get("left")) if (_id := _id_of(o))}
    right = {_id: _text_of(o) for o in _entries(presentation.get("right")) if (_id := _id_of(o))}
    return {
        "optionen": {label: options.get(i, "") for label, i in beschriftung.optionen.items()},
        "links": {label: left.get(i, "") for label, i in beschriftung.links.items()},
        "rechts": {label: right.get(i, "") for label, i in beschriftung.rechts.items()},
    }


def _id_of(entry: object) -> str:
    value = entry.get("id") if isinstance(entry, Mapping) else None
    return value if isinstance(value, str) else ""


class HandschriftLeser:
    """One vision-model reading per call; never sees the expected answer."""

    def __init__(self, gateway_factory: Callable[[], ModelGateway]) -> None:
        # One gateway per call: each call runs on its own event loop (asyncio.run).
        self._gateway_factory = gateway_factory

    def lesen(
        self, task: dict[str, object], png: bytes
    ) -> tuple[GelesenV1, dict[str, object] | None]:
        bild = png_bild(png)  # signature and size are checked before any model call
        task_type = str(task.get("type", ""))
        presentation = task.get("presentation")
        beschriftung = beschriften(
            task_type,
            presentation if isinstance(presentation, dict) else {},
            [o for o in _entries(task.get("options")) if isinstance(o, dict)],
        )
        # Whitelist: only what the learner sees. Nothing else of the task is forwarded.
        payload: dict[str, object] = {
            "aufgabentyp": task_type,
            "aufgabentext": str(task.get("prompt", ""))[:5_000],
            "etiketten": _etiketten(beschriftung, task),
            "antwortformat": _FORMAT.get(task_type, {}),
        }
        invocation = ModelInvocation(
            role="handschrift_leser",
            stage=ROLE_CONTRACTS["handschrift_leser"][0],
            correlation_id=str(uuid4()),
            prompt_version=PROMPT_VERSION,
            output_schema_version=ROLE_CONTRACTS["handschrift_leser"][2],
            input_payload=payload,
            image_inputs=(bild,),
        )
        gelesen = asyncio.run(self._lesen(invocation))
        antwort = (
            antwort_aus_etiketten(task_type, beschriftung, gelesen.roh)
            if gelesen.lesbar
            else None
        )
        if antwort is None and gelesen.lesbar:
            gelesen = gelesen.model_copy(update={"lesbar": False})
        return gelesen, antwort

    async def _lesen(self, invocation: ModelInvocation) -> GelesenV1:
        gateway = self._gateway_factory()
        try:
            result = await gateway.generate(invocation, _ModellLesungV1)
            output = result.output
            return GelesenV1(lesbar=output.lesbar, roh=dict(output.roh), text=output.text)
        finally:
            close = getattr(gateway, "aclose", None)
            if close is not None:
                await close()
