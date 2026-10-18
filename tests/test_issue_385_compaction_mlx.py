import pytest

from agent_runtime._compactor_local import CompactionConfig, Compactor, RetentionList


class _Resp:
    def __init__(self, content: str):
        self.content = content


class _Gateway:
    def __init__(self, content="MLX SUMMARY", exc=None):
        self.content = content
        self.exc = exc
        self.calls = 0

    async def chat(self, **kwargs):
        self.calls += 1
        if self.exc:
            raise self.exc
        return _Resp(self.content)


def _long_messages(rounds=40, size=2000) -> list[dict]:
    msgs = [{"role": "system", "content": "system prompt"}]
    for i in range(rounds):
        msgs.append({"role": "user", "content": f"question {i} TODO item {i}"})
        msgs.append({"role": "assistant", "content": "x" * size})
        msgs.append({"role": "tool", "content": "tool output " + "y" * size})
    return msgs


def _config() -> CompactionConfig:
    return CompactionConfig(context_window=8000, warning_buffer=500, error_buffer=200, manual_buffer=100)


def test_chinese_retention_buckets():
    msgs = [
        {"role": "user", "content": "待办：补充接口文档"},
        {"role": "assistant", "content": "约束：不能改上游代码"},
        {"role": "assistant", "content": "进行中：迁移画布"},
        {"role": "assistant", "content": "async def run_task():"},
    ]
    rl = RetentionList.from_messages(msgs)
    assert rl.todos and "补充接口文档" in rl.todos[0]
    assert rl.constraints and "不能改上游代码" in rl.constraints[0]
    assert rl.active_tasks and "迁移画布" in rl.active_tasks[0]
    assert any("run_task" in item for item in rl.interfaces)


@pytest.mark.asyncio
async def test_compact_async_uses_mlx_summary(monkeypatch):
    monkeypatch.delenv("FUSION_COMPACT_MLX", raising=False)
    gateway = _Gateway()
    compactor = Compactor(config=_config(), llm_gateway=gateway)
    out = await compactor.compact_async(_long_messages(), "error")
    text = "\n".join(str(m.get("content")) for m in out)
    assert "MLX SUMMARY" in text
    assert gateway.calls >= 1


@pytest.mark.asyncio
async def test_compact_async_falls_back_on_error(monkeypatch):
    monkeypatch.delenv("FUSION_COMPACT_MLX", raising=False)
    gateway = _Gateway(exc=RuntimeError("mlx down"))
    compactor = Compactor(config=_config(), llm_gateway=gateway)
    out = await compactor.compact_async(_long_messages(), "error")
    text = "\n".join(str(m.get("content")) for m in out)
    assert "[Compacted prior context]" in text
    assert "mlx down" not in text


@pytest.mark.asyncio
async def test_mlx_disabled_by_env(monkeypatch):
    monkeypatch.setenv("FUSION_COMPACT_MLX", "0")
    gateway = _Gateway()
    compactor = Compactor(config=_config(), llm_gateway=gateway)
    await compactor.compact_async(_long_messages(), "warning")
    assert gateway.calls == 0


def test_compaction_brings_tokens_under_budget():
    compactor = Compactor(config=_config())
    msgs = _long_messages()
    before = compactor.estimate_tokens(msgs)
    out = compactor.compact(msgs, "error")
    after = compactor.estimate_tokens(out)
    assert before > after
    assert after < compactor.config.error_threshold()
