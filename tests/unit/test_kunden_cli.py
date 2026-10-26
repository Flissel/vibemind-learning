"""The customer command talks to LearnHouse's service API and prints the invite link."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from spaces.learning.services.kunden import cli


@pytest.fixture()
def service(monkeypatch):
    monkeypatch.setenv("LEARNHOUSE_LEARNING_SERVICE_URL", "http://learnhouse-api:9000/api/v1/learning")
    monkeypatch.setenv("LEARNHOUSE_LEARNING_SERVICE_KEY", "s" * 32)
    monkeypatch.setenv("LEARNING_EMBED_ANCESTORS", "http://100.67.177.45:13000")
    calls = []

    def post(url, *, headers, json, timeout):
        calls.append((url, headers, json))
        return SimpleNamespace(status_code=200, text="", json=lambda: {
            "usergroup_uuid": "usergroup_1", "name": json["name"], "courses": ["course_x"],
            "members": 0, "invite_code": "Ab12Cd34", "signup_path": "/signup?inviteCode=Ab12Cd34",
        })

    monkeypatch.setattr(cli.httpx, "post", post)
    return calls


def test_create_sends_the_customer_with_the_service_key_and_gives_the_invite_link(service) -> None:
    customer = cli.create("Musterkunde GmbH", ["7fc84b76-94fa-4ee6-878d-67ecb7c50fe5"])
    url, headers, body = service[0]
    assert url.endswith("/customers") and headers["X-LearnHouse-Learning-Service-Key"] == "s" * 32
    assert body == {"name": "Musterkunde GmbH", "course_ids": ["7fc84b76-94fa-4ee6-878d-67ecb7c50fe5"]}
    assert customer["einladung"] == "http://100.67.177.45:13000/signup?inviteCode=Ab12Cd34"


def test_without_service_settings_it_refuses(monkeypatch) -> None:
    monkeypatch.delenv("LEARNHOUSE_LEARNING_SERVICE_URL", raising=False)
    with pytest.raises(SystemExit):
        cli.create("X GmbH", ["c"])
