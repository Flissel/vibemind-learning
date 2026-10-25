"""Per-sequence link token: HMAC of the sequence id with a key derived from the service key."""

from __future__ import annotations

import hashlib
import hmac
import os


def sequence_token(sequence_id: str, service_key: str | None = None) -> str:
    key = service_key if service_key is not None else os.environ.get("LEARNING_VIBEMIND_SERVICE_KEY", "")
    if len(key) < 32:
        raise RuntimeError("Learning service key is not configured")
    derived = hashlib.sha256(b"lernsequenz:" + key.encode()).digest()
    return hmac.new(derived, sequence_id.encode(), hashlib.sha256).hexdigest()[:40]
