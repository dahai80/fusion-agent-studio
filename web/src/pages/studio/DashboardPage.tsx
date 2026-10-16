import { DataTable, ErrorState, MetricTile, SkeletonLines, useAutoRetry } from '@fusion/web-ui';
import { formatRelativeTime } from '@fusion/web-ui/format';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { useT } from '../../i18n';

// Dashboard: overview stats + mlx status + agent usage + audit log — 4 parallel RPCs.
interface Overview {
    [key: string]: unknown;
}

interface MlxStatus {
    model?: string;
    model_name?: string;
    loaded?: boolean;
    memory?: number;
    memory_used?: number;
    [key: string]: unknown;
}

interface AgentUsage {
    agent_id?: string;
    agent_name?: string;
    runs?: number;
    tokens?: number;
    duration?: number;
}

interface AuditEntry {
    action?: string;
    actor_id?: string;
    created_at?: number;
}

interface DashboardData {
    overview?: { overview?: Overview };
    mlx?: { status?: MlxStatus };
    usage?: { usage?: AgentUsage[] };
    audit?: { entries?: AuditEntry[]; audit?: AuditEntry[] };
}

const prettify = (key: string) => key.replaceAll('_', ' ').replace(/^\w/, (c) => c.toUpperCase());

const isNumeric = (v: unknown) => typeof v === 'number' && Number.isFinite(v);

export function DashboardPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(async () => {
        const [overview, mlx, usage, audit] = await Promise.all([
            rpc<DashboardData['overview']>('dashboard.overview', {}),
            rpc<DashboardData['mlx']>('mlx.status', {}),
            rpc<DashboardData['usage']>('analytics.agent_usage', { limit: 10 }),
            rpc<DashboardData['audit']>('audit.list', { limit: 15 }),
        ]);
        return { overview, mlx, usage, audit };
    }, []);

    const ov = data?.overview?.overview ?? data?.overview ?? {};
    const st = data?.mlx?.status ?? {};
    const usageRows =
        data?.usage?.usage ?? (data?.usage as unknown as { agents?: AgentUsage[] })?.agents ?? [];
    const auditRows =
        data?.audit?.entries ??
        data?.audit?.audit ??
        (data?.audit as unknown as { data?: AuditEntry[] })?.data ??
        [];
    const statEntries = Object.entries(ov).filter(([, v]) => isNumeric(v));

    const model = st.model_name ?? st.model;
    const memory = st.memory_used ?? st.memory;

    return (
        <div>
            <PageHeader title={t('仪表盘', 'Dashboard')} />
            <div className="space-y-6 px-4 pt-3 pb-6">
                {loading && attempt === 0 ? (
                    <SkeletonLines rows={8} />
                ) : error ? (
                    <ErrorState
                        message={(error as Error).message}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                ) : (
                    <>
                        <section>
                            <h3 className="mb-2 text-sm font-medium text-text-primary">
                                {t('概览', 'Overview')}
                            </h3>
                            {statEntries.length > 0 ? (
                                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                                    {statEntries.map(([k, v]) => (
                                        <MetricTile key={k} label={prettify(k)} value={String(v)} />
                                    ))}
                                </div>
                            ) : null}
                        </section>
                        <section>
                            <h3 className="mb-2 text-sm font-medium text-text-primary">
                                {t('模型', 'Model')}
                            </h3>
                            <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                                <MetricTile label={t('模型', 'Model')} value={model ?? '-'} />
                                <MetricTile
                                    label={t('已加载', 'Loaded')}
                                    value={
                                        st.loaded === undefined
                                            ? '-'
                                            : st.loaded
                                              ? t('是', 'Yes')
                                              : t('否', 'No')
                                    }
                                />
                                <MetricTile
                                    label={t('内存', 'Memory')}
                                    value={isNumeric(memory) ? String(memory) : '-'}
                                />
                                {st.loaded === undefined &&
                                model === undefined &&
                                memory === undefined ? (
                                    <p className="text-xs text-text-muted">
                                        {t('暂无模型状态', 'no model status')}
                                    </p>
                                ) : null}
                            </div>
                        </section>
                        {usageRows.length > 0 ? (
                            <section>
                                <h3 className="mb-2 text-sm font-medium text-text-primary">
                                    {t('Agent 用量', 'Agent usage')}
                                </h3>
                                <DataTable<AgentUsage>
                                    rows={usageRows}
                                    rowKey={(u) => u.agent_id ?? u.agent_name ?? '-'}
                                    pageSize={10}
                                    columns={[
                                        {
                                            key: 'agent',
                                            header: t('Agent', 'Agent'),
                                            render: (u) => u.agent_name ?? u.agent_id ?? '-',
                                        },
                                        {
                                            key: 'runs',
                                            header: t('运行', 'Runs'),
                                            render: (u) => String(u.runs ?? '-'),
                                        },
                                        {
                                            key: 'tokens',
                                            header: t('Tokens', 'Tokens'),
                                            render: (u) => String(u.tokens ?? '-'),
                                        },
                                    ]}
                                />
                            </section>
                        ) : null}
                        {auditRows.length > 0 ? (
                            <section>
                                <h3 className="mb-2 text-sm font-medium text-text-primary">
                                    {t('审计日志', 'Audit log')}
                                </h3>
                                <DataTable<AuditEntry>
                                    rows={auditRows}
                                    rowKey={(a) => `${a.action ?? '-'}-${a.created_at ?? 0}`}
                                    pageSize={15}
                                    columns={[
                                        {
                                            key: 'action',
                                            header: t('操作', 'Action'),
                                            render: (a) => a.action ?? '-',
                                        },
                                        {
                                            key: 'actor',
                                            header: t('操作者', 'Actor'),
                                            render: (a) => a.actor_id ?? '-',
                                        },
                                        {
                                            key: 'time',
                                            header: t('时间', 'Time'),
                                            render: (a) =>
                                                a.created_at
                                                    ? formatRelativeTime(a.created_at * 1000)
                                                    : '-',
                                        },
                                    ]}
                                />
                            </section>
                        ) : null}
                    </>
                )}
            </div>
        </div>
    );
}
