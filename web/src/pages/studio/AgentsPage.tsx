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
import { formatRelativeTime } from '@fusion/web-ui/format';
import { useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Modal } from '../../components/ui/Modal';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Agents: list + create/publish/unpublish/clone/archive/delete — agent.* RPC.
interface Agent {
    id?: string;
    agent_id?: string;
    name: string;
    description?: string;
    status?: string;
    graph_id?: string;
    version?: string;
    updated_at?: number;
}

const STATUS_KIND: Record<string, 'success' | 'running' | 'offline'> = {
    published: 'success',
    archived: 'offline',
};

export function AgentsPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ agents: Agent[] }>('agent.list', {}),
        []
    );
    const [showCreate, setShowCreate] = useState(false);
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');

    const rows = data?.agents ?? [];

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
                ) : rows.length === 0 ? (
                    <EmptyState
                        title={t('暂无智能体', 'no agents')}
                        description={t('创建第一个智能体', 'create your first agent')}
                    />
                ) : (
                    <DataTable<Agent>
                        rows={rows}
                        rowKey={(a) => a.id ?? a.agent_id ?? ''}
                        pageSize={15}
                        columns={[
                            { key: 'name', header: t('名称', 'Name'), render: (a) => a.name },
                            {
                                key: 'description',
                                header: t('描述', 'Description'),
                                render: (a) => (
                                    <span className="line-clamp-2">{a.description ?? '-'}</span>
                                ),
                            },
                            {
                                key: 'status',
                                header: t('状态', 'Status'),
                                render: (a) => (
                                    <StatusBadge
                                        status={STATUS_KIND[a.status ?? ''] ?? 'running'}
                                        label={a.status ?? '-'}
                                    />
                                ),
                            },
                            {
                                key: 'updated_at',
                                header: t('更新时间', 'Updated'),
                                render: (a) =>
                                    a.updated_at ? formatRelativeTime(a.updated_at * 1000) : '-',
                            },
                            {
                                key: 'actions',
                                header: '',
                                render: (a) => (
                                    <div className="flex gap-2 text-xs">
                                        <button
                                            type="button"
                                            onClick={() => {
                                                window.location.hash = `#/agents/${a.id ?? a.agent_id}`;
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
                                                        a.id ?? a.agent_id ?? '',
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
                                                    act(
                                                        a.id ?? a.agent_id ?? '',
                                                        'publish',
                                                        {},
                                                        t('已发布', 'published')
                                                    )
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
                                                    a.id ?? a.agent_id ?? '',
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
                                                act(
                                                    a.id ?? a.agent_id ?? '',
                                                    'archive',
                                                    {},
                                                    t('已归档', 'archived')
                                                )
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
                                ),
                            },
                        ]}
                    />
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
