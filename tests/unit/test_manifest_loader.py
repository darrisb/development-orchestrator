"""Loading a manifest from a repository (build.md section 5)."""

from __future__ import annotations

from pathlib import Path

import pytest

from apps.orchestrator.domain.errors import ManifestError
from apps.orchestrator.services.manifest_loader import (
    MANIFEST_FILENAME,
    load_manifest,
    load_repository_manifest,
    manifest_path_for,
)


def test_manifest_path_is_the_conventional_repository_file():
    assert manifest_path_for("/workspace/p") == Path("/workspace/p") / MANIFEST_FILENAME


def test_a_repository_manifest_round_trips_through_yaml(manifest_file: Path):
    manifest = load_repository_manifest(manifest_file.parent)
    assert manifest.external_id == "tracestack"
    assert [task.external_id for task in manifest.tasks] == ["TS-001", "TS-002"]


def test_a_missing_manifest_is_a_manifest_error(tmp_path: Path):
    with pytest.raises(ManifestError, match="Manifest not found"):
        load_repository_manifest(tmp_path)


def test_a_directory_in_place_of_a_manifest_is_a_manifest_error(tmp_path: Path):
    (tmp_path / MANIFEST_FILENAME).mkdir()
    with pytest.raises(ManifestError, match="Cannot read manifest"):
        load_repository_manifest(tmp_path)


def test_invalid_yaml_is_a_manifest_error(tmp_path: Path):
    path = tmp_path / MANIFEST_FILENAME
    path.write_text("tasks: [\n  - id: 'unterminated\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid YAML"):
        load_manifest(path)


def test_an_empty_manifest_is_a_manifest_error(tmp_path: Path):
    path = tmp_path / MANIFEST_FILENAME
    path.write_text("# nothing here\n", encoding="utf-8")
    with pytest.raises(ManifestError, match="is empty"):
        load_manifest(path)
