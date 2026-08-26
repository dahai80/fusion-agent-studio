"""God-object split verification — audit 0826 tech-debt #2 (P2-4).

runtime.py was 3604 LOC, AgentRuntime ~48 methods. Audit flagged the
"5 bundle 全加法修复从未提取模块" god-object as tracked tech-debt. The
fix extracts method groups into 4 mixin modules while preserving the
public AgentRuntime(...) interface verbatim:

  _SafetyApprovalMixin   (_runtime_safety.py)
  _CheckpointMixin       (_runtime_checkpoint.py)
  _DynamicToolsMixin     (_runtime_dynamic_tools.py)
  _NodeExecutorsMixin    (_runtime_nodes.py)

Shared module-level helpers/ConditionEngine moved to leaf _runtime_helpers.py
to break the circular import. This test pins the split: all 4 mixins present
in MRO, the moved methods still resolve on AgentRuntime, and the backward-
compat re-exports from runtime still import.
"""
from __future__ import annotations

from agent_runtime._runtime_checkpoint import _CheckpointMixin
from agent_runtime._runtime_dynamic_tools import _DynamicToolsMixin
from agent_runtime._runtime_helpers import ConditionEngine, logger
from agent_runtime._runtime_nodes import _NodeExecutorsMixin
from agent_runtime._runtime_safety import _SafetyApprovalMixin
from agent_runtime.runtime import (
    _MAX_RETRY_CONTEXT_MESSAGES,
    _MAX_TOOL_CALL_CHAIN,
    AgentRuntime,
    _env_flag,
    _max_sub_graph_depth,
    _parallel_branch_concurrency,
)


class TestGodObjectSplit:
    def test_mro_contains_all_four_mixins(self):
        mro_names = {cls.__name__ for cls in AgentRuntime.__mro__}
        assert _SafetyApprovalMixin.__name__ in mro_names
        assert _CheckpointMixin.__name__ in mro_names
        assert _DynamicToolsMixin.__name__ in mro_names
        assert _NodeExecutorsMixin.__name__ in mro_names

    def test_mixin_methods_resolve_on_runtime(self):
        # representative method from each mixin must resolve to the mixin's fn
        assert AgentRuntime.approve_action.__qualname__.startswith(
            _SafetyApprovalMixin.__name__ + "."
        )
        assert AgentRuntime.resume_from_checkpoint.__qualname__.startswith(
            _CheckpointMixin.__name__ + "."
        )
        assert AgentRuntime._dynamic_register_tool.__qualname__.startswith(
            _DynamicToolsMixin.__name__ + "."
        )
        assert AgentRuntime._execute_llm_node.__qualname__.startswith(
            _NodeExecutorsMixin.__name__ + "."
        )

    def test_no_method_name_overlap_across_mixins(self):
        # MRO resolution would silently shadow on overlap; assert none exists
        # so method order in the bases tuple never affects behavior.
        import inspect

        seen: dict[str, str] = {}
        for mixin in (
            _SafetyApprovalMixin,
            _CheckpointMixin,
            _DynamicToolsMixin,
            _NodeExecutorsMixin,
        ):
            for name, _ in inspect.getmembers(mixin, predicate=inspect.isfunction):
                if name.startswith("__") and name.endswith("__"):
                    continue
                owner = seen.get(name)
                assert owner is None, (
                    f"method '{name}' defined in both {owner} and {mixin.__name__}"
                )
                seen[name] = mixin.__name__

    def test_backward_compat_reexports_from_runtime(self):
        # external code imports these from agent_runtime.runtime; must remain
        # importable after the helper relocation to _runtime_helpers.
        assert _MAX_TOOL_CALL_CHAIN == 10
        assert _MAX_RETRY_CONTEXT_MESSAGES == 20
        assert _max_sub_graph_depth() >= 1
        assert callable(_parallel_branch_concurrency)
        assert callable(_env_flag)
        assert ConditionEngine is not None
        assert logger is not None

    def test_construction_interface_unchanged(self):
        # AgentRuntime() still constructs with no required args (all defaulted)
        rt = AgentRuntime()
        assert isinstance(rt, AgentRuntime)
        # moved attributes/methods still accessible on the instance
        assert hasattr(rt, "approve_action")
        assert hasattr(rt, "_save_checkpoint")
        assert hasattr(rt, "_dynamic_register_tool")
        assert hasattr(rt, "_execute_llm_node")
