"""
Shared LLM client configuration.
Routes Anthropic Messages API calls through OpenRouter while keeping the
existing message/tool format intact.
"""
import os
from typing import Optional

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
