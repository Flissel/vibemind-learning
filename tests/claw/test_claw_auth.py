"""Bearer-Schutz des learning-claw-Sidecars (ASGI-Huelle, ohne Port)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from spaces.learning.claw import server

TOKEN = "t" * 40


def _inner() -> Starlette:
    async def mcp(request):  # noqa: ANN001
        return PlainTextResponse("drin")

    return Starlette(routes=[Route("/mcp", mcp, methods=["GET", "POST"])])


def _client() -> TestClient:
    return TestClient(server.BearerAuth(_inner(), TOKEN))


def test_valid_bearer_passes() -> None:
    r = _client().post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.text == "drin"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong"},
        {"Authorization": TOKEN},
        {"Authorization": f"Basic {TOKEN}"},
        {"Authorization": f"bearer {TOKEN}"},
        {"Authorization": f"Bearer {TOKEN}x"},
        {"Authorization": "Bearer "},
    ],
)
def test_missing_or_wrong_bearer_gets_empty_401(headers: dict[str, str]) -> None:
    r = _client().post("/mcp", headers=headers)
    assert r.status_code == 401
    assert r.content == b""
    assert TOKEN not in str(r.headers)


def test_unknown_path_also_needs_the_token() -> None:
    assert _client().get("/anything").status_code == 401


@pytest.mark.parametrize("value", [None, "", "short", "x" * 31])
def test_token_missing_or_short_refuses_start(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(server.TOKEN_ENV, raising=False)
    else:
        monkeypatch.setenv(server.TOKEN_ENV, value)
    with pytest.raises(SystemExit):
        server.require_token()


def test_token_of_32_chars_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(server.TOKEN_ENV, "a" * 32)
    assert server.require_token() == "a" * 32


def test_main_refuses_without_token_before_serving(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(server.TOKEN_ENV, raising=False)
    monkeypatch.delenv("LEARNING_CLAW_MCP_HOST", raising=False)
    monkeypatch.setattr(server, "_load_env_fallback", lambda: None)
    with pytest.raises(SystemExit):
        server.main()


def test_build_app_wraps_the_server_app() -> None:
    class Stub:
        def streamable_http_app(self):  # noqa: ANN201
            return _inner()

    client = TestClient(server.build_app(Stub(), TOKEN))
    assert client.post("/mcp").status_code == 401
    assert client.post("/mcp", headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 200


def test_build_app_refuses_server_without_asgi_app() -> None:
    with pytest.raises(SystemExit):
        server.build_app(object(), TOKEN)


@pytest.mark.parametrize("value", [None, "", "k" * 31])
def test_main_refuses_without_service_key(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    monkeypatch.setenv(server.TOKEN_ENV, "a" * 40)
    monkeypatch.delenv("LEARNING_CLAW_MCP_HOST", raising=False)
    monkeypatch.delenv("LEARNING_CLAW_MCP_PORT", raising=False)
    if value is None:
        monkeypatch.delenv("LEARNING_VIBEMIND_SERVICE_KEY", raising=False)
    else:
        monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", value)
    monkeypatch.setattr(server, "_load_env_fallback", lambda: None)
    import uvicorn

    def never_serve(*args: object, **kwargs: object) -> None:
        raise SystemExit("would serve")

    monkeypatch.setattr(uvicorn, "run", never_serve)
    with pytest.raises(SystemExit) as info:
        server.main()
    assert "LEARNING_VIBEMIND_SERVICE_KEY" in str(info.value.code)
    assert "k" * 31 not in str(info.value.code)


def test_service_key_of_32_chars_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_VIBEMIND_SERVICE_KEY", "k" * 32)
    assert server.require_service_key() == "k" * 32
