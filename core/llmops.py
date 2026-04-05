"""
Lightweight LLMOps helpers for recording model usage to a local JSONL file.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any


logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
LLM_ACTIVITY_PATH = ROOT / "data" / "llm_activity.jsonl"
_WRITE_LOCK = Lock()
_TOKENS_PER_MILLION = 1_000_000
_MODEL_PRICE_HINTS_PER_MILLION: dict[str, tuple[float, float]] = {
    "anthropic/claude-sonnet-4.6": (3.0, 15.0),
    "anthropic/claude-sonnet-4.5": (3.0, 15.0),
    "anthropic/claude-sonnet-4": (3.0, 15.0),
    "anthropic/claude-3.7-sonnet": (3.0, 15.0),
    "anthropic/claude-3.5-sonnet": (3.0, 15.0),
    "anthropic/claude-3.5-haiku": (0.8, 4.0),
    "anthropic/claude-3-haiku": (0.25, 1.25),
    "anthropic/claude-3-opus": (15.0, 75.0),
}


def _usage_int(usage: Any, attr: str) -> int:
    value = getattr(usage, attr, 0)
    if value in (None, ""):
        return 0
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0


def usage_from_response(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "total_tokens": 0,
        }

    input_tokens = _usage_int(usage, "input_tokens")
    output_tokens = _usage_int(usage, "output_tokens")
    cache_creation_input_tokens = _usage_int(usage, "cache_creation_input_tokens")
    cache_read_input_tokens = _usage_int(usage, "cache_read_input_tokens")
    total_tokens = input_tokens + output_tokens + cache_creation_input_tokens + cache_read_input_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "total_tokens": total_tokens,
    }


def estimate_cost_usd(model: str, usage: dict[str, int]) -> float | None:
    input_cost_per_million, output_cost_per_million = _MODEL_PRICE_HINTS_PER_MILLION.get(model, (None, None))
    if input_cost_per_million is None or output_cost_per_million is None:
        return None

    billable_input_tokens = (
        usage.get("input_tokens", 0)
        + usage.get("cache_creation_input_tokens", 0)
        + usage.get("cache_read_input_tokens", 0)
    )
    estimated_cost = (
        (billable_input_tokens / _TOKENS_PER_MILLION) * input_cost_per_million
        + (usage.get("output_tokens", 0) / _TOKENS_PER_MILLION) * output_cost_per_million
    )
    return round(estimated_cost, 6)


def record_llm_call(
    *,
    task: str,
    model: str,
    status: str,
    started_at: str | None = None,
    latency_ms: float | int | None = None,
    response: Any | None = None,
    stop_reason: str | None = None,
    error: str = "",
    metadata: dict[str, Any] | None = None,
) -> None:
    usage = usage_from_response(response)
    estimated_cost_usd = estimate_cost_usd(model, usage)
    payload: dict[str, Any] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "task": task,
        "model": model,
        "status": status,
        "latency_ms": round(float(latency_ms or 0.0), 2),
        "stop_reason": stop_reason or getattr(response, "stop_reason", "") or "",
        "input_tokens": usage["input_tokens"],
        "output_tokens": usage["output_tokens"],
        "cache_creation_input_tokens": usage["cache_creation_input_tokens"],
        "cache_read_input_tokens": usage["cache_read_input_tokens"],
        "total_tokens": usage["total_tokens"],
        "estimated_cost_usd": estimated_cost_usd,
        "error": error[:500],
    }
    if metadata:
        payload["metadata"] = metadata

    try:
        LLM_ACTIVITY_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, ensure_ascii=False)
        with _WRITE_LOCK:
            with LLM_ACTIVITY_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception as exc:
        logger.warning("Failed to record LLM activity: %s", exc)
