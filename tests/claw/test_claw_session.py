"""MCP session identity: the server fills `mcp_sitzung` from the request, never the model."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from spaces.learning.claw import server


class _Request:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers


def _context(headers: dict[str, str] | None) -> Any:
    from mcp.server.fastmcp import Context
    from mcp.shared.context import RequestContext

    request = _Request(headers) if headers is not None else None
    return Context(
        request_context=RequestContext(
            request_id=1, meta=None, session=None, lifespan_context=None, request=request
        )
    )


def test_session_id_comes_from_the_mcp_session_header() -> None:
    assert server.sitzungs_id(_context({"mcp-session-id": "abc123"})) == "abc123"


@pytest.mark.parametrize("headers", [None, {}, {"mcp-session-id": "  "}])
def test_missing_session_id_is_none(headers: dict[str, str] | None) -> None:
    assert server.sitzungs_id(_context(headers)) is None


def test_session_id_outside_a_request_is_none() -> None:
    from mcp.server.fastmcp import Context

    assert server.sitzungs_id(None) is None
    assert server.sitzungs_id(Context()) is None


def _fastmcp() -> Any:
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("learning-claw-test")
    server.register_tools(mcp)
    return mcp


def test_registered_publication_tools_hide_session_and_context() -> None:
    tools = {t.name: t for t in asyncio.run(_fastmcp().list_tools())}
    assert set(tools) == {f.__name__ for f in server.WERKZEUGE}
    props = {name: set(t.inputSchema.get("properties", {})) for name, t in tools.items()}
    assert props["veroeffentlichen"] == {"job_id", "revision", "kurs_revision"}
    assert props["veroeffentlichung_anzeigen"] == {"job_id"}
    for name, names in props.items():
        assert "mcp_sitzung" not in names and "ctx" not in names, name


def test_wrapper_passes_the_request_session_to_the_tool() -> None:
    from mcp.server.fastmcp import FastMCP

    seen: list[str | None] = []

    def probe(job_id: str, *, mcp_sitzung: str | None = None) -> dict[str, Any]:
        seen.append(mcp_sitzung)
        return {"ok": True, "daten": {"job_id": job_id}}

    mcp = FastMCP("probe")
    mcp.tool()(server.mit_sitzung(probe))
    manager = mcp._tool_manager
    asyncio.run(manager.call_tool("probe", {"job_id": "j"}, context=_context({"mcp-session-id": "s1"})))
    asyncio.run(manager.call_tool("probe", {"job_id": "j", "mcp_sitzung": "forged"}, context=_context({})))
    assert seen == ["s1", None]


def test_publication_through_the_server_without_session_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from spaces.learning.claw import werkzeuge

    def no_network() -> Any:
        raise AssertionError("no Learning call without a session id")

    monkeypatch.setattr(werkzeuge, "_client", no_network)
    result = asyncio.run(
        _fastmcp()._tool_manager.call_tool(
            "veroeffentlichen",
            {"job_id": "33333333-3333-4333-8333-333333333333", "revision": 7, "kurs_revision": 3,
             "mcp_sitzung": "forged"},
        )
    )
    assert result["ok"] is False and "Sitzung" in result["fehler"]
