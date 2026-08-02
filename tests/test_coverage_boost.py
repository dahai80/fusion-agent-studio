"""Coverage boost tests for json_schema, triggers, and debugger modules."""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_runtime.debugger import DebuggerState, StepDebugger
from agent_runtime.json_schema import JsonSchemaValidator
from agent_runtime.triggers import CronJob, CronManager, Webhook, WebhookManager

logger = logging.getLogger(__name__)


# ── JsonSchemaValidator ──


class TestJsonSchemaValidate:
    def test_validate_empty_schema_returns_empty_list(self):
        v = JsonSchemaValidator(schema=None)
        assert v.validate({"any": "data"}) == []

    def test_validate_empty_schema_dict(self):
        v = JsonSchemaValidator(schema={})
        assert v.validate({"any": "data"}) == []

    def test_validate_missing_required_field(self):
        schema = {
            "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
            "required": ["name", "age"],
        }
        v = JsonSchemaValidator(schema=schema)
        errors = v.validate({"name": "alice"})
        assert any("Missing required field" in e and "age" in e for e in errors)

    def test_validate_type_mismatch(self):
        schema = {
            "properties": {"count": {"type": "integer"}},
            "required": [],
        }
        v = JsonSchemaValidator(schema=schema)
        errors = v.validate({"count": "not_an_int"})
        assert any("expected integer" in e for e in errors)

    def test_validate_unknown_fields(self):
        schema = {
            "properties": {"name": {"type": "string"}},
            "required": [],
        }
        v = JsonSchemaValidator(schema=schema)
        errors = v.validate({"name": "ok", "extra": 42})
        assert any("Unknown field" in e for e in errors)

    def test_validate_unknown_field_not_in_required_or_properties(self):
        schema = {
            "properties": {"age": {"type": "integer"}},
            "required": ["age"],
        }
        v = JsonSchemaValidator(schema=schema)
        errors = v.validate({"age": 10, "bogus": True})
        assert any("Unknown field" in e and "bogus" in e for e in errors)


class TestJsonSchemaExtractFromText:
    def test_extract_code_block_with_two_plus_lines(self):
        v = JsonSchemaValidator(schema=None)
        text = '```json\n{"key": "value"}\n```'
        result = v.extract_from_text(text)
        assert result == {"key": "value"}

    def test_extract_code_block_single_line_returns_empty_dict(self):
        v = JsonSchemaValidator(schema=None)
        text = "```{}```"
        result = v.extract_from_text(text)
        assert result == {}

    def test_extract_parsed_json_not_dict_returns_none(self):
        v = JsonSchemaValidator(schema=None)
        text = "```json\n[1, 2, 3]\n```"
        result = v.extract_from_text(text)
        assert result is None

    def test_extract_plain_json_array_not_dict(self):
        v = JsonSchemaValidator(schema=None)
        text = "[1, 2, 3]"
        result = v.extract_from_text(text)
        assert result is None

    def test_extract_regex_search_curly_braces(self):
        v = JsonSchemaValidator(schema=None)
        text = 'Here is data: {"x": 1} end'
        result = v.extract_from_text(text)
        assert result == {"x": 1}

    def test_extract_regex_json_array_not_dict(self):
        v = JsonSchemaValidator(schema=None)
        text = "Result: [1, 2, 3] done"
        result = v.extract_from_text(text)
        assert result is None

    def test_extract_regex_invalid_json_in_braces(self):
        v = JsonSchemaValidator(schema=None)
        text = "Some text {not valid json} more text"
        result = v.extract_from_text(text)
        assert result is None

    def test_extract_no_json_at_all(self):
        v = JsonSchemaValidator(schema=None)
        text = "No JSON here at all"
        result = v.extract_from_text(text)
        assert result is None


class TestJsonSchemaCheckType:
    def test_check_type_unknown_type_returns_true(self):
        v = JsonSchemaValidator(schema=None)
        assert v._check_type(42, "custom_unknown") is True
        assert v._check_type("anything", "weird_type") is True


class TestJsonSchemaCoerceValue:
    def test_coerce_string(self):
        v = JsonSchemaValidator(schema=None)
        assert v._coerce_value(42, "string") == "42"
        assert v._coerce_value(True, "string") == "True"

    def test_coerce_number(self):
        v = JsonSchemaValidator(schema=None)
        assert v._coerce_value("3.14", "number") == 3.14
        assert v._coerce_value("42", "number") == 42.0

    def test_coerce_boolean_string_true(self):
        v = JsonSchemaValidator(schema=None)
        assert v._coerce_value("true", "boolean") is True
        assert v._coerce_value("1", "boolean") is True
        assert v._coerce_value("yes", "boolean") is True
        assert v._coerce_value("false", "boolean") is False
        assert v._coerce_value("0", "boolean") is False
        assert v._coerce_value("no", "boolean") is False

    def test_coerce_boolean_non_string(self):
        v = JsonSchemaValidator(schema=None)
        assert v._coerce_value(1, "boolean") is True
        assert v._coerce_value(0, "boolean") is False

    def test_coerce_json_string(self):
        v = JsonSchemaValidator(schema=None)
        result = v._coerce_value('{"a": 1}', "json")
        assert result == {"a": 1}

    def test_coerce_json_non_string(self):
        v = JsonSchemaValidator(schema=None)
        data = {"already": "a dict"}
        assert v._coerce_value(data, "json") is data

    def test_coerce_json_invalid_string(self):
        v = JsonSchemaValidator(schema=None)
        bad = "not json at all"
        result = v._coerce_value(bad, "json")
        assert result == bad

    def test_coerce_value_error_returns_original(self):
        v = JsonSchemaValidator(schema=None)
        result = v._coerce_value("not_a_number", "number")
        assert result == "not_a_number"

    def test_coerce_value_type_error_returns_original(self):
        v = JsonSchemaValidator(schema=None)
        result = v._coerce_value(None, "integer")
        assert result is None


class TestJsonSchemaCoerce:
    def test_coerce_with_field_present(self):
        schema = {
            "properties": {
                "count": {"type": "integer"},
                "name": {"type": "string"},
            }
        }
        v = JsonSchemaValidator(schema=schema)
        result = v.coerce({"count": "5", "name": "alice"})
        assert result["count"] == 5
        assert result["name"] == "alice"

    def test_coerce_injects_default_values(self):
        schema = {
            "properties": {
                "name": {"type": "string"},
                "role": {"type": "string", "default": "user"},
            }
        }
        v = JsonSchemaValidator(schema=schema)
        result = v.coerce({"name": "bob"})
        assert result["name"] == "bob"
        assert result["role"] == "user"

    def test_coerce_no_coercion_needed(self):
        schema = {
            "properties": {
                "active": {"type": "boolean"},
            }
        }
        v = JsonSchemaValidator(schema=schema)
        result = v.coerce({"active": True})
        assert result["active"] is True


class TestJsonSchemaToInstruction:
    def test_to_instruction_empty_schema(self):
        v = JsonSchemaValidator(schema=None)
        assert v.to_instruction() == ""

    def test_to_instruction_empty_dict(self):
        v = JsonSchemaValidator(schema={})
        assert v.to_instruction() == ""

    def test_to_instruction_with_schema(self):
        schema = {"properties": {"x": {"type": "integer"}}}
        v = JsonSchemaValidator(schema=schema)
        instruction = v.to_instruction()
        assert "JSON" in instruction
        assert "x" in instruction


class TestJsonSchemaIsEmpty:
    def test_is_empty_none(self):
        v = JsonSchemaValidator(schema=None)
        assert v.is_empty is True

    def test_is_empty_dict(self):
        v = JsonSchemaValidator(schema={})
        assert v.is_empty is True

    def test_is_not_empty(self):
        v = JsonSchemaValidator(schema={"properties": {"x": {"type": "string"}}})
        assert v.is_empty is False


# ── WebhookManager ──


class TestWebhookManagerHandle:
    @pytest.mark.asyncio
    async def test_handle_signature_verification_failure(self):
        mgr = WebhookManager()
        wh = Webhook(id="wh1", name="test", secret="mysecret", graph_id="g1")
        mgr.register(wh)
        headers = {"x-webhook-signature": "wrong_signature"}
        result = await mgr.handle("wh1", {"data": 1}, headers)
        assert "error" in result
        assert "Invalid signature" in result["error"]

    @pytest.mark.asyncio
    async def test_handle_correct_signature(self):
        mgr = WebhookManager()
        wh = Webhook(id="wh1", name="test", secret="mysecret", graph_id="g1")
        mgr.register(wh)
        payload = {"data": 1}
        sig = mgr._compute_signature(payload, "mysecret")
        headers = {"x-webhook-signature": sig}
        result = await mgr.handle("wh1", payload, headers)
        assert result.get("status") == "received"

    @pytest.mark.asyncio
    async def test_handle_no_handler_just_logs(self):
        mgr = WebhookManager()
        wh = Webhook(id="wh2", name="nohandler", secret="", graph_id="g1")
        mgr.register(wh)
        result = await mgr.handle("wh2", {"x": 1})
        assert result["status"] == "received"
        assert result["webhook_id"] == "wh2"

    @pytest.mark.asyncio
    async def test_handle_with_handler(self):
        mgr = WebhookManager()
        wh = Webhook(id="wh3", name="withhandler", secret="", graph_id="g1")
        called = {}

        async def my_handler(w, p):
            called["webhook"] = w
            called["payload"] = p
            return {"custom": "result"}

        mgr.register(wh, my_handler)
        result = await mgr.handle("wh3", {"hello": "world"})
        assert result == {"custom": "result"}
        assert called["webhook"].id == "wh3"
        assert called["payload"] == {"hello": "world"}

    @pytest.mark.asyncio
    async def test_handle_webhook_not_found(self):
        mgr = WebhookManager()
        result = await mgr.handle("nonexistent", {})
        assert "error" in result

    @pytest.mark.asyncio
    async def test_handle_webhook_disabled(self):
        mgr = WebhookManager()
        wh = Webhook(id="wh4", name="disabled", enabled=False)
        mgr.register(wh)
        result = await mgr.handle("wh4", {})
        assert "error" in result


class TestWebhookManagerBasicOps:
    def test_unregister(self):
        mgr = WebhookManager()
        wh = Webhook(id="wh5", name="test")
        mgr.register(wh)
        assert mgr.get("wh5") is not None
        mgr.unregister("wh5")
        assert mgr.get("wh5") is None

    def test_get_returns_none_for_missing(self):
        mgr = WebhookManager()
        assert mgr.get("missing") is None

    def test_list_webhooks(self):
        mgr = WebhookManager()
        wh = Webhook(id="wh6", name="listed", graph_id="g1")
        mgr.register(wh)
        items = mgr.list()
        assert len(items) == 1
        assert items[0]["id"] == "wh6"

    def test_count(self):
        mgr = WebhookManager()
        assert mgr.count == 0
        mgr.register(Webhook(id="wh7", name="c1"))
        assert mgr.count == 1


# ── CronManager ──


class TestCronManagerStart:
    def test_start_when_already_running(self):
        mgr = CronManager()
        mgr._running = True
        mgr.start()
        assert mgr._task is None

    @pytest.mark.asyncio
    async def test_start_normal(self):
        mgr = CronManager()
        mgr.start()
        assert mgr._running is True
        assert mgr._task is not None
        mgr.stop()


class TestCronManagerRunLoop:
    @pytest.mark.asyncio
    async def test_run_loop_executes_handler(self):
        mgr = CronManager()
        executed = []

        async def handler(job):
            executed.append(job.id)

        job = CronJob(
            id="j1",
            name="test",
            expression="* * * * *",
            graph_id="g1",
            enabled=True,
            next_run=time.time() - 1,
        )
        mgr._jobs[job.id] = job
        mgr._handlers[job.id] = handler
        mgr._running = True

        with patch(
            "agent_runtime.triggers.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:

            async def stop_after_first(*args, **kwargs):
                mgr._running = False

            mock_sleep.side_effect = stop_after_first
            await mgr._run_loop()

        assert "j1" in executed

    @pytest.mark.asyncio
    async def test_run_loop_handler_exception(self):
        mgr = CronManager()

        async def bad_handler(job):
            raise RuntimeError("boom")

        job = CronJob(
            id="j2",
            name="failing",
            expression="* * * * *",
            enabled=True,
            next_run=time.time() - 1,
        )
        mgr._jobs[job.id] = job
        mgr._handlers[job.id] = bad_handler
        mgr._running = True

        with patch(
            "agent_runtime.triggers.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:

            async def stop_after_first(*args, **kwargs):
                mgr._running = False

            mock_sleep.side_effect = stop_after_first
            await mgr._run_loop()

        assert job.last_run > 0

    @pytest.mark.asyncio
    async def test_run_loop_skips_disabled_job(self):
        mgr = CronManager()
        executed = []

        async def handler(job):
            executed.append(job.id)

        job = CronJob(
            id="j3",
            name="disabled_job",
            expression="* * * * *",
            enabled=False,
            next_run=time.time() - 1,
        )
        mgr._jobs[job.id] = job
        mgr._handlers[job.id] = handler
        mgr._running = True

        with patch(
            "agent_runtime.triggers.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:

            async def stop_after_first(*args, **kwargs):
                mgr._running = False

            mock_sleep.side_effect = stop_after_first
            await mgr._run_loop()

        assert "j3" not in executed

    @pytest.mark.asyncio
    async def test_run_loop_no_handler(self):
        mgr = CronManager()
        job = CronJob(
            id="j4",
            name="nohandler",
            expression="* * * * *",
            enabled=True,
            next_run=time.time() - 1,
        )
        mgr._jobs[job.id] = job
        mgr._running = True

        with patch(
            "agent_runtime.triggers.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:

            async def stop_after_first(*args, **kwargs):
                mgr._running = False

            mock_sleep.side_effect = stop_after_first
            await mgr._run_loop()

        assert job.last_run > 0

    @pytest.mark.asyncio
    async def test_run_loop_skips_future_job(self):
        mgr = CronManager()
        executed = []

        async def handler(job):
            executed.append(job.id)

        job = CronJob(
            id="j5",
            name="future",
            expression="* * * * *",
            enabled=True,
            next_run=time.time() + 9999,
        )
        mgr._jobs[job.id] = job
        mgr._handlers[job.id] = handler
        mgr._running = True

        with patch(
            "agent_runtime.triggers.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep:

            async def stop_after_first(*args, **kwargs):
                mgr._running = False

            mock_sleep.side_effect = stop_after_first
            await mgr._run_loop()

        assert "j5" not in executed


class TestCronComputeNextRun:
    def test_invalid_parts_count(self):
        mgr = CronManager()
        result = mgr._compute_next_run("1 2 3")
        assert result > 0

    def test_wildcard_minute(self):
        mgr = CronManager()
        result = mgr._compute_next_run("* * * * *")
        assert result > 0

    def test_interval_minute(self):
        mgr = CronManager()
        result = mgr._compute_next_run("*/5 * * * *")
        assert result > 0

    def test_specific_minute_before_current(self):
        mgr = CronManager()
        from datetime import datetime

        now = datetime.now()
        target_min = (now.minute + 30) % 60
        result = mgr._compute_next_run(f"{target_min} * * * *")
        assert result > 0

    def test_specific_minute_after_current(self):
        mgr = CronManager()
        from datetime import datetime

        now = datetime.now()
        target_min = (now.minute + 5) % 60
        result = mgr._compute_next_run(f"{target_min} * * * *")
        assert result > 0

    def test_compute_next_run_exception_fallback(self):
        mgr = CronManager()
        result = mgr._compute_next_run("not_a_number * * * *")
        assert result > 0


class TestCronManagerRegister:
    def test_register_sets_next_run(self):
        mgr = CronManager()
        job = CronJob(id="j10", name="reg", expression="*/10 * * * *")
        mgr.register(job)
        assert job.next_run > 0

    def test_register_with_handler(self):
        mgr = CronManager()
        job = CronJob(id="j10b", name="reghandler", expression="* * * * *")

        async def dummy_handler(j):
            pass

        mgr.register(job, dummy_handler)
        assert "j10b" in mgr._handlers


class TestCronManagerUnregister:
    def test_unregister(self):
        mgr = CronManager()
        job = CronJob(id="j11", name="unreg", expression="* * * * *")
        mgr.register(job)
        mgr.unregister("j11")
        assert mgr.get("j11") is None


class TestCronManagerList:
    def test_list_jobs(self):
        mgr = CronManager()
        job = CronJob(id="j12", name="listed", expression="* * * * *", graph_id="g1")
        mgr.register(job)
        items = mgr.list()
        assert len(items) == 1
        assert items[0]["id"] == "j12"


class TestCronManagerCount:
    def test_count(self):
        mgr = CronManager()
        assert mgr.count == 0
        mgr.register(CronJob(id="j13", name="c", expression="* * * * *"))
        assert mgr.count == 1


class TestCronManagerStop:
    def test_stop(self):
        mgr = CronManager()
        mgr._running = True
        mock_task = MagicMock()
        mgr._task = mock_task
        mgr.stop()
        assert mgr._running is False
        mock_task.cancel.assert_called_once()
        assert mgr._task is None


# ── StepDebugger ──


class TestStepDebuggerStepInto:
    @pytest.mark.asyncio
    async def test_step_into_sets_state_and_pause_event(self):
        dbg = StepDebugger()
        await dbg.pause()
        assert dbg.state == DebuggerState.PAUSED
        assert not dbg._pause_event.is_set()

        await dbg.step_into()
        assert dbg.state == DebuggerState.STEP_INTO
        assert dbg._pause_event.is_set()


class TestStepDebuggerCheckPause:
    @pytest.mark.asyncio
    async def test_check_pause_breakpoint_hit(self):
        dbg = StepDebugger()
        dbg.add_breakpoint("node_a")
        assert dbg.has_breakpoint("node_a")

        task = asyncio.create_task(dbg.check_pause("node_a", {"x": 1}))
        event = await asyncio.wait_for(dbg.next_event(), timeout=1.0)
        await task

        assert event.type == "breakpoint_hit"
        assert event.node_id == "node_a"
        assert "hit 1" in event.message
        assert dbg.state == DebuggerState.PAUSED

    @pytest.mark.asyncio
    async def test_check_pause_breakpoint_hit_count_increments(self):
        dbg = StepDebugger()
        dbg.add_breakpoint("node_b")

        for i in range(3):
            await dbg.resume()
            task = asyncio.create_task(dbg.check_pause("node_b"))
            await asyncio.wait_for(dbg.next_event(), timeout=1.0)
            await task

        bp = dbg._breakpoints["node_b"]
        assert bp.hit_count == 3

    @pytest.mark.asyncio
    async def test_check_pause_step_over(self):
        dbg = StepDebugger()
        await dbg.step_over()
        assert dbg.state == DebuggerState.STEP_OVER

        task = asyncio.create_task(dbg.check_pause("node_c", {"v": 42}))
        event = await asyncio.wait_for(dbg.next_event(), timeout=1.0)
        await task

        assert event.type == "step"
        assert event.node_id == "node_c"
        assert dbg.state == DebuggerState.PAUSED

    @pytest.mark.asyncio
    async def test_check_pause_running_does_not_block(self):
        dbg = StepDebugger()
        assert dbg.state == DebuggerState.RUNNING

        await dbg.check_pause("node_d")
        assert dbg.state == DebuggerState.RUNNING

    @pytest.mark.asyncio
    async def test_check_pause_with_variables(self):
        dbg = StepDebugger()
        dbg.add_breakpoint("node_e")

        task = asyncio.create_task(dbg.check_pause("node_e", {"foo": "bar"}))
        event = await asyncio.wait_for(dbg.next_event(), timeout=1.0)
        await task

        assert event.variables == {"foo": "bar"}

    @pytest.mark.asyncio
    async def test_check_pause_without_variables_defaults_empty(self):
        dbg = StepDebugger()
        dbg.add_breakpoint("node_f")

        task = asyncio.create_task(dbg.check_pause("node_f"))
        event = await asyncio.wait_for(dbg.next_event(), timeout=1.0)
        await task

        assert event.variables == {}


class TestStepDebuggerPauseResume:
    @pytest.mark.asyncio
    async def test_pause_and_resume(self):
        dbg = StepDebugger()
        await dbg.pause()
        assert dbg.state == DebuggerState.PAUSED

        pause_event = await asyncio.wait_for(dbg.next_event(), timeout=1.0)
        assert pause_event.type == "pause"

        await dbg.resume()
        assert dbg.state == DebuggerState.RUNNING

        resume_event = await asyncio.wait_for(dbg.next_event(), timeout=1.0)
        assert resume_event.type == "resume"


class TestStepDebuggerBreakpoints:
    def test_add_and_remove_breakpoint(self):
        dbg = StepDebugger()
        dbg.add_breakpoint("n1")
        assert dbg.has_breakpoint("n1")
        dbg.remove_breakpoint("n1")
        assert not dbg.has_breakpoint("n1")

    def test_breakpoint_with_condition(self):
        dbg = StepDebugger()
        dbg.add_breakpoint("n2", condition="x > 5")
        assert dbg._breakpoints["n2"].condition == "x > 5"

    def test_has_breakpoint_disabled(self):
        dbg = StepDebugger()
        dbg.add_breakpoint("n3")
        dbg._breakpoints["n3"].enabled = False
        assert not dbg.has_breakpoint("n3")

    def test_remove_nonexistent_breakpoint(self):
        dbg = StepDebugger()
        dbg.remove_breakpoint("nonexistent")


class TestStepDebuggerStop:
    def test_stop(self):
        dbg = StepDebugger()
        dbg.stop()
        assert dbg.state == DebuggerState.STOPPED
        assert dbg._pause_event.is_set()


# ── VariableManager edge cases ──


class TestVariableManagerEdgeCases:
    def test_set_boolean_non_string(self):
        from agent_runtime.variable_manager import VariableManager

        vm = VariableManager()
        vm.set("flag", 1, coerce="boolean")
        assert vm.get("flag") is True
        vm.set("flag", 0, coerce="boolean")
        assert vm.get("flag") is False

    def test_set_json_string(self):
        from agent_runtime.variable_manager import VariableManager

        vm = VariableManager()
        vm.set("data", '{"x": 1}', coerce="json")
        assert vm.get("data") == {"x": 1}

    def test_get_nested_key_not_found(self):
        from agent_runtime.variable_manager import VariableManager

        vm = VariableManager()
        vm.set("obj", {"a": 1})
        assert vm.get("obj.b", "default") == "default"

    def test_get_nested_list_index_error(self):
        from agent_runtime.variable_manager import VariableManager

        vm = VariableManager()
        vm.set("arr", [10, 20])
        assert vm.get("arr.5", "fallback") == "fallback"

    def test_get_nested_non_dict_list(self):
        from agent_runtime.variable_manager import VariableManager

        vm = VariableManager()
        vm.set("val", 42)
        assert vm.get("val.foo", "nope") == "nope"
