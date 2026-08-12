from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from app.services.agent.events import normalize
from tests.fakes import (
    fake_assistant,
    fake_init,
    fake_result,
    fake_text,
    fake_thinking,
    fake_tool_result,
    fake_tool_use,
    fake_user,
)


class TestInit:
    def test_fake_init_produces_init_event(self):
        events = normalize(fake_init("sess-1"))
        assert events == [{"type": "init", "session_id": "sess-1"}]

    def test_real_system_message_non_init_subtype_produces_nothing(self):
        msg = SystemMessage(subtype="task_started", data={})
        assert normalize(msg) == []

    def test_real_system_message_init_subtype(self):
        msg = SystemMessage(subtype="init", data={"session_id": "sess-real"})
        assert normalize(msg) == [{"type": "init", "session_id": "sess-real"}]


class TestAssistant:
    def test_text_and_tool_use_blocks_produce_two_events(self):
        msg = fake_assistant(
            fake_text("hi there"),
            fake_tool_use("t1", "Bash", {"cmd": "ls"}),
        )
        events = normalize(msg)
        assert events == [
            {"type": "text", "text": "hi there"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
        ]

    def test_thinking_block(self):
        msg = fake_assistant(fake_thinking("pondering"))
        assert normalize(msg) == [{"type": "thinking", "text": "pondering"}]

    def test_real_assistant_message_blocks(self):
        msg = AssistantMessage(
            content=[
                TextBlock(text="hello"),
                ThinkingBlock(thinking="hmm", signature="sig"),
                ToolUseBlock(id="tu1", name="Read", input={"path": "x"}),
            ],
            model="claude-x",
        )
        events = normalize(msg)
        assert events == [
            {"type": "text", "text": "hello"},
            {"type": "thinking", "text": "hmm"},
            {"type": "tool_use", "id": "tu1", "name": "Read", "input": {"path": "x"}},
        ]

    def test_empty_content_produces_no_events(self):
        assert normalize(fake_assistant()) == []


class TestUser:
    def test_tool_result_block(self):
        msg = fake_user(fake_tool_result("t1", content="the output", is_error=False))
        events = normalize(msg)
        assert events == [
            {
                "type": "tool_result",
                "tool_use_id": "t1",
                "is_error": False,
                "content": "the output",
            }
        ]

    def test_tool_result_error_flag(self):
        msg = fake_user(fake_tool_result("t2", content="boom", is_error=True))
        events = normalize(msg)
        assert events[0]["is_error"] is True

    def test_tool_result_content_list_of_text_blocks_is_flattened(self):
        msg = fake_user(
            fake_tool_result(
                "t3",
                content=[{"type": "text", "text": "part one "}, {"type": "text", "text": "part two"}],
            )
        )
        events = normalize(msg)
        assert events[0]["content"] == "part one part two"

    def test_real_user_message_tool_result(self):
        msg = UserMessage(
            content=[ToolResultBlock(tool_use_id="tr1", content="ok", is_error=False)]
        )
        events = normalize(msg)
        assert events == [
            {"type": "tool_result", "tool_use_id": "tr1", "is_error": False, "content": "ok"}
        ]

    def test_user_message_string_content_produces_no_events(self):
        msg = fake_user()
        assert normalize(msg) == []


class TestResult:
    def test_fake_result_shape(self):
        msg = fake_result(
            "sess-1",
            cost=0.0123,
            input_tokens=10,
            output_tokens=20,
            duration_ms=500,
            num_turns=3,
        )
        events = normalize(msg)
        assert len(events) == 1
        event = events[0]
        assert event["type"] == "result"
        assert event["session_id"] == "sess-1"
        assert event["usage"] == {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_tokens": 0,
        }
        assert event["cost_usd"] == 0.0123
        assert event["duration_ms"] == 500
        assert event["num_turns"] == 3
        assert event["subtype"] == "success"
        assert event["is_error"] is False
        assert event["result"] == "ok"

    def test_real_result_message(self):
        msg = ResultMessage(
            subtype="success",
            duration_ms=100,
            duration_api_ms=90,
            is_error=False,
            num_turns=1,
            session_id="sess-real",
            total_cost_usd=0.5,
            usage={"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3},
            result="done",
        )
        events = normalize(msg)
        assert events == [
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "done",
                "session_id": "sess-real",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cache_read_tokens": 3,
                },
                "cost_usd": 0.5,
                "duration_ms": 100,
                "num_turns": 1,
            }
        ]

    def test_result_with_missing_usage_defaults_to_zeros(self):
        msg = fake_result("sess-2")
        msg.usage = None
        events = normalize(msg)
        assert events[0]["usage"] == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
        }


class TestUnknown:
    def test_unrecognized_message_returns_empty_list(self):
        class Weird:
            pass

        assert normalize(Weird()) == []

    def test_none_returns_empty_list(self):
        assert normalize(None) == []
