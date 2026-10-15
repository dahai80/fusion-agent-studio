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
import { type GraphItem, api } from '../api';
import { PageHeader } from '../components/PageHeader';
import { StreamPanel } from '../components/StreamPanel';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// Workflows (agent graphs) list + per-graph quick actions: submit a tracked
// run (task store) or a live streaming test (SSE). Four states per spec S1.
export function WorkflowsPage() {
    const t = useT();
    const [selected, setSelected] = useState<GraphItem | null>(null);
    const [goal, setGoal] = useState('');
    const [goalError, setGoalError] = useState<string | null>(null);
    const [streaming, setStreaming] = useState(false);
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => api.graphs(1, 200),
        []
    );

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
        </div>
    );
}
