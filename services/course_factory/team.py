from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from autogen_agentchat.teams import RoundRobinGroupChat

from spaces.learning.services.course_factory.chapter_verification import (
    course_title,
    merge_chapters,
    split_by_chapter,
)
from spaces.learning.services.course_factory.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelInvocation,
    ROLE_CONTRACTS,
)
from spaces.learning.services.course_factory.artifact_store import (
    CourseFactoryArtifactStore,
)
from spaces.learning.services.course_factory.repository import (
    CourseFactoryRepository,
    FactoryJobRecord,
    StageArtifactInput,
)
from spaces.learning.services.course_factory.roles import build_role_agent
from spaces.learning.services.course_factory.state_machine import FactoryState
from spaces.learning.services.course_factory.schemas import CourseDraft
from spaces.learning.services.course_factory.roles.schemas import (
    ChapterVerificationOutput,
    QualityReviewOutput,
)
from spaces.learning.services.db.repository import PersistenceConflict


@dataclass(frozen=True)
class CourseTeamInput:
    job_id: str
    expected_revision: int
    correlation_id: str
    course_id: str
    audience: str
    target_outcome: str
    source_refs: tuple[str, ...]
    # Reasons the AI reviewer gave on the previous attempt.
    revision_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        UUID(self.job_id)
        UUID(self.correlation_id)
        UUID(self.course_id)
        if self.expected_revision < 1:
            raise ValueError("course team revision must be positive")
        if not self.audience.strip() or len(self.audience) > 1_000:
            raise ValueError("course team audience is invalid")
        if not self.target_outcome.strip() or len(self.target_outcome) > 2_000:
            raise ValueError("course team target outcome is invalid")
        if (
            not self.source_refs
            or len(self.source_refs) > 256
            or any(not ref or len(ref) > 20_000 for ref in self.source_refs)
            or sum(len(ref.encode("utf-8")) for ref in self.source_refs) > 450_000
        ):
            raise ValueError("course team source references are invalid")


_STAGE_GROUPS = (
    (
        FactoryState.STRUCTURING,
        ("architect", "concept_mapper"),
        FactoryState.AUTHORING,
    ),
    (FactoryState.AUTHORING, ("lesson_author",), FactoryState.ASSESSING),
    (
        FactoryState.ASSESSING,
        ("assessment_designer",),
        FactoryState.VERIFYING,
    ),
    (
        FactoryState.VERIFYING,
        ("chapter_verifier",),
        FactoryState.QUALITY_GATE,
    ),
)


class CourseAgentTeam:
    def __init__(
        self,
        repository: CourseFactoryRepository,
        gateway: ModelGateway,
        artifact_store: CourseFactoryArtifactStore,
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._artifact_store = artifact_store

    async def run(self, request: CourseTeamInput) -> FactoryJobRecord:
        job = self._repository.get(request.job_id)
        if job.revision != request.expected_revision:
            raise PersistenceConflict("course factory revision conflict")
        if job.course_id != request.course_id:
            raise PersistenceConflict("course factory course identity conflict")
        if job.state not in {
            FactoryState.STRUCTURING,
            FactoryState.AUTHORING,
            FactoryState.ASSESSING,
            FactoryState.VERIFYING,
            FactoryState.QUALITY_GATE,
        }:
            raise ValueError("course factory attempt is not in a generation stage")

        context: dict[str, object] = {
            # The draft schema carries job_id and the quality gate rejects a
            # mismatch; without it here the model can only guess (live: course id).
            "job_id": request.job_id,
            "course_id": request.course_id,
            "audience": request.audience,
            "target_outcome": request.target_outcome,
            "source_refs": list(request.source_refs),
            "role_outputs": {},
        }
        if request.revision_notes:
            # The previous attempt was sent back; every role addresses these points.
            context["reviewer_feedback"] = list(request.revision_notes)
        try:
            for stage, roles, next_state in _STAGE_GROUPS:
                if _stage_position(job.state) > _stage_position(stage):
                    prior = self._artifact_store.read_stage_output(
                        job.id,
                        attempt_number=job.attempt_number,
                        stage=stage,
                    )
                    context["role_outputs"] = {
                        **dict(context["role_outputs"]),
                        **prior,
                    }
                    continue
                if job.state is not stage:
                    raise PersistenceConflict("course factory stage changed concurrently")
                if stage is FactoryState.VERIFYING:
                    input_hash, role_outputs, evidence_refs = await self._verify_by_chapter(
                        job, request, context
                    )
                else:
                    input_hash, role_outputs, evidence_refs = await self._run_roles(
                        roles, request, context
                    )
                context["role_outputs"] = {
                    **dict(context["role_outputs"]),
                    **role_outputs,
                }
                output_artifact = self._artifact_store.write_stage_output(
                    job,
                    stage=stage,
                    payload=role_outputs,
                )
                evidence_refs.append(
                    f"learning-artifact://{output_artifact.artifact_id}"
                )
                self._repository.record_stage_artifact(
                    job.id,
                    expected_revision=job.revision,
                    artifact=StageArtifactInput(
                        stage=stage,
                        input_hash=input_hash,
                        output_hash=output_artifact.content_hash,
                        evidence_refs=tuple(evidence_refs),
                        output_artifact=output_artifact,
                    ),
                )
                if next_state is not None:
                    job = self._repository.advance(
                        job.id,
                        expected_revision=job.revision,
                        target=next_state,
                    )
            return self._repository.get(job.id)
        except ModelGatewayError:
            self._mark_failed(job.id, "model_gateway_failed")
            raise
        except RuntimeError:
            self._mark_failed(job.id, "agent_team_failed")
            raise

    async def _run_roles(
        self, roles: tuple[str, ...], request: CourseTeamInput, context: dict[str, object]
    ) -> tuple[str, dict[str, object], list[str]]:
        agents = [
            build_role_agent(role, correlation_id=request.correlation_id, gateway=self._gateway)
            for role in roles
        ]
        stage_input = {
            "context": context,
            "role_contracts": [
                {
                    "role": role,
                    "stage": ROLE_CONTRACTS[role][0],
                    "prompt_version": ROLE_CONTRACTS[role][1],
                    "output_schema_version": ROLE_CONTRACTS[role][2],
                }
                for role in roles
            ],
        }
        input_hash = _hash_json(stage_input)
        autogen_team = RoundRobinGroupChat(agents, max_turns=len(agents))
        try:
            await autogen_team.run(task=_canonical_json(stage_input))
        except RuntimeError as error:
            gateway_error = next(
                (agent.last_error for agent in agents if agent.last_error),
                None,
            )
            if gateway_error is not None:
                raise gateway_error from error
            raise
        role_outputs: dict[str, object] = {}
        evidence_refs: list[str] = []
        for agent in agents:
            if agent.last_result is None:
                raise RuntimeError("AutoGen role completed without durable output")
            role_outputs[agent.name] = agent.last_result.output.model_dump(mode="json")
            evidence_refs.append(agent.last_result.evidence_ref)
        return input_hash, role_outputs, evidence_refs

    async def _verify_by_chapter(
        self, job: FactoryJobRecord, request: CourseTeamInput, context: dict[str, object]
    ) -> tuple[str, dict[str, object], list[str]]:
        """One short verifier call per chapter, merged without a model.

        A single call rewriting the whole draft exceeded the subscription's 540 s
        limit once a course had more than about a dozen tasks.
        """
        role_outputs = dict(context["role_outputs"])
        slices = split_by_chapter(role_outputs)
        contract = ROLE_CONTRACTS["chapter_verifier"]
        stage_inputs = [
            {
                "job_id": request.job_id,
                "course_id": request.course_id,
                "audience": request.audience,
                "target_outcome": request.target_outcome,
                "chapter_index": piece.index,
                "chapter_title": piece.title,
                "concepts": piece.concepts,
                "lessons": piece.lessons,
                "activities": piece.activities,
                "source_refs": list(request.source_refs),
                **(
                    {"reviewer_feedback": list(request.revision_notes)}
                    if request.revision_notes
                    else {}
                ),
            }
            for piece in slices
        ]
        fragments: list[ChapterVerificationOutput] = []
        evidence_refs: list[str] = []
        for stage_input in stage_inputs:
            result = await self._gateway.generate(
                ModelInvocation(
                    role="chapter_verifier",
                    stage=contract[0],
                    correlation_id=request.correlation_id,
                    prompt_version=contract[1],
                    output_schema_version=contract[2],
                    input_payload=stage_input,
                ),
                ChapterVerificationOutput,
            )
            fragments.append(result.output)
            evidence_refs.append(result.evidence_ref)
        architect = role_outputs["architect"]
        merged = merge_chapters(
            fragments,
            job_id=job.id,
            attempt_number=job.attempt_number,
            title=course_title(request.target_outcome, fallback=slices[0].title if slices else "Kurs"),
            outcomes=[str(item) for item in architect.get("outcomes", [])],
            source_refs=request.source_refs,
        )
        return (
            _hash_json(stage_inputs),
            {"source_verifier": merged.model_dump(mode="json")},
            evidence_refs,
        )

    async def run_quality_review(
        self,
        request: CourseTeamInput,
        *,
        draft: CourseDraft,
        deterministic_issue_codes: tuple[str, ...],
    ) -> QualityReviewOutput:
        job = self._repository.get(request.job_id)
        if job.revision != request.expected_revision:
            raise PersistenceConflict("course factory revision conflict")
        if job.course_id != request.course_id:
            raise PersistenceConflict("course factory course identity conflict")
        if job.state is not FactoryState.QUALITY_GATE:
            raise ValueError("quality review requires quality_gate state")
        if deterministic_issue_codes:
            raise ValueError("quality review requires deterministic approval")
        agent = build_role_agent(
            "quality_reviewer",
            correlation_id=request.correlation_id,
            gateway=self._gateway,
        )
        stage_input: dict[str, object] = {
            "draft": draft.model_dump(mode="json"),
            "deterministic_issue_codes": [],
            "role_contract": {
                "role": "quality_reviewer",
                "stage": ROLE_CONTRACTS["quality_reviewer"][0],
                "prompt_version": ROLE_CONTRACTS["quality_reviewer"][1],
                "output_schema_version": ROLE_CONTRACTS["quality_reviewer"][2],
            },
        }
        autogen_team = RoundRobinGroupChat([agent], max_turns=1)
        try:
            await autogen_team.run(task=_canonical_json(stage_input))
        except RuntimeError as error:
            if agent.last_error is not None:
                self._mark_failed(job.id, "model_gateway_failed")
                raise agent.last_error from error
            self._mark_failed(job.id, "agent_team_failed")
            raise
        if agent.last_result is None or not isinstance(
            agent.last_result.output, QualityReviewOutput
        ):
            self._mark_failed(job.id, "agent_team_failed")
            raise RuntimeError("quality reviewer completed without valid output")
        role_output = agent.last_result.output.model_dump(mode="json")
        output_artifact = self._artifact_store.write_stage_output(
            job,
            stage=FactoryState.QUALITY_GATE,
            payload={"quality_reviewer": role_output},
        )
        self._repository.record_stage_artifact(
            job.id,
            expected_revision=job.revision,
            artifact=StageArtifactInput(
                stage=FactoryState.QUALITY_GATE,
                input_hash=_hash_json(stage_input),
                output_hash=output_artifact.content_hash,
                evidence_refs=(
                    agent.last_result.evidence_ref,
                    f"learning-artifact://{output_artifact.artifact_id}",
                ),
                output_artifact=output_artifact,
            ),
        )
        return agent.last_result.output

    def _mark_failed(self, job_id: str, reason_code: str) -> None:
        current = self._repository.get(job_id)
        if current.state not in {
            FactoryState.FAILED,
            FactoryState.CANCELLED,
            FactoryState.REJECTED,
            FactoryState.PUBLISHED,
        }:
            self._repository.advance(
                current.id,
                expected_revision=current.revision,
                target=FactoryState.FAILED,
                reason_code=reason_code,
            )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _stage_position(stage: FactoryState) -> int:
    order = (
        FactoryState.STRUCTURING,
        FactoryState.AUTHORING,
        FactoryState.ASSESSING,
        FactoryState.VERIFYING,
        FactoryState.QUALITY_GATE,
    )
    return order.index(stage)
