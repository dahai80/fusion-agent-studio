// R4 (#351): governance management page — budget caps, per-agent permission
// rules, funnel/SLO events. Budget.set is mutating (audited via rpc bridge).
import {
    DataTable,
    EmptyState,
    ErrorState,
    FormField,
    SkeletonLines,
    SubmitButton,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { useCallback, useEffect, useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Tabs } from '../../components/ui/Tabs';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

interface BudgetStatus {
    max_tokens: number;
    spent_tokens: number;
    exceeded: boolean;
}

interface AgentRow {
    agent_id: string;
    name?: string;
    [k: string]: unknown;
}

interface PermRow {
    agent_id?: string;
    [k: string]: unknown;
}

interface SloSummary {
    sessions_completed: number;
    compactions_total: number;
    approvals_total: number;
    avg_approval_latency_ms: number;
    p99_approval_latency_ms: number;
    avg_session_duration_ms: number;
    p99_session_duration_ms: number;
}

interface Metrics {
    counters: Record<string, number>;
    latencies: Record<string, number>;
}

function BudgetTab() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<BudgetStatus>('budget.status', {}),
        []
    );
    const [maxTokens, setMaxTokens] = useState('');

    const setBudget = async () => {
        const n = Number(maxTokens);
        if (!Number.isFinite(n) || n < 0) {
            sdk.toast.show({ level: 'error', message: t('无效预算', 'Invalid budget') });
            return;
        }
        try {
            await rpc('budget.set', { max_tokens: Math.floor(n) });
            sdk.toast.show({ level: 'success', message: t('预算已设置', 'Budget set') });
            setMaxTokens('');
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    if (loading) return <SkeletonLines />;
    if (error)
        return <ErrorState message={String(error)} retrying={attempt} onRetry={manualRetry} />;
    if (!data) return <EmptyState title={t('无预算数据', 'No budget data')} />;

    const pct =
        data.max_tokens > 0
            ? Math.min(100, Math.round((data.spent_tokens / data.max_tokens) * 100))
            : 0;

    return (
        <div className="space-y-4">
            <div className="rounded border border-border bg-bg-elevated p-4 space-y-2">
                <div className="flex items-baseline justify-between">
                    <span className="text-sm text-text-secondary">
                        {t('Token 预算使用', 'Token budget usage')}
                    </span>
                    <span className="text-sm font-mono">
                        {data.spent_tokens} / {data.max_tokens || '∞'}
                    </span>
                </div>
                <div className="h-2 rounded bg-bg overflow-hidden">
                    <div
                        className={`h-full ${data.exceeded ? 'bg-error' : pct > 80 ? 'bg-warning' : 'bg-brand'}`}
                        style={{ width: `${pct}%` }}
                    />
                </div>
                {data.exceeded && (
                    <div className="text-xs text-error">{t('预算已超限', 'Budget exceeded')}</div>
                )}
            </div>
            <div className="flex items-end gap-2">
                <div className="w-64">
                    <FormField label={t('最大 Token 数', 'Max tokens')}>
                        <TextInput value={maxTokens} onChange={setMaxTokens} placeholder="0" />
                    </FormField>
                </div>
                <SubmitButton label={t('设置预算', 'Set budget')} onAction={() => setBudget()} />
            </div>
        </div>
    );
}

function PermissionsTab() {
    const t = useT();
    const agents = useAutoRetry(() => rpc<{ agents: AgentRow[] }>('agent.list', {}), []);
    const [agentId, setAgentId] = useState('');
    const perms = useAutoRetry(async () => {
        if (!agentId) return { permissions: [], denied_tools: [], tools: [] };
        return rpc<{ permissions: PermRow[]; denied_tools: string[]; tools: string[] }>(
            'permission.list',
            { agent_id: agentId }
        );
    }, [agentId]);

    useEffect(() => {
        if (!agentId && agents.data?.agents?.length) {
            setAgentId(agents.data.agents[0].agent_id);
        }
    }, [agents.data, agentId]);

    const toggle = useCallback(
        async (tool: string, denied: boolean) => {
            try {
                await rpc('permission.update', {
                    agent_id: agentId,
                    tool,
                    level: denied ? 'allow' : 'deny',
                });
                sdk.toast.show({ level: 'success', message: t('已更新', 'Updated') });
                perms.manualRetry();
            } catch (e) {
                sdk.toast.show({ level: 'error', message: (e as Error).message });
            }
        },
        [agentId, perms, t]
    );

    if (agents.loading) return <SkeletonLines />;
    if (agents.error)
        return (
            <ErrorState
                message={String(agents.error)}
                retrying={agents.attempt}
                onRetry={agents.manualRetry}
            />
        );

    const tools: string[] = perms.data?.tools || [];
    const denied: string[] = perms.data?.denied_tools || [];

    return (
        <div className="space-y-3">
            <div className="w-72">
                <FormField label={t('选择 Agent', 'Select agent')}>
                    <select
                        className="w-full rounded border border-border bg-bg px-2 py-1.5 text-sm"
                        value={agentId}
                        onChange={(e) => setAgentId(e.target.value)}
                    >
                        {(agents.data?.agents || []).map((a) => (
                            <option key={a.agent_id} value={a.agent_id}>
                                {a.name || a.agent_id}
                            </option>
                        ))}
                    </select>
                </FormField>
            </div>
            {tools.length === 0 ? (
                <EmptyState title={t('该 Agent 无工具清单', 'No tool manifest for this agent')} />
            ) : (
                <DataTable<string>
                    rows={tools}
                    rowKey={(tool) => tool}
                    pageSize={15}
                    columns={[
                        {
                            key: 'tool',
                            header: t('工具', 'Tool'),
                            render: (tool) => <span className="font-mono text-xs">{tool}</span>,
                        },
                        {
                            key: 'state',
                            header: t('状态', 'State'),
                            render: (tool) =>
                                denied.includes(tool) ? (
                                    <span className="rounded bg-error/10 text-error px-1.5 text-xs">
                                        {t('拒绝', 'deny')}
                                    </span>
                                ) : (
                                    <span className="rounded bg-brand/10 text-brand px-1.5 text-xs">
                                        {t('允许', 'allow')}
                                    </span>
                                ),
                        },
                        {
                            key: 'action',
                            header: t('操作', 'Action'),
                            render: (tool) => (
                                <button
                                    type="button"
                                    className="rounded border border-border px-2 py-0.5 text-xs hover:border-brand"
                                    onClick={() => void toggle(tool, denied.includes(tool))}
                                >
                                    {denied.includes(tool) ? t('允许', 'Allow') : t('拒绝', 'Deny')}
                                </button>
                            ),
                        },
                    ]}
                />
            )}
        </div>
    );
}

function EventsTab() {
    const t = useT();
    const slo = useAutoRetry(() => rpc<SloSummary>('telemetry.session_slo', {}), []);
    const metrics = useAutoRetry(() => rpc<Metrics>('telemetry.metrics', {}), []);

    if (slo.loading || metrics.loading) return <SkeletonLines />;
    if (slo.error)
        return (
            <ErrorState
                message={String(slo.error)}
                retrying={slo.attempt}
                onRetry={slo.manualRetry}
            />
        );

    const s = slo.data;
    const counters = metrics.data?.counters || {};

    return (
        <div className="space-y-4">
            <div className="grid grid-cols-2 gap-3">
                {[
                    {
                        label: t('完成会话', 'Sessions completed'),
                        value: s?.sessions_completed ?? 0,
                    },
                    { label: t('压缩次数', 'Compactions'), value: s?.compactions_total ?? 0 },
                    { label: t('审批次数', 'Approvals'), value: s?.approvals_total ?? 0 },
                    {
                        label: t('P99 审批延迟', 'P99 approval latency'),
                        value: `${Math.round(s?.p99_approval_latency_ms ?? 0)} ms`,
                    },
                    {
                        label: t('平均会话时长', 'Avg session duration'),
                        value: `${Math.round(s?.avg_session_duration_ms ?? 0)} ms`,
                    },
                    {
                        label: t('P99 会话时长', 'P99 session duration'),
                        value: `${Math.round(s?.p99_session_duration_ms ?? 0)} ms`,
                    },
                ].map((card) => (
                    <div
                        key={card.label}
                        className="rounded border border-border bg-bg-elevated p-3"
                    >
                        <div className="text-xs text-text-secondary">{card.label}</div>
                        <div className="text-lg font-mono mt-1">{card.value}</div>
                    </div>
                ))}
            </div>
            <div className="rounded border border-border bg-bg-elevated p-3">
                <div className="text-sm font-medium mb-2">{t('引擎计数器', 'Engine counters')}</div>
                <div className="flex flex-wrap gap-2">
                    {Object.entries(counters).map(([k, v]) => (
                        <span
                            key={k}
                            className="rounded bg-bg px-2 py-1 text-xs font-mono border border-border"
                        >
                            {k}: {v}
                        </span>
                    ))}
                </div>
            </div>
        </div>
    );
}

export function GovernancePage() {
    const t = useT();
    const [tab, setTab] = useState('budget');
    return (
        <div className="pb-8">
            <PageHeader
                title={t('治理', 'Governance')}
                subtitle={t('预算 / 权限 / 事件', 'Budget / permissions / events')}
            />
            <Tabs
                tabs={[
                    { key: 'budget', label: t('预算', 'Budget') },
                    { key: 'permissions', label: t('权限', 'Permissions') },
                    { key: 'events', label: t('事件', 'Events') },
                ]}
                active={tab}
                onChange={setTab}
            />
            <div className="p-4">
                {tab === 'budget' && <BudgetTab />}
                {tab === 'permissions' && <PermissionsTab />}
                {tab === 'events' && <EventsTab />}
            </div>
        </div>
    );
}
