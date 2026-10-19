from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from spaces.learning.contracts.course_factory_api import (
    HttpsImportSourceV1,
    LocalImportSourceV1,
)
from spaces.learning.services.db.models import Base, LearningArtifact
from spaces.learning.services.ingestion.material_staging import (
    MAX_MATERIAL_BYTES,
    MaterialStageRejected,
    MaterialStagingService,
    RemoteResponse,
)


@dataclass
class _Fetcher:
    """Scripted HTTPS hops keyed by URL; records every requested URL."""

    responses: dict[str, RemoteResponse]
    requested: list[str] = field(default_factory=list)

    def __call__(self, url: str) -> RemoteResponse:
        self.requested.append(url)
        return self.responses[url]


def _body(*chunks: bytes) -> Iterator[bytes]:
    yield from chunks


def _resolver(mapping: dict[str, tuple[str, ...]]):
    def resolve(host: str) -> tuple[str, ...]:
        return mapping[host]

    return resolve


PUBLIC = {"example.com": ("93.184.216.34",), "cdn.example.com": ("93.184.216.35",)}


@pytest.fixture()
def roots(tmp_path: Path) -> tuple[Path, Path, sessionmaker]:
    engine = create_engine(f"sqlite:///{tmp_path / 'staging.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    import_root = tmp_path / "imports"
    artifact_root = tmp_path / "artifacts"
    import_root.mkdir()
    artifact_root.mkdir()
    return import_root, artifact_root, factory


def _stager(roots, fetcher=None, resolver=None) -> MaterialStagingService:
    import_root, artifact_root, factory = roots
    return MaterialStagingService(
        session_factory=factory,
        import_root=import_root,
        artifact_root=artifact_root,
        fetcher=fetcher,
        resolver=resolver or _resolver(PUBLIC),
    )


def _artifact(roots, artifact_id: str) -> LearningArtifact | None:
    with roots[2]() as session:
        return session.get(LearningArtifact, artifact_id)


def test_local_import_stays_beneath_allowlisted_root(roots) -> None:
    (roots[0] / "manual.md").write_text("# Safety", encoding="utf-8")
    staged = _stager(roots).stage(LocalImportSourceV1(kind="local_import", relative_path="manual.md"))

    assert staged.media_type == "text/markdown"
    assert staged.content_hash == hashlib.sha256(b"# Safety").hexdigest()
    artifact = _artifact(roots, staged.artifact_id)
    assert artifact is not None and artifact.aggregate_type == "source_upload"
    stored = roots[1] / Path(*artifact.relative_path.split("/"))
    assert stored.read_bytes() == b"# Safety"


def test_local_import_rejects_missing_and_unsupported_files(roots) -> None:
    (roots[0] / "tool.exe").write_bytes(b"MZ")
    with pytest.raises(MaterialStageRejected):
        _stager(roots).stage(LocalImportSourceV1(kind="local_import", relative_path="missing.md"))
    with pytest.raises(MaterialStageRejected):
        _stager(roots).stage(LocalImportSourceV1(kind="local_import", relative_path="tool.exe"))


def test_local_import_rejects_symlinks_out_of_the_root(roots, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    link = roots[0] / "link.md"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    with pytest.raises(MaterialStageRejected):
        _stager(roots).stage(LocalImportSourceV1(kind="local_import", relative_path="link.md"))


def test_local_import_rejects_oversized_files(roots, monkeypatch) -> None:
    (roots[0] / "big.txt").write_bytes(b"x" * 11)
    monkeypatch.setattr(
        "spaces.learning.services.ingestion.material_staging.MAX_MATERIAL_BYTES", 10
    )
    with pytest.raises(MaterialStageRejected):
        _stager(roots).stage(LocalImportSourceV1(kind="local_import", relative_path="big.txt"))


def test_remote_import_downloads_public_https_material(roots) -> None:
    fetcher = _Fetcher({
        "https://example.com/manual.md": RemoteResponse(
            status=200, headers={"content-type": "text/markdown; charset=utf-8"},
            body=_body(b"# Remote", b" manual"),
        )
    })
    staged = _stager(roots, fetcher=fetcher).stage(
        HttpsImportSourceV1(kind="https", url="https://example.com/manual.md")
    )
    assert staged.media_type == "text/markdown"
    assert staged.size_bytes == len(b"# Remote manual")


@pytest.mark.parametrize(
    "addresses",
    [("127.0.0.1",), ("10.0.0.8",), ("169.254.169.254",), ("::1",), ("93.184.216.34", "192.168.1.5")],
)
def test_remote_import_rejects_private_resolution(roots, addresses) -> None:
    fetcher = _Fetcher({})
    stager = _stager(roots, fetcher=fetcher, resolver=_resolver({"example.com": addresses}))
    with pytest.raises(MaterialStageRejected):
        stager.stage(HttpsImportSourceV1(kind="https", url="https://example.com/a.md"))
    assert fetcher.requested == []


def test_remote_import_rechecks_every_redirect_hop(roots) -> None:
    fetcher = _Fetcher({
        "https://example.com/a.md": RemoteResponse(
            status=302, headers={"location": "https://internal.example.com/a.md"}, body=_body()
        ),
    })
    resolver = _resolver({**PUBLIC, "internal.example.com": ("10.1.2.3",)})
    with pytest.raises(MaterialStageRejected):
        _stager(roots, fetcher=fetcher, resolver=resolver).stage(
            HttpsImportSourceV1(kind="https", url="https://example.com/a.md")
        )
    assert fetcher.requested == ["https://example.com/a.md"]


def test_remote_import_rejects_http_redirects_and_redirect_loops(roots) -> None:
    downgrade = _Fetcher({
        "https://example.com/a.md": RemoteResponse(
            status=301, headers={"location": "http://example.com/a.md"}, body=_body()
        ),
    })
    with pytest.raises(MaterialStageRejected):
        _stager(roots, fetcher=downgrade).stage(
            HttpsImportSourceV1(kind="https", url="https://example.com/a.md")
        )
    hops = {
        f"https://example.com/{index}.md": RemoteResponse(
            status=302,
            headers={"location": f"https://example.com/{index + 1}.md"},
            body=_body(),
        )
        for index in range(5)
    }
    loop = _Fetcher(hops)
    with pytest.raises(MaterialStageRejected):
        _stager(roots, fetcher=loop).stage(
            HttpsImportSourceV1(kind="https", url="https://example.com/0.md")
        )
    assert len(loop.requested) == 4


def test_remote_import_rejects_unsupported_type_and_errors(roots) -> None:
    html = _Fetcher({
        "https://example.com/a": RemoteResponse(
            status=200, headers={"content-type": "text/html"}, body=_body(b"<html>")
        ),
        "https://example.com/b": RemoteResponse(status=404, headers={}, body=_body()),
        "https://example.com/c.md": RemoteResponse(
            status=200, headers={"content-type": "text/html"}, body=_body(b"<html>")
        ),
    })
    for url in ("https://example.com/a", "https://example.com/b", "https://example.com/c.md"):
        with pytest.raises(MaterialStageRejected):
            _stager(roots, fetcher=html).stage(HttpsImportSourceV1(kind="https", url=url))


def test_remote_import_caps_size_and_cleans_partial_files(roots, monkeypatch) -> None:
    monkeypatch.setattr(
        "spaces.learning.services.ingestion.material_staging.MAX_MATERIAL_BYTES", 8
    )
    fetcher = _Fetcher({
        "https://example.com/big.txt": RemoteResponse(
            status=200, headers={"content-type": "text/plain"},
            body=_body(b"12345", b"67890"),
        )
    })
    with pytest.raises(MaterialStageRejected):
        _stager(roots, fetcher=fetcher).stage(
            HttpsImportSourceV1(kind="https", url="https://example.com/big.txt")
        )
    leftovers = [path for path in roots[1].rglob("*") if path.is_file()]
    assert leftovers == []


def test_untyped_remote_body_falls_back_to_the_url_suffix(roots) -> None:
    fetcher = _Fetcher({
        "https://example.com/notes.txt": RemoteResponse(
            status=200, headers={"content-type": "application/octet-stream"},
            body=_body(b"plain notes"),
        )
    })
    staged = _stager(roots, fetcher=fetcher).stage(
        HttpsImportSourceV1(kind="https", url="https://example.com/notes.txt")
    )
    assert staged.media_type == "text/plain"


def test_remote_import_timeout_is_a_rejection(roots) -> None:
    def failing(url: str) -> RemoteResponse:
        raise TimeoutError("read timed out")

    with pytest.raises(MaterialStageRejected):
        _stager(roots, fetcher=failing).stage(
            HttpsImportSourceV1(kind="https", url="https://example.com/a.md")
        )


def test_limit_is_fifty_megabytes() -> None:
    assert MAX_MATERIAL_BYTES == 50 * 1024 * 1024
