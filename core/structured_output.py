"""
Helpers for prompt-based JSON extraction with validation and model fallback.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable, Optional

from core.llm_client import create_llm_client, get_model_candidates

logger = logging.getLogger(__name__)


class StructuredOutputError(ValueError):
    pass


def response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []):
        block_type = getattr(block, "type", None)
        if block_type == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def extract_json_object(raw_text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    candidates = [raw_text]
    if "```" in raw_text:
        chunks = raw_text.split("```")
        for chunk in chunks:
            stripped = chunk.strip()
            if stripped:
                normalized = stripped
                if normalized.lower().startswith("json"):
                    normalized = normalized[4:].strip()
                candidates.append(normalized)

    for candidate in candidates:
        for idx, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed

    raise StructuredOutputError("No JSON object found in model response.")


def generate_validated_json(
    *,
    task: str,
    messages: list[dict],
    max_tokens: int,
    validator: Callable[[dict[str, Any]], Any],
    system: Optional[str] = None,
    allow_fallback: bool = True,
) -> Any:
    client = create_llm_client()
    last_error: Exception | None = None

    for model in get_model_candidates(task, allow_fallback=allow_fallback):
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            request["system"] = system

        response = client.messages.create(**request)
        raw = response_text(response)

        try:
            data = extract_json_object(raw)
            return validator(data)
        except Exception as exc:
            last_error = exc
            logger.warning(
                "Structured output validation failed for task=%s model=%s: %s",
                task,
                model,
                exc,
            )

    raise StructuredOutputError(str(last_error or f"Structured output failed for task {task}."))
