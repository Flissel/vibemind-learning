from __future__ import annotations

from collections.abc import Mapping

from spaces.learning.bridge.dispatcher import ApplicationGateway, ApplicationOutcomeV1
from spaces.learning.contracts.events import LearningToolName
from spaces.learning.contracts.mcp_models import ToolRequestV1
from spaces.learning.contracts.outcomes import TruthReadbackV1
from spaces.learning.services.course_factory.application import (
    CourseFactoryApplication,
)
from spaces.learning.services.course_factory.artifact_store import (
    CourseFactoryArtifactStore,
)
from spaces.learning.services.course_factory.publisher import CourseDraftPublisher
from spaces.learning.services.course_factory.repository import (
    CourseFactoryRepository,
    SessionFactory,
)


class CourseFactoryGateway:
    """MCP compatibility adapter; all lifecycle logic lives in the application."""

    def __init__(
        self,
        application: CourseFactoryApplication | None = None,
        *,
        session_factory: SessionFactory | None = None,
        repository: CourseFactoryRepository | None = None,
        publisher: CourseDraftPublisher | None = None,
        artifact_store: CourseFactoryArtifactStore | None = None,
    ) -> None:
        if application is None:
            if (
                session_factory is None
                or repository is None
                or publisher is None
                or artifact_store is None
            ):
                raise ValueError("CourseFactoryGateway requires an application")
            application = CourseFactoryApplication(
                session_factory=session_factory,
                repository=repository,
                publisher=publisher,
                artifact_store=artifact_store,
            )
        self.application = application

    def execute(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        return self.application.execute(request)

    def readback(
        self, request: ToolRequestV1, outcome: ApplicationOutcomeV1
    ) -> TruthReadbackV1 | None:
        return self.application.readback(request, outcome)


class GeneratedCourseRouter:
    def __init__(
        self, factory: CourseFactoryGateway, fallback: ApplicationGateway
    ) -> None:
        self._factory = factory
        self._fallback = fallback

    def _selected(self, request: ToolRequestV1) -> ApplicationGateway:
        return self._factory if "job_id" in request.event.payload else self._fallback

    def execute(self, request: ToolRequestV1) -> ApplicationOutcomeV1:
        return self._selected(request).execute(request)

    def readback(
        self, request: ToolRequestV1, outcome: ApplicationOutcomeV1
    ) -> TruthReadbackV1 | None:
        return self._selected(request).readback(request, outcome)


def build_generation_gateways(
    *,
    application: CourseFactoryApplication,
    learnhouse_fallback: ApplicationGateway,
) -> Mapping[LearningToolName, ApplicationGateway]:
    factory = CourseFactoryGateway(application)
    return {
        LearningToolName.COURSE_GENERATE: factory,
        LearningToolName.GENERATION_STATUS: factory,
        LearningToolName.COURSE_REVIEW: GeneratedCourseRouter(
            factory, learnhouse_fallback
        ),
        LearningToolName.COURSE_PUBLISH: GeneratedCourseRouter(
            factory, learnhouse_fallback
        ),
    }
