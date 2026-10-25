"""After publication the players are rebuilt and embedded, so a re-projection cannot lose them."""

from __future__ import annotations

import asyncio
from pathlib import Path

from spaces.learning.contracts.lernsequenz import DrawnExplanation, DrawStep, Stroke
from spaces.learning.services.lernsequenz.aus_kurs import as_video, build_presentation
from spaces.learning.services.lernsequenz.einbettung import SequenceEmbedder
from spaces.learning.services.lernsequenz.store import SequenceStore
from spaces.learning.services.lernsequenz.token import sequence_token
from spaces.learning.tests.unit.test_lernsequenz_aus_kurs import COURSE, draft

KEY = "k" * 32


class _LearnHouse:
    def __init__(self) -> None:
        self.calls = []

    async def embed_sequences(self, **kwargs):
        self.calls.append(kwargs)
        return {"embeds": len(kwargs["embeds"])}


def test_publication_embeds_presentation_and_existing_video_keeping_ai_drawings(tmp_path: Path) -> None:
    store = SequenceStore(tmp_path)
    earlier = build_presentation(draft(), course_id=COURSE)
    drawing = DrawnExplanation(
        checkpoint_id="ki-1-drawn_explanation", after_section=earlier.sections[0].section_id, title="Tafel",
        steps=(DrawStep(narration="Erst prüfen.", strokes=(Stroke(path="M10 10 h100"),)),),
    )
    store.save(earlier.model_copy(update={"checkpoints": (*earlier.checkpoints, drawing)}))
    times = {s.section_id: {"start": 0.0, "end": 5.0} for s in earlier.sections}
    store.save(as_video(earlier, media_name="kurs.mp4", slide_times=times))
    learnhouse = _LearnHouse()
    embedder = SequenceEmbedder(store=store, learnhouse=learnhouse, service_key=KEY, public_base="http://127.0.0.1:8090")

    result = asyncio.run(embedder.embed(course_id=COURSE, draft=draft(), correlation_id="c"))

    call = learnhouse.calls[0]
    assert call["lesson_id"] == "l1" and result["embeds"] == 2
    assert [e["heading"] for e in call["embeds"]] == ["Erklärvideo zum Kurs", "Interaktive Präsentation zum Kurs"]
    presentation_id = earlier.sequence_id
    assert call["embeds"][1]["url"].endswith(f"/lernsequenz/{presentation_id}?k={sequence_token(presentation_id, KEY)}")
    rebuilt = store.load(presentation_id)
    assert "ki-1-drawn_explanation" in {c.checkpoint_id for c in rebuilt.checkpoints}


def test_without_a_video_only_the_presentation_is_embedded(tmp_path: Path) -> None:
    learnhouse = _LearnHouse()
    embedder = SequenceEmbedder(store=SequenceStore(tmp_path), learnhouse=learnhouse, service_key=KEY,
                                public_base="http://127.0.0.1:8090/")
    asyncio.run(embedder.embed(course_id=COURSE, draft=draft(), correlation_id="c"))
    assert [e["heading"] for e in learnhouse.calls[0]["embeds"]] == ["Interaktive Präsentation zum Kurs"]
    assert learnhouse.calls[0]["embeds"][0]["url"].startswith("http://127.0.0.1:8090/lernsequenz/")
