from __future__ import annotations

from pathlib import Path

from apps.orchestrator.config.settings import Settings, WorkerBackend


def _settings(**overrides) -> Settings:
    base = {
        "database_url": "postgresql+psycopg://u:secret@db:5432/orchestrator",
        "artifact_root": Path("./data"),
        "review_api_key": "sk-should-never-be-logged",
    }
    return Settings(**{**base, **overrides})


def test_push_is_disabled_by_default():
    """Section 10: never push automatically unless explicitly enabled."""
    assert Settings().git_push_enabled is False


def test_worker_backend_defaults_to_docker():
    assert Settings().worker_backend is WorkerBackend.DOCKER


def test_artifact_root_is_resolved_to_an_absolute_path():
    assert _settings().artifact_root.is_absolute()


def test_masked_settings_hide_credentials():
    """Section 36: secrets must not reach logs or API responses."""
    masked = _settings().masked()
    assert masked["review_api_key"] == "***redacted***"
    assert masked["database_url"] == "***redacted***"
    assert "secret" not in str(masked)


def test_derived_artifact_directories_sit_under_the_root():
    settings = _settings()
    for directory in (settings.runs_dir, settings.training_dir, settings.artifacts_dir):
        assert directory.parent == settings.artifact_root


def test_git_escape_hatches_are_off_by_default():
    """Force-pushing and starting dirty are operator decisions (section 10)."""
    defaults = Settings(_env_file=None)
    assert defaults.git_force_push_enabled is False
    assert defaults.git_allow_dirty_start is False
    assert defaults.git_push_remote == "origin"


def test_worktree_root_is_resolved_to_an_absolute_path():
    """Worktree paths are validated against it, so it must not be relative."""
    assert _settings(worktree_root=Path("./workspace/worktrees")).worktree_root.is_absolute()
