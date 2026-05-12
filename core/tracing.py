from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from config import settings
from core import llmops


logger = logging.getLogger(__name__)

TRACE_VERSION = "jarvis-observability-v1"
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_LONG_DIGITS_RE = re.compile(r"\b\d{7,}\b")
_CLIENT: Any | None = None
_CLIENT_INIT_ATTEMPTED = False


class _NoopObservation:
    def update(self, **_kwargs: Any) -> "_NoopObservation":
        return self

    def update_trace(self, **_kwargs: Any) -> "_NoopObservation":
        return self

    def end(self, **_kwargs: Any) -> "_NoopObservation":
        return self


NOOP_OBSERVATION = _NoopObservation()


class ObservationProxy:
    def __init__(self, observation: Any):
        self._observation = observation

    def update(self, **kwargs: Any) -> "ObservationProxy":
        payload = dict(kwargs)
        if "input" in payload:
            payload["input"] = _prepare_value(payload["input"])
        if "output" in payload:
            payload["output"] = _prepare_value(payload["output"])
        if "metadata" in payload:
            payload["metadata"] = _prepare_metadata(payload["metadata"])
        self._observation.update(**payload)
        return self

    def update_trace(self, **kwargs: Any) -> "ObservationProxy":
        payload = dict(kwargs)
        if "input" in payload:
            payload["input"] = _prepare_value(payload["input"])
        if "output" in payload:
            payload["output"] = _prepare_value(payload["output"])
        if "metadata" in payload:
            payload["metadata"] = _prepare_metadata(payload["metadata"])
        update_trace_fn = getattr(self._observation, "update_trace", None)
        if callable(update_trace_fn):
            update_trace_fn(**payload)
            return self

        set_trace_io_fn = getattr(self._observation, "set_trace_io", None)
        trace_input = payload.pop("input", None)
        trace_output = payload.pop("output", None)
        if callable(set_trace_io_fn) and (trace_input is not None or trace_output is not None):
            set_trace_io_fn(input=trace_input, output=trace_output)

        fallback_metadata = dict(payload.pop("metadata", {}) or {})
        for key in ("session_id", "user_id", "tags"):
            if key in payload:
                fallback_metadata[f"trace_{key}"] = payload.pop(key)

        update_fn = getattr(self._observation, "update", None)
        if callable(update_fn):
            fallback_payload = dict(payload)
            if fallback_metadata:
                fallback_payload["metadata"] = fallback_metadata
            if fallback_payload:
                update_fn(**fallback_payload)
        return self

    def end(self, **kwargs: Any) -> "ObservationProxy":
        self._observation.end(**kwargs)
        return self

    def __getattr__(self, name: str) -> Any:
        return getattr(self._observation, name)


def _safe_preview(text: str, *, limit: int = 180) -> str:
    preview = str(text or "").strip()
    if not preview:
        return ""
    preview = _EMAIL_RE.sub("[redacted-email]", preview)
    preview = _LONG_DIGITS_RE.sub("[redacted-id]", preview)
    preview = re.sub(r"\s+", " ", preview).strip()
    return preview[:limit]


def summarize_text(text: str, *, label: str = "text", preview_limit: int = 180) -> dict[str, Any]:
    raw = str(text or "")
    return {
        "label": label,
        "chars": len(raw),
        "preview": _safe_preview(raw, limit=preview_limit),
    }


def summarize_bytes(data: bytes | bytearray | None, *, label: str = "bytes") -> dict[str, Any]:
    return {
        "label": label,
        "bytes": len(data or b""),
    }


def _capture_content_enabled() -> bool:
    return bool(getattr(settings, "JARVIS_LANGFUSE_CAPTURE_CONTENT", False))


def _prepare_value(value: Any) -> Any:
    if value is None:
        return None
    if _capture_content_enabled():
        return value
    if isinstance(value, str):
        return summarize_text(value)
    if isinstance(value, (bytes, bytearray)):
        return summarize_bytes(value)
    if isinstance(value, dict):
        try:
            encoded = json.dumps(value, ensure_ascii=False, default=str)
        except TypeError:
            encoded = repr(value)
        return {
            "type": "dict",
            "keys": list(value.keys())[:12],
            "chars": len(encoded),
            "preview": _safe_preview(encoded),
        }
    if isinstance(value, (list, tuple, set)):
        try:
            encoded = json.dumps(list(value), ensure_ascii=False, default=str)
        except TypeError:
            encoded = repr(value)
        return {
            "type": "sequence",
            "length": len(value),
            "chars": len(encoded),
            "preview": _safe_preview(encoded),
        }
    return value


def _prepare_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not metadata:
        return None
    return {str(key): _prepare_value(value) for key, value in metadata.items()}


def _langfuse_enabled() -> bool:
    if not getattr(settings, "JARVIS_LANGFUSE_ENABLED", False):
        return False
    if not getattr(settings, "LANGFUSE_PUBLIC_KEY", "") or not getattr(settings, "LANGFUSE_SECRET_KEY", ""):
        return False
    return True


def _get_client() -> Any | None:
    global _CLIENT, _CLIENT_INIT_ATTEMPTED

    if _CLIENT_INIT_ATTEMPTED:
        return _CLIENT
    _CLIENT_INIT_ATTEMPTED = True

    if not _langfuse_enabled():
        return None

    try:
        from langfuse import Langfuse
    except Exception as exc:
        logger.warning("Langfuse SDK unavailable; tracing disabled: %s", exc)
        return None

    try:
        _CLIENT = Langfuse(
            public_key=settings.LANGFUSE_PUBLIC_KEY,
            secret_key=settings.LANGFUSE_SECRET_KEY,
            host=settings.LANGFUSE_BASE_URL,
        )
    except Exception as exc:
        logger.warning("Failed to initialize Langfuse client; tracing disabled: %s", exc)
        _CLIENT = None

    return _CLIENT


@dataclass
class TraceContext:
    session_id: str | None = None
    user_id: str | None = None
    version: str = TRACE_VERSION
    tags: list[str] | None = None


@contextmanager
def _observation_context(
    *,
    name: str,
    as_type: str,
    input: Any = None,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
    model: str | None = None,
    model_parameters: dict[str, Any] | None = None,
    usage_details: dict[str, int] | None = None,
    cost_details: dict[str, float] | None = None,
    trace_context: TraceContext | None = None,
) -> Iterator[Any]:
    client = _get_client()
    if client is None:
        yield NOOP_OBSERVATION
        return

    try:
        observation_metadata = dict(metadata or {})
        if trace_context is not None:
            if trace_context.session_id is not None:
                observation_metadata.setdefault("trace_session_id", trace_context.session_id)
            if trace_context.user_id is not None:
                observation_metadata.setdefault("trace_user_id", trace_context.user_id)
            if trace_context.tags:
                observation_metadata.setdefault("trace_tags", trace_context.tags)

        with client.start_as_current_observation(
            name=name,
            as_type=as_type,
            input=_prepare_value(input),
            output=_prepare_value(output),
            metadata=_prepare_metadata(observation_metadata),
            model=model,
            model_parameters=model_parameters,
            usage_details=usage_details,
            cost_details=cost_details,
            version=trace_context.version if trace_context is not None else None,
        ) as observation:
            wrapped = ObservationProxy(observation)
            if trace_context is not None:
                wrapped.update_trace(
                    name=name,
                    session_id=trace_context.session_id,
                    user_id=trace_context.user_id,
                    metadata=metadata,
                    tags=trace_context.tags,
                )
            yield wrapped
    except Exception as exc:
        logger.warning("Langfuse observation failed for %s: %s", name, exc)
        yield NOOP_OBSERVATION


@contextmanager
def start_trace(
    *,
    name: str,
    session_id: str | None = None,
    user_id: str | None = None,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    tags: list[str] | None = None,
    version: str = TRACE_VERSION,
) -> Iterator[Any]:
    with _observation_context(
        name=name,
        as_type="agent",
        input=input,
        metadata=metadata,
        trace_context=TraceContext(
            session_id=session_id,
            user_id=user_id,
            version=version,
            tags=tags,
        ),
    ) as observation:
        yield observation


@contextmanager
def start_span(*, name: str, input: Any = None, metadata: dict[str, Any] | None = None) -> Iterator[Any]:
    with _observation_context(
        name=name,
        as_type="span",
        input=input,
        metadata=metadata,
    ) as observation:
        yield observation


@contextmanager
def start_generation(
    *,
    name: str,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    model: str | None = None,
    model_parameters: dict[str, Any] | None = None,
) -> Iterator[Any]:
    with _observation_context(
        name=name,
        as_type="generation",
        input=input,
        metadata=metadata,
        model=model,
        model_parameters=model_parameters,
    ) as observation:
        yield observation


@contextmanager
def start_tool_observation(*, name: str, input: Any = None, metadata: dict[str, Any] | None = None) -> Iterator[Any]:
    with _observation_context(
        name=name,
        as_type="tool",
        input=input,
        metadata=metadata,
    ) as observation:
        yield observation


def generation_usage_details(response: Any) -> dict[str, int]:
    return llmops.usage_from_response(response)


def generation_cost_details(model: str, response: Any) -> dict[str, float] | None:
    usage = generation_usage_details(response)
    breakdown_fn = getattr(llmops, "estimate_cost_breakdown_usd", None)
    if callable(breakdown_fn):
        return breakdown_fn(model, usage)

    total_fn = getattr(llmops, "estimate_cost_usd", None)
    if not callable(total_fn):
        return None
    total = total_fn(model, usage)
    if total is None:
        return None
    return {"input": 0.0, "output": round(float(total), 6), "total": round(float(total), 6)}


def flush() -> None:
    client = _get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception as exc:
        logger.warning("Failed to flush Langfuse client: %s", exc)
