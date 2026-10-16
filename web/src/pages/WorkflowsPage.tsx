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
import { useState } from 'react';
import { type GraphItem, api, rpc } from '../api';
import { PageHeader } from '../components/PageHeader';
import { StreamPanel } from '../components/StreamPanel';
import { JsonEditor, parseJson } from '../components/ui/JsonEditor';
import { Modal } from '../components/ui/Modal';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// Minimal valid graph skeleton pre-filled in the create/edit JSON editor.
// Node payloads use the {id, type, config?} shape consumed by
// daemon graph.create/_node_config_from_payload (#297 nested-config merge).
const GRAPH_SKELETON = JSON.stringify(
    {
        nodes: [
            { id: 'start', type: 'start' },
            { id: 'llm-1', type: 'llm', config: { prompt: '{{input}}' } },
            { id: 'end', type: 'end' },
        ],
        edges: [
            { source_id: 'start', target_id: 'llm-1' },
            { source_id: 'llm-1', target_id: 'end' },
        ],
    },
    null,
    4
);

interface GraphGetResult {
    graph_id: string;
    name: string;
    description: string;
    nodes: Record<string, Record<string, unknown>>;
    edges: Array<Record<string, unknown>>;
}

// Workflows (agent graphs) list + per-graph quick actions: submit a tracked
// run (task store) or a live streaming test (SSE). Four states per spec S1.
// #331: create / edit / delete via graph.* RPC bridge (POST /v1/rpc).
export function WorkflowsPage() {
    const t = useT();
    const [selected, setSelected] = useState<GraphItem | null>(null);
    const [goal, setGoal] = useState('');
    const [goalError, setGoalError] = useState<string | null>(null);
    const [streaming, setStreaming] = useState(false);
    const [editorOpen, setEditorOpen] = useState(false);
    const [editingGraph, setEditingGraph] = useState<GraphItem | null>(null);
    const [wfName, setWfName] = useState('');
    const [wfDesc, setWfDesc] = useState('');
    const [wfJson, setWfJson] = useState(GRAPH_SKELETON);
    const [wfError, setWfError] = useState<string | null>(null);
    const [saving, setSaving] = useState(false);
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => api.graphs(1, 200),
        []
    );

    const openCreate = () => {
        setEditingGraph(null);
        setWfName('');
        setWfDesc('');
        setWfJson(GRAPH_SKELETON);
        setWfError(null);
        setEditorOpen(true);
    };

    const openEdit = async (g: GraphItem) => {
        setEditingGraph(g);
        setWfName(g.name || '');
        setWfDesc(g.description || '');
        setWfJson('');
        setWfError(null);
        setEditorOpen(true);
        try {
            const res = await rpc<GraphGetResult>('graph.get', { graph_id: g.graph_id });
            const nodes = Object.entries(res.nodes ?? {}).map(([id, n]) => ({ id, ...n }));
            setWfJson(JSON.stringify({ nodes, edges: res.edges ?? [] }, null, 4));
        } catch (e) {
            console.warn('[web] graph.get prefill failed, using row data fallback', e);
            const fallback = {
                nodes: Object.entries(g.graph_data.nodes ?? {}).map(([id, n]) => ({ id, ...n })),
                edges: (g.graph_data.edges ?? []) as Array<Record<string, unknown>>,
            };
            setWfJson(JSON.stringify(fallback, null, 4));
        }
    };

    const submitEditor = async () => {
        if (!wfName.trim()) {
            setWfError(t('请输入名称', 'name required'));
            return;
        }
        const parsed = parseJson(wfJson);
        if (!parsed.ok) {
            setWfError(parsed.error);
            return;
        }
        const body = parsed.data as { nodes?: unknown; edges?: unknown };
        if (
            !body ||
            typeof body !== 'object' ||
            !Array.isArray(body.nodes) ||
            !Array.isArray(body.edges)
        ) {
            setWfError(
                t('JSON 须包含 nodes 和 edges 数组', 'JSON must contain nodes and edges arrays')
            );
            return;
        }
        setSaving(true);
        try {
            if (editingGraph) {
                await rpc('graph.update', {
                    graph_id: editingGraph.graph_id,
                    name: wfName.trim(),
                    description: wfDesc.trim(),
                    nodes: body.nodes,
                    edges: body.edges,
                });
                sdk.toast.show({
                    level: 'success',
                    message: t('工作流已更新', 'workflow updated'),
                });
            } else {
                await rpc('graph.create', {
                    name: wfName.trim(),
                    description: wfDesc.trim(),
                    nodes: body.nodes,
                    edges: body.edges,
                });
                sdk.toast.show({
                    level: 'success',
                    message: t('工作流已创建', 'workflow created'),
                });
            }
            setEditorOpen(false);
            manualRetry();
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
                                            onClick={() => void openEdit(g)}
                                        >
                                            {t('编辑', 'Edit')}
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
                title={
                    editingGraph
                        ? t('编辑工作流', 'Edit workflow')
                        : t('新建工作流', 'New workflow')
                }
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
                            label={editingGraph ? t('保存', 'Save') : t('创建', 'Create')}
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
                <FormField label={t('图定义 (JSON)', 'Graph definition (JSON)')} error={wfError}>
                    <JsonEditor value={wfJson} onChange={setWfJson} rows={10} />
                </FormField>
            </Modal>
        </div>
    );
}
