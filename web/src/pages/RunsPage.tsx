import { createPoller } from '@fusion/web-sdk';
import {
    DataTable,
    EmptyState,
    ErrorState,
    MetricTile,
    SelectInput,
    SkeletonLines,
    StatusBadge,
    useAutoRetry,
} from '@fusion/web-ui';
import { formatRelativeTime, formatTokenCount } from '@fusion/web-ui/format';
import { useCallback, useEffect, useRef, useState } from 'react';
import { type DashboardData, type RunItem, api, statusKind } from '../api';
import { PageHeader } from '../components/PageHeader';
import { useT } from '../i18n';

// Run monitor (spec §6.3): visibility-aware 5s poller via the SDK; hidden
// tabs throttle to 30s. Status filter + four states + dashboard tiles.
const STATUS_FILTERS = ['', 'running', 'completed', 'failed', 'canceled'] as const;

export function RunsPage() {
    const t = useT();
    const [status, setStatus] = useState<string>('');
    const [runs, setRuns] = useState<RunItem[]>([]);
    const [total, setTotal] = useState(0);
    const [pollError, setPollError] = useState('');
    const [loaded, setLoaded] = useState(false);
    const [refreshTick, setRefreshTick] = useState(0);
    const statusRef = useRef(status);
    statusRef.current = status;

    const {
        data: dashboard,
        error: dashError,
        attempt: dashAttempt,
        manualRetry: dashRetry,
    } = useAutoRetry<DashboardData>(() => api.dashboard(), [refreshTick]);

    const refresh = useCallback(async () => {
        const res = await api.runs(statusRef.current, 100);
        setRuns(res.runs ?? []);
        setTotal(res.total ?? 0);
        setPollError('');
        setLoaded(true);
    }, []);

    // status/refreshTick are intentional triggers — the poller re-primes
    // (start(true)) when the filter or manual refresh changes; status itself
    // is read via statusRef.
    // biome-ignore lint/correctness/useExhaustiveDependencies: intentional extra deps
    useEffect(() => {
        const poller = createPoller({
            intervalMs: 5000,
            onTick: refresh,
            onError: (err) => setPollError(err.message),
        });
        poller.start(true);
        return () => poller.stop();
    }, [refresh, status, refreshTick]);

    const statusLabel = (s: string): string => {
        const map: Record<string, [string, string]> = {
            pending: ['等待中', 'pending'],
            submitted: ['已提交', 'submitted'],
            running: ['运行中', 'running'],
            completed: ['已完成', 'completed'],
            failed: ['失败', 'failed'],
            canceled: ['已取消', 'canceled'],
        };
        const pair = map[s];
        return pair ? t(pair[0], pair[1]) : s;
    };

    return (
        <div>
            <PageHeader
                title={t('运行', 'Runs')}
                subtitle={t(`共 ${total} 条`, `${total} total`)}
                actions={
                    <div className="flex items-center gap-2">
                        <button
                            type="button"
                            onClick={() => setRefreshTick((v) => v + 1)}
                            className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-muted"
                        >
                            {t('刷新', 'Refresh')}
                        </button>
                        <div className="w-32">
                            <SelectInput
                                value={status}
                                onChange={setStatus}
                                options={STATUS_FILTERS.map((s) => ({
                                    value: s,
                                    label:
                                        s === '' ? t('全部状态', 'all statuses') : statusLabel(s),
                                }))}
                            />
                        </div>
                    </div>
                }
            />
            {pollError ? (
                <div className="mx-4 mt-2 rounded-md bg-warning/10 px-3 py-2 text-xs text-warning">
                    {t('轮询失败', 'poll failed')}: {pollError}
                </div>
            ) : null}
            {dashError ? (
                <div className="px-4 pt-3">
                    <ErrorState
                        message={t('加载面板失败', 'failed to load dashboard')}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={dashRetry}
                        retrying={dashAttempt}
                    />
                </div>
            ) : (
                <div className="grid grid-cols-2 gap-2 px-4 pt-3 md:grid-cols-4">
                    <MetricTile
                        label={t('今日请求', 'today requests')}
                        value={dashboard?.today_requests ?? 0}
                    />
                    <MetricTile
                        label={t('活跃 agent', 'active agents')}
                        value={dashboard?.active_agents ?? 0}
                    />
                    <MetricTile
                        label={t('今日错误', 'today errors')}
                        value={dashboard?.error_count ?? 0}
                    />
                    <MetricTile
                        label={t('累计 token', 'total tokens')}
                        value={formatTokenCount(dashboard?.total_token_consumption ?? 0)}
                    />
                </div>
            )}
            <div className="px-4 pt-3">
                {!loaded ? (
                    <SkeletonLines rows={4} />
                ) : runs.length === 0 && pollError === '' ? (
                    <EmptyState
                        title={t('暂无运行', 'no runs yet')}
                        description={t(
                            '在“工作流”页提交一次运行',
                            'submit a run from the Workflows page'
                        )}
                    />
                ) : (
                    <DataTable<RunItem>
                        rows={runs}
                        rowKey={(r) => r.task_id}
                        pageSize={20}
                        columns={[
                            {
                                key: 'title',
                                header: t('标题', 'Title'),
                                render: (r) => (
                                    <a
                                        href={`#/runs/${encodeURIComponent(r.task_id)}`}
                                        className="text-brand hover:underline"
                                    >
                                        {r.title || r.task_id}
                                    </a>
                                ),
                            },
                            {
                                key: 'status',
                                header: t('状态', 'Status'),
                                sortable: true,
                                render: (r) => (
                                    <StatusBadge
                                        status={statusKind(r.status)}
                                        label={statusLabel(r.status)}
                                    />
                                ),
                            },
                            {
                                key: 'graph_id',
                                header: t('工作流', 'Graph'),
                                render: (r) => r.graph_id || '-',
                            },
                            {
                                key: 'created_at',
                                header: t('创建时间', 'Created'),
                                sortable: true,
                                render: (r) =>
                                    r.created_at ? formatRelativeTime(r.created_at * 1000) : '-',
                            },
                        ]}
                        renderCard={(r) => (
                            <div className="flex flex-col gap-1">
                                <a
                                    href={`#/runs/${encodeURIComponent(r.task_id)}`}
                                    className="text-sm text-brand"
                                >
                                    {r.title || r.task_id}
                                </a>
                                <div className="flex items-center justify-between">
                                    <StatusBadge
                                        status={statusKind(r.status)}
                                        label={statusLabel(r.status)}
                                    />
                                    <span className="text-xs text-text-muted">
                                        {r.created_at
                                            ? formatRelativeTime(r.created_at * 1000)
                                            : '-'}
                                    </span>
                                </div>
                            </div>
                        )}
                    />
                )}
            </div>
        </div>
    );
}
