"""Persisted model-policy routing (build.md section 31).

Two properties are under test together, because neither is worth much alone:
a project's declared policy survives import and a re-import, and the workflow
routes each task's roles by it. The safety property is the third: an explicit
choice that cannot be honoured fails the run rather than being served by a
different model (principle 10).

Nothing here calls a model. Providers are *built* -- which opens an httpx
transport and sends nothing -- so the test can see which endpoint a task
would have used, and every transport it opens is closed again.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.enums import Complexity, ModelRole, WorkerProfile
from apps.orchestrator.domain.manifest import parse_manifest
from apps.orchestrator.domain.model_policy import ModelPolicy
from apps.orchestrator.domain.models import Project, Task
from apps.orchestrator.providers import ProviderNotConfigured
from apps.orchestrator.repositories import ProjectRepository, TaskRepository
from apps.orchestrator.services import model_providers as provider_service
from apps.orchestrator.services.projects import create_project
from apps.orchestrator.services.task_importer import import_manifest
from apps.orchestrator.workflow import WorkflowRunner
from tests.integration.test_fix_loop import ScriptedModel, reviewer

pytestmark = pytest.mark.integration

LOCAL = "http://localhost:8080/v1"


def isolated_settings() -> Settings:
    """Settings that configure no provider of their own.

    The environment contributes a default coder and reviewer to every
    resolution, and this suite is about what the *registered* models and the
    policy decide. With none configured, the candidate list is exactly the
    rows the test registered.
    """
    return Settings(_env_file=None)


def register(
    session: Session, model_name: str, role: ModelRole, *, enabled: bool = True
) -> None:
    provider_service.register_model(
        session,
        provider="openai_compatible",
        model_name=model_name,
        role=role,
        endpoint=f"{LOCAL}/{model_name}",
        enabled=enabled,
    )


def project_with(session: Session, policy: ModelPolicy) -> Project:
    return create_project(
        session,
        name="routed",
        repository_path="/workspace/routed",
        worker_profile=WorkerProfile.PYTHON,
        model_policy=policy,
    )


# --- Persistence -------------------------------------------------------------


def test_an_imported_manifest_persists_its_model_policy(
    session: Session, manifest_document: dict
):
    report = import_manifest(session, parse_manifest(manifest_document))

    project = ProjectRepository(session).get(report.project_id)
    assert project is not None
    assert project.model_policy == ModelPolicy(
        default_coder="qwen-coder-14b",
        high_complexity_coder="qwen-coder-30b",
        reviewer="primary-reviewer",
    )


def test_a_policy_round_trips_through_the_project_repository(session: Session):
    policy = ModelPolicy(default_coder="qwen-14b", reviewer="gpt-reviewer")
    created = project_with(session, policy)

    reloaded = ProjectRepository(session).get(created.id)

    assert reloaded is not None
    assert reloaded.model_policy == policy


def test_a_project_declaring_nothing_stores_an_empty_policy(session: Session):
    created = create_project(
        session, name="plain", repository_path="/workspace/plain"
    )

    reloaded = ProjectRepository(session).get(created.id)

    assert reloaded is not None
    assert reloaded.model_policy == ModelPolicy()
    assert reloaded.model_policy.is_empty


def test_a_reimport_synchronises_a_changed_policy(
    session: Session, manifest_document: dict
):
    report = import_manifest(session, parse_manifest(manifest_document))

    manifest_document["model_policy"]["high_complexity_coder"] = "qwen-coder-72b"
    del manifest_document["model_policy"]["reviewer"]
    import_manifest(session, parse_manifest(manifest_document))

    project = ProjectRepository(session).get(report.project_id)
    assert project is not None
    assert project.model_policy == ModelPolicy(
        default_coder="qwen-coder-14b", high_complexity_coder="qwen-coder-72b"
    )


def test_an_unchanged_policy_is_not_reported_as_a_change(
    session: Session, manifest_document: dict
):
    """Declarative synchronisation: the same manifest twice changes nothing."""
    manifest = parse_manifest(manifest_document)
    import_manifest(session, manifest)
    second = import_manifest(session, manifest)

    assert second.updated == ()


# --- Role resolution ---------------------------------------------------------


def test_ordinary_work_resolves_to_the_declared_default_coder(session: Session):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "qwen-30b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)
    policy = ModelPolicy(default_coder="qwen-14b", high_complexity_coder="qwen-30b")

    for complexity in (Complexity.LOW, Complexity.MEDIUM):
        selection = provider_service.resolve_roles(
            session, policy, complexity, settings=isolated_settings()
        )
        assert selection.coder.model_name == "qwen-14b"


def test_a_high_complexity_task_resolves_to_the_stronger_coder(session: Session):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "qwen-30b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)

    selection = provider_service.resolve_roles(
        session,
        ModelPolicy(default_coder="qwen-14b", high_complexity_coder="qwen-30b"),
        Complexity.HIGH,
        settings=isolated_settings(),
    )

    assert selection.coder.model_name == "qwen-30b"


def test_a_high_complexity_task_falls_back_to_the_default_coder(session: Session):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "qwen-30b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)

    selection = provider_service.resolve_roles(
        session,
        ModelPolicy(default_coder="qwen-14b"),
        Complexity.HIGH,
        settings=isolated_settings(),
    )

    assert selection.coder.model_name == "qwen-14b"


def test_two_enabled_coders_do_not_make_insertion_order_decide(session: Session):
    """The declared model wins over whichever row happens to come first."""
    register(session, "first-registered", ModelRole.CODER)
    register(session, "second-registered", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)
    settings = isolated_settings()

    unrouted = provider_service.resolve_for_role(
        session, ModelRole.CODER, settings=settings
    )
    routed = provider_service.resolve_roles(
        session,
        ModelPolicy(default_coder="second-registered"),
        Complexity.MEDIUM,
        settings=settings,
    )

    assert unrouted.model_name == "first-registered"
    assert routed.coder.model_name == "second-registered"


def test_the_declared_reviewer_is_chosen_among_several(session: Session):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)
    register(session, "strict-reviewer", ModelRole.REVIEWER)

    selection = provider_service.resolve_roles(
        session,
        ModelPolicy(reviewer="strict-reviewer"),
        Complexity.MEDIUM,
        settings=isolated_settings(),
    )

    assert selection.reviewer.model_name == "strict-reviewer"


def test_an_undeclared_reviewer_keeps_the_existing_choice(session: Session):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)
    register(session, "strict-reviewer", ModelRole.REVIEWER)

    selection = provider_service.resolve_roles(
        session, ModelPolicy(), Complexity.MEDIUM, settings=isolated_settings()
    )

    assert selection.reviewer.model_name == "local-reviewer"


def test_an_unavailable_declared_coder_fails_closed(session: Session):
    """The safety property: never another coder, however well configured."""
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)

    with pytest.raises(ProviderNotConfigured, match="gpt-example"):
        provider_service.resolve_roles(
            session,
            ModelPolicy(default_coder="gpt-example"),
            Complexity.MEDIUM,
            settings=isolated_settings(),
        )


def test_a_disabled_declared_coder_fails_closed(session: Session):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "qwen-30b", ModelRole.CODER, enabled=False)
    register(session, "local-reviewer", ModelRole.REVIEWER)

    with pytest.raises(ProviderNotConfigured, match="qwen-30b"):
        provider_service.resolve_roles(
            session,
            ModelPolicy(default_coder="qwen-30b"),
            Complexity.MEDIUM,
            settings=isolated_settings(),
        )


def test_a_coder_declared_for_the_wrong_role_fails_closed(session: Session):
    """A reviewer is not a coder, whatever the manifest calls it."""
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "strict-reviewer", ModelRole.REVIEWER)

    with pytest.raises(ProviderNotConfigured, match="strict-reviewer"):
        provider_service.resolve_roles(
            session,
            ModelPolicy(default_coder="strict-reviewer"),
            Complexity.MEDIUM,
            settings=isolated_settings(),
        )


def test_planning_runs_on_the_selected_coder_when_no_planner_is_registered(
    session: Session,
):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "qwen-30b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)

    selection = provider_service.resolve_roles(
        session,
        ModelPolicy(default_coder="qwen-14b", high_complexity_coder="qwen-30b"),
        Complexity.HIGH,
        settings=isolated_settings(),
    )

    assert selection.planner is None
    assert selection.coder.model_name == "qwen-30b"


def test_a_registered_planner_is_used_instead_of_the_coder(session: Session):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "qwen-planner", ModelRole.PLANNER)
    register(session, "local-reviewer", ModelRole.REVIEWER)

    selection = provider_service.resolve_roles(
        session,
        ModelPolicy(default_coder="qwen-14b"),
        Complexity.MEDIUM,
        settings=isolated_settings(),
    )

    assert selection.planner is not None
    assert selection.planner.model_name == "qwen-planner"


# --- What the workflow hands a task ------------------------------------------


@pytest.fixture
def runner(session: Session, engine) -> WorkflowRunner:
    """A runner that resolves its own providers, as ``configured`` builds one.

    The constructor arguments stand in for the installation's providers; the
    routing flag is what ``configured`` sets, and is what makes a declared
    policy apply.
    """
    return WorkflowRunner(
        sessionmaker(bind=engine, expire_on_commit=False),
        coder=ScriptedModel(),
        reviewer=reviewer(),
        settings=isolated_settings(),
        route_by_project_policy=True,
    )


def task_in(session: Session, project: Project, complexity: Complexity) -> Task:
    return TaskRepository(session).add(
        Task(
            project_id=project.id,
            external_task_id=f"TS-{complexity.value}",
            title="route me",
            complexity=complexity,
        )
    )


@pytest.mark.asyncio
async def test_a_task_gets_providers_built_for_its_declared_models(
    session: Session, runner: WorkflowRunner
):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "qwen-30b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)
    register(session, "strict-reviewer", ModelRole.REVIEWER)
    project = project_with(
        session,
        ModelPolicy(
            default_coder="qwen-14b",
            high_complexity_coder="qwen-30b",
            reviewer="strict-reviewer",
        ),
    )
    hard = task_in(session, project, Complexity.HIGH)
    easy = task_in(session, project, Complexity.LOW)

    for task, expected in ((hard, "qwen-30b"), (easy, "qwen-14b")):
        providers = await runner._task_providers(session, task, project)
        try:
            assert providers.owned
            assert providers.coder.config.model_name == expected
            # No planner is registered, so planning follows this task's coder
            # rather than the runner's.
            assert providers.planner is providers.coder
        finally:
            await providers.aclose()


@pytest.mark.asyncio
async def test_a_project_without_a_policy_keeps_the_runners_providers(
    session: Session, runner: WorkflowRunner
):
    """Backward compatibility: no policy, no re-resolution, no new transport."""
    project = project_with(session, ModelPolicy())
    task = task_in(session, project, Complexity.MEDIUM)

    providers = await runner._task_providers(session, task, project)

    assert not providers.owned
    assert providers.coder is runner.coder
    assert providers.planner is runner.planner
    assert providers.reviewer is runner.reviewer


@pytest.mark.asyncio
async def test_borrowed_providers_are_never_closed(
    session: Session, runner: WorkflowRunner
):
    """A task must not close transports that outlive it."""
    closed: list[str] = []
    runner.coder.aclose = lambda: _record(closed, "coder")  # type: ignore[method-assign]
    runner.reviewer.aclose = lambda: _record(closed, "reviewer")  # type: ignore[method-assign]
    project = project_with(session, ModelPolicy())
    task = task_in(session, project, Complexity.MEDIUM)

    providers = await runner._task_providers(session, task, project)
    await providers.aclose()

    assert closed == []


async def _record(closed: list[str], name: str) -> None:
    closed.append(name)


@pytest.mark.asyncio
async def test_every_owned_transport_is_released(
    session: Session, runner: WorkflowRunner
):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "qwen-planner", ModelRole.PLANNER)
    register(session, "local-reviewer", ModelRole.REVIEWER)
    project = project_with(session, ModelPolicy(default_coder="qwen-14b"))
    task = task_in(session, project, Complexity.MEDIUM)

    providers = await runner._task_providers(session, task, project)
    assert providers.planner is not providers.coder
    clients = [
        providers.coder._client,  # type: ignore[attr-defined]
        providers.planner._client,  # type: ignore[attr-defined]
    ]
    await providers.aclose()

    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
async def test_a_coder_doubling_as_planner_is_closed_once(
    session: Session, runner: WorkflowRunner
):
    """Closing the shared instance twice would close one transport twice."""
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)
    project = project_with(session, ModelPolicy(default_coder="qwen-14b"))
    task = task_in(session, project, Complexity.MEDIUM)

    providers = await runner._task_providers(session, task, project)
    assert providers.planner is providers.coder
    closed: list[str] = []
    real_aclose = providers.coder.aclose
    providers.coder.aclose = lambda: _record(closed, "coder")  # type: ignore[method-assign]

    await providers.aclose()
    assert closed == ["coder"]
    await real_aclose()


@pytest.mark.asyncio
async def test_an_unavailable_declared_model_reaches_the_workflow_as_a_failure(
    session: Session, runner: WorkflowRunner
):
    register(session, "qwen-14b", ModelRole.CODER)
    register(session, "local-reviewer", ModelRole.REVIEWER)
    project = project_with(session, ModelPolicy(default_coder="gpt-example"))
    task = task_in(session, project, Complexity.MEDIUM)

    with pytest.raises(ProviderNotConfigured, match="gpt-example"):
        await runner._task_providers(session, task, project)
