from __future__ import annotations

import json
import hashlib
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from spaces.learning.services.course_factory.model_gateway import (
    GatewaySchemaError,
    GatewayUnavailable,
    ModelInvocation,
    ModelImageInput,
    OpenFangModelGateway,
)
from spaces.learning.services.course_factory.roles.schemas import ArchitectOutput


def _architect_payload() -> dict:
    return {
        "schema_version": "architect-v1",
        "audience": "Advanced practitioners",
        "prerequisites": ["Python"],
        "outcomes": ["Build a grounded agent system"],
        "chapters": ["Authority", "Retrieval"],
    }


def _invocation() -> ModelInvocation:
    return ModelInvocation(
        role="architect",
        stage="structuring",
        correlation_id=str(uuid4()),
        prompt_version="architect-prompt-v1",
        output_schema_version="architect-v1",
        input_payload={"course_id": str(uuid4()), "source_refs": ["source://one/2"]},
    )


@pytest.mark.asyncio
async def test_gateway_uses_only_authenticated_openfang_contract_and_correlation() -> None:
    invocation = _invocation()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://openfang.test/v1/chat/completions")
        assert request.headers["Authorization"] == "Bearer " + "k" * 32
        assert request.headers["X-VibeMind-Correlation-ID"] == invocation.correlation_id
        body = json.loads(request.content)
        assert body["model"] == "learning-course-factory"
        assert "tools" not in body
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][1]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "id": "completion-1",
                "choices": [
                    {"message": {"content": json.dumps(_architect_payload())}}
                ],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://openfang.test"
    )
    gateway = OpenFangModelGateway(
        "http://openfang.test",
        "k" * 32,
        client=client,
    )

    result = await gateway.generate(invocation, ArchitectOutput)

    assert result.output.schema_version == "architect-v1"
    assert result.evidence_ref == "openfang://completion/completion-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_retries_only_bounded_transient_failures() -> None:
    calls = 0
    sleeper = AsyncMock()

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"error": "not ready"})
        return httpx.Response(
            200,
            json={
                "id": "completion-3",
                "choices": [
                    {"message": {"content": json.dumps(_architect_payload())}}
                ],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://openfang.test"
    )
    gateway = OpenFangModelGateway(
        "http://openfang.test",
        "k" * 32,
        client=client,
        max_attempts=3,
        sleeper=sleeper,
    )

    result = await gateway.generate(_invocation(), ArchitectOutput)

    assert result.evidence_ref.endswith("completion-3")
    assert calls == 3
    assert sleeper.await_count == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_fails_closed_on_timeout_without_leaking_secret() -> None:
    secret = "s" * 32
    sleeper = AsyncMock()

    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("raw upstream timeout " + secret, request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://openfang.test"
    )
    gateway = OpenFangModelGateway(
        "http://openfang.test",
        secret,
        client=client,
        max_attempts=2,
        sleeper=sleeper,
    )

    with pytest.raises(GatewayUnavailable) as caught:
        await gateway.generate(_invocation(), ArchitectOutput)

    assert secret not in str(caught.value)
    assert sleeper.await_count == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_gateway_rejects_schema_invalid_output_without_retry() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "id": "completion-invalid",
                "choices": [{"message": {"content": "{\"schema_version\":\"wrong\"}"}}],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://openfang.test"
    )
    gateway = OpenFangModelGateway(
        "http://openfang.test",
        "k" * 32,
        client=client,
    )

    with pytest.raises(GatewaySchemaError):
        await gateway.generate(_invocation(), ArchitectOutput)

    assert calls == 1
    await client.aclose()


def test_gateway_rejects_unapproved_roles_and_credential_fields() -> None:
    values = {
        "role": "architect",
        "stage": "structuring",
        "correlation_id": str(uuid4()),
        "prompt_version": "architect-prompt-v1",
        "output_schema_version": "architect-v1",
        "input_payload": {"course_id": str(uuid4())},
    }

    with pytest.raises(ValueError, match="authorized"):
        ModelInvocation(**{**values, "role": "publisher"})
    with pytest.raises(ValueError, match="credentials"):
        ModelInvocation(
            **{**values, "input_payload": {"provider_api_key": "forbidden"}}
        )


@pytest.mark.asyncio
async def test_gateway_sends_authorized_verified_canvas_image() -> None:
    png = b"\x89PNG\r\n\x1a\nverified-canvas"
    invocation = ModelInvocation(
        role="rubric_evaluator",
        stage="evaluation",
        correlation_id=str(uuid4()),
        prompt_version="rubric-prompt-v1",
        output_schema_version="rubric-evaluation-v1",
        input_payload={"response": {"kind": "penecho_canvas"}},
        image_inputs=(ModelImageInput(
            media_type="image/png", content=png,
            sha256=hashlib.sha256(png).hexdigest(),
        ),),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][1]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(200, json={
            "id": "canvas-completion",
            "choices": [{"message": {"content": json.dumps({
                "schema_version": "rubric-evaluation-v1",
                "criteria": [{
                    "criterion_id": "authority", "awarded_points": 1,
                    "feedback": "Grounded.", "source_refs": ["source://authority/1"],
                }],
                "confidence": 0.9, "misconception_tags": [],
            })}}],
        })

    from spaces.learning.services.evaluation.schemas import RubricModelOutput

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://openfang.test")
    gateway = OpenFangModelGateway("http://openfang.test", "k" * 32, client=client)
    result = await gateway.generate(invocation, RubricModelOutput)
    assert result.evidence_ref.endswith("canvas-completion")
    await client.aclose()


def test_gateway_rejects_images_for_non_evaluator_roles() -> None:
    png = b"\x89PNG\r\n\x1a\nverified-canvas"
    with pytest.raises(ValueError, match="not authorized"):
        ModelInvocation(
            role="architect", stage="structuring", correlation_id=str(uuid4()),
            prompt_version="architect-prompt-v1", output_schema_version="architect-v1",
            input_payload={"course_id": str(uuid4())},
            image_inputs=(ModelImageInput(
                media_type="image/png", content=png,
                sha256=hashlib.sha256(png).hexdigest(),
            ),),
        )


@pytest.mark.asyncio
async def test_gateway_stops_reading_oversized_response() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1_000_001)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://openfang.test"
    )
    gateway = OpenFangModelGateway(
        "http://openfang.test",
        "k" * 32,
        client=client,
    )

    with pytest.raises(GatewaySchemaError, match="size"):
        await gateway.generate(_invocation(), ArchitectOutput)

    await client.aclose()


# --- inline prompt transport -------------------------------------------------

from spaces.learning.services.course_factory.model_gateway import (  # noqa: E402
    ROLE_PROMPTS,
    GatewayInputError,
    _canonical_json,
    build_openfang_model_gateway,
)


def _completion(content: str) -> dict:
    return {"id": "inline-1", "choices": [{"message": {"content": content}}]}


async def _run_inline(invocation: ModelInvocation, reply: str, output_model=ArchitectOutput):
    captured: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=_completion(reply))

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://openfang.test"
    )
    gateway = OpenFangModelGateway(
        "http://openfang.test", "k" * 32, client=client, prompt_transport="inline"
    )
    try:
        result = await gateway.generate(invocation, output_model)
    finally:
        await client.aclose()
    return captured["body"], result


@pytest.mark.asyncio
async def test_inline_transport_sends_one_user_message_with_role_prompt() -> None:
    invocation = _invocation()
    body, _ = await _run_inline(invocation, json.dumps(_architect_payload()))

    assert [m["role"] for m in body["messages"]] == ["user"]
    assert "response_format" not in body
    text = body["messages"][0]["content"]
    assert text.startswith(ROLE_PROMPTS["architect"])
    assert "Return only JSON matching schema architect-v1." in text
    assert text.rstrip().endswith(_canonical_json(invocation.input_payload))


@pytest.mark.asyncio
async def test_inline_transport_extracts_fenced_json() -> None:
    reply = "Here:\n```json\n" + json.dumps(_architect_payload()) + "\n```"
    _, result = await _run_inline(_invocation(), reply)

    assert result.output.schema_version == "architect-v1"
    assert result.evidence_ref == "openfang://completion/inline-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["no json", '{"a":1} {"b":2}'])
async def test_inline_transport_rejects_zero_or_two_json_objects(reply: str) -> None:
    with pytest.raises(GatewaySchemaError):
        await _run_inline(_invocation(), reply)


@pytest.mark.asyncio
async def test_inline_transport_refuses_image_roles() -> None:
    from spaces.learning.services.evaluation.schemas import RubricModelOutput

    png = b"\x89PNG\r\n\x1a\nverified-canvas"
    invocation = ModelInvocation(
        role="rubric_evaluator",
        stage="evaluation",
        correlation_id=str(uuid4()),
        prompt_version="rubric-prompt-v1",
        output_schema_version="rubric-evaluation-v1",
        input_payload={"response": {"kind": "penecho_canvas"}},
        image_inputs=(ModelImageInput(
            media_type="image/png", content=png,
            sha256=hashlib.sha256(png).hexdigest(),
        ),),
    )
    with pytest.raises(GatewayInputError):
        await _run_inline(invocation, "{}", RubricModelOutput)


def test_builder_reads_transport_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_OPENFANG_URL", "http://openfang.test")
    monkeypatch.setenv("LEARNING_OPENFANG_API_KEY", "k" * 32)
    monkeypatch.setenv("LEARNING_OPENFANG_PROMPT_TRANSPORT", "inline")
    monkeypatch.setenv("LEARNING_OPENFANG_TIMEOUT_SECONDS", "600")
    gateway = build_openfang_model_gateway()
    assert gateway.prompt_transport == "inline"
    assert gateway.timeout_seconds == 600


def test_builder_defaults_to_system_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_OPENFANG_URL", "http://openfang.test")
    monkeypatch.setenv("LEARNING_OPENFANG_API_KEY", "k" * 32)
    monkeypatch.delenv("LEARNING_OPENFANG_PROMPT_TRANSPORT", raising=False)
    monkeypatch.delenv("LEARNING_OPENFANG_TIMEOUT_SECONDS", raising=False)
    gateway = build_openfang_model_gateway()
    assert gateway.prompt_transport == "system"
    assert gateway.timeout_seconds == 60


def test_builder_rejects_unknown_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEARNING_OPENFANG_URL", "http://openfang.test")
    monkeypatch.setenv("LEARNING_OPENFANG_API_KEY", "k" * 32)
    monkeypatch.setenv("LEARNING_OPENFANG_PROMPT_TRANSPORT", "shell")
    with pytest.raises(ValueError):
        build_openfang_model_gateway()
