"""Abnahme-Probe: welche Werkzeuge fuehrt der learning-claw-Sidecar wirklich?

`openclaw mcp probe` kann das nicht: der Gateway kennt den Sidecar nicht (die
Werkzeuge laufen in der Claude-CLI des Host-Shims). Diese Probe fragt den Sidecar
selbst per MCP (streamable-http) nach `tools/list` und gibt die Namen aus, einen
je Zeile, sortiert. Nur Standardbibliothek; nur loopback.

Aufruf: python werkzeug_probe.py [URL]   (Vorgabe http://127.0.0.1:8139/mcp)
Rueckgabe 0 mit Namen auf stdout; 1 mit Meldung auf stderr.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from urllib.parse import urlparse

DEFAULT_URL = "http://127.0.0.1:8139/mcp"
_LOOPBACK = ("127.0.0.1", "::1", "localhost")


def is_loopback(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in _LOOPBACK


def _json_payloads(body: str) -> list[dict]:
    """JSON-Objekte aus einer Antwort: reines JSON oder SSE (`data:`-Zeilen)."""
    text = body.strip()
    if not text:
        return []
    candidates = [text] if text.startswith("{") else [
        line[5:].strip() for line in text.splitlines() if line.startswith("data:")
    ]
    out: list[dict] = []
    for item in candidates:
        try:
            parsed = json.loads(item)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def tool_names(body: str) -> list[str]:
    """Sortierte Werkzeugnamen aus der Antwort auf `tools/list`."""
    names: set[str] = set()
    for payload in _json_payloads(body):
        result = payload.get("result")
        if isinstance(result, dict):
            for tool in result.get("tools") or []:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    names.add(tool["name"])
    return sorted(names)


def _post(url: str, message: dict, session: str | None) -> tuple[str, str | None]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    request = urllib.request.Request(url, json.dumps(message).encode(), headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 -- loopback geprueft
        return response.read().decode("utf-8", "replace"), response.headers.get("Mcp-Session-Id")


def fetch_tool_names(url: str = DEFAULT_URL) -> list[str]:
    if not is_loopback(url):
        raise ValueError(f"nur loopback erlaubt, nicht {url!r}")
    _, session = _post(url, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "abnahme", "version": "1"}},
    }, None)
    _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
    body, _ = _post(url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session)
    return tool_names(body)


def main(argv: list[str]) -> int:
    url = argv[1] if len(argv) > 1 else DEFAULT_URL
    try:
        names = fetch_tool_names(url)
    except Exception as exc:  # noqa: BLE001 -- jede Stoerung ist ein Abnahmefehler
        print(f"FEHL Sidecar nicht befragbar ({url}): {exc}", file=sys.stderr)
        return 1
    if not names:
        print(f"FEHL Sidecar lieferte keine Werkzeugliste ({url})", file=sys.stderr)
        return 1
    print("\n".join(names))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
