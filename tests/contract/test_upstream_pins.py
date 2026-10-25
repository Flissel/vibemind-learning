from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

import yaml


# The Learning Space is its own repository (mounted at spaces/learning in
# vibemind-os); it owns the LearnHouse and PenEcho gitlinks itself.
LEARNING_ROOT = Path(__file__).resolve().parents[2]
LOCK_PATH = LEARNING_ROOT / "deployment" / "upstream-lock.yml"
README_PATH = LEARNING_ROOT / "README.md"

EXPECTED = {
    "learnhouse": {
        "path": "learnhouse",
        "url": "https://github.com/Flissel/learnhouse.git",
        "upstream": "https://github.com/learnhouse/learnhouse.git",
        # vibemind/learning: the Learning extension rescued from the running image (25.10.);
        # the old pin 1363478 existed in no branch of the fork.
        "commit": "ec774980429b52b20c2adb4071d5fae7bb428200",
        "license": "AGPL-3.0-only",
    },
    "penecho": {
        "path": "penecho",
        "url": "https://github.com/Flissel/penecho.git",
        "upstream": "https://github.com/penecho/penecho.git",
        "commit": "0327c3977fdfad861a218d96b5522f3da284d68d",
        "license": "AGPL-3.0-only",
    },
}


def _gitlinks() -> dict[str, tuple[str, str]]:
    result = subprocess.run(
        ["git", "ls-files", "--stage", "--", "learnhouse", "penecho"],
        cwd=LEARNING_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    links: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        metadata, path = line.split("\t", 1)
        mode, object_id, _stage = metadata.split()
        if mode == "160000":
            links[path] = (mode, object_id)
    return links


def test_learning_upstreams_are_exact_controlled_gitlinks() -> None:
    modules = configparser.ConfigParser()
    modules.read(LEARNING_ROOT / ".gitmodules", encoding="utf-8")
    links = _gitlinks()

    for expected in EXPECTED.values():
        section = f'submodule "{expected["path"]}"'
        assert modules[section]["path"] == expected["path"]
        assert modules[section]["url"] == expected["url"]
        assert links[expected["path"]] == ("160000", expected["commit"])


def test_upstream_lock_records_provenance_and_license() -> None:
    lock = yaml.safe_load(LOCK_PATH.read_text(encoding="utf-8"))
    assert lock["version"] == 1
    assert lock["sources"] == EXPECTED


def test_learning_readme_documents_update_and_baseline_procedure() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    for required_text in (
        "AGPL-3.0-only",
        "corresponding source",
        "git fetch upstream",
        "baseline",
        "LearnHouse",
        "PenEcho",
    ):
        assert required_text in readme
