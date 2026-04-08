"""
Shared LLM client configuration.
Routes Anthropic Messages API calls through OpenRouter while keeping the
existing message/tool format intact.
"""
import os
import random
import time
from typing import Any, Callable, Optional

import anthropic

from config import settings


_TASK_MODELS = {
    "relevance": settings.OPENROUTER_MODEL_RELEVANCE,
    "financial": settings.OPENROUTER_MODEL_FINANCIAL,
    "classification": settings.OPENROUTER_MODEL_CLASSIFICATION,
    "vision": settings.OPENROUTER_MODEL_VISION,
}


def create_llm_client() -> anthropic.Anthropic:
    # The Anthropic SDK falls back to ANTHROPIC_API_KEY from the environment if present.
    # Clear it so OpenRouter bearer auth is the only authentication path used.
    os.environ.pop("ANTHROPIC_API_KEY", None)
    return anthropic.Anthropic(
        auth_token=settings.OPENROUTER_API_KEY,
        base_url=settings.OPENROUTER_BASE_URL,
    )


def get_model_name(task: Optional[str] = None) -> str:
    if task and _TASK_MODELS.get(task):
        return _TASK_MODELS[task]
    return settings.OPENROUTER_MODEL


def get_model_candidates(task: Optional[str] = None, *, allow_fallback: bool = True) -> list[str]:
    primary = get_model_name(task)
    models = [primary]
    if allow_fallback and primary != settings.OPENROUTER_MODEL:
        models.append(settings.OPENROUTER_MODEL)
    return models


_FREE_RETRY_DELAYS = [5, 10, 20]  # seconds between retries for :free models


def call_with_free_model_retry(fn: Callable[[], Any], model: str) -> Any:
    """Call fn(). For :free models, retry up to 3 times on 429 with exponential backoff.
    For all other models, passes through immediately with no retry logic."""
    import anthropic as _anthropic

    if ":free" not in model:
        return fn()

    last_exc: Exception | None = None
    for delay in [0] + _FREE_RETRY_DELAYS:
        if delay > 0:
            time.sleep(delay + random.uniform(0, delay * 0.2))
        try:
            return fn()
        except _anthropic.RateLimitError as exc:
            last_exc = exc
        except Exception:
            raise  # non-429 errors propagate immediately

    raise last_exc
