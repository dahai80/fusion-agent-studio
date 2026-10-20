import {
    DataTable,
    EmptyState,
    ErrorState,
    FormField,
    SkeletonLines,
    StatusBadge,
    SubmitButton,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { useCallback, useEffect, useState } from 'react';
import { type GraphItem, api, rpc } from '../api';
import { PageHeader } from '../components/PageHeader';
import { StreamPanel } from '../components/StreamPanel';
import { Modal } from '../components/ui/Modal';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// O9 (#386): topology is edited ONLY on the canvas. This page creates an
// empty graph and hands off; the edit action jumps to the canvas with the
// graph preselected. No JSON topology entry point exists here.
const CANVAS_GRAPH_KEY = 'fusion.canvas.graphId';

export function openCanvas(graphId: string): void {
    try {
        window.sessionStorage.setItem(CANVAS_GRAPH_KEY, graphId);
    } catch {
        // sessionStorage unavailable — canvas falls back to its selector
    }
    window.location.hash = '#/canvas';
}

// Workflows (agent graphs) list + per-graph quick actions: submit a tracked
// run (task store) or a live streaming test (SSE). Four states per spec S1.
// #331: create / delete via graph.* RPC bridge (POST /v1/rpc).
export function WorkflowsPage() {
    const t = useT();
    const [selected, setSelected] = useState<GraphItem | null>(null);
    const [goal, setGoal] = useState('');
    const [goalError, setGoalError] = useState<string | null>(null);
    const [streaming, setStreaming] = useState(false);
    const [editorOpen, setEditorOpen] = useState(false);
    const [wfName, setWfName] = useState('');
    const [wfDesc, setWfDesc] = useState('');
    const [wfError, setWfError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => api.graphs(1, 200),
        []
    );

    const openCreate = () => {
        setWfName('');
        setWfDesc('');
        setWfError(null);
        setEditorOpen(true);
    };

    const submitEditor = async () => {
        if (!wfName.trim()) {
            setWfError(t('请输入名称', 'name required'));
            return;
        }
        setSaving(true);
        try {
            const res = await rpc<{ graph_id?: string }>('graph.create', {
                name: wfName.trim(),
                description: wfDesc.trim(),
                nodes: [],
                edges: [],
            });
            sdk.toast.show({
                level: 'success',
                message: t('工作流已创建，正在打开画布', 'workflow created, opening canvas'),
            });
            setEditorOpen(false);
            manualRetry();
            if (res?.graph_id) openCanvas(res.graph_id);
        } catch (e) {
            setWfError((e as Error).message);
        } finally {
            setSaving(false);
        }
    };

    const deleteGraph = async (g: GraphItem) => {
        const ok = window.confirm(
            t(
                `确认删除工作流「${g.name || g.graph_id}」？`,
                `Delete workflow "${g.name || g.graph_id}"?`
            )
        );
        if (!ok) return;
        try {
            await rpc('graph.delete', { graph_id: g.graph_id });
            sdk.toast.show({
                level: 'success',
                message: t('工作流已删除', 'workflow deleted'),
            });
            if (selected?.graph_id === g.graph_id) setSelected(null);
            manualRetry();
        } catch (e) {
            sdk.toast.show({
                level: 'error',
                message: `${t('删除失败', 'delete failed')}: ${(e as Error).message}`,
            });
        }
    };

    const submitRun = async () => {
        if (!selected) return;
        if (!goal.trim()) {
            setGoalError(t('请输入目标', 'goal required'));
            return;
        }
        setGoalError(null);
        try {
            const res = await api.submitRun({ goal: goal.trim(), graph_id: selected.graph_id });
            sdk.toast.show({
                level: 'success',
                message: t(`已提交 ${res.run_id}`, `submitted ${res.run_id}`),
            });
            window.location.hash = `#/runs/${encodeURIComponent(res.run_id)}`;
        } catch (e) {
            sdk.toast.show({
                level: 'error',
                message: `${t('提交失败', 'submit failed')}: ${(e as Error).message}`,
            });
        }
    };

    const rows = data?.data ?? [];

    return (
        <div>
            <PageHeader
                title={t('工作流', 'Workflows')}
                subtitle={t(
                    `${rows.length} 个 agent 图`,
                    `${rows.length} agent graph${rows.length === 1 ? '' : 's'}`
                )}
                actions={
                    <button
                        type="button"
                        onClick={() => void openCreate()}
                        className="rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-muted"
                    >
                        {t('新建工作流', 'New workflow')}
                    </button>
                }
            />
            <div className="px-4 pt-3">
                {loading && attempt === 0 && rows.length === 0 ? (
                    <SkeletonLines rows={4} />
                ) : error ? (
                    <ErrorState
                        message={t('加载工作流失败', 'failed to load workflows')}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                ) : rows.length === 0 ? (
                    <EmptyState
                        title={t('暂无工作流', 'no workflows yet')}
                        description={t(
                            '通过 daemon RPC graph.create 或 SDK 创建第一个 agent 图',
                            'create your first agent graph via the graph.create RPC or SDK'
                        )}
                    />
                ) : (
                    <DataTable<GraphItem>
                        rows={rows}
                        rowKey={(g) => g.graph_id}
                        columns={[
                            {
                                key: 'name',
                                header: t('名称', 'Name'),
                                sortable: true,
                                render: (g) => (
                                    <button
                                        type="button"
                                        className="text-brand hover:underline"
                                        onClick={() =>
                                            setSelected(
                                                selected?.graph_id === g.graph_id ? null : g
                                            )
                                        }
                                    >
                                        {g.name || g.graph_id}
                                    </button>
                                ),
                            },
                            {
                                key: 'nodes',
                                header: t('节点', 'Nodes'),
                                sortable: true,
                                render: (g) => Object.keys(g.graph_data.nodes ?? {}).length,
                            },
                            {
                                key: 'description',
                                header: t('描述', 'Description'),
                                render: (g) => (
                                    <span className="text-text-secondary">
                                        {g.description || '-'}
                                    </span>
                                ),
                            },
                            {
                                key: 'actions',
                                header: t('操作', 'Actions'),
                                render: (g) => (
                                    <div className="flex items-center gap-2">
                                        <button
                                            type="button"
                                            className="text-brand hover:underline"
                                            onClick={() => openCanvas(g.graph_id)}
                                        >
                                            {t('在画布中编辑', 'Edit on canvas')}
                                        </button>
                                        <button
                                            type="button"
                                            className="text-error hover:underline"
                                            onClick={() => void deleteGraph(g)}
                                        >
                                            {t('删除', 'Delete')}
                                        </button>
                                    </div>
                                ),
                            },
                        ]}
                    />
                )}
            </div>
            {selected ? (
                <div className="mt-4 flex flex-col gap-3 px-4">
                    <div className="rounded-card border border-border bg-bg-elevated p-4">
                        <div className="flex items-center justify-between">
                            <span className="text-sm text-text-primary">
                                {selected.name || selected.graph_id}
                            </span>
                            {streaming ? (
                                <StatusBadge
                                    status="running"
                                    label={t('流式运行中', 'streaming')}
                                />
                            ) : null}
                        </div>
                        <div className="mt-3 flex flex-col gap-2">
                            <FormField
                                label={t('运行目标', 'Goal')}
                                error={goalError}
                                required
                                htmlFor="wf-goal"
                            >
                                <TextInput
                                    id="wf-goal"
                                    value={goal}
                                    onChange={setGoal}
                                    placeholder={t(
                                        '例如：总结今天的新闻并写入摘要文件',
                                        'e.g. summarize today’s news into a digest file'
                                    )}
                                />
                            </FormField>
                            <div className="flex flex-wrap items-center gap-2">
                                <SubmitButton
                                    label={t('提交运行', 'Submit run')}
                                    busyLabel={t('提交中…', 'submitting…')}
                                    disabled={streaming}
                                    onAction={submitRun}
                                />
                                <button
                                    type="button"
                                    onClick={() => setStreaming((v) => !v)}
                                    className="rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-muted"
                                >
                                    {streaming
                                        ? t('关闭流式试运行', 'close live test')
                                        : t('流式试运行', 'live test (stream)')}
                                </button>
                            </div>
                        </div>
                    </div>
                    {streaming ? (
                        <StreamPanel
                            graphId={selected.graph_id}
                            input={goal}
                            onActiveChange={(active) =>
                                sdk.tab.setActiveTask(
                                    active
                                        ? {
                                              name: `${selected.name || selected.graph_id} ${t(
                                                  '流式试运行',
                                                  'live test'
                                              )}`,
                                          }
                                        : null
                                )
                            }
                        />
                    ) : null}
                </div>
            ) : null}
            <Modal
                open={editorOpen}
                title={t('新建工作流', 'New workflow')}
                onClose={() => setEditorOpen(false)}
                footer={
                    <div className="flex items-center justify-end gap-2">
                        <button
                            type="button"
                            onClick={() => setEditorOpen(false)}
                            className="rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-muted"
                        >
                            {t('取消', 'Cancel')}
                        </button>
                        <SubmitButton
                            label={t('创建并打开画布', 'Create & open canvas')}
                            busyLabel={t('保存中…', 'saving…')}
                            disabled={saving}
                            onAction={submitEditor}
                        />
                    </div>
                }
            >
                <FormField label={t('名称', 'Name')} required htmlFor="wf-name">
                    <TextInput
                        id="wf-name"
                        value={wfName}
                        onChange={setWfName}
                        placeholder={t('例如：新闻摘要工作流', 'e.g. news digest workflow')}
                    />
                </FormField>
                <FormField label={t('描述', 'Description')} htmlFor="wf-desc">
                    <TextInput
                        id="wf-desc"
                        value={wfDesc}
                        onChange={setWfDesc}
                        placeholder={t('可选', 'optional')}
                    />
                </FormField>
                {wfError ? <div className="text-xs text-error">{wfError}</div> : null}
                <div className="text-xs text-text-secondary">
                    {t(
                        '创建后在画布上添加节点与连线（拓扑只在画布编辑）',
                        'Add nodes and edges on the canvas after creation (topology is canvas-only)'
                    )}
                </div>
            </Modal>
            <OrchestrationsSection />
        </div>
    );
}

// #397 (audit-1020-5): multi-agent orchestration patterns in GUI — the
// workflow engine's six patterns surfaced via workflow.create/execute/
// status/list/cancel. Phases reference existing agents (agent.list);
// runs of the selected workflow poll every 5s while the section is open.
const WF_PATTERNS = [
    'pipeline',
    'parallel_barrier',
    'loop_until_dry',
    'loop_until_budget',
    'adversarial_verify',
    'judge_panel',
] as const;

interface WfPhaseDraft {
    name: string;
    pattern: string;
    agentIds: string[];
}

interface WfRunItem {
    id: string;
    workflow_id: string;
    status: string;
    current_phase?: number;
    phase_results?: Array<Record<string, unknown>>;
    final_result?: Record<string, unknown>;
    error?: string;
}

interface WfConfigItem {
    id: string;
    name: string;
    phases?: Array<{ name: string; pattern: string; agent_configs?: unknown[] }>;
    created_at?: number;
}

function OrchestrationsSection() {
    const t = useT();
    const [workflows, setWorkflows] = useState<WfConfigItem[]>([]);
    const [agents, setAgents] = useState<Array<{ id: string; name: string }>>([]);
    const [error, setError] = useState('');
    const [creating, setCreating] = useState(false);
    const [saving, setSaving] = useState(false);
    const [wfName, setWfName] = useState('');
    const [phases, setPhases] = useState<WfPhaseDraft[]>([
        { name: 'phase-1', pattern: 'pipeline', agentIds: [] },
    ]);
    const [execWf, setExecWf] = useState<WfConfigItem | null>(null);
    const [execInput, setExecInput] = useState('');
    const [runs, setRuns] = useState<WfRunItem[]>([]);
    const [busy, setBusy] = useState(false);

    const refresh = useCallback(async () => {
        try {
            const [wl, al] = await Promise.all([
                rpc<{ workflows?: WfConfigItem[] }>('workflow.list', {}),
                rpc<{ agents?: Array<{ id: string; name: string }> }>('agent.list', {}),
            ]);
            setWorkflows(wl.workflows || []);
            setAgents((al.agents || []).map((a) => ({ id: a.id, name: a.name || a.id })));
            setError('');
        } catch (e) {
            setError((e as Error).message);
        }
    }, []);

    useEffect(() => {
        void refresh();
    }, [refresh]);

    useEffect(() => {
        if (!execWf) {
            setRuns([]);
            return;
        }
        const load = async () => {
            try {
                const res = await rpc<{ runs?: WfRunItem[] }>('workflow.list', {
                    workflow_id: execWf.id,
                });
                setRuns(res.runs || []);
            } catch {
                // transient poll error — keep last snapshot
            }
        };
        void load();
        const timer = window.setInterval(() => void load(), 5000);
        return () => window.clearInterval(timer);
    }, [execWf]);

    const createWorkflow = async () => {
        if (!wfName.trim()) {
            setError(t('请输入编排名称', 'orchestration name required'));
            return;
        }
        setSaving(true);
        try {
            await rpc('workflow.create', {
                name: wfName.trim(),
                phases: phases.map((p) => ({
                    name: p.name || 'phase',
                    pattern: p.pattern,
                    agent_configs: p.agentIds.map((id) => ({ agent_id: id, name: id })),
                })),
            });
            sdk.toast.show({
                level: 'success',
                message: t('编排已创建', 'orchestration created'),
            });
            setCreating(false);
            setWfName('');
            setPhases([{ name: 'phase-1', pattern: 'pipeline', agentIds: [] }]);
            await refresh();
        } catch (e) {
            setError((e as Error).message);
        } finally {
            setSaving(false);
        }
    };

    const executeWorkflow = async () => {
        if (!execWf) return;
        setBusy(true);
        try {
            const res = await rpc<{ run_id?: string }>('workflow.execute', {
                workflow_id: execWf.id,
                input: execInput,
            });
            sdk.toast.show({
                level: 'success',
                message: t(
                    `编排运行已启动 ${res.run_id || ''}`,
                    `orchestration run started ${res.run_id || ''}`
                ),
            });
        } catch (e) {
            sdk.toast.show({
                level: 'error',
                message: `${t('启动失败', 'start failed')}: ${(e as Error).message}`,
            });
        } finally {
            setBusy(false);
        }
    };

    const cancelRun = async (runId: string) => {
        try {
            await rpc('workflow.cancel', { run_id: runId });
            sdk.toast.show({ level: 'success', message: t('已请求取消', 'cancel requested') });
        } catch (e) {
            sdk.toast.show({
                level: 'error',
                message: `${t('取消失败', 'cancel failed')}: ${(e as Error).message}`,
            });
        }
    };

    const deleteWorkflow = async (wf: WfConfigItem) => {
        if (
            !window.confirm(t(`确认删除编排「${wf.name}」？`, `Delete orchestration "${wf.name}"?`))
        ) {
            return;
        }
        try {
            await rpc('workflow.delete', { workflow_id: wf.id });
            if (execWf?.id === wf.id) setExecWf(null);
            await refresh();
        } catch (e) {
            sdk.toast.show({
                level: 'error',
                message: `${t('删除失败', 'delete failed')}: ${(e as Error).message}`,
            });
        }
    };

    return (
        <div className="mt-6 px-4 pb-6">
            <PageHeader
                title={t('编排', 'Orchestrations')}
                subtitle={t(
                    `${workflows.length} 个多智能体编排（6 种模式）`,
                    `${workflows.length} multi-agent orchestrations (6 patterns)`
                )}
                actions={
                    <button
                        type="button"
                        onClick={() => setCreating(true)}
                        className="rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-muted"
                    >
                        {t('新建编排', 'New orchestration')}
                    </button>
                }
            />
            {error ? <div className="mt-2 text-xs text-error">{error}</div> : null}
            {workflows.length === 0 ? (
                <div className="text-xs text-text-secondary">
                    {t(
                        '暂无编排。编排 = 多阶段多智能体模式（pipeline / parallel_barrier / loop / adversarial_verify / judge_panel）。',
                        'No orchestrations yet. An orchestration = multi-phase multi-agent patterns (pipeline / parallel_barrier / loop / adversarial_verify / judge_panel).'
                    )}
                </div>
            ) : (
                <div className="mt-2 space-y-2">
                    {workflows.map((wf) => (
                        <div
                            key={wf.id}
                            className="rounded-card border border-border bg-bg-elevated p-3 text-xs"
                        >
                            <div className="flex items-center justify-between">
                                <button
                                    type="button"
                                    className="text-sm text-brand hover:underline"
                                    onClick={() => setExecWf(execWf?.id === wf.id ? null : wf)}
                                >
                                    {wf.name}
                                </button>
                                <button
                                    type="button"
                                    className="text-error hover:underline"
                                    onClick={() => void deleteWorkflow(wf)}
                                >
                                    {t('删除', 'Delete')}
                                </button>
                            </div>
                            <div className="mt-1 flex flex-wrap gap-1 text-[10px] text-text-secondary">
                                {(wf.phases || []).map((p, i) => (
                                    <span key={i} className="rounded bg-brand/10 text-brand px-1">
                                        {p.name || `phase-${i + 1}`} · {p.pattern} ·{' '}
                                        {(p.agent_configs || []).length} agent(s)
                                    </span>
                                ))}
                            </div>
                            {execWf?.id === wf.id ? (
                                <div className="mt-2 space-y-2 border-t border-border pt-2">
                                    <textarea
                                        className="h-16 w-full rounded border border-border bg-bg px-2 py-1"
                                        placeholder={t('编排输入', 'orchestration input')}
                                        value={execInput}
                                        onChange={(e) => setExecInput(e.target.value)}
                                    />
                                    <button
                                        type="button"
                                        disabled={busy}
                                        onClick={() => void executeWorkflow()}
                                        className="rounded bg-brand px-3 py-1 text-white disabled:opacity-50"
                                    >
                                        {busy
                                            ? t('启动中…', 'starting…')
                                            : t('执行编排', 'Execute')}
                                    </button>
                                    {runs.length > 0 ? (
                                        <div className="space-y-1">
                                            <div className="text-text-secondary">
                                                {t('运行记录', 'Runs')}
                                            </div>
                                            {runs.map((r) => (
                                                <div
                                                    key={r.id}
                                                    className="flex items-center gap-2 rounded border border-border px-2 py-1"
                                                >
                                                    <StatusBadge
                                                        status={
                                                            r.status === 'completed'
                                                                ? 'success'
                                                                : r.status === 'failed' ||
                                                                    r.status === 'cancelled'
                                                                  ? 'error'
                                                                  : r.status === 'paused'
                                                                    ? 'warning'
                                                                    : 'running'
                                                        }
                                                        label={r.status}
                                                    />
                                                    <span className="font-mono">{r.id}</span>
                                                    <span className="text-text-secondary">
                                                        phase {r.current_phase ?? 0}
                                                    </span>
                                                    {r.error ? (
                                                        <span className="text-error">
                                                            {r.error.slice(0, 80)}
                                                        </span>
                                                    ) : null}
                                                    {r.status === 'running' ||
                                                    r.status === 'paused' ||
                                                    r.status === 'pending' ? (
                                                        <button
                                                            type="button"
                                                            className="ml-auto text-error hover:underline"
                                                            onClick={() => void cancelRun(r.id)}
                                                        >
                                                            {t('取消', 'Cancel')}
                                                        </button>
                                                    ) : null}
                                                </div>
                                            ))}
                                        </div>
                                    ) : null}
                                </div>
                            ) : null}
                        </div>
                    ))}
                </div>
            )}
            <Modal
                open={creating}
                title={t('新建编排', 'New orchestration')}
                onClose={() => setCreating(false)}
                footer={
                    <div className="flex items-center justify-end gap-2">
                        <button
                            type="button"
                            onClick={() => setCreating(false)}
                            className="rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-muted"
                        >
                            {t('取消', 'Cancel')}
                        </button>
                        <SubmitButton
                            label={t('创建编排', 'Create orchestration')}
                            busyLabel={t('保存中…', 'saving…')}
                            disabled={saving}
                            onAction={createWorkflow}
                        />
                    </div>
                }
            >
                <FormField label={t('名称', 'Name')} required htmlFor="orch-name">
                    <TextInput
                        id="orch-name"
                        value={wfName}
                        onChange={setWfName}
                        placeholder={t(
                            '例如：研究→写作→审校流水线',
                            'e.g. research → write → review pipeline'
                        )}
                    />
                </FormField>
                <div className="space-y-2">
                    {phases.map((ph, idx) => (
                        <div key={idx} className="rounded border border-border p-2 text-xs">
                            <div className="flex items-center gap-2">
                                <TextInput
                                    value={ph.name}
                                    onChange={(v) =>
                                        setPhases((ps) =>
                                            ps.map((p, i) => (i === idx ? { ...p, name: v } : p))
                                        )
                                    }
                                    placeholder={t('阶段名', 'phase name')}
                                />
                                <select
                                    className="rounded border border-border bg-bg px-2 py-1"
                                    value={ph.pattern}
                                    onChange={(e) =>
                                        setPhases((ps) =>
                                            ps.map((p, i) =>
                                                i === idx ? { ...p, pattern: e.target.value } : p
                                            )
                                        )
                                    }
                                >
                                    {WF_PATTERNS.map((pt) => (
                                        <option key={pt} value={pt}>
                                            {pt}
                                        </option>
                                    ))}
                                </select>
                                <button
                                    type="button"
                                    className="text-error"
                                    disabled={phases.length === 1}
                                    onClick={() =>
                                        setPhases((ps) => ps.filter((_, i) => i !== idx))
                                    }
                                >
                                    ✕
                                </button>
                            </div>
                            <div className="mt-1 flex flex-wrap gap-2">
                                {agents.length === 0 ? (
                                    <span className="text-text-secondary">
                                        {t('暂无可选 agent', 'no agents available')}
                                    </span>
                                ) : (
                                    agents.map((a) => (
                                        <label key={a.id} className="flex items-center gap-1">
                                            <input
                                                type="checkbox"
                                                checked={ph.agentIds.includes(a.id)}
                                                onChange={(e) =>
                                                    setPhases((ps) =>
                                                        ps.map((p, i) =>
                                                            i === idx
                                                                ? {
                                                                      ...p,
                                                                      agentIds: e.target.checked
                                                                          ? [...p.agentIds, a.id]
                                                                          : p.agentIds.filter(
                                                                                (x) => x !== a.id
                                                                            ),
                                                                  }
                                                                : p
                                                        )
                                                    )
                                                }
                                            />
                                            {a.name}
                                        </label>
                                    ))
                                )}
                            </div>
                        </div>
                    ))}
                    <button
                        type="button"
                        className="rounded border border-border px-2 py-1 text-xs"
                        onClick={() =>
                            setPhases((ps) => [
                                ...ps,
                                {
                                    name: `phase-${ps.length + 1}`,
                                    pattern: 'pipeline',
                                    agentIds: [],
                                },
                            ])
                        }
                    >
                        + {t('添加阶段', 'Add phase')}
                    </button>
                </div>
                {error ? <div className="text-xs text-error">{error}</div> : null}
            </Modal>
        </div>
    );
}
