"""Learning's own drawing canvas behind the PenEcho learning contract.

The PenEcho fork that carried the learning bridge (/api/learning/sessions) is lost:
not in the image, not local, not on GitHub. Learning therefore serves canvas tasks
itself: this backend speaks the same contract as PenEchoClient (create, open,
save, export), so evaluation, artifact checks and the practice session stay
unchanged and the real bridge can be plugged back in. Sessions are files under
the artifact root, so they survive a restart.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import select

from spaces.learning.bridge.penecho_client import (
    PenEchoError,
    PenEchoExportV1,
    PenEchoRevisionConflict,
    PenEchoSessionV1,
)
from spaces.learning.contracts.penecho import (
    CanvasArtifactV1,
    CanvasDocumentV1,
    CanvasHelpPolicyV1,
    CanvasSaveV1,
    PenEchoLaunchContextV1,
)
from spaces.learning.services.adaptive_engine.models import (
    AdaptiveItem,
    LearningAttempt,
    LearningSession,
)
from spaces.learning.services.course_factory.repository import SessionFactory
from spaces.learning.services.evaluation.canvas_artifacts import encode_canvas_document
from spaces.learning.services.evaluation.penecho import canvas_source_reference

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_SNAPSHOT_BYTES = 20_000_000
_NAMESPACE = UUID("5f0c9a52-8a6e-4c43-9a51-8f1c2c5b7e10")


class LocalCanvasNotFound(PenEchoError):
    pass


class LocalCanvasBackend:
    """File-backed canvas sessions with PenEchoClient's method contract."""

    def __init__(self, *, root: Path, origin: str) -> None:
        self._root = root
        self.origin = origin.rstrip("/")

    def create_session(
        self, launch: PenEchoLaunchContextV1, document: CanvasDocumentV1
    ) -> PenEchoSessionV1:
        if launch.penecho_origin != self.origin:
            raise ValueError("canvas launch origin does not match the local canvas origin")
        path = self._path(launch.session_id)
        if path.exists():
            existing = self._load(launch.session_id)
            if existing["launch"] == launch.model_dump(mode="json"):
                return self._session(existing)
            # The canvas is bound to the practice session; the next canvas task of
            # the same session gets a fresh canvas. Anything else is a conflict.
            if existing["launch"]["activity_id"] == str(launch.activity_id):
                raise PenEchoError("canvas session already exists with another launch")
        state = {
            "session_ref": launch.bridge_session_ref,
            "launch": launch.model_dump(mode="json"),
            "document": document.model_copy(update={"revision": 0}).model_dump(mode="json"),
            "artifacts": [],
            "snapshot_png_base64": None,
        }
        self._store(launch.session_id, state)
        return self._session(state)

    def open_session(self, session_id: UUID, session_ref: str) -> PenEchoSessionV1:
        return self._session(self._load(session_id, session_ref))

    def save_session(
        self,
        session_id: UUID,
        session_ref: str,
        save: CanvasSaveV1,
        *,
        snapshot_png_base64: str,
    ) -> PenEchoSessionV1:
        state = self._load(session_id, session_ref)
        revision = int(state["document"]["revision"])
        if save.expected_revision != revision:
            raise PenEchoRevisionConflict("canvas revision conflict")
        try:
            snapshot = base64.b64decode(snapshot_png_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("canvas snapshot encoding is invalid") from error
        if not snapshot.startswith(_PNG_SIGNATURE) or len(snapshot) > _MAX_SNAPSHOT_BYTES:
            raise ValueError("canvas snapshot must be a PNG image")
        new_revision = revision + 1
        document = save.document.model_copy(update={"revision": new_revision})
        structured = encode_canvas_document(document)
        state["document"] = document.model_dump(mode="json")
        state["snapshot_png_base64"] = snapshot_png_base64
        state["artifacts"] = [
            self._artifact(session_id, new_revision, "structured_canvas", structured),
            self._artifact(session_id, new_revision, "rendered_snapshot", snapshot),
        ]
        self._store(session_id, state)
        return self._session(state)

    def export_session(self, session_id: UUID, session_ref: str) -> PenEchoExportV1:
        state = self._load(session_id, session_ref)
        if not state["snapshot_png_base64"]:
            raise PenEchoError("canvas has no saved drawing to submit")
        return PenEchoExportV1.model_validate(
            {
                "id": str(session_id),
                "revision": state["document"]["revision"],
                "document": state["document"],
                "artifacts": state["artifacts"],
                "snapshot_png_base64": state["snapshot_png_base64"],
            }
        )

    @staticmethod
    def _artifact(session_id: UUID, revision: int, kind: str, content: bytes) -> dict[str, object]:
        suffix = "structured.json" if kind == "structured_canvas" else "snapshot.png"
        return CanvasArtifactV1(
            artifact_id=uuid5(_NAMESPACE, f"{session_id}:{revision}:{kind}"),
            artifact_kind=kind,
            media_type="application/json" if kind == "structured_canvas" else "image/png",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            locator=f"artifact://learning/canvas/{session_id}/{revision}/{suffix}",
            canvas_revision=revision,
        ).model_dump(mode="json")

    @staticmethod
    def _session(state: dict[str, object]) -> PenEchoSessionV1:
        document = state["document"]
        return PenEchoSessionV1.model_validate(
            {
                "id": state["launch"]["session_id"],
                "revision": document["revision"],
                "launch": state["launch"],
                "document": document,
                "artifacts": state["artifacts"],
            }
        )

    def _path(self, session_id: UUID) -> Path:
        return self._root / f"{UUID(str(session_id))}.json"

    def _load(self, session_id: UUID, session_ref: str | None = None) -> dict[str, object]:
        path = self._path(session_id)
        if not path.exists():
            raise LocalCanvasNotFound("canvas session is unknown")
        state = json.loads(path.read_text(encoding="utf-8"))
        if session_ref is not None and state["session_ref"] != session_ref:
            raise PermissionError("canvas session reference does not match")
        return state

    def _store(self, session_id: UUID, state: dict[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(session_id)
        handle, temporary = tempfile.mkstemp(dir=self._root, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(state, stream)
        os.replace(temporary, path)


class LocalCanvasLauncher:
    """Creates the canvas launch for the session's current canvas task on open."""

    def __init__(self, *, session_factory: SessionFactory, backend: LocalCanvasBackend) -> None:
        self._session_factory = session_factory
        self._backend = backend

    def ensure(self, *, session_id: UUID, activity_id: UUID, session_ref: str, actor_id: str) -> None:
        try:
            current = self._backend.open_session(session_id, session_ref)
            if current.launch is not None and current.launch.activity_id == activity_id:
                return
        except LocalCanvasNotFound:
            pass
        with self._session_factory() as db:
            session = db.get(LearningSession, str(session_id))
            if session is None or session.actor_id != actor_id:
                raise PermissionError("canvas session does not belong to this person")
            attempt = db.scalar(
                select(LearningAttempt)
                .where(LearningAttempt.session_id == session.id)
                .order_by(LearningAttempt.ordinal.desc())
                .limit(1)
            )
            item = db.get(AdaptiveItem, attempt.item_id) if attempt is not None else None
            if item is None or item.id != str(activity_id) or item.item_type != "penecho_canvas":
                raise ValueError("the current task is not this canvas task")
            refs = [ref for ref in item.scoring_config.get("source_refs", []) if isinstance(ref, str)]
            training = session.mode == "training"
            launch = PenEchoLaunchContextV1(
                activity_id=activity_id,
                session_id=session_id,
                course_id=UUID(session.course_id),
                course_revision=session.course_revision,
                actor_id=actor_id,
                mode=session.mode,
                help_policy=CanvasHelpPolicyV1(
                    hint_mode="selected_region" if training else "none",
                    tutor_allowed=training,
                    max_hints=2 if training else 0,
                ),
                source_refs=tuple(canvas_source_reference(ref) for ref in refs),
                penecho_origin=self._backend.origin,
                bridge_session_ref=session_ref,
            )
        document = CanvasDocumentV1(
            project_id=uuid5(_NAMESPACE, f"{session_id}:project"),
            canvas_id=uuid5(_NAMESPACE, f"{session_id}:{activity_id}"),
            revision=0,
            background="white",
            objects=(),
        )
        self._backend.create_session(launch, document)
