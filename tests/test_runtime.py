import asyncio

import pytest

from app.services.agent.runtime import AgentRuntime, RunConfig, run_and_collect
from tests.fakes import fake_assistant, fake_init, fake_result, fake_text


@pytest.fixture
def anyio_backend():
    return "asyncio"


def make_cfg(**overrides) -> RunConfig:
    defaults = dict(
        prompt="hello",
        cwd="/tmp/ws",
        system_prompt=None,
        allowed_tools=["Bash"],
        permission_mode="default",
        mcp_servers={},
        model=None,
        max_turns=5,
        resume=None,
        timeout_s=5,
    )
    defaults.update(overrides)
    return RunConfig(**defaults)


class TestStreamHappyPath:
    @pytest.mark.anyio
    async def test_init_text_result_order_preserved_and_no_api_key_in_env(self):
        captured_options = {}

        async def fake_query(prompt, options):
            captured_options["options"] = options
            yield fake_init("sess-1")
            yield fake_assistant(fake_text("hi there"))
            yield fake_result("sess-1", result="done")

        runtime = AgentRuntime(query_fn=fake_query)
        cfg = make_cfg()
        events = [ev async for ev in runtime.stream(cfg)]

        assert [ev["type"] for ev in events] == ["init", "text", "result"]
        assert events[0]["session_id"] == "sess-1"
        assert events[1]["text"] == "hi there"
        assert events[2]["result"] == "done"

        options = captured_options["options"]
        assert "ANTHROPIC_API_KEY" not in (options.env or {})
        assert options.cwd == "/tmp/ws"
        assert options.allowed_tools == ["Bash"]
        assert options.permission_mode == "default"
        assert options.max_turns == 5


class TestStreamResultThenRaise:
    @pytest.mark.anyio
    async def test_result_present_when_query_raises_after_yielding_result(self):
        async def fake_query(prompt, options):
            yield fake_init("sess-2")
            yield fake_result("sess-2", result="ok-but-then-error")
            raise RuntimeError("boom after result")

        runtime = AgentRuntime(query_fn=fake_query)
        cfg = make_cfg()
        events = [ev async for ev in runtime.stream(cfg)]

        types = [ev["type"] for ev in events]
        assert "result" in types
        result_event = next(ev for ev in events if ev["type"] == "result")
        assert result_event["result"] == "ok-but-then-error"
        # no unhandled exception propagated out of the async generator
        assert types == ["init", "result"]


class TestStreamRaisesBeforeResult:
    @pytest.mark.anyio
    async def test_error_event_when_query_raises_before_any_result(self):
        async def fake_query(prompt, options):
            yield fake_init("sess-3")
            raise RuntimeError("boom before result")

        runtime = AgentRuntime(query_fn=fake_query)
        cfg = make_cfg()
        events = [ev async for ev in runtime.stream(cfg)]

        types = [ev["type"] for ev in events]
        assert types == ["init", "error"]
        assert events[-1]["message"] == "boom before result"


class TestStreamTimeout:
    @pytest.mark.anyio
    async def test_timeout_yields_error_event(self):
        async def fake_query(prompt, options):
            yield fake_init("sess-4")
            await asyncio.sleep(10)
            yield fake_result("sess-4")

        runtime = AgentRuntime(query_fn=fake_query)
        cfg = make_cfg(timeout_s=0)
        events = [ev async for ev in runtime.stream(cfg)]

        types = [ev["type"] for ev in events]
        assert types[-1] == "error"
        assert events[-1]["message"] == "run timed out"


class TestRunAndCollect:
    @pytest.mark.anyio
    async def test_run_and_collect_returns_result_event_and_calls_on_event(self):
        async def fake_query(prompt, options):
            yield fake_init("sess-5")
            yield fake_assistant(fake_text("partial"))
            yield fake_result("sess-5", result="final answer")

        runtime = AgentRuntime(query_fn=fake_query)
        cfg = make_cfg()

        seen = []

        async def on_event(ev):
            seen.append(ev)

        final = await run_and_collect(runtime, cfg, on_event)

        assert final["type"] == "result"
        assert final["result"] == "final answer"
        assert [ev["type"] for ev in seen] == ["init", "text", "result"]

    @pytest.mark.anyio
    async def test_run_and_collect_returns_error_event_when_no_result(self):
        async def fake_query(prompt, options):
            raise RuntimeError("total failure")
            yield  # pragma: no cover - unreachable, makes this an async generator

        runtime = AgentRuntime(query_fn=fake_query)
        cfg = make_cfg()

        final = await run_and_collect(runtime, cfg, None)

        assert final["type"] == "error"
        assert final["message"] == "total failure"
