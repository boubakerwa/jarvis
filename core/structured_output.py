"""
Helpers for prompt-based JSON extraction with validation and model fallback.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Callable, Optional

from core.llmops import record_llm_call
from core.llm_client import call_with_free_model_retry, create_llm_client, get_model_candidates
from core.tracing import generation_cost_details, generation_usage_details, start_generation

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
    validator_name = getattr(validator, "__name__", validator.__class__.__name__)
    model_candidates = get_model_candidates(task, allow_fallback=allow_fallback)

    for model in model_candidates:
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        if system:
            request["system"] = system

        started_at = datetime.now(timezone.utc).isoformat()
        started_clock = monotonic()
        with start_generation(
            name=f"structured-output:{task}",
            input={
                "message_count": len(messages),
                "system_chars": len(system or ""),
                "message_chars": sum(len(str(message.get("content", ""))) for message in messages),
            },
            metadata={
                "task": task,
                "validator": validator_name,
                "model_candidates": model_candidates,
                "fallback_used": model != model_candidates[0],
            },
            model=model,
            model_parameters={"max_tokens": max_tokens},
        ) as generation:
            try:
                response = call_with_free_model_retry(lambda: client.messages.create(**request), model)
            except Exception as exc:
                record_llm_call(
                    task=task,
                    model=model,
                    status="api_error",
                    started_at=started_at,
                    latency_ms=(monotonic() - started_clock) * 1000,
                    error=str(exc),
                    metadata={
                        "channel": "structured_output",
                        "validator": validator_name,
                        "model_candidates": model_candidates,
                        "fallback_used": model != model_candidates[0],
                    },
                )
                generation.update(
                    metadata={"task": task, "validator": validator_name, "status": "api_error"},
                    status_message=str(exc),
                )
                raise

            raw = response_text(response)

            try:
                data = extract_json_object(raw)
                validated = validator(data)
                record_llm_call(
                    task=task,
                    model=model,
                    status="ok",
                    started_at=started_at,
                    latency_ms=(monotonic() - started_clock) * 1000,
                    response=response,
                    metadata={
                        "channel": "structured_output",
                        "validator": validator_name,
                        "model_candidates": model_candidates,
                        "fallback_used": model != model_candidates[0],
                        "raw_output_chars": len(raw),
                    },
                )
                generation.update(
                    output=raw,
                    metadata={
                        "task": task,
                        "validator": validator_name,
                        "fallback_used": model != model_candidates[0],
                        "raw_output_chars": len(raw),
                        "validation_status": "ok",
                    },
                    usage_details=generation_usage_details(response),
                    cost_details=generation_cost_details(model, response),
                )
                return validated
            except Exception as exc:
                last_error = exc
                record_llm_call(
                    task=task,
                    model=model,
                    status="validation_error",
                    started_at=started_at,
                    latency_ms=(monotonic() - started_clock) * 1000,
                    response=response,
                    error=str(exc),
                    metadata={
                        "channel": "structured_output",
                        "validator": validator_name,
                        "model_candidates": model_candidates,
                        "fallback_used": model != model_candidates[0],
                        "raw_output_chars": len(raw),
                    },
                )
                generation.update(
                    output=raw,
                    metadata={
                        "task": task,
                        "validator": validator_name,
                        "fallback_used": model != model_candidates[0],
                        "raw_output_chars": len(raw),
                        "validation_status": "validation_error",
                    },
                    usage_details=generation_usage_details(response),
                    cost_details=generation_cost_details(model, response),
                    status_message=str(exc),
                )
                logger.warning(
                    "Structured output validation failed for task=%s model=%s: %s",
                    task,
                    model,
                    exc,
                )

    raise StructuredOutputError(str(last_error or f"Structured output failed for task {task}."))
