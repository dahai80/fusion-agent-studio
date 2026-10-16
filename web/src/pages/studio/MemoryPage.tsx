import {
    DataTable,
    EmptyState,
    ErrorState,
    FormField,
    SelectInput,
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
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Memory console (macOS MemoryView/MemoryTab equivalent): recall / recent /
// store / delete via memory.* RPC. Backend SSOT — no local cache beyond
// useAutoRetry.
interface MemoryEntry {
    id: string;
    content: string;
    scope: string;
    tags: string;
    importance: number;
    tier: string;
    memory_type?: string;
    created_at: number;
}

export function MemoryPage() {
    const t = useT();
    const [query, setQuery] = useState('');
    const [results, setResults] = useState<MemoryEntry[] | null>(null);
    const [busy, setBusy] = useState(false);
    const [showStore, setShowStore] = useState(false);
    const [content, setContent] = useState('');
    const [scope, setScope] = useState('default');
    const [tier, setTier] = useState('short_term');
    const [tags, setTags] = useState('');
    const [importance, setImportance] = useState('5');

    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ entries: MemoryEntry[] }>('memory.list_recent', { limit: 50 }),
        []
    );

    const recall = async () => {
        if (!query.trim()) return;
        setBusy(true);
        try {
            const res = await rpc<{ results?: MemoryEntry[]; entries?: MemoryEntry[] }>(
                'memory.recall',
                { query: query.trim(), limit: 20 }
            );
            setResults(res.results ?? res.entries ?? []);
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setBusy(false);
        }
    };

    const store = async () => {
        if (!content.trim()) return;
        try {
            await rpc('memory.store', {
                content: content.trim(),
                scope: scope.trim() || 'default',
                tier,
                tags,
                importance: Number(importance) || 5,
            });
            sdk.toast.show({ level: 'success', message: t('已存入记忆', 'memory stored') });
            setShowStore(false);
            setContent('');
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const remove = async (id: string) => {
        try {
            await rpc('memory.delete', { entry_id: id });
            sdk.toast.show({ level: 'success', message: t('已删除', 'deleted') });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const entries = results ?? data?.entries ?? [];
    const tierOptions = [
        { value: 'short_term', label: t('短期', 'short-term') },
        { value: 'long_term', label: t('长期', 'long-term') },
    ];

    return (
        <div>
            <PageHeader
                title={t('记忆', 'Memory')}
                subtitle={t(
                    `${entries.length} 条记忆 (最近)`,
                    `${entries.length} entries (recent)`
                )}
                actions={
                    <div className="flex items-center gap-2">
                        <button
                            type="button"
                            onClick={() => setShowStore(true)}
                            className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                        >
                            {t('存入记忆', 'Store')}
                        </button>
                    </div>
                }
            />
            <div className="flex gap-2 px-4 pt-3">
                <TextInput
                    value={query}
                    onChange={setQuery}
                    placeholder={t('搜索记忆…', 'search memory…')}
                />
                <button
                    type="button"
                    onClick={recall}
                    disabled={busy}
                    className="whitespace-nowrap rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                >
                    {t('召回', 'Recall')}
                </button>
            </div>
            <div className="px-4 pt-3">
                {loading && attempt === 0 && entries.length === 0 ? (
                    <SkeletonLines rows={4} />
                ) : error ? (
                    <ErrorState
                        message={(error as Error).message}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                ) : entries.length === 0 ? (
                    <EmptyState
                        title={t('暂无记忆', 'no memories yet')}
                        description={t('存入第一条记忆', 'store your first memory')}
                    />
                ) : (
                    <DataTable<MemoryEntry>
                        rows={entries}
                        rowKey={(e) => e.id}
                        pageSize={15}
                        columns={[
                            {
                                key: 'content',
                                header: t('内容', 'Content'),
                                render: (e) => (
                                    <span className="line-clamp-2 text-sm">{e.content}</span>
                                ),
                            },
                            {
                                key: 'scope',
                                header: t('范围', 'Scope'),
                                render: (e) => e.scope,
                            },
                            {
                                key: 'tier',
                                header: t('层级', 'Tier'),
                                render: (e) => e.tier,
                            },
                            {
                                key: 'importance',
                                header: t('重要性', 'Importance'),
                                render: (e) => String(e.importance),
                            },
                            {
                                key: 'created_at',
                                header: t('时间', 'Created'),
                                render: (e) =>
                                    e.created_at ? formatRelativeTime(e.created_at * 1000) : '-',
                            },
                            {
                                key: 'actions',
                                header: '',
                                render: (e) => (
                                    <button
                                        type="button"
                                        onClick={() => remove(e.id)}
                                        className="text-xs text-error hover:underline"
                                    >
                                        {t('删除', 'Delete')}
                                    </button>
                                ),
                            },
                        ]}
                    />
                )}
            </div>
            <Modal
                open={showStore}
                title={t('存入记忆', 'Store memory')}
                onClose={() => setShowStore(false)}
                footer={<SubmitButton label={t('存入', 'Store')} onAction={store} />}
            >
                <FormField label={t('内容', 'Content')} required>
                    <Textarea value={content} onChange={setContent} rows={4} />
                </FormField>
                <FormField label={t('范围', 'Scope')}>
                    <TextInput value={scope} onChange={setScope} />
                </FormField>
                <FormField label={t('层级', 'Tier')}>
                    <SelectInput value={tier} onChange={setTier} options={tierOptions} />
                </FormField>
                <FormField label={t('标签', 'Tags')}>
                    <TextInput value={tags} onChange={setTags} />
                </FormField>
                <FormField label={t('重要性 (1-10)', 'Importance (1-10)')}>
                    <TextInput value={importance} onChange={setImportance} type="number" />
                </FormField>
            </Modal>
            {busy ? <StatusBadge status="running" label={t('召回中…', 'recalling…')} /> : null}
        </div>
    );
}
