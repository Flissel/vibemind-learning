"""Deployment of the Learning claw: gateway container, acceptance script, start script.

Nothing here starts a container, a service or a script -- the files are only inspected.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml

from spaces.learning.claw import server

CLAW_DIR = Path(__file__).resolve().parents[2] / "claw"
GATEWAY = CLAW_DIR / "gateway"
OPENCLAW_JSON = GATEWAY / "config" / "openclaw.json"
COMPOSE = GATEWAY / "docker-compose.yml"
ANBINDEN = GATEWAY / "anbinden.sh"
PROBE = GATEWAY / "werkzeug_probe.py"
START = CLAW_DIR / "scripts" / "learning-claw-starten.ps1"
MCP_JSON = CLAW_DIR / "learning-mcp.json"


def _load_probe():
    spec = importlib.util.spec_from_file_location("learning_werkzeug_probe", PROBE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_gateway_has_no_channels_and_loopback_port():
    cfg = json.loads(OPENCLAW_JSON.read_text(encoding="utf-8"))
    assert cfg["gateway"]["port"] == 18897
    assert cfg["gateway"]["bind"] == "loopback"
    assert cfg["gateway"]["auth"]["mode"] == "token"
    assert "channels" not in cfg
    shim = cfg["models"]["providers"]["shim"]
    assert shim["baseUrl"] == "http://host.docker.internal:8141/v1"
    assert [m["id"] for m in shim["models"]] == ["claude-code-sonnet"]
    assert cfg["agents"]["defaults"]["model"]["primary"] == "shim/claude-code-sonnet"
    assert cfg["tools"]["web"]["search"]["enabled"] is False
    assert cfg["tools"]["web"]["fetch"]["enabled"] is False


def test_gateway_configures_no_mcp_server():
    # The tools run inside the Claude CLI loop of the host shim, not in the gateway.
    text = OPENCLAW_JSON.read_text(encoding="utf-8")
    cfg = json.loads(text)
    assert "mcp" not in cfg
    assert "8139" not in text
    for port in ("1338", "9000", "13000", "3888"):
        assert port not in text


def test_compose_publishes_no_host_port():
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["learning-claw"]
    assert "ports" not in service
    assert service["container_name"] == "learning-claw"
    assert "host.docker.internal:host-gateway" in service["extra_hosts"]
    assert "18897" in json.dumps(service["healthcheck"])
    assert any(v.startswith("learning-claw-state:") for v in service["volumes"])
    assert any(v.startswith("learning-claw-keys:") for v in service["volumes"])


def test_workspace_copies_are_not_duplicated_in_git():
    workspace = GATEWAY / "config" / "workspace"
    assert not workspace.exists()
    text = ANBINDEN.read_text(encoding="utf-8")
    assert "$CLAW/AGENTS.md" in text and "$CLAW/skills/" in text


def test_compose_mounts_no_unused_seed_workspace():
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["learning-claw"]
    assert not any("/seed/" in v or "config/workspace" in v for v in service["volumes"])


def test_acceptance_script_reuses_only_a_well_formed_gateway_token():
    text = ANBINDEN.read_text(encoding="utf-8")
    code = re.sub(r"(?m)^\s*#.*$", "", text)
    assert "^[A-Za-z0-9_-]{32,}$" in code
    lines = code.splitlines()
    check = next(i for i, ln in enumerate(lines) if "^[A-Za-z0-9_-]{32,}$" in ln)
    fetch = next(i for i, ln in enumerate(lines) if ln.startswith('TOKEN="$(docker compose exec'))
    assert fetch < check


def test_start_script_refuses_partial_start_without_preset_token():
    text = START.read_text(encoding="utf-8")
    assert "if ($TokenErzeugt -and $Dienste.Count -lt $Alle.Count) {" in text
    block = text[text.index("if ($TokenErzeugt -and $Dienste.Count -lt $Alle.Count) {"):]
    assert "throw" in block.split("}")[0] and "-Nur" in block.split("}")[0]
    # the check comes before anything is started
    assert text.index("$Dienste.Count -lt $Alle.Count") < text.index("Start-Process")


def test_readme_says_to_set_the_token_before_starting():
    readme = (CLAW_DIR.parent / "README.md").read_text(encoding="utf-8")
    assert "LEARNING_CLAW_MCP_TOKEN" in readme and "vor dem Start" in readme


def test_start_script_declares_both_services_on_their_ports():
    text = START.read_text(encoding="utf-8")
    assert "learning_claw_mcp" in text and "learning_claw_shim" in text
    assert "8139" in text and "8141" in text
    assert "VIBEMIND_AGENT" in text and "learning-claw" in text
    assert "SHIM_EXTRA_MCP_CONFIG" in text and "learning-mcp.json" in text
    assert "MCP_TOOL_TIMEOUT" in text and "900000" in text
    assert "VIBEMIND_BUDGET_MODUL" in text and "claude_budget.py" in text
    assert "LEARNING_CLAW_SHIM_SCRIPT" in text and "marketing_shim.py" in text
    assert "spaces.learning.claw.server" in text
    assert "127.0.0.1" in text
    assert "logs\\learning" in text
    assert "-RedirectStandardOutput" in text and "-RedirectStandardError" in text
    assert "$Liste" in text and "$Nur" in text


def test_acceptance_script_checks_forbidden_tools():
    text = ANBINDEN.read_text(encoding="utf-8")
    assert re.search(r"send\\\|approve\\\|save\\\|submit", text)
    assert "werkzeug_probe.py" in text
    for port in ("1338", "9000", "13000", "3888"):
        assert port in text
    assert "docker compose cp" in text
    assert "openclaw skills" in text
    for skill in ("kurs-aus-material", "ueben", "zeichenaufgabe"):
        assert skill in text
    assert "bash -x" not in text
    assert "set -x" not in text
    assert "mcp probe" not in re.sub(r"(?m)^\s*#.*$", "", text)


def test_acceptance_script_lists_all_expected_tools():
    text = ANBINDEN.read_text(encoding="utf-8")
    for name in server.WERKZEUGE:
        assert name.__name__ in text


def test_probe_extracts_tool_names_from_json_and_sse():
    probe = _load_probe()
    body = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "b"}, {"name": "a"}]}}
    assert probe.tool_names(json.dumps(body)) == ["a", "b"]
    sse = "event: message\ndata: " + json.dumps(body) + "\n\n"
    assert probe.tool_names(sse) == ["a", "b"]
    assert probe.tool_names("") == []


def test_probe_allows_only_loopback_url():
    probe = _load_probe()
    assert probe.DEFAULT_URL == "http://127.0.0.1:8139/mcp"
    assert probe.is_loopback("http://127.0.0.1:8139/mcp")
    assert not probe.is_loopback("http://192.168.1.5:8139/mcp")
    assert not probe.is_loopback("http://host.docker.internal:8139/mcp")


def test_mcp_json_matches_probe_expectation():
    allowed = json.loads(MCP_JSON.read_text(encoding="utf-8"))["allowedTools"]
    assert {a.split("__")[-1] for a in allowed} == {f.__name__ for f in server.WERKZEUGE}


def test_acceptance_script_keeps_gateway_token_off_command_lines():
    text = ANBINDEN.read_text(encoding="utf-8")
    code = re.sub(r"(?m)^\s*#.*$", "", text)
    assert "-e NEU" not in code and "config set" not in code
    assert "mktemp" in code and "umask 077" in code
    assert "docker compose cp \"$TMP\"" in code
    assert "LEARNING_CLAW_MCP_TOKEN" in code
    assert re.search(r'grep -qE ":\(1338\|9000\|13000\|3888\)', code)


def test_start_script_passes_token_only_to_both_children_and_restores_env():
    text = START.read_text(encoding="utf-8")
    assert "RandomNumberGenerator" in text
    assert text.count("LEARNING_CLAW_MCP_TOKEN = $McpToken") == 2
    assert "} finally {" in text
    # no token in logs or on disk
    assert "Out-File" not in text and "Set-Content" not in text and "Add-Content" not in text
    assert "Write-Output $McpToken" not in text and "Write-Host $McpToken" not in text


def test_probe_requires_token_and_sends_bearer():
    probe = _load_probe()
    assert probe.auth_headers({"LEARNING_CLAW_MCP_TOKEN": "x" * 40}) == {"Authorization": "Bearer " + "x" * 40}
    import pytest

    with pytest.raises(ValueError):
        probe.auth_headers({})


def test_gitattributes_forces_lf_for_shell_scripts():
    attrs = (Path(__file__).resolve().parents[2] / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attrs


def test_acceptance_script_tolerates_missing_gateway_token_under_pipefail():
    text = ANBINDEN.read_text(encoding="utf-8")
    assert "set -euo pipefail" in text
    line = next(ln for ln in text.splitlines() if ln.startswith('TOKEN="$(docker compose exec'))
    assert "|| true)" in line


def test_start_script_rejects_short_preset_token():
    text = START.read_text(encoding="utf-8")
    assert "kuerzer als 32 Zeichen" in text
    assert "if ($McpToken -and $McpToken.Length -lt 32) {" in text
    assert "if (-not $McpToken) {" in text


def test_main_starts_uvicorn_without_proxy_headers(monkeypatch) -> None:
    """tailscale serve adds X-Forwarded-For; uvicorn must not rewrite scope["client"] from it."""
    import uvicorn

    seen: dict[str, object] = {}
    monkeypatch.setattr(server, "_load_env_fallback", lambda: None)
    monkeypatch.setattr(server, "resolve_host_port", lambda: ("127.0.0.1", 8139))
    monkeypatch.setattr(server, "require_token", lambda: "t" * 40)
    monkeypatch.setattr(server, "require_service_key", lambda: None)
    monkeypatch.setattr(server, "register_tools", lambda _s: None)
    monkeypatch.setattr(server, "build_app", lambda *_a, **_k: object())
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))

    server.main()

    assert seen["proxy_headers"] is False
    assert seen["host"] == "127.0.0.1"
