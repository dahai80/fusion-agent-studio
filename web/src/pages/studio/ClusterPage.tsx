import { EmptyState, ErrorState, SkeletonLines, StatusBadge, useAutoRetry } from '@fusion/web-ui';
import { useState } from 'react';
import { apiGet, apiPost, apiPut } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// #583: cluster ops panel — stats, node metrics, task actions
// (cancel/degrade/migrate), autoscaler config and routing strategy, all
// through the daemon's same-origin /v1/cluster-ops/* proxy to the
// cluster_server (:11457). Cluster unreachable surfaces the 503 detail.
interface ClusterStats {
    cluster?: {
        online_nodes?: number;
        total_nodes?: number;
        active_tasks?: number;
        total_memory_gb?: number;
        available_memory_gb?: number;
        utilization?: number;
    };
    tasks?: { total?: number; completed?: number; failed?: number };
}

interface NodeRow {
    node_id?: string;
    status?: string;
    active_tasks?: number;
    max_tasks?: number;
    score?: number;
    capabilities?: string[];
}

interface TaskRow {
    task_id?: string;
    name?: string;
    status?: string;
    model_name?: string;
    assigned_nodes?: string[];
    priority?: number;
}

interface AutoscalerConfig {
    enabled?: boolean;
    min_nodes?: number;
    max_nodes?: number;
    target_cpu?: number;
    scale_up_threshold?: number;
    scale_down_threshold?: number;
    [k: string]: unknown;
}

export function ClusterPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => apiGet<ClusterStats>('/v1/cluster-ops/stats'),
        []
    );
    const [tab, setTab] = useState<'tasks' | 'nodes' | 'autoscaler' | 'routing'>('tasks');
    const tabs: Array<{ key: typeof tab; label: string; labelEn: string }> = [
        { key: 'tasks', label: '任务', labelEn: 'Tasks' },
        { key: 'nodes', label: '节点', labelEn: 'Nodes' },
        { key: 'autoscaler', label: '弹性', labelEn: 'Autoscaler' },
        { key: 'routing', label: '路由', labelEn: 'Routing' },
    ];
    return (
        <div>
            <PageHeader
                title={t('集群', 'Cluster')}
                subtitle={t(
                    `${data?.cluster?.online_nodes ?? '?'}/${data?.cluster?.total_nodes ?? '?'} 节点 · ${data?.cluster?.active_tasks ?? '?'} 运行中任务`,
                    `${data?.cluster?.online_nodes ?? '?'}/${data?.cluster?.total_nodes ?? '?'} nodes · ${data?.cluster?.active_tasks ?? '?'} running tasks`
                )}
            />
            <div className="mx-4 mt-2 flex gap-1">
                {tabs.map((x) => (
                    <button
                        key={x.key}
                        type="button"
                        onClick={() => setTab(x.key)}
                        className={`rounded-md px-2.5 py-1 text-xs ${
                            tab === x.key
                                ? 'bg-bg-muted text-brand'
                                : 'text-text-secondary hover:text-text-primary'
                        }`}
                    >
                        {t(x.label, x.labelEn)}
                    </button>
                ))}
            </div>
            <div className="mx-4 mt-2">
                {error ? (
                    <ErrorState
                        message={(error as Error).message}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                ) : tab === 'tasks' ? (
                    <TasksSection />
                ) : tab === 'nodes' ? (
                    <NodesSection />
                ) : tab === 'autoscaler' ? (
                    <AutoscalerSection />
                ) : (
                    <RoutingSection />
                )}
            </div>
            {loading && attempt === 0 && !error ? (
                <div className="mx-4 mt-2">
                    <SkeletonLines rows={4} />
                </div>
            ) : null}
        </div>
    );
}

function TasksSection() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => apiGet<{ total?: number; tasks?: TaskRow[] }>('/v1/cluster-ops/tasks'),
        []
    );
    const [migrateTarget, setMigrateTarget] = useState<Record<string, string>>({});
    const [degradeModel, setDegradeModel] = useState<Record<string, string>>({});

    const act = async (path: string, body: unknown, ok: string) => {
        try {
            await apiPost(path, body);
            sdk.toast.show({ level: 'success', message: ok });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const rows = data?.tasks ?? [];
    return (
        <div data-testid="cluster-tasks">
            {error ? (
                <ErrorState
                    message={(error as Error).message}
                    onRetry={manualRetry}
                    retrying={attempt}
                />
            ) : loading && attempt === 0 ? (
                <SkeletonLines rows={3} />
            ) : rows.length === 0 ? (
                <EmptyState title={t('暂无集群任务', 'no cluster tasks')} />
            ) : (
                <div className="space-y-2">
                    {rows.map((task) => {
                        const id = task.task_id ?? '';
                        return (
                            <div
                                key={id}
                                className="rounded-md border border-border bg-bg-card px-3 py-2 text-xs"
                            >
                                <div className="flex items-center gap-2">
                                    <span className="font-mono text-text-primary">{id}</span>
                                    <span className="text-text-primary">{task.name || '-'}</span>
                                    <StatusBadge
                                        status={task.status === 'running' ? 'success' : 'error'}
                                        label={task.status ?? '-'}
                                    />
                                    <span className="text-text-secondary">
                                        {t('模型', 'model')}: {task.model_name || '-'}
                                    </span>
                                    <span className="text-text-secondary">
                                        {t('节点', 'nodes')}:{' '}
                                        {(task.assigned_nodes ?? []).join(', ') || '-'}
                                    </span>
                                </div>
                                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                                    <button
                                        type="button"
                                        onClick={() =>
                                            act(
                                                `/v1/cluster-ops/tasks/${id}/cancel`,
                                                {},
                                                t('已取消', 'cancelled')
                                            )
                                        }
                                        className="rounded-md border border-border px-2 py-0.5 text-text-secondary hover:bg-bg-muted"
                                    >
                                        {t('取消', 'Cancel')}
                                    </button>
                                    <input
                                        type="text"
                                        value={degradeModel[id] ?? ''}
                                        placeholder={t('目标模型', 'target model')}
                                        onChange={(e) =>
                                            setDegradeModel((m) => ({ ...m, [id]: e.target.value }))
                                        }
                                        className="w-32 rounded-md border border-border bg-bg-elevated px-2 py-0.5 text-xs"
                                    />
                                    <button
                                        type="button"
                                        onClick={() =>
                                            act(
                                                `/v1/cluster-ops/tasks/${id}/degrade`,
                                                { target_model: degradeModel[id] || undefined },
                                                t('已降级', 'degraded')
                                            )
                                        }
                                        className="rounded-md border border-border px-2 py-0.5 text-text-secondary hover:bg-bg-muted"
                                    >
                                        {t('降级', 'Degrade')}
                                    </button>
                                    <input
                                        type="text"
                                        value={migrateTarget[id] ?? ''}
                                        placeholder={t('目标节点', 'target node')}
                                        onChange={(e) =>
                                            setMigrateTarget((m) => ({
                                                ...m,
                                                [id]: e.target.value,
                                            }))
                                        }
                                        className="w-32 rounded-md border border-border bg-bg-elevated px-2 py-0.5 text-xs"
                                    />
                                    <button
                                        type="button"
                                        onClick={() =>
                                            act(
                                                `/v1/cluster-ops/tasks/${id}/migrate`,
                                                { target_node_id: migrateTarget[id] || '' },
                                                t('已迁移', 'migrated')
                                            )
                                        }
                                        className="rounded-md border border-border px-2 py-0.5 text-text-secondary hover:bg-bg-muted"
                                    >
                                        {t('迁移', 'Migrate')}
                                    </button>
                                </div>
                            </div>
                        );
                    })}
                </div>
            )}
        </div>
    );
}

function NodesSection() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => apiGet<{ nodes?: NodeRow[] }>('/v1/cluster-ops/nodes'),
        []
    );
    const rows = data?.nodes ?? [];
    return (
        <div data-testid="cluster-nodes" className="space-y-2">
            {error ? (
                <ErrorState
                    message={(error as Error).message}
                    onRetry={manualRetry}
                    retrying={attempt}
                />
            ) : loading && attempt === 0 ? (
                <SkeletonLines rows={3} />
            ) : rows.length === 0 ? (
                <EmptyState title={t('暂无节点', 'no nodes')} />
            ) : (
                rows.map((n) => (
                    <div
                        key={n.node_id}
                        className="flex items-center gap-2 rounded-md border border-border bg-bg-card px-3 py-2 text-xs"
                    >
                        <span className="font-mono text-text-primary">{n.node_id}</span>
                        <StatusBadge
                            status={
                                n.status === 'online'
                                    ? 'success'
                                    : n.status === 'busy'
                                      ? 'warning'
                                      : 'error'
                            }
                            label={n.status ?? '-'}
                        />
                        <span className="text-text-secondary">
                            {t('任务', 'tasks')}: {n.active_tasks ?? 0}/{n.max_tasks ?? '-'}
                        </span>
                        <span className="text-text-secondary">
                            {t('评分', 'score')}: {n.score ?? '-'}
                        </span>
                    </div>
                ))
            )}
        </div>
    );
}

function AutoscalerSection() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => apiGet<AutoscalerConfig>('/v1/cluster-ops/autoscaler/config'),
        []
    );
    const [cfg, setCfg] = useState('');
    const apply = async () => {
        try {
            const parsed = JSON.parse(cfg) as Record<string, unknown>;
            await apiPut('/v1/cluster-ops/autoscaler/config', parsed);
            sdk.toast.show({ level: 'success', message: t('已保存', 'saved') });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };
    return (
        <div data-testid="cluster-autoscaler">
            {error ? (
                <ErrorState
                    message={(error as Error).message}
                    onRetry={manualRetry}
                    retrying={attempt}
                />
            ) : loading && attempt === 0 ? (
                <SkeletonLines rows={3} />
            ) : (
                <div className="rounded-md border border-border bg-bg-card px-3 py-2 text-xs">
                    <pre className="overflow-x-auto rounded bg-bg-muted p-2 font-mono text-text-secondary">
                        {JSON.stringify(data ?? {}, null, 2)}
                    </pre>
                    <textarea
                        value={cfg}
                        placeholder={t('以 JSON 覆盖配置字段', 'override config fields as JSON')}
                        onChange={(e) => setCfg(e.target.value)}
                        rows={4}
                        className="mt-2 w-full rounded-md border border-border bg-bg-elevated p-2 font-mono text-xs text-text-primary"
                    />
                    <button
                        type="button"
                        onClick={apply}
                        className="mt-2 rounded-md border border-border px-3 py-1 text-text-primary hover:bg-bg-muted"
                    >
                        {t('保存配置', 'Save config')}
                    </button>
                </div>
            )}
        </div>
    );
}

function RoutingSection() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => apiGet<Record<string, unknown>>('/v1/cluster-ops/routing/summary'),
        []
    );
    const [strategy, setStrategy] = useState('');
    const apply = async () => {
        try {
            await apiPost('/v1/cluster-ops/routing/strategy', { strategy: strategy.trim() });
            sdk.toast.show({ level: 'success', message: t('已切换', 'switched') });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };
    return (
        <div data-testid="cluster-routing">
            {error ? (
                <ErrorState
                    message={(error as Error).message}
                    onRetry={manualRetry}
                    retrying={attempt}
                />
            ) : loading && attempt === 0 ? (
                <SkeletonLines rows={2} />
            ) : (
                <div className="rounded-md border border-border bg-bg-card px-3 py-2 text-xs">
                    <pre className="overflow-x-auto rounded bg-bg-muted p-2 font-mono text-text-secondary">
                        {JSON.stringify(data ?? {}, null, 2)}
                    </pre>
                    <div className="mt-2 flex items-center gap-2">
                        <input
                            type="text"
                            value={strategy}
                            placeholder={t('路由策略', 'routing strategy')}
                            onChange={(e) => setStrategy(e.target.value)}
                            className="w-48 rounded-md border border-border bg-bg-elevated px-2 py-1 text-xs"
                        />
                        <button
                            type="button"
                            onClick={apply}
                            className="rounded-md border border-border px-3 py-1 text-text-primary hover:bg-bg-muted"
                        >
                            {t('切换策略', 'Switch strategy')}
                        </button>
                    </div>
                </div>
            )}
        </div>
    );
}
