"""Model provider abstraction (build.md section 13).

The workflow imports from here and never from a concrete adapter, so a
provider can be replaced without touching an agent.
"""

from ..domain.tokens import CHARS_PER_TOKEN
from .base import (
    ConnectionReport,
    Message,
    MessageRole,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ProviderConfig,
    StructuredSchema,
    TokenUsage,
)
from .errors import (
    InvalidModelResponse,
    ModelProviderError,
    ModelRequestRejected,
    ModelTimeout,
    ModelUnavailable,
    PromptTooLarge,
    ProviderNotConfigured,
)
from .openai_compatible import OpenAICompatibleProvider
from .registry import (
    ENV_CODER_PROVIDER_ID,
    ENV_REVIEWER_PROVIDER_ID,
    OPENAI_COMPATIBLE,
    build_provider,
    build_review_provider,
    coder_config_from_settings,
    config_from_model,
    configs_from_settings,
    provider_for_model,
    reviewer_config_from_settings,
    select_for_role,
)
from .review import (
    ModelReviewProvider,
    ReviewCall,
    ReviewerUnavailable,
    ReviewProvider,
    ReviewRequest,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "ENV_CODER_PROVIDER_ID",
    "ENV_REVIEWER_PROVIDER_ID",
    "OPENAI_COMPATIBLE",
    "ConnectionReport",
    "InvalidModelResponse",
    "Message",
    "MessageRole",
    "ModelProvider",
    "ModelProviderError",
    "ModelRequest",
    "ModelRequestRejected",
    "ModelResponse",
    "ModelTimeout",
    "ModelReviewProvider",
    "ModelUnavailable",
    "OpenAICompatibleProvider",
    "PromptTooLarge",
    "ProviderConfig",
    "ProviderNotConfigured",
    "ReviewCall",
    "ReviewProvider",
    "ReviewRequest",
    "ReviewerUnavailable",
    "StructuredSchema",
    "TokenUsage",
    "build_provider",
    "build_review_provider",
    "coder_config_from_settings",
    "config_from_model",
    "configs_from_settings",
    "provider_for_model",
    "reviewer_config_from_settings",
    "select_for_role",
]
