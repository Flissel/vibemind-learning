from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from spaces.learning.services.db.models import Base, utc_now


class WerkstattKurs(Base):
    __tablename__ = "learning_werkstatt_kurse"

    course_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    audience: Mapped[str] = mapped_column(String(500), nullable=False)
    default_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    outcomes: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    source_provenance: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    owner_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class WerkstattAufgabe(Base):
    __tablename__ = "learning_werkstatt_aufgaben"

    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    chapter_title: Mapped[str] = mapped_column(String(300), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    herkunft_key: Mapped[str | None] = mapped_column(String(200))
    lektion_herkunft_key: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class WerkstattFassung(Base):
    __tablename__ = "learning_werkstatt_fassungen"
    __table_args__ = (
        UniqueConstraint("task_id", "number", name="learning_werkstatt_fassungen_task_number_key"),
        CheckConstraint("art IN ('neu','textkorrektur','zurueckgeholt','uebernommen')"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    based_on: Mapped[int | None] = mapped_column(Integer)
    author_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    art: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    findings: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class WerkstattAuftrag(Base):
    __tablename__ = "learning_werkstatt_auftraege"
    __table_args__ = (
        CheckConstraint("kind IN ('grobentwurf','ausformulieren')"),
        CheckConstraint("state IN ('queued','running','fertig','fehler')"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    task_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(10), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WerkstattFreigabe(Base):
    __tablename__ = "learning_werkstatt_freigaben"
    __table_args__ = (
        UniqueConstraint("course_id", "nummer", name="learning_werkstatt_freigaben_course_nummer_key"),
        CheckConstraint("zustand IN ('angefordert','freigegeben','verworfen')"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    course_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    nummer: Mapped[int] = mapped_column(Integer, nullable=False)
    zustand: Mapped[str] = mapped_column(String(16), nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    vergleich: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    befunde: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    learnhouse_revision_vorher: Mapped[int | None] = mapped_column(Integer)
    learnhouse_revision: Mapped[int | None] = mapped_column(Integer)
    approval_ref: Mapped[str | None] = mapped_column(String(128))
    angefordert_von: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    freigegeben_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
