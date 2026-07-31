import pytest
from agent_runtime.workflow_engine import (
    WorkflowEngine,
    WorkflowConfig,
    WorkflowPhase,
    WorkflowPattern,
    WorkflowRun,
    WorkflowStatus,
)


@pytest.fixture
def engine():
    return WorkflowEngine()


class TestWorkflowConfig:
    def test_create_workflow(self, engine):
        wf = engine.create_workflow(
            name="Test WF",
            phases=[
                {"name": "step1", "pattern": "pipeline"},
                {"name": "step2", "pattern": "parallel_barrier"},
            ],
        )
        assert wf.name == "Test WF"
        assert len(wf.phases) == 2
        assert wf.phases[0].pattern == WorkflowPattern.PIPELINE
        assert wf.phases[1].pattern == WorkflowPattern.PARALLEL_BARRIER
        assert wf.id.startswith("wf_")

    def test_workflow_to_dict_from_dict(self, engine):
        wf = engine.create_workflow(
            name="Roundtrip",
            phases=[{"name": "p1", "pattern": "adversarial_verify", "config": {"voter_count": 5}}],
        )
        d = wf.to_dict()
        wf2 = WorkflowConfig.from_dict(d)
        assert wf2.name == "Roundtrip"
        assert wf2.phases[0].pattern == WorkflowPattern.ADVERSARIAL_VERIFY
        assert wf2.phases[0].config["voter_count"] == 5

    def test_get_workflow(self, engine):
        wf = engine.create_workflow(name="X", phases=[])
        assert engine.get_workflow(wf.id) is wf
        assert engine.get_workflow("nonexistent") is None

    def test_list_workflows(self, engine):
        engine.create_workflow(name="A", phases=[])
        engine.create_workflow(name="B", phases=[])
        assert len(engine.list_workflows()) >= 2

    def test_delete_workflow(self, engine):
        wf = engine.create_workflow(name="Del", phases=[])
        assert engine.delete_workflow(wf.id) is True
        assert engine.get_workflow(wf.id) is None
        assert engine.delete_workflow("nonexistent") is False


class TestWorkflowPhase:
    def test_phase_from_dict(self):
        p = WorkflowPhase.from_dict({"name": "test", "pattern": "loop_until_dry", "config": {"dry_threshold": 3}})
        assert p.name == "test"
        assert p.pattern == WorkflowPattern.LOOP_UNTIL_DRY
        assert p.config["dry_threshold"] == 3

    def test_phase_to_dict(self):
        p = WorkflowPhase(name="test", pattern=WorkflowPattern.JUDGE_PANEL, config={"judge_agent": {"name": "j"}})
        d = p.to_dict()
        assert d["pattern"] == "judge_panel"
        assert d["config"]["judge_agent"]["name"] == "j"


class TestWorkflowRun:
    def test_run_to_dict(self):
        run = WorkflowRun(workflow_id="wf_123", status=WorkflowStatus.COMPLETED)
        d = run.to_dict()
        assert d["workflow_id"] == "wf_123"
        assert d["status"] == "completed"
        assert d["id"].startswith("wf_run_")

    def test_run_from_dict(self):
        run = WorkflowRun.from_dict({
            "id": "wf_run_abc",
            "workflow_id": "wf_123",
            "status": "failed",
            "current_phase": 2,
            "error": "boom",
        })
        assert run.status == WorkflowStatus.FAILED
        assert run.current_phase == 2
        assert run.error == "boom"


class TestWorkflowExecution:
    @pytest.mark.asyncio
    async def test_execute_empty_workflow(self, engine):
        wf = engine.create_workflow(name="Empty", phases=[])
        run = await engine.execute_workflow(wf.id, initial_input="hello")
        assert run.status == WorkflowStatus.COMPLETED
        assert run.final_result["output"] == "hello"

    @pytest.mark.asyncio
    async def test_execute_workflow_not_found(self, engine):
        with pytest.raises(ValueError, match="Workflow not found"):
            await engine.execute_workflow("nonexistent")

    @pytest.mark.asyncio
    async def test_pause_resume_cancel(self, engine):
        wf = engine.create_workflow(name="Control", phases=[])
        run = await engine.execute_workflow(wf.id, initial_input="test")
        run_id = run.id

        assert engine.pause_run(run_id) is False
        assert engine.resume_run(run_id) is False
        assert engine.cancel_run(run_id) is False

    @pytest.mark.asyncio
    async def test_list_runs(self, engine):
        wf = engine.create_workflow(name="Runs", phases=[])
        run1 = await engine.execute_workflow(wf.id, initial_input="a")
        run2 = await engine.execute_workflow(wf.id, initial_input="b")
        all_runs = engine.list_runs()
        ids = [r.id for r in all_runs]
        assert run1.id in ids
        assert run2.id in ids

        wf_runs = engine.list_runs(wf.id)
        assert len(wf_runs) == 2

    @pytest.mark.asyncio
    async def test_get_run_status(self, engine):
        wf = engine.create_workflow(name="Status", phases=[])
        run = await engine.execute_workflow(wf.id, initial_input="x")
        status = engine.get_run_status(run.id)
        assert status is not None
        assert status.status == WorkflowStatus.COMPLETED
        assert engine.get_run_status("nonexistent") is None
