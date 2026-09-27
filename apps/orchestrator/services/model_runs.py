"""Recording what each model call cost (build.md sections 7, 34 and 35).

``model_runs`` is the table sections 34 and 35 are arithmetic over: training
capture reads which model produced an accepted run, and model evaluation reads
tokens, duration and outcome per purpose. A call that is not recorded here did
not happen as far as either is concerned.

There is one obstacle in the schema, and this module exists to remove it.
``model_runs.model_id`` is a non-nullable foreign key to ``models``, but the
default coder and the reviewer are configured from the *environment* so that a
fresh installation runs without anyone registering a row first (section 48).
Those providers had nowhere to point.

The fix is to give them a row rather than to loosen the column: an
environment-configured provider is upserted into ``models`` the first time it
is called, keyed by the table's own natural key (provider, model name, role)
and marked as environment-derived. That keeps every call attributable and
keeps the schema honest -- a nullable ``model_id`` would have made "we do not
know which model wrote this" representable, and section 34 depends on that
never being true.

**Credentials never reach this module's writes.** A row records the endpoint
and the model name; an API key is read from the environment at call time and
is not part of a provider's identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session

from ..config.logging import get_logger
from ..domain.enums import ModelPurpose, RunStatus
from ..domain.models import Model, ModelRun
from ..domain.redaction import Redactor
from ..providers import ModelResponse, ProviderConfig, TokenUsage
from ..providers.registry import OPENAI_COMPATIBLE
from ..repositories import ModelRepository, ModelRunRepository

logger = get_logger(__name__)

#: Marks a ``models`` row the orchestrator created for itself rather than one
#: an operator registered. An operator reading the table should be able to
#: tell which rows they own.
ENVIRONMENT_SOURCE = "environment"

#: How much of a failure's own text is kept. Enough to name the error and where
#: it came from; a provider's traceback is not an audit record and pasting one
#: whole into every failed row is how a table stops being readable.
MAX_ERROR_DETAIL_CHARS = 500


def describe_error(error: BaseException) -> str:
    """What a failed call's ``error_detail`` says, safe to store.

    Sanitized because this text reaches a database an operator reads and a
    dashboard they may share, and because an exception's message is whatever
    the failing library put there -- an ``httpx`` timeout message carries the
    request URL, and a URL can carry a key. The redactor masks the shapes it
    recognises and the length is bounded so one pathological traceback cannot
    dominate the row.
    """
    redacted = Redactor.for_values([]).redact(f"{type(error).__name__}: {error}")
    collapsed = " ".join(redacted.split())
    if len(collapsed) <= MAX_ERROR_DETAIL_CHARS:
        return collapsed
    return collapsed[: MAX_ERROR_DETAIL_CHARS - 1] + "…"



@dataclass(frozen=True, slots=True)
class RecordedCall:
    """A model call and the row it left behind."""

    model: Model
    model_run: ModelRun

    @property
    def model_id(self) -> UUID:
        return self.model.id


def ensure_model(session: Session, config: ProviderConfig) -> Model:
    """The ``models`` row for ``config``, created if this is its first call.

    A config built from a registered row carries that row's id as its
    ``provider_id``, so it is looked up directly. An environment config is
    matched on the natural key and inserted if absent.

    The endpoint and the served window are refreshed on every call: an
    operator who repoints ``DEFAULT_LOCAL_MODEL_BASE_URL`` at another host
    should not end up with a row describing where the model used to be.
    """
    models = ModelRepository(session)

    registered = _registered_model(models, config)
    if registered is not None:
        return registered

    existing = models.get_by_identity(OPENAI_COMPATIBLE, config.model_name, config.role)
    if existing is not None:
        return _refreshed(models, existing, config)

    model = models.add(
        Model(
            provider=OPENAI_COMPATIBLE,
            model_name=config.model_name,
            role=config.role,
            endpoint=config.base_url,
            timeout_seconds=int(config.timeout_seconds),
            context_window=config.context_window,
            enabled=True,
            metadata={"source": ENVIRONMENT_SOURCE, "provider_id": config.provider_id},
        )
    )
    logger.info(
        "model_registered_from_environment",
        model_id=str(model.id),
        provider_id=config.provider_id,
        model_name=model.model_name,
        role=model.role.value,
    )
    return model


def record_model_call(
    session: Session,
    *,
    task_run_id: UUID,
    config: ProviderConfig,
    purpose: ModelPurpose,
    status: RunStatus,
    duration_ms: int = 0,
    usage: TokenUsage | None = None,
    prompt_artifact: str | None = None,
    response_artifact: str | None = None,
    started_at: datetime | None = None,
    error_detail: str | None = None,
    attempt: int | None = None,
    review_cycle: int | None = None,
) -> RecordedCall:
    """Record one call to a model, successful or not.

    A failed call is recorded too, and with the same detail it would have had
    on success. A table that held only the calls that worked would make every
    reliability question -- how often does this model time out, how often does
    it answer in a shape we cannot use -- unanswerable from the data (section
    35).

    ``duration_ms`` is measured by the caller, not derived here, because the
    only value that is true is the time the call actually took: a default of
    zero for a call that raised is indistinguishable from a call that never
    reached the endpoint, and a 600-second timeout recorded as zero is worse
    than no row at all -- it looks like a fact.

    ``attempt`` and ``review_cycle`` say where in the run's work the call sat.
    They are what makes a call survive as evidence: both are written by the
    loop before and during a turn, so a rollback that unwinds the turn must not
    be able to unwind the record of the call that caused it.

    ``started_at`` is the caller's because only the caller knows when the call
    began. When a caller does not bracket its call, the start is derived from the
    measured duration rather than left equal to the completion time, because a
    row saying a three-minute call started when it finished is a row that
    contradicts itself: section 35 reads ``duration_ms`` and a reader who
    sanity-checks it against the timestamps would be right to disbelieve the
    table.
    """
    model = ensure_model(session, config)
    completed = datetime.now(UTC)
    model_run = ModelRunRepository(session).add(
        ModelRun(
            task_run_id=task_run_id,
            model_id=model.id,
            purpose=purpose,
            status=status,
            input_tokens=(usage or TokenUsage()).input_tokens,
            output_tokens=(usage or TokenUsage()).output_tokens,
            duration_ms=duration_ms,
            prompt_artifact=prompt_artifact,
            response_artifact=response_artifact,
            error_detail=error_detail,
            attempt=attempt,
            review_cycle=review_cycle,
            started_at=started_at or completed - timedelta(milliseconds=duration_ms),
            completed_at=completed,
        )
    )
    logger.info(
        "model_call_recorded",
        run_id=str(task_run_id),
        model_id=str(model.id),
        model_name=model.model_name,
        purpose=purpose.value,
        status=status.value,
        duration_ms=duration_ms,
        input_tokens=model_run.input_tokens,
        output_tokens=model_run.output_tokens,
        attempt=attempt,
        review_cycle=review_cycle,
        error_detail=error_detail,
    )
    return RecordedCall(model=model, model_run=model_run)


def record_response(
    session: Session,
    response: ModelResponse,
    *,
    task_run_id: UUID,
    config: ProviderConfig,
    purpose: ModelPurpose,
    prompt_artifact: str | None = None,
    response_artifact: str | None = None,
    started_at: datetime | None = None,
    attempt: int | None = None,
    review_cycle: int | None = None,
) -> RecordedCall:
    """Record a successful call from the response it produced.

    ``started_at`` is passed rather than derived from the response because the
    response's own ``duration_ms`` is measured by the provider client, and the
    wall-clock bracket around the call is what makes the row's two timestamps
    agree with it.
    """
    return record_model_call(
        session,
        task_run_id=task_run_id,
        config=config,
        purpose=purpose,
        status=RunStatus.SUCCEEDED,
        duration_ms=response.duration_ms,
        usage=response.usage,
        prompt_artifact=prompt_artifact,
        response_artifact=response_artifact,
        started_at=started_at,
        attempt=attempt,
        review_cycle=review_cycle,
    )


def coding_purpose(is_fix_attempt: bool) -> ModelPurpose:
    """``FIX`` once a reviewer has asked for changes, ``CODE`` before that.

    Section 35 compares a model's first attempts with its corrections, which
    only works if the two are distinguishable in the table.
    """
    return ModelPurpose.FIX if is_fix_attempt else ModelPurpose.CODE


def _registered_model(models: ModelRepository, config: ProviderConfig) -> Model | None:
    """The row a database-sourced config names, if its id still resolves."""
    try:
        model_id = UUID(config.provider_id)
    except ValueError:
        return None
    return models.get(model_id)


def _refreshed(models: ModelRepository, model: Model, config: ProviderConfig) -> Model:
    changes: dict[str, object] = {}
    if model.endpoint != config.base_url:
        changes["endpoint"] = config.base_url
    if config.context_window and model.context_window != config.context_window:
        changes["context_window"] = config.context_window
    if not changes:
        return model
    logger.info(
        "model_row_refreshed",
        model_id=str(model.id),
        model_name=model.model_name,
        fields=sorted(changes),
    )
    return models.update_fields(model.id, **changes)


__all__ = [
    "ENVIRONMENT_SOURCE",
    "MAX_ERROR_DETAIL_CHARS",
    "RecordedCall",
    "coding_purpose",
    "describe_error",
    "ensure_model",
    "record_model_call",
    "record_response",
]

