import {
    ErrorState,
    FormField,
    SkeletonLines,
    StatusBadge,
    SubmitButton,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { formatRelativeTime } from '@fusion/web-ui/format';
import { useMemo, useState } from 'react';
import { api, rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Modal } from '../../components/ui/Modal';
import { useT } from '../../i18n';
import { type AgentModel, modelLabel } from '../../lib/agentModel';
import { sdk } from '../../sdk';

// Agents: list + create/publish/unpublish/clone/archive/delete — agent.* RPC.
// #411: §7.0 Anthropic-style card grid — one card per agent (name, description,
// model, last run, usage count) with a create-card first tile; usage counters
// are aggregated client-side from the existing runs list (no new backend).
interface Agent {
    id?: string;
    agent_id?: string;
    name: string;
    description?: string;
    // #413: legacy manifests carry an object here, not a string.
    model?: AgentModel;
    status?: string;
    graph_id?: string;
    version?: string;
    updated_at?: number;
}

interface Usage {
    count: number;
    lastRun?: number;
}

const STATUS_KIND: Record<string, 'success' | 'running' | 'offline'> = {
    published: 'success',
    archived: 'offline',
};

export function AgentsPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(async () => {
        const [agents, runs] = await Promise.all([
            rpc<{ agents: Agent[] }>('agent.list', {}),
            api.runs('', 200).catch(() => ({ runs: [], total: 0 })),
        ]);
        return { agents: agents.agents ?? [], runs: runs.runs ?? [] };
    }, []);
    const [showCreate, setShowCreate] = useState(false);
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');

    const rows = data?.agents ?? [];
    const usage = useMemo(() => {
        const map: Record<string, Usage> = {};
        for (const run of data?.runs ?? []) {
            const id = run.agent_id ?? '';
            if (!id) continue;
            const entry = map[id] ?? { count: 0 };
            entry.count += 1;
            const at = run.updated_at ?? run.created_at ?? 0;
            if (at > (entry.lastRun ?? 0)) entry.lastRun = at;
            map[id] = entry;
        }
        return map;
    }, [data?.runs]);

    const act = async (
        agentId: string,
        verb: string,
        payload: Record<string, unknown>,
        label: string
    ) => {
        try {
            await rpc(`agent.${verb}`, { agent_id: agentId, ...payload });
            sdk.toast.show({ level: 'success', message: label });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const remove = async (a: Agent) => {
        if (!window.confirm(t('确认删除？', 'Delete this agent?'))) return;
        await act(a.id ?? a.agent_id ?? '', 'delete', {}, t('已删除', 'deleted'));
    };

    const create = async () => {
        try {
            await rpc('agent.create', { name: name.trim(), description: description.trim() });
            sdk.toast.show({ level: 'success', message: t('已创建', 'created') });
            setShowCreate(false);
            setName('');
            setDescription('');
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    return (
        <div>
            <PageHeader
                title={t('智能体', 'Agents')}
                subtitle={t(`${rows.length} 个智能体`, `${rows.length} agents`)}
                actions={
                    <button
                        type="button"
                        onClick={() => setShowCreate(true)}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        {t('新建', 'New')}
                    </button>
                }
            />
            <div className="px-4 pt-3">
                {loading && attempt === 0 && rows.length === 0 ? (
                    <SkeletonLines rows={3} />
                ) : error ? (
                    <ErrorState
                        message={(error as Error).message}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                ) : (
                    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                        <button
                            type="button"
                            onClick={() => setShowCreate(true)}
                            className="flex min-h-36 flex-col items-center justify-center gap-1 rounded-card border border-dashed border-border p-4 text-text-secondary hover:border-brand hover:text-brand"
                        >
                            <span className="text-2xl leading-none">+</span>
                            <span className="text-xs">{t('新建智能体', 'New agent')}</span>
                        </button>
                        {rows.map((a) => {
                            const id = a.id ?? a.agent_id ?? '';
                            const u = usage[id];
                            return (
                                <div
                                    key={id}
                                    className="flex min-h-36 flex-col gap-2 rounded-card border border-border bg-bg-elevated p-3"
                                >
                                    <div className="flex items-start justify-between gap-2">
                                        <button
                                            type="button"
                                            onClick={() => {
                                                window.location.hash = `#/agents/${id}`;
                                            }}
                                            className="text-left text-sm font-medium text-text-primary hover:underline"
                                        >
                                            {a.name}
                                        </button>
                                        <StatusBadge
                                            status={STATUS_KIND[a.status ?? ''] ?? 'running'}
                                            label={a.status ?? '-'}
                                        />
                                    </div>
                                    <div className="line-clamp-2 min-h-8 text-xs text-text-secondary">
                                        {a.description || t('无描述', 'No description')}
                                    </div>
                                    <div className="flex flex-wrap items-center gap-1 text-[10px]">
                                        <span className="rounded bg-brand/10 px-1 font-mono text-brand">
                                            {modelLabel(a.model)}
                                        </span>
                                        {a.graph_id ? (
                                            <span className="rounded bg-bg-muted px-1 font-mono text-text-secondary">
                                                {a.graph_id}
                                            </span>
                                        ) : null}
                                    </div>
                                    <div className="text-[10px] text-text-muted">
                                        {u?.count
                                            ? t(
                                                  `${u.count} 次运行 · 最近 ${formatRelativeTime((u.lastRun ?? 0) * 1000)}`,
                                                  `${u.count} runs · last ${formatRelativeTime((u.lastRun ?? 0) * 1000)}`
                                              )
                                            : t('暂无运行', 'No runs yet')}
                                    </div>
                                    <div className="mt-auto flex flex-wrap gap-2 text-[11px]">
                                        <button
                                            type="button"
                                            onClick={() => {
                                                window.location.hash = `#/agents/${id}`;
                                            }}
                                            className="text-text-secondary hover:underline"
                                        >
                                            {t('详情', 'Detail')}
                                        </button>
                                        {a.status === 'published' ? (
                                            <button
                                                type="button"
                                                onClick={() =>
                                                    act(
                                                        id,
                                                        'unpublish',
                                                        {},
                                                        t('已取消发布', 'unpublished')
                                                    )
                                                }
                                                className="text-warning hover:underline"
                                            >
                                                {t('取消发布', 'Unpublish')}
                                            </button>
                                        ) : (
                                            <button
                                                type="button"
                                                onClick={() =>
                                                    act(id, 'publish', {}, t('已发布', 'published'))
                                                }
                                                className="text-success hover:underline"
                                            >
                                                {t('发布', 'Publish')}
                                            </button>
                                        )}
                                        <button
                                            type="button"
                                            onClick={() =>
                                                act(
                                                    id,
                                                    'clone',
                                                    { name: `${a.name} copy` },
                                                    t('已克隆', 'cloned')
                                                )
                                            }
                                            className="text-text-secondary hover:underline"
                                        >
                                            {t('克隆', 'Clone')}
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() =>
                                                act(id, 'archive', {}, t('已归档', 'archived'))
                                            }
                                            className="text-text-secondary hover:underline"
                                        >
                                            {t('归档', 'Archive')}
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => remove(a)}
                                            className="text-error hover:underline"
                                        >
                                            {t('删除', 'Delete')}
                                        </button>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                )}
            </div>
            <Modal
                open={showCreate}
                title={t('新建智能体', 'New agent')}
                onClose={() => setShowCreate(false)}
                footer={<SubmitButton label={t('创建', 'Create')} onAction={create} />}
            >
                <FormField label={t('名称', 'Name')} required>
                    <TextInput value={name} onChange={setName} />
                </FormField>
                <FormField label={t('描述', 'Description')}>
                    <TextInput value={description} onChange={setDescription} />
                </FormField>
            </Modal>
        </div>
    );
}
