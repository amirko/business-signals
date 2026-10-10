"""Optional, privacy-conscious LangSmith tracing for operational monitoring."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from langsmith import Client
from langsmith.run_trees import RunTree

from business_signals.config import settings

logger = logging.getLogger("uvicorn.error")


def _fingerprint(value: str) -> str:
    """Correlate sensitive content without sending it to the monitoring service."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass
class TraceSpan:
    """A safe handle for attaching a compact outcome to an optional trace."""

    run: RunTree | None = None
    output: dict[str, Any] = field(default_factory=dict)
    usage_metadata: dict[str, int] | None = None
    error: BaseException | None = None

    def set_output(self, **values: Any) -> None:
        self.output.update(values)

    def set_error(self, error: BaseException) -> None:
        self.error = error

    def set_usage_metadata(self, usage_metadata: dict[str, int] | None) -> None:
        """Attach LangSmith's provider-neutral token-usage shape to an LLM span."""
        self.usage_metadata = usage_metadata


class LangSmithMonitor:
    """Create nested runs without allowing observability failures into product flow."""

    def __init__(self) -> None:
        self._current_run: ContextVar[RunTree | None] = ContextVar("langsmith_current_run", default=None)
        self._client: Client | None = None
        self._client_settings: tuple[str, str] | None = None
        self._missing_key_warned = False

    @property
    def enabled(self) -> bool:
        if not settings.langsmith_tracing:
            return False
        if settings.langsmith_api_key:
            return True
        if not self._missing_key_warned:
            logger.warning("LangSmith tracing is enabled but LANGSMITH_API_KEY is not configured; tracing is disabled")
            self._missing_key_warned = True
        return False

    def _get_client(self) -> Client:
        assert settings.langsmith_api_key is not None
        client_settings = (settings.langsmith_api_key, settings.langsmith_endpoint)
        if self._client is None or self._client_settings != client_settings:
            self._client = Client(
                api_key=settings.langsmith_api_key,
                api_url=settings.langsmith_endpoint,
                # We explicitly avoid recording inputs/outputs unless opted in.
                hide_inputs=not settings.langsmith_capture_content,
                hide_outputs=not settings.langsmith_capture_content,
            )
            self._client_settings = client_settings
        return self._client

    @asynccontextmanager
    async def span(
        self,
        name: str,
        *,
        run_type: str,
        inputs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
    ) -> AsyncIterator[TraceSpan]:
        """Yield a child span; tracing errors are logged and otherwise ignored."""
        span = TraceSpan()
        if not self.enabled:
            yield span
            return

        try:
            parent = self._current_run.get()
            safe_inputs = inputs if settings.langsmith_capture_content else {}
            extra = {"metadata": metadata or {}}
            if parent is None:
                run = RunTree(
                    name=name,
                    run_type=run_type,
                    inputs=safe_inputs,
                    extra=extra,
                    tags=tags or [],
                    project_name=settings.langsmith_project,
                    ls_client=self._get_client(),
                )
            else:
                run = parent.create_child(
                    name=name,
                    run_type=run_type,
                    inputs=safe_inputs,
                    extra=extra,
                    tags=tags or [],
                )
            run.post()
        except Exception:
            logger.warning("Could not start LangSmith trace; continuing without monitoring", exc_info=True)
            yield span
            return

        span.run = run
        token = self._current_run.set(run)
        try:
            yield span
        except BaseException as exc:
            span.set_error(exc)
            raise
        finally:
            self._current_run.reset(token)
            try:
                error = None
                if span.error is not None:
                    error = type(span.error).__name__
                    if settings.langsmith_capture_content:
                        error = f"{error}: {str(span.error)[:300]}"
                outputs = span.output if settings.langsmith_capture_content else {"outcome": span.output}
                run.end(outputs=outputs, error=error)
                if span.usage_metadata is not None:
                    # ``usage_metadata`` is LangSmith's canonical schema. It is
                    # metadata rather than prompt/output content, so it remains
                    # available even when content capture is disabled.
                    run.set(usage_metadata=span.usage_metadata)
                run.patch(exclude_inputs=not settings.langsmith_capture_content)
            except Exception:
                logger.warning("Could not finish LangSmith trace; continuing without monitoring", exc_info=True)

    def content_or_fingerprint(self, value: str) -> dict[str, str]:
        """Return traceable content only after a deliberate operator opt-in."""
        result = {"fingerprint": _fingerprint(value)}
        if settings.langsmith_capture_content:
            result["content"] = value
        return result


monitor = LangSmithMonitor()
