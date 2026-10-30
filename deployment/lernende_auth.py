"""Shared caller check for routes that LearnHouse proxies on behalf of a signed-in learner.

Both headers are required: the shared service key (compared in constant time) and the learner
id the LearnHouse backend derived from its own session.
"""

from __future__ import annotations

import hmac
import os
import re

from fastapi import Request, status

from spaces.learning.deployment.vibemind_auth import ApiError

SERVICE_HEADER = "X-LearnHouse-Learning-Service-Key"
ACTOR_HEADER = "X-Learning-Actor"
KEY_ENV = "LEARNHOUSE_LEARNING_SERVICE_KEY"
_ACTOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def lernende_person(request: Request) -> str:
    configured = os.environ.get(KEY_ENV, "")
    if len(configured) < 32:
        raise ApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "dependency_unavailable", "Learning service key is not configured")
    supplied = request.headers.get(SERVICE_HEADER, "")
    if not hmac.compare_digest(supplied.encode(), configured.encode()):
        raise ApiError(status.HTTP_403_FORBIDDEN, "forbidden", "Service key is missing or wrong")
    actor_id = request.headers.get(ACTOR_HEADER, "")
    if not _ACTOR.match(actor_id):
        raise ApiError(status.HTTP_400_BAD_REQUEST, "invalid_request", "X-Learning-Actor is required")
    return actor_id
