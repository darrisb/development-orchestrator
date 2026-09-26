"""Loading a project manifest from disk (build.md section 5).

Kept separate from ``domain.manifest`` so the parser stays pure data-in,
data-out and the I/O boundary is testable on its own.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..domain.errors import ManifestError
from ..domain.manifest import ProjectManifest, parse_manifest

#: Machine-readable manifest expected at the root of every managed repository.
MANIFEST_FILENAME = "build.tasks.yaml"


def manifest_path_for(repository_path: str | Path) -> Path:
    return Path(repository_path) / MANIFEST_FILENAME


def load_manifest(path: str | Path) -> ProjectManifest:
    """Read and validate the manifest at ``path``.

    Raises:
        ManifestError: the file is missing, unreadable, not valid YAML, or does
            not describe a valid manifest.
    """
    manifest_file = Path(path)
    try:
        raw = manifest_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestError(f"Manifest not found: {manifest_file}") from None
    except OSError as exc:
        raise ManifestError(f"Cannot read manifest {manifest_file}: {exc}") from exc

    try:
        # safe_load: a manifest is untrusted configuration, never a Python object graph.
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ManifestError(f"Manifest {manifest_file} is not valid YAML: {exc}") from exc

    if document is None:
        raise ManifestError(f"Manifest {manifest_file} is empty")
    return parse_manifest(document)


def load_repository_manifest(repository_path: str | Path) -> ProjectManifest:
    return load_manifest(manifest_path_for(repository_path))
