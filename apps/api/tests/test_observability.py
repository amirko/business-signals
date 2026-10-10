from __future__ import annotations

import business_signals.observability as observability
import pytest
from business_signals.config import settings
from business_signals.observability import LangSmithMonitor


class FakeRunTree:
    created: list[FakeRunTree] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.children: list[FakeRunTree] = []
        self.ended: dict[str, object] | None = None
        self.patch_exclude_inputs: bool | None = None
        self.created.append(self)

    def create_child(self, **kwargs: object) -> FakeRunTree:
        child = FakeRunTree(**kwargs)
        self.children.append(child)
        return child

    def post(self) -> None:
        return None

    def end(self, **kwargs: object) -> None:
        self.ended = kwargs

    def patch(self, **kwargs: object) -> None:
        self.patch_exclude_inputs = kwargs.get("exclude_inputs")  # type: ignore[assignment]

    def set(self, **kwargs: object) -> None:
        self.usage_metadata = kwargs.get("usage_metadata")


@pytest.mark.asyncio
async def test_langsmith_monitor_is_a_noop_without_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "langsmith_tracing", False)
    monitor = LangSmithMonitor()

    async with monitor.span("ignored", run_type="chain", inputs={"secret": "nope"}) as span:
        span.set_output(status="ok")

    assert span.run is None
    safe_value = monitor.content_or_fingerprint("Milan, Italy")
    assert set(safe_value) == {"fingerprint"}
    assert safe_value["fingerprint"] == monitor.content_or_fingerprint("Milan, Italy")["fingerprint"]
    assert "Milan" not in safe_value["fingerprint"]


@pytest.mark.asyncio
async def test_langsmith_monitor_redacts_inputs_and_nests_spans(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeRunTree.created.clear()
    monkeypatch.setattr(settings, "langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "test-key")
    monkeypatch.setattr(settings, "langsmith_capture_content", False)
    monkeypatch.setattr(observability, "RunTree", FakeRunTree)
    monkeypatch.setattr(observability, "Client", lambda **_kwargs: object())
    monitor = LangSmithMonitor()

    async with monitor.span("root", run_type="chain", inputs={"question": "sensitive"}) as root:
        async with monitor.span("child", run_type="tool", inputs={"sql": "SELECT secret"}) as child:
            child.set_output(row_count=2)
        root.set_output(status="completed")

    root_run, child_run = FakeRunTree.created
    assert root_run.kwargs["inputs"] == {}
    assert child_run.kwargs["inputs"] == {}
    assert root_run.kwargs["extra"] == {"metadata": {}}
    assert root_run.children == [child_run]
    assert root_run.ended == {"outputs": {"outcome": {"status": "completed"}}, "error": None}
    assert child_run.ended == {"outputs": {"outcome": {"row_count": 2}}, "error": None}
    assert root_run.patch_exclude_inputs is True


@pytest.mark.asyncio
async def test_langsmith_monitor_redacts_error_content_in_safe_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeRunTree.created.clear()
    monkeypatch.setattr(settings, "langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "test-key")
    monkeypatch.setattr(settings, "langsmith_capture_content", False)
    monkeypatch.setattr(observability, "RunTree", FakeRunTree)
    monkeypatch.setattr(observability, "Client", lambda **_kwargs: object())
    monitor = LangSmithMonitor()

    with pytest.raises(RuntimeError, match="SELECT sensitive"):
        async with monitor.span("root", run_type="chain"):
            raise RuntimeError("SELECT sensitive FROM customer_records")

    assert FakeRunTree.created[0].ended == {"outputs": {"outcome": {}}, "error": "RuntimeError"}


@pytest.mark.asyncio
async def test_langsmith_monitor_sends_usage_without_content_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeRunTree.created.clear()
    monkeypatch.setattr(settings, "langsmith_tracing", True)
    monkeypatch.setattr(settings, "langsmith_api_key", "test-key")
    monkeypatch.setattr(settings, "langsmith_capture_content", False)
    monkeypatch.setattr(observability, "RunTree", FakeRunTree)
    monkeypatch.setattr(observability, "Client", lambda **_kwargs: object())
    monitor = LangSmithMonitor()

    async with monitor.span("LLM", run_type="llm", inputs={"prompt": "sensitive"}) as span:
        span.set_usage_metadata({"input_tokens": 12, "output_tokens": 8, "total_tokens": 20})

    assert FakeRunTree.created[0].usage_metadata == {"input_tokens": 12, "output_tokens": 8, "total_tokens": 20}
