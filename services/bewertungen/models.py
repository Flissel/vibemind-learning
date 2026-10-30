from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from spaces.learning.services.db.models import Base, utc_now


class KursBewertung(Base):
    __tablename__ = "learning_course_ratings"
    __table_args__ = (
        UniqueConstraint("course_id", "actor_id", name="uq_learning_course_ratings_course_actor"),
        Index("ix_learning_course_ratings_gemeldet", "gemeldet"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    kurs_titel: Mapped[str] = mapped_column(String(300), nullable=False)
    smiley: Mapped[str] = mapped_column(String(16), nullable=False)
    stichworte: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    erstellt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    aktualisiert: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    gemeldet: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
