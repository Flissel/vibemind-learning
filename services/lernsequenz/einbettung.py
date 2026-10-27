"""After publication: build the course presentation and embed the players in LearnHouse.

Runs as the last step of publishing (it never fails the publication). The presentation is
built deterministically from the verified draft; a previously built explainer video of
the same course (``video-…``) is embedded too. Re-publishing a regenerated course puts
the players back, so the embed is no longer lost when LearnHouse is re-projected.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

from spaces.learning.services.lernsequenz.token import sequence_token
from spaces.learning.services.course_factory.schemas import CourseDraft
from spaces.learning.services.lernsequenz.aus_kurs import build_presentation
from spaces.learning.services.lernsequenz.store import SequenceStore

logger = logging.getLogger(__name__)
PUBLIC_BASE_ENV = "LEARNING_PUBLIC_API_URL"


class EmbedGateway(Protocol):
    async def embed_sequences(
        self, *, course_id: str, lesson_id: str, embeds: list[dict[str, object]], correlation_id: str
    ) -> dict[str, object]: ...


class SequenceEmbedder:
    def __init__(self, *, store: SequenceStore, learnhouse: EmbedGateway, service_key: str, public_base: str) -> None:
        self._store = store
        self._learnhouse = learnhouse
        self._service_key = service_key
        self._base = public_base.rstrip("/")

    def _link(self, sequence_id: str) -> str:
        return f"{self._base}/lernsequenz/{sequence_id}?k={sequence_token(sequence_id, self._service_key)}"

    async def embed(self, *, course_id: str, draft: CourseDraft, correlation_id: str) -> dict[str, object]:
        presentation = build_presentation(draft, course_id=course_id)
        try:
            earlier = self._store.load(presentation.sequence_id)
        except LookupError:
            earlier = None
        if earlier is not None:
            if any(c.kind == "profile" for c in earlier.checkpoints):
                # "Dein Weg" was switched on by hand (praesentation --profil): keep it on.
                presentation = build_presentation(draft, course_id=course_id, profile=True)
            # Keep model-designed checkpoints and hand-placed terminal checks of the earlier presentation.
            sections = {s.section_id for s in presentation.sections}
            extra = tuple(
                c for c in earlier.checkpoints
                if (c.checkpoint_id.startswith("ki-") or c.kind == "command_check") and c.after_section in sections
            )
            presentation = presentation.model_copy(update={"checkpoints": (*presentation.checkpoints, *extra)})
        self._store.save(presentation)
        embeds: list[dict[str, object]] = []
        video_id = "video-" + presentation.sequence_id.removeprefix("praes-")
        try:
            # The video is rendered on the host (voice, ffmpeg); embed it when one exists.
            video = self._store.load(video_id)
            embeds.append({"heading": "Erklärvideo zum Kurs", "url": self._link(video.sequence_id), "height": 900})
        except LookupError:
            pass
        embeds.append({"heading": "Interaktive Präsentation zum Kurs", "url": self._link(presentation.sequence_id), "height": 900})
        first_lesson = next(
            (lesson_id for chapter in draft.chapters for lesson_id in chapter.lesson_ids), None
        )
        if first_lesson is None:
            raise ValueError("the course has no lesson to embed into")
        result = await self._learnhouse.embed_sequences(
            course_id=course_id, lesson_id=first_lesson, embeds=embeds, correlation_id=correlation_id
        )
        return {"presentation": presentation.sequence_id, "embeds": len(embeds), "learnhouse": result}


def build_sequence_embedder(learnhouse: EmbedGateway) -> SequenceEmbedder | None:
    """None when the artifact root or the service key is missing (embedding is optional)."""
    root = os.environ.get("LEARNING_ARTIFACT_ROOT", "").strip()
    key = os.environ.get("LEARNING_VIBEMIND_SERVICE_KEY", "")
    if not root or len(key) < 32:
        return None
    return SequenceEmbedder(
        store=SequenceStore(Path(root)),
        learnhouse=learnhouse,
        service_key=key,
        public_base=os.environ.get(PUBLIC_BASE_ENV, "http://127.0.0.1:8090"),
    )

