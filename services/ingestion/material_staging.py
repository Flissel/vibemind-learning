"""Bounded staging of caller-referenced material into Learning artifacts.

Clients never hand the Learning runtime a host path. A local import names a
POSIX path *relative to* ``LEARNING_IMPORT_ROOT``; a remote import names a
public HTTPS URL. Both end as immutable bytes beneath the artifact root plus
one ``LearningArtifact(aggregate_type="source_upload")`` row, which the
existing ``learning.material.import`` operation then ingests.

Remote fetches resolve and check every hop (including redirects) before any
request is sent and refuse private, loopback, link-local, and reserved
addresses. Residual risk: the check and the connection resolve the name
separately, so a DNS answer that changes between them (rebinding) is not
pinned. The runtime binds to loopback and is single-owner in V1.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import stat
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx
from sqlalchemy.orm import Session

from spaces.learning.contracts.course_factory_api import (
    HttpsImportSourceV1,
    LocalImportSourceV1,
)
from spaces.learning.services.db.models import LearningArtifact


MAX_MATERIAL_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 3
FETCH_TIMEOUT_SECONDS = 30.0

_MEDIA_BY_SUFFIX = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_SUFFIX_BY_MEDIA = {
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
# Servers that do not type their body; only then does the URL suffix decide.
_UNTYPED_MEDIA = frozenset({"", "application/octet-stream", "binary/octet-stream"})
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

SessionFactory = Callable[[], Session]


class MaterialStageRejected(ValueError):
    """The referenced material cannot be staged; the message is public-safe."""


@dataclass(frozen=True)
class StagedMaterial:
    artifact_id: str
    media_type: str
    content_hash: str
    size_bytes: int


@dataclass(frozen=True)
class RemoteResponse:
    status: int
    headers: Mapping[str, str]
    body: Iterator[bytes]


Fetcher = Callable[[str], RemoteResponse]
Resolver = Callable[[str], tuple[str, ...]]


def resolve_host(host: str) -> tuple[str, ...]:
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return tuple(sorted({str(info[4][0]) for info in infos}))


def httpx_fetcher(url: str) -> RemoteResponse:
    client = httpx.Client(timeout=FETCH_TIMEOUT_SECONDS, follow_redirects=False)
    request = client.build_request("GET", url)
    response = client.send(request, stream=True)

    def body() -> Iterator[bytes]:
        try:
            yield from response.iter_bytes()
        finally:
            response.close()
            client.close()

    return RemoteResponse(
        status=response.status_code,
        headers={key.lower(): value for key, value in response.headers.items()},
        body=body(),
    )


class MaterialStagingService:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        import_root: Path,
        artifact_root: Path,
        fetcher: Fetcher | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._import_root = import_root.resolve(strict=True)
        self._artifact_root = artifact_root.resolve(strict=True)
        self._fetcher = fetcher or httpx_fetcher
        self._resolver = resolver or resolve_host

    def stage(self, source: LocalImportSourceV1 | HttpsImportSourceV1) -> StagedMaterial:
        if isinstance(source, LocalImportSourceV1):
            return self._stage_local(source)
        return self._stage_remote(source)

    def _stage_local(self, source: LocalImportSourceV1) -> StagedMaterial:
        candidate = self._import_root.joinpath(*PurePosixPath(source.relative_path).parts)
        try:
            info = os.lstat(candidate)
        except OSError as error:
            raise MaterialStageRejected("local material is unavailable") from error
        if stat.S_ISLNK(info.st_mode) or (
            getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise MaterialStageRejected("local material must not be a link")
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(self._import_root) or not resolved.is_file():
            raise MaterialStageRejected("local material escaped the import root")
        media_type = _media_from_suffix(resolved.name)
        if resolved.stat().st_size > MAX_MATERIAL_BYTES:
            raise MaterialStageRejected("material exceeds the size limit")
        with resolved.open("rb") as handle:
            return self._store(_chunks(handle), media_type=media_type)

    def _stage_remote(self, source: HttpsImportSourceV1) -> StagedMaterial:
        url = source.url
        for _hop in range(MAX_REDIRECTS + 1):
            self._assert_public(url)
            try:
                response = self._fetcher(url)
            except Exception as error:
                raise MaterialStageRejected("remote material could not be fetched") from error
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get("location", "")
                if not location:
                    raise MaterialStageRejected("remote redirect has no location")
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise MaterialStageRejected("remote material is unavailable")
            declared = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if declared in _SUFFIX_BY_MEDIA:
                media_type = declared
            elif declared in _UNTYPED_MEDIA:
                media_type = _media_from_suffix(urlsplit(url).path)
            else:
                raise MaterialStageRejected("material media type is not supported")
            return self._store(response.body, media_type=media_type)
        raise MaterialStageRejected("remote material exceeded the redirect limit")

    def _assert_public(self, url: str) -> None:
        try:
            HttpsImportSourceV1(kind="https", url=url)
        except ValueError as error:
            raise MaterialStageRejected("remote material URL is not allowed") from error
        host = urlsplit(url).hostname or ""
        try:
            addresses = self._resolver(host)
        except Exception as error:
            raise MaterialStageRejected("remote host could not be resolved") from error
        if not addresses:
            raise MaterialStageRejected("remote host could not be resolved")
        for address in addresses:
            if not ipaddress.ip_address(address).is_global:
                raise MaterialStageRejected("remote material must use a public address")

    def _store(self, chunks: Iterator[bytes], *, media_type: str) -> StagedMaterial:
        artifact_id = str(uuid4())
        relative = f"uploads/{artifact_id}{_SUFFIX_BY_MEDIA[media_type]}"
        target = self._artifact_root / "uploads" / f"{artifact_id}{_SUFFIX_BY_MEDIA[media_type]}"
        partial = target.with_name(target.name + ".partial")
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        try:
            with partial.open("xb") as handle:
                for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_MATERIAL_BYTES:
                        raise MaterialStageRejected("material exceeds the size limit")
                    digest.update(chunk)
                    handle.write(chunk)
            if size == 0:
                raise MaterialStageRejected("material is empty")
            partial.replace(target)
            with self._session_factory() as session, session.begin():
                session.add(LearningArtifact(
                    id=artifact_id,
                    relative_path=relative,
                    content_hash=digest.hexdigest(),
                    media_type=media_type,
                    size_bytes=size,
                    aggregate_type="source_upload",
                    aggregate_id=artifact_id,
                    revision=1,
                ))
        except BaseException:
            partial.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return StagedMaterial(
            artifact_id=artifact_id,
            media_type=media_type,
            content_hash=digest.hexdigest(),
            size_bytes=size,
        )


def _media_from_suffix(name: str) -> str:
    media_type = _MEDIA_BY_SUFFIX.get(PurePosixPath(name).suffix.lower())
    if media_type is None:
        raise MaterialStageRejected("material media type is not supported")
    return media_type


def _chunks(handle, size: int = 1024 * 1024) -> Iterator[bytes]:
    while chunk := handle.read(size):
        yield chunk
