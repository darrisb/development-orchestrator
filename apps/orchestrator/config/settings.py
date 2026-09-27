"""Environment configuration (build.md section 48).

Infrastructure comes from the environment; project-specific behaviour comes
from each managed repository's manifest, never from here.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ..domain.enums import WorkerProfile


class WorkerBackend(StrEnum):
    DOCKER = "docker"
    #: Development fallback with weaker isolation than a container. Never use
    #: this against a repository you would not hand to an untrusted process.
    SUBPROCESS = "subprocess"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = Field(
        default="postgresql+psycopg://orchestrator:orchestrator@localhost:5432/orchestrator"
    )
    #: How long a statement may wait for a row lock before failing (concern 57).
    #: A run competes for its own rows with nothing but another run of itself,
    #: so a wait this long is contention that will not clear on its own. Zero
    #: restores PostgreSQL's default of waiting forever, which is what produced
    #: an orchestrator that could not answer its own health check.
    db_lock_timeout_seconds: float = Field(default=30.0, ge=0.0)
    #: How long a connection may sit inside an open transaction doing nothing
    #: before the server ends it. This is what bounds the *holder*: a request
    #: whose client went away leaves a live connection holding row locks, and
    #: before this only a restart released them.
    db_idle_in_transaction_timeout_seconds: float = Field(default=300.0, ge=0.0)
    artifact_root: Path = Field(default=Path("./data"))

    default_local_model_base_url: str = Field(default="http://192.168.0.126:8080/v1")
    default_local_model: str = Field(default="")
    #: Served context window. The context builder budgets against this, so it
    #: must track the endpoint's n_ctx rather than the model's trained maximum.
    local_model_context_window: int = Field(default=32768)
    local_model_timeout_seconds: int = Field(default=600)

    review_provider: str = Field(default="openai_compatible")
    review_base_url: str = Field(default="")
    review_model: str = Field(default="")
    review_api_key: str = Field(default="", repr=False)
    review_timeout_seconds: int = Field(default=300)

    # --- Reviewer (sections 21, 22 and 37) --------------------------------
    #: The reviewer's served context window. Separate from the coder's: the
    #: reviewer is usually the stronger model, and budgeting its package
    #: against the local coder's window would throw away the headroom that
    #: made it worth calling. Zero falls back to the coder's window.
    review_context_window: int = Field(default=0, ge=0)
    #: Hard ceiling for a review package. Zero derives one from the window.
    review_max_package_tokens: int = Field(default=0, ge=0)
    #: Share of the reviewer's window a package may occupy when
    #: ``review_max_package_tokens`` is unset. Higher than the coder's share:
    #: the reviewer's answer is a verdict and a list of issues, not a file.
    review_package_share: float = Field(default=0.6, gt=0.0, le=0.9)
    #: Floor for the diff inside a package. Everything else is shed before
    #: the change itself is clipped.
    review_min_diff_tokens: int = Field(default=3000, ge=1)
    #: Section 21: a reviewer less sure than this does not get to approve on
    #: its own. It never works the other way -- confidence cannot clear the
    #: section 37 gate. Zero disables the check.
    review_min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Section 37's "deleting significant files": more deletions than this in
    #: one task is a human's decision.
    review_max_deleted_files: int = Field(default=3, ge=0)
    #: Turn off the section 37 human-approval gate entirely. Off is the unsafe
    #: setting and exists for a sandbox project, never for a real repository.
    review_human_approval_enabled: bool = Field(default=True)
    #: Mask secrets in the review package before it is sent (section 36,
    #: concern 28). Unset means "on unless the reviewer is on this machine":
    #: the package carries the raw diff and the raw contents of supporting
    #: files, and a reviewer off this host is a third party as far as section 36
    #: is concerned. Setting it explicitly overrides the guess in both
    #: directions, because masking costs the reviewer the ability to comment on
    #: a masked line and only an operator knows whether that trade is worth it.
    review_redact_package: bool | None = Field(default=None)
    #: Stop spending attempts when this many consecutive reviews raise the
    #: identical non-empty set of blocking findings. Zero disables the
    #: convergence guard.
    fix_loop_stagnant_review_limit: int = Field(default=2, ge=0)
    #: Number of earlier distinct correction messages kept beside the newest
    #: findings so a later attempt does not regress an earlier fix.
    fix_loop_feedback_history_limit: int = Field(default=3, ge=0)

    git_push_enabled: bool = Field(default=False)
    git_push_remote: str = Field(default="origin")
    #: Rule 5 of section 10. Flipping this on is a deliberate operator choice,
    #: never something a run decides for itself.
    git_force_push_enabled: bool = Field(default=False)
    #: Rule 4 of section 10: a dirty managed repository aborts the run unless
    #: policy explicitly allows it.
    git_allow_dirty_start: bool = Field(default=False)
    git_author_name: str = Field(default="AI Orchestrator")
    git_author_email: str = Field(default="orchestrator@localhost")
    git_command_timeout_seconds: int = Field(default=120)
    #: Every task worktree is created beneath this directory and nowhere else,
    #: so a stray path can never land inside the managed repository itself.
    worktree_root: Path = Field(default=Path("./workspace/worktrees"))
    #: Host-side spelling of ``WORKTREE_ROOT`` when the orchestrator itself is
    #: containerized. Docker resolves bind sources on the daemon's host, not
    #: inside the orchestrator container (concern 2).
    host_worktree_root: Path | None = Field(default=None)

    # --- Context builder (section 15) ------------------------------------
    #: Hard ceiling for a context package. Zero derives one from the served
    #: window, which is the right default: the budget must follow the endpoint
    #: that will actually serve the prompt, not a number someone set once.
    context_max_tokens: int = Field(default=0, ge=0)
    #: Share of the served window a context package may occupy when
    #: ``context_max_tokens`` is unset. The rest is the task block, the system
    #: prompt, review feedback on a retry, and the answer itself.
    context_window_share: float = Field(default=0.45, gt=0.0, le=0.9)
    #: Per-item ceiling; a larger file is clipped with a visible marker rather
    #: than dropped, because the head of a file is usually the useful part.
    context_max_item_tokens: int = Field(default=2000, ge=1)
    context_max_files: int = Field(default=40, ge=0)
    #: A file larger than this is never read into a prompt at all. It exists
    #: so a stray minified bundle cannot be clipped to 2000 tokens of noise.
    context_max_file_bytes: int = Field(default=262_144, ge=1)
    #: Concern 62: absolute growth allowance for a complete writable file.
    #: Added to the source size and the proportional headroom to give medium
    #: files room for legitimate additions beyond proportional growth.
    context_absolute_growth_allowance_bytes: int = Field(default=2500, ge=0)
    context_max_decisions: int = Field(default=5, ge=0)
    context_max_lessons: int = Field(default=5, ge=0)
    context_recent_commits: int = Field(default=5, ge=0)
    #: Entries in the repository map. A map that lists every file in a large
    #: repository is not orientation, it is the budget spent on a directory
    #: listing.
    context_map_max_entries: int = Field(default=300, ge=0)

    #: Bound duplicate training copies; authoritative run artifacts remain.
    training_max_captured_per_project: int = Field(default=500, ge=1)

    # --- Worker runtime (sections 11 and 12) ------------------------------
    worker_backend: WorkerBackend = Field(default=WorkerBackend.DOCKER)
    #: Ceiling for one whole task run's worth of commands.
    worker_timeout_seconds: int = Field(default=1800, ge=1)
    #: Ceiling for a single command. Separate from the run ceiling so one
    #: hanging test suite is killed long before the run gives up.
    worker_command_timeout_seconds: int = Field(default=900, ge=1)
    #: Per-stream capture ceiling. A log past this is clipped with a visible
    #: marker; the command is still drained, so it cannot block on a full pipe.
    worker_max_output_bytes: int = Field(default=1_000_000, ge=1024)
    worker_node_image: str = Field(default="orchestrator-worker-node:latest")
    worker_java_image: str = Field(default="orchestrator-worker-java:latest")
    worker_python_image: str = Field(default="orchestrator-worker-python:latest")
    #: Section 11: restricted network by default. ``none`` means a verification
    #: command cannot reach the internet, which also means it cannot install
    #: dependencies -- the repository must already have them.
    worker_network: str = Field(default="none")
    worker_cpus: float = Field(default=2.0, gt=0)
    worker_memory: str = Field(default="4g")
    worker_pids_limit: int = Field(default=512, ge=16)
    #: ``uid:gid`` for the container. Empty means "match the orchestrator's own
    #: user", so files the worker writes into the worktree stay host-owned.
    worker_user: str = Field(default="")
    #: Executables a project needs beyond its profile's list. Comma-separated.
    #: Never overrides the never-permitted list (``domain.commands``).
    worker_extra_executables: str = Field(default="")
    #: Permit ``./script.sh``-style verification commands. Off by default: a
    #: repository script is arbitrary code, and a coder that may edit a
    #: permitted script has a shell.
    worker_allow_relative_scripts: bool = Field(default=False)
    #: Non-secret host variables to pass into a worker, comma-separated.
    #: A credential-shaped name is refused here; secrets are injected per run.
    worker_env_passthrough: str = Field(default="")
    #: Keep a failed worker's container for inspection. Leaks containers by
    #: design; never leave it on.
    worker_retain_on_failure: bool = Field(default=False)
    docker_binary: str = Field(default="docker")

    log_level: str = Field(default="INFO")

    @field_validator("artifact_root", "worktree_root")
    @classmethod
    def _resolve_directory(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("host_worktree_root")
    @classmethod
    def _resolve_optional_directory(cls, value: Path | None) -> Path | None:
        return value.expanduser().resolve() if value is not None else None

    def context_token_budget(self) -> int:
        """The package ceiling, explicit or derived from the served window."""
        if self.context_max_tokens:
            return self.context_max_tokens
        return max(1, int(self.local_model_context_window * self.context_window_share))

    def redact_review_package(self) -> bool:
        """Whether this installation masks a review package before sending it.

        The default is derived rather than fixed, and it is derived
        conservatively: anything that is not recognisably this machine counts as
        remote. An unset ``REVIEW_BASE_URL`` is treated as local because there
        is no endpoint to leak to -- that is a stub or a test, not a reviewer.
        """
        if self.review_redact_package is not None:
            return self.review_redact_package
        return not _is_local_url(self.review_base_url)

    def worker_images(self) -> dict[WorkerProfile, str]:
        """Image per worker profile (section 11)."""
        return {
            WorkerProfile.NODE: self.worker_node_image,
            WorkerProfile.JAVA: self.worker_java_image,
            WorkerProfile.PYTHON: self.worker_python_image,
        }

    def worker_extra_executables_list(self) -> tuple[str, ...]:
        return _split_list(self.worker_extra_executables)

    def worker_env_passthrough_list(self) -> tuple[str, ...]:
        return _split_list(self.worker_env_passthrough)

    @property
    def runs_dir(self) -> Path:
        return self.artifact_root / "runs"

    @property
    def training_dir(self) -> Path:
        return self.artifact_root / "training"

    @property
    def artifacts_dir(self) -> Path:
        return self.artifact_root / "artifacts"

    def masked(self) -> dict[str, object]:
        """Settings safe to log or expose over the API (section 36)."""
        data = self.model_dump(mode="json")
        for secret_key in ("review_api_key", "database_url"):
            if data.get(secret_key):
                data[secret_key] = "***redacted***"
        return data


#: Hosts that are this machine. ``host.docker.internal`` is not among them: it
#: resolves to the host from inside a container, which is this machine, but it is
#: also exactly how a container reaches a proxy it does not control, so it is
#: left for an operator to allow explicitly.
_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


def _is_local_url(url: str) -> bool:
    """Whether ``url`` points at this machine. An empty URL points nowhere."""
    if not url.strip():
        return True
    host = urlsplit(url if "//" in url else f"//{url}").hostname
    return host is not None and host.casefold() in _LOCAL_HOSTS


def _split_list(value: str) -> tuple[str, ...]:
    """Comma-separated setting to a tuple, ignoring blanks."""
    return tuple(entry.strip() for entry in value.split(",") if entry.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
