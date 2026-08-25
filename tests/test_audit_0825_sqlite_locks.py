"""审计 A-6/R-7/3M-3 回归: 5 个 SQLite 库统一加 threading.RLock + WAL + busy_timeout.

memory_engine 已有 (#174), 其余 4 库 (persistence/task_store/triggers/metrics/knowledge)
在 v0.3.45 (47d9e4c) 无锁无 WAL, 跨线程 asyncio.to_thread 共享单连接写竞态.
本测试验证:
  1. 各库连接开启 WAL + busy_timeout PRAGMA
  2. 各库实例持有 _write_lock (threading.RLock)
  3. 并发 asyncio.to_thread 写不抛 OperationalError/ProgrammingError 且结果完整
"""
import asyncio
import threading

from agent_runtime.knowledge_engine import KnowledgeEngine
from agent_runtime.metrics_engine import InferenceMetrics, MetricsEngine
from agent_runtime.persistence import AgentStore
from agent_runtime.task_store import Task, TaskStore
from agent_runtime.triggers import CronManager


def _assert_pragmas(conn, label):
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode == "wal", f"{label}: journal_mode={mode} expected wal"
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000, f"{label}: busy_timeout={timeout} expected 5000"


def test_persistence_lock_and_pragma(tmp_path):
    store = AgentStore(db_path=str(tmp_path / "s.db"))
    assert isinstance(store._write_lock, type(threading.RLock()))
    _assert_pragmas(store._get_conn(), "persistence")
    store.close()


def test_task_store_lock_and_pragma(tmp_path):
    ts = TaskStore(db_path=str(tmp_path / "t.db"))
    assert isinstance(ts._write_lock, type(threading.RLock()))
    _assert_pragmas(ts._conn, "task_store")
    ts.close()


def test_triggers_lock_and_pragma(tmp_path):
    cm = CronManager(db_path=str(tmp_path / "c.db"))
    assert isinstance(cm._write_lock, type(threading.RLock()))
    _assert_pragmas(cm._conn, "triggers")
    cm.close()


def test_metrics_lock_and_pragma(tmp_path):
    me = MetricsEngine(db_path=str(tmp_path / "m.db"))
    assert isinstance(me._write_lock, type(threading.RLock()))
    _assert_pragmas(me._conn, "metrics")
    me.close()


def test_knowledge_lock_and_pragma(tmp_path):
    ke = KnowledgeEngine(db_path=str(tmp_path / "k.db"))
    assert isinstance(ke._write_lock, type(threading.RLock()))
    _assert_pragmas(ke._conn, "knowledge")
    ke.close()


async def test_task_store_concurrent_submit_is_atomic(tmp_path):
    ts = TaskStore(db_path=str(tmp_path / "ct.db"))

    async def _one(i):
        task = Task(title=f"t{i}", graph_id="g1")
        return await asyncio.to_thread(ts.submit, task)

    ids = await asyncio.gather(*(_one(i) for i in range(16)))
    assert len(ids) == 16
    assert len({t.task_id for t in ids}) == 16, "并发提交 id 必须唯一 (原子自增)"
    assert ts.list(limit=100) and len(ts.list(limit=100)) == 16
    ts.close()


async def test_persistence_concurrent_save_graph(tmp_path):
    from agent_runtime.graph import AgentGraph, NodeConfig

    store = AgentStore(db_path=str(tmp_path / "cg.db"))

    async def _one(i):
        g = AgentGraph(id=f"g{i}", name=f"graph{i}")
        g.add_node("s", NodeConfig(type="start"))
        await asyncio.to_thread(store.save_graph, g)

    await asyncio.gather(*(_one(i) for i in range(12)))
    graphs = store.list_graphs()
    assert len(graphs) == 12
    store.close()


async def test_metrics_concurrent_record(tmp_path):
    me = MetricsEngine(db_path=str(tmp_path / "cm.db"))

    async def _one(i):
        m = InferenceMetrics(model=f"m{i}", latency_ms=float(i), tokens_in=i)
        return await asyncio.to_thread(me.record_inference, m)

    await asyncio.gather(*(_one(i) for i in range(10)))
    summary = me.get_summary()
    assert summary.total_inferences == 10
    me.close()


async def test_knowledge_concurrent_ingest(tmp_path):
    ke = KnowledgeEngine(db_path=str(tmp_path / "ck.db"))

    async def _one(i):
        return await asyncio.to_thread(ke.ingest, f"content {i}", scope="cc")

    await asyncio.gather(*(_one(i) for i in range(8)))
    assert ke.count(scope="cc") == 8
    ke.close()
