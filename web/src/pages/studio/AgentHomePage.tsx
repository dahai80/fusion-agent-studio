import { ErrorState, SkeletonLines, StatusBadge, useAutoRetry } from '@fusion/web-ui';
import { formatRelativeTime } from '@fusion/web-ui/format';
import { useMemo } from 'react';
import { api, apiBase, rpc, statusKind } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { useT } from '../../i18n';
import { type AgentModel, modelLabel } from '../../lib/agentModel';

// #528 (DC-2): AI Agent Dashboard — the module entry. Top-right
// [＋构建智能体] (conversational builder) + [🔑获取API密钥]; below:
// ready-to-use agents ([使用] opens a working conversation, [编辑] the
// form editor on the same config), a drafts strip, and recent runs.
// Reorganization only — data comes from the existing agent.list /
// analytics.agent_usage / /v1/runs surfaces, nothing new server-side.
interface Agent {
    id?: string;
    agent_id?: string;
    name: string;
    description?: string;
    model?: AgentModel;
    status?: string;
    updated_at?: number;
    tools?: string[];
    tags?: string[];
}

interface UsageAgg {
    agent_id?: string;
    runs?: number;
    tokens?: number;
    last_run?: number;
}

function editorHref(agentId: string): string {
    return `${apiBase()}/console/agents.html?agent=${encodeURIComponent(agentId)}`;
}

export function AgentHomePage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(async () => {
        const [agents, usageAgg, runs] = await Promise.all([
            rpc<{ agents: Agent[] }>('agent.list', {}),
            rpc<{ agents?: UsageAgg[] }>('analytics.agent_usage', {}).catch(
                () => ({}) as { agents?: UsageAgg[] }
            ),
            api.runs('', 10).catch(() => ({ runs: [], total: 0 })),
        ]);
        return { agents: agents.agents ?? [], usageAgg, runs: runs.runs ?? [] };
    }, []);

    const usage = useMemo(() => {
        const map: Record<string, UsageAgg> = {};
        for (const agg of data?.usageAgg?.agents ?? []) {
            if (agg.agent_id) map[agg.agent_id] = agg;
        }
        return map;
    }, [data?.usageAgg]);

    const all = data?.agents ?? [];
    const ready = all.filter((a) => a.status === 'published');
    const drafts = all.filter((a) => (a.status ?? 'draft') !== 'published');

    return (
        <div>
            <PageHeader
                title={t('AI Agent', 'AI Agent')}
                subtitle={t(
                    `${ready.length} 个可直接使用 · ${drafts.length} 个草稿`,
                    `${ready.length} ready-to-use · ${drafts.length} drafts`
                )}
                actions={
                    <div className="flex gap-2">
                        <a
                            href="#/apikeys"
                            className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                        >
                            🔑 {t('获取 API 密钥', 'Get API key')}
                        </a>
                        <a
                            href="#/builder"
                            className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90"
                        >
                            ＋ {t('构建智能体', 'Build agent')}
                        </a>
                    </div>
                }
            />
            <div className="space-y-6 px-4 pt-3 pb-6">
                {loading && attempt === 0 && all.length === 0 ? (
                    <SkeletonLines rows={6} />
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
                                {t('可直接使用的智能体', 'Ready-to-use agents')}
                            </h3>
                            {ready.length === 0 ? (
                                <p className="text-xs text-text-muted">
                                    {t(
                                        '暂无已部署智能体 — 点击右上「构建智能体」开始',
                                        'none yet — click "Build agent" to start'
                                    )}
                                </p>
                            ) : (
                                <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                                    {ready.map((a) => {
                                        const id = a.id ?? a.agent_id ?? '';
                                        const u = usage[id];
                                        return (
                                            <div
                                                key={id}
                                                className="flex min-h-32 flex-col gap-2 rounded-card border border-border bg-bg-elevated p-3"
                                            >
                                                <div className="flex items-start justify-between gap-2">
                                                    <span className="text-sm font-medium text-text-primary">
                                                        {a.name}
                                                    </span>
                                                    <StatusBadge status="success" label="ready" />
                                                </div>
                                                <div className="line-clamp-2 min-h-8 text-xs text-text-secondary">
                                                    {a.description || t('无描述', 'No description')}
                                                </div>
                                                <div className="flex flex-wrap items-center gap-1 text-[10px]">
                                                    <span className="rounded bg-brand/10 px-1 font-mono text-brand">
                                                        {modelLabel(a.model)}
                                                    </span>
                                                    <span className="rounded bg-bg-muted px-1 font-mono text-text-secondary">
                                                        {t(
                                                            `${a.tools?.length ?? 0} 工具`,
                                                            `${a.tools?.length ?? 0} tools`
                                                        )}
                                                    </span>
                                                    {u?.runs ? (
                                                        <span className="rounded bg-bg-muted px-1 font-mono text-text-secondary">
                                                            {t(
                                                                `${u.runs} 次运行`,
                                                                `${u.runs} runs`
                                                            )}
                                                        </span>
                                                    ) : null}
                                                </div>
                                                <div className="mt-auto flex gap-2 text-[11px]">
                                                    <a
                                                        href={`#/aiagent?agent=${encodeURIComponent(id)}`}
                                                        className="rounded-md bg-brand/10 px-2 py-1 text-brand hover:bg-brand/20"
                                                    >
                                                        {t('使用', 'Use')}
                                                    </a>
                                                    <a
                                                        href={editorHref(id)}
                                                        className="rounded-md border border-border px-2 py-1 text-text-secondary hover:text-text-primary"
                                                    >
                                                        {t('编辑', 'Edit')}
                                                    </a>
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            )}
                        </section>
                        {drafts.length > 0 ? (
                            <section>
                                <h3 className="mb-2 text-sm font-medium text-text-primary">
                                    {t('构建中的草稿', 'Drafts')}
                                </h3>
                                <div className="flex gap-3 overflow-x-auto pb-1">
                                    {drafts.map((a) => {
                                        const id = a.id ?? a.agent_id ?? '';
                                        return (
                                            <div
                                                key={id}
                                                className="flex w-56 shrink-0 flex-col gap-1.5 rounded-card border border-dashed border-border bg-bg-elevated p-3"
                                            >
                                                <span className="text-sm font-medium text-text-primary">
                                                    {a.name}
                                                </span>
                                                <span className="line-clamp-2 text-xs text-text-secondary">
                                                    {a.description ||
                                                        t(
                                                            '未测试，构建进行中',
                                                            'untested, in progress'
                                                        )}
                                                </span>
                                                <a
                                                    href={editorHref(id)}
                                                    className="mt-auto self-start text-xs text-brand hover:underline"
                                                >
                                                    {t('继续构建 →', 'Continue building →')}
                                                </a>
                                            </div>
                                        );
                                    })}
                                </div>
                            </section>
                        ) : null}
                        <section>
                            <div className="mb-2 flex items-center justify-between">
                                <h3 className="text-sm font-medium text-text-primary">
                                    {t('最近运行', 'Recent runs')}
                                </h3>
                                <div className="flex gap-3 text-xs text-brand">
                                    <a href="#/dashboard" className="hover:underline">
                                        {t('用量与评测', 'Usage & evals')}
                                    </a>
                                    <a href="#/runs" className="hover:underline">
                                        {t('全部 →', 'All →')}
                                    </a>
                                </div>
                            </div>
                            {(data?.runs ?? []).length === 0 ? (
                                <p className="text-xs text-text-muted">
                                    {t('暂无运行', 'no runs yet')}
                                </p>
                            ) : (
                                <div className="divide-y divide-border rounded-card border border-border">
                                    {(data?.runs ?? []).map((r) => (
                                        <a
                                            key={r.task_id}
                                            href={`#/runs/${encodeURIComponent(r.task_id)}`}
                                            className="flex items-center gap-3 px-3 py-2 text-xs hover:bg-bg-muted"
                                        >
                                            <StatusBadge
                                                status={statusKind(r.status)}
                                                label={r.status}
                                            />
                                            <span className="min-w-0 flex-1 truncate text-text-primary">
                                                {r.title || r.task_id}
                                            </span>
                                            <span className="shrink-0 text-text-muted">
                                                {r.updated_at
                                                    ? formatRelativeTime(r.updated_at * 1000)
                                                    : ''}
                                            </span>
                                        </a>
                                    ))}
                                </div>
                            )}
                        </section>
                    </>
                )}
            </div>
        </div>
    );
}
