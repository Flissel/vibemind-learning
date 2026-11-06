"""MCP-Client der Huelle fuer Learning (JSON-RPC `tools/call` ueber HTTP).

Der Umschlag folgt `contracts/mcp_models.EventEnvelopeV1`. Die Lernenden-ID kommt nur aus
der Einstellung (Konstruktor), nie aus einer Anfrage. Fehler werden nur als Typname geloggt.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import uuid4

import httpx

log = logging.getLogger(__name__)

# Modellaufrufe (Bewertung, Tutor) dauern; wie im Fork: ueber dem Gateway-Timeout von 600 s.
TIMEOUT_S = 660.0
_FRISCHE_SITZUNG = "learning_session_start"


class LearningWeg(Exception):
    """Learning antwortet nicht (Netz, 5xx, keine oder kaputte Antwort, Dienst meldet `unavailable`).

    `code` traegt Learnings Fehlercode, wenn der Dienst ihn bei `unavailable` nennt
    (etwa `session_grading_unavailable`: Leser/Rubrik gerade nicht verfuegbar, wiederholbar).
    """

    def __init__(self, *args: object, code: str | None = None) -> None:
        super().__init__(*args)
        self.code = code


class LearningAbgelehnt(Exception):
    """Learning hat den Aufruf abgelehnt (`isError`)."""

    def __init__(self, nachricht: str, code: str = "abgelehnt") -> None:
        super().__init__(nachricht)
        self.nachricht = nachricht
        self.code = code


def _typ(fehler: BaseException) -> str:
    return type(fehler).__name__


def idempotenz_schluessel(tool: str, event: dict[str, Any]) -> str:
    """`hu-<sha256(tool + sortiertes Event)[:40]>`, ohne die je Aufruf neuen IDs."""
    keyed = {k: v for k, v in event.items() if k not in ("invocation_id", "correlation_id")}
    return "hu-" + hashlib.sha256((tool + json.dumps(keyed, sort_keys=True)).encode()).hexdigest()[:40]


class LernMcp:
    def __init__(
        self,
        mcp_url: str,
        actor_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = TIMEOUT_S,
    ) -> None:
        self._url = mcp_url
        self._actor = {"actor_id": actor_id, "actor_type": "local_user"}
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False)

    def rufe(
        self,
        tool: str,
        event_type: str,
        *,
        course_id: str | None = None,
        session_id: str | None = None,
        expected_revision: int | None = None,
        payload: dict[str, Any] | None = None,
        schreibend: bool = False,
        confirmation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event: dict[str, Any] = {
            "version": "1",
            "event_type": event_type,
            "actor": dict(self._actor),
            "payload": payload or {},
        }
        if course_id is not None:
            event["course_id"] = course_id
        if session_id is not None:
            event["session_id"] = session_id
        if expected_revision is not None:
            event["expected_revision"] = expected_revision
        if confirmation is not None:
            event["confirmation"] = dict(confirmation)
        if schreibend:
            # Ein neuer Start braucht einen frischen Schluessel, sonst kaeme immer die erste Sitzung zurueck.
            event["idempotency_key"] = (
                f"hu-{uuid4().hex}" if tool == _FRISCHE_SITZUNG else idempotenz_schluessel(tool, event)
            )
        event = {"invocation_id": str(uuid4()), "correlation_id": str(uuid4()), **event}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": event}}
        try:
            antwort = self._client.post(self._url, json=body)
        except httpx.HTTPError as fehler:
            log.warning("Learning-Aufruf %s nicht erreichbar: %s", tool, _typ(fehler))
            raise LearningWeg(tool) from None
        if antwort.status_code != 200:
            log.warning("Learning-Aufruf %s: HTTP %s", tool, antwort.status_code)
            raise LearningWeg(tool)
        try:
            nachricht = antwort.json()
            ergebnis = nachricht["result"]
            inhalt = json.loads(ergebnis["content"][0]["text"])
            if not isinstance(inhalt, dict):
                raise TypeError("inhalt")
        except (KeyError, IndexError, TypeError, ValueError) as fehler:
            log.warning("Learning-Aufruf %s ohne Antwort: %s", tool, _typ(fehler))
            raise LearningWeg(tool) from None
        if ergebnis.get("isError"):
            fehler = inhalt.get("error")
            if isinstance(fehler, dict):
                code = str(fehler.get("code") or "abgelehnt")
                text = str(fehler.get("message") or code)
            else:
                code = str(fehler or "abgelehnt")
                text = code
            if inhalt.get("state") == "unavailable":
                log.warning("Learning-Aufruf %s: Dienst nicht verfuegbar", tool)
                raise LearningWeg(tool, code=code[:60])
            raise LearningAbgelehnt(text[:300], code[:60])
        result = inhalt.get("result")
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise LearningWeg(tool)
        return result
