"""Recording model calls (build.md sections 7, 34 and 35; concern 3).

`model_runs.model_id` is a non-nullable foreign key, and the default coder and
the reviewer are configured from the environment rather than registered by an
operator -- so until this service existed they had nowhere to point and no
call could be recorded. These tests are about that seam: a provider gets
exactly one row, whichever way it was configured, and every call lands against
it.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from apps.orchestrator.domain.enums import (
    ModelPurpose,
    ModelRole,
    RunStatus,
    TaskStatus,
)
from apps.orchestrator.domain.models import Project, Task, TaskRun
from apps.orchestrator.providers import ProviderConfig, TokenUsage
from apps.orchestrator.repositories import (
    ModelRepository,
    ModelRunRepository,
    ProjectRepository,
    TaskRepository,
)
from apps.orchestrator.services.model_providers import register_model
from apps.orchestrator.services.model_runs import (
    coding_purpose,
    ensure_model,
    record_model_call,
)
from apps.orchestrator.services.runs import create_run

pytestmark = pytest.mark.integration


@pytest.fixture
def run(session: Session) -> TaskRun:
    project = ProjectRepository(session).add(
        Project(name="TraceStack", repository_path="/workspace/tracestack")
    )
    tasks = TaskRepository(session)
    task = tasks.add(
        Task(project_id=project.id, external_task_id="TS-004", title="Navigation")
    )
    tasks.transition(task.id, TaskStatus.READY)
    return create_run(session, task.id)


def env_config(**overrides) -> ProviderConfig:
    return ProviderConfig(
        **{
            "provider_id": "env:local-coder",
            "base_url": "http://192.168.0.126:8080/v1",
            "model_name": "qwen-coder-14b",
            "role": ModelRole.CODER,
            "context_window": 32768,
            **overrides,
        }
    )


# --- giving an environment provider somewhere to point -----------------------


def test_an_environment_provider_is_given_a_models_row(session: Session):
    model = ensure_model(session, env_config())

    assert model.model_name == "qwen-coder-14b"
    assert model.role is ModelRole.CODER
    assert model.endpoint == "http://192.168.0.126:8080/v1"
    assert model.context_window == 32768
    assert model.metadata["source"] == "environment"


def test_the_same_provider_is_not_registered_twice(session: Session):
    first = ensure_model(session, env_config())
    second = ensure_model(session, env_config())

    assert first.id == second.id
    assert len(ModelRepository(session).list()) == 1


def test_a_coder_and_a_reviewer_of_the_same_model_are_separate_rows(session: Session):
    """The table's natural key includes the role, and a reviewer is not a
    coder (principle 10). The same weights serving both is two registrations."""
    coder = ensure_model(session, env_config())
    reviewer = ensure_model(session, env_config(role=ModelRole.REVIEWER))

    assert coder.id != reviewer.id


def test_repointing_the_endpoint_updates_the_row_rather_than_adding_one(
    session: Session,
):
    """An operator who moves the model to another host should not be left
    with a row describing where it used to be."""
    original = ensure_model(session, env_config())

    moved = ensure_model(session, env_config(base_url="http://10.0.0.5:8080/v1"))

    assert moved.id == original.id
    assert moved.endpoint == "http://10.0.0.5:8080/v1"
    assert len(ModelRepository(session).list()) == 1


def test_a_registered_model_is_used_as_it_stands(session: Session):
    """A config built from a `models` row carries that row's id, so it is
    looked up rather than matched or duplicated."""
    registered = register_model(
        session,
        provider="openai_compatible",
        model_name="qwen-coder-30b",
        role=ModelRole.CODER,
        endpoint="http://192.168.0.126:8080/v1",
    )

    found = ensure_model(session, env_config(provider_id=str(registered.id)))

    assert found.id == registered.id
    assert len(ModelRepository(session).list()) == 1


def test_a_provider_id_naming_a_model_that_is_gone_falls_back_to_the_key(
    session: Session,
):
    """A stale id must not crash a run: the natural key still identifies the
    model, and a recorded call is worth more than a matching UUID."""
    model = ensure_model(session, env_config(provider_id=str(uuid4())))

    assert model.model_name == "qwen-coder-14b"


# --- recording the calls ------------------------------------------------------


def test_a_call_records_what_it_cost(session: Session, run: TaskRun):
    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=env_config(),
        purpose=ModelPurpose.CODE,
        status=RunStatus.SUCCEEDED,
        duration_ms=4200,
        usage=TokenUsage(input_tokens=1800, output_tokens=640),
        prompt_artifact="runs/RUN-1/prompt.txt",
        response_artifact="runs/RUN-1/coder-response.txt",
    )

    stored = ModelRunRepository(session).get(recorded.model_run.id)
    assert stored.model_id == recorded.model.id
    assert stored.purpose is ModelPurpose.CODE
    assert stored.status is RunStatus.SUCCEEDED
    assert stored.duration_ms == 4200
    assert stored.input_tokens == 1800
    assert stored.prompt_artifact == "runs/RUN-1/prompt.txt"


def test_unreported_usage_is_recorded_as_unknown_rather_than_zero(
    session: Session, run: TaskRun
):
    """Section 34: an endpoint that reports no usage must not contribute a
    guessed number to the training data."""
    recorded = record_model_call(
        session,
        task_run_id=run.id,
        config=env_config(),
        purpose=ModelPurpose.PLAN,
        status=RunStatus.SUCCEEDED,
    )

    assert recorded.model_run.input_tokens is None
    assert recorded.model_run.output_tokens is None


def test_tokens_for_a_run_add_up_across_calls(session: Session, run: TaskRun):
    for tokens in (TokenUsage(100, 20), TokenUsage(300, 80), TokenUsage()):
        record_model_call(
            session,
            task_run_id=run.id,
            config=env_config(),
            purpose=ModelPurpose.CODE,
            status=RunStatus.SUCCEEDED,
            usage=tokens,
        )

    assert ModelRunRepository(session).tokens_for_run(run.id) == (400, 100)


def test_calls_can_be_read_back_per_purpose(session: Session, run: TaskRun):
    for purpose in (ModelPurpose.PLAN, ModelPurpose.CODE, ModelPurpose.CODE):
        record_model_call(
            session,
            task_run_id=run.id,
            config=env_config(),
            purpose=purpose,
            status=RunStatus.SUCCEEDED,
        )

    repository = ModelRunRepository(session)
    assert len(repository.list_for_purpose(run.id, ModelPurpose.CODE)) == 2
    assert len(repository.list_for_purpose(run.id, ModelPurpose.PLAN)) == 1


def test_a_fix_is_a_different_purpose_from_first_code(session: Session):
    assert coding_purpose(is_fix_attempt=False) is ModelPurpose.CODE
    assert coding_purpose(is_fix_attempt=True) is ModelPurpose.FIX
