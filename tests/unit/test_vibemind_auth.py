from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from spaces.learning.deployment.vibemind_auth import (
    SERVICE_KEY_ENV,
    require_vibemind_context,
)


KEY = "k" * 32


def _context(**overrides: str):
    values = {
        "authorization": "Bearer " + KEY,
        "actor_id": "local-owner",
        "space_id": "learning",
        "roles": "learning.author,learning.reviewer",
        "correlation_id": str(uuid4()),
    }
    values.update(overrides)
    return require_vibemind_context(**values)


def test_request_context_requires_valid_key_learning_space_and_role(monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_KEY_ENV, KEY)
    context = _context()
    context.require("learning.author")
    assert context.space_id == "learning"
    with pytest.raises(HTTPException) as denied:
        context.require("learning.publisher")
    assert denied.value.status_code == 403


def test_insecure_server_configuration_is_unavailable(monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_KEY_ENV, "short")
    with pytest.raises(HTTPException) as error:
        _context(authorization="Bearer short")
    assert error.value.status_code == 503


def test_missing_configuration_is_unavailable(monkeypatch) -> None:
    monkeypatch.delenv(SERVICE_KEY_ENV, raising=False)
    with pytest.raises(HTTPException) as error:
        _context()
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "authorization", ["", "Bearer " + "x" * 32, KEY, "Basic " + KEY, "Bearer"]
)
def test_invalid_credentials_are_unauthenticated(monkeypatch, authorization: str) -> None:
    monkeypatch.setenv(SERVICE_KEY_ENV, KEY)
    with pytest.raises(HTTPException) as error:
        _context(authorization=authorization)
    assert error.value.status_code == 401


def test_space_normalizes_to_canonical_learning(monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_KEY_ENV, KEY)
    assert _context(space_id=" Learning ").space_id == "learning"
    with pytest.raises(HTTPException) as error:
        _context(space_id="course-factory")
    assert error.value.status_code == 403


def test_unknown_roles_and_unsafe_actors_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_KEY_ENV, KEY)
    with pytest.raises(HTTPException) as role:
        _context(roles="learning.author,learning.admin")
    assert role.value.status_code == 403
    with pytest.raises(HTTPException) as empty:
        _context(roles="")
    assert empty.value.status_code == 403
    with pytest.raises(HTTPException) as actor:
        _context(actor_id="../root")
    assert actor.value.status_code == 401


def test_correlation_id_must_be_a_uuid(monkeypatch) -> None:
    monkeypatch.setenv(SERVICE_KEY_ENV, KEY)
    with pytest.raises(HTTPException) as error:
        _context(correlation_id="not-a-uuid")
    assert error.value.status_code == 400
