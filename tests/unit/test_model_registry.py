"""Provider configuration and selection (build.md sections 13 and 31).

The rule under test throughout: selection never falls back (principle 10).
"""

from __future__ import annotations

import pytest

from apps.orchestrator.config.settings import Settings
from apps.orchestrator.domain.enums import ModelRole
from apps.orchestrator.domain.models import Model
from apps.orchestrator.providers import (
    ENV_CODER_PROVIDER_ID,
    ProviderConfig,
    ProviderNotConfigured,
    build_provider,
    coder_config_from_settings,
    config_from_model,
    configs_from_settings,
    provider_for_model,
    reviewer_config_from_settings,
    select_for_role,
)


def config(role: ModelRole, model_name: str, *, enabled: bool = True) -> ProviderConfig:
    return ProviderConfig(
        provider_id=f"test:{model_name}",
        base_url="http://localhost:8080/v1",
        model_name=model_name,
        role=role,
        enabled=enabled,
    )


def settings(**overrides) -> Settings:
    defaults = {
        "_env_file": None,
        "default_local_model_base_url": "http://localhost:8080/v1",
        "default_local_model": "qwen-coder-30b",
    }
    return Settings(**{**defaults, **overrides})


# --- Selection ---------------------------------------------------------------


def test_the_configured_provider_for_the_role_is_returned() -> None:
    chosen = select_for_role(
        ModelRole.CODER, [config(ModelRole.REVIEWER, "gpt"), config(ModelRole.CODER, "qwen")]
    )
    assert chosen.model_name == "qwen"


def test_a_reviewer_is_never_substituted_for_a_missing_coder() -> None:
    with pytest.raises(ProviderNotConfigured, match="role 'coder'"):
        select_for_role(ModelRole.CODER, [config(ModelRole.REVIEWER, "gpt")])


def test_a_disabled_provider_is_not_selected() -> None:
    with pytest.raises(ProviderNotConfigured):
        select_for_role(ModelRole.CODER, [config(ModelRole.CODER, "qwen", enabled=False)])


def test_a_requested_model_is_matched_by_name() -> None:
    chosen = select_for_role(
        ModelRole.CODER,
        [config(ModelRole.CODER, "qwen-7b"), config(ModelRole.CODER, "qwen-30b")],
        preferred_model="qwen-30b",
    )
    assert chosen.model_name == "qwen-30b"


def test_a_requested_model_that_is_not_configured_raises() -> None:
    """A manifest asking for a model this installation does not serve must
    stop the run: quietly serving a different one would make the recorded
    model_id a lie."""
    with pytest.raises(ProviderNotConfigured, match="qwen-30b"):
        select_for_role(
            ModelRole.CODER,
            [config(ModelRole.CODER, "qwen-7b")],
            preferred_model="qwen-30b",
        )


def test_an_empty_candidate_list_raises_rather_than_returning_none() -> None:
    with pytest.raises(ProviderNotConfigured):
        select_for_role(ModelRole.CODER, [])


# --- Configuration from the environment --------------------------------------


def test_the_local_coder_comes_from_the_environment() -> None:
    coder = coder_config_from_settings(settings(local_model_context_window=16384))

    assert coder.provider_id == ENV_CODER_PROVIDER_ID
    assert coder.role is ModelRole.CODER
    assert coder.context_window == 16384
    assert coder.api_key is None


def test_a_missing_local_model_name_is_reported_not_guessed() -> None:
    with pytest.raises(ProviderNotConfigured, match="DEFAULT_LOCAL_MODEL"):
        coder_config_from_settings(settings(default_local_model=""))


def test_an_unconfigured_reviewer_raises() -> None:
    with pytest.raises(ProviderNotConfigured, match="REVIEW_BASE_URL"):
        reviewer_config_from_settings(settings())


def test_an_unimplemented_reviewer_provider_raises() -> None:
    with pytest.raises(ProviderNotConfigured, match="has no adapter"):
        reviewer_config_from_settings(
            settings(
                review_provider="anthropic",
                review_base_url="http://localhost:9000/v1",
                review_model="reviewer",
            )
        )


def test_configs_from_settings_skips_what_is_not_configured() -> None:
    """A fresh installation has a coder and no reviewer; that must list, not raise."""
    configs = configs_from_settings(settings())

    assert [c.role for c in configs] == [ModelRole.CODER]


def test_the_reviewer_key_stays_out_of_the_config_repr() -> None:
    reviewer = reviewer_config_from_settings(
        settings(
            review_base_url="http://localhost:9000/v1",
            review_model="reviewer",
            review_api_key="sk-secret-value",
        )
    )

    assert reviewer.api_key == "sk-secret-value"
    assert "sk-secret-value" not in repr(reviewer)
    assert "sk-secret-value" not in str(reviewer.describe())
    assert reviewer.describe()["authenticated"] is True


# --- Configuration from a registered model row -------------------------------


def registered(**overrides) -> Model:
    defaults = {
        "provider": "openai_compatible",
        "model_name": "qwen-coder-30b",
        "role": ModelRole.CODER,
        "endpoint": "http://gpu-box:8080/v1",
    }
    return Model(**{**defaults, **overrides})


def test_a_row_becomes_provider_configuration() -> None:
    built = config_from_model(registered(context_window=32768, timeout_seconds=900))

    assert built.base_url == "http://gpu-box:8080/v1"
    assert built.context_window == 32768
    assert built.timeout_seconds == 900.0


def test_the_external_model_id_overrides_the_display_name() -> None:
    """The name an endpoint knows is often not the name an operator uses."""
    built = config_from_model(
        registered(model_name="qwen-coder-30b", external_model_id="Qwen3-Coder-30B-Q3_K_M")
    )
    assert built.model_name == "Qwen3-Coder-30B-Q3_K_M"


def test_an_api_key_is_read_from_the_named_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The database holds the variable's name; only the process holds its value."""
    monkeypatch.setenv("REVIEWER_TOKEN", "sk-from-env")
    built = config_from_model(registered(metadata={"api_key_env": "REVIEWER_TOKEN"}))

    assert built.api_key == "sk-from-env"


def test_an_unset_key_variable_yields_no_credential(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REVIEWER_TOKEN", raising=False)
    assert config_from_model(registered(metadata={"api_key_env": "REVIEWER_TOKEN"})).api_key is None


def test_a_row_naming_an_unimplemented_provider_raises() -> None:
    with pytest.raises(ProviderNotConfigured, match="no adapter"):
        provider_for_model(registered(provider="bedrock"))


def test_a_disabled_provider_cannot_be_built() -> None:
    with pytest.raises(ProviderNotConfigured, match="disabled"):
        build_provider(config(ModelRole.CODER, "qwen", enabled=False))


def test_endpoint_specific_options_travel_in_extra_body() -> None:
    """llama.cpp's knobs must not become part of the domain (section 2)."""
    built = config_from_model(registered(metadata={"extra_body": {"cache_prompt": True}}))
    assert built.extra_body == {"cache_prompt": True}
