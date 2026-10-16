import {
    DataTable,
    EmptyState,
    ErrorState,
    SelectInput,
    SkeletonLines,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Marketplace browser: search + install/uninstall via marketplace.* RPC.
interface MarketplaceEntry {
    id?: string;
    name: string;
    type?: string;
    description?: string;
    author?: string;
    downloads?: number;
    installed?: boolean;
    version?: string;
}

export function MarketplacePage() {
    const t = useT();
    const [query, setQuery] = useState('');
    const [category, setCategory] = useState('');
    const [searchKey, setSearchKey] = useState(0);
    const [busyName, setBusyName] = useState('');

    const search = useAutoRetry(
        () =>
            rpc<{ entries: MarketplaceEntry[] }>('marketplace.search', {
                query: query.trim(),
                category,
                limit: 30,
            }),
        [searchKey]
    );
    const cats = useAutoRetry(
        () => rpc<{ categories: string[] }>('marketplace.list_categories', {}),
        []
    );

    const runSearch = () => {
        setBusyName('');
        setSearchKey((k) => k + 1);
    };

    const act = async (entry: MarketplaceEntry, verb: 'install' | 'uninstall') => {
        const entryId = entry.id ?? entry.name;
        setBusyName(entryId);
        try {
            await rpc(`marketplace.${verb}`, { entry_id: entryId });
            sdk.toast.show({
                level: 'success',
                message: verb === 'install' ? t('已安装', 'installed') : t('已卸载', 'uninstalled'),
            });
            setBusyName('');
            runSearch();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
            setBusyName('');
        }
    };

    const rows = search.data?.entries ?? [];
    const categoryOptions = [
        { value: '', label: t('全部', 'All') },
        ...(cats.data?.categories ?? []).map((c) => ({ value: c, label: c })),
    ];

    return (
        <div>
            <PageHeader
                title={t('市场', 'Marketplace')}
                subtitle={t(`${rows.length} 个条目`, `${rows.length} entries`)}
            />
            <div className="flex gap-2 px-4 pt-3">
                <TextInput
                    value={query}
                    onChange={setQuery}
                    placeholder={t('搜索市场…', 'search marketplace…')}
                />
                <div className="w-44">
                    <SelectInput
                        value={category}
                        onChange={setCategory}
                        options={categoryOptions}
                    />
                </div>
                <button
                    type="button"
                    onClick={runSearch}
                    disabled={search.loading}
                    className="whitespace-nowrap rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                >
                    {t('搜索', 'Search')}
                </button>
            </div>
            <div className="px-4 pt-3">
                {search.loading && search.attempt === 0 && rows.length === 0 ? (
                    <SkeletonLines rows={4} />
                ) : search.error ? (
                    <ErrorState
                        message={(search.error as Error).message}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={search.manualRetry}
                        retrying={search.attempt}
                    />
                ) : rows.length === 0 ? (
                    <EmptyState
                        title={t('暂无市场条目', 'no marketplace entries')}
                        description={t('调整搜索条件后重试', 'adjust the search and try again')}
                    />
                ) : (
                    <DataTable<MarketplaceEntry>
                        rows={rows}
                        rowKey={(e) => e.id ?? e.name}
                        pageSize={15}
                        columns={[
                            { key: 'name', header: t('名称', 'Name'), render: (e) => e.name },
                            {
                                key: 'type',
                                header: t('类型', 'Type'),
                                render: (e) => e.type ?? '-',
                            },
                            {
                                key: 'description',
                                header: t('描述', 'Description'),
                                render: (e) => (
                                    <span className="line-clamp-2 text-sm">
                                        {e.description ?? '-'}
                                    </span>
                                ),
                            },
                            {
                                key: 'author',
                                header: t('作者', 'Author'),
                                render: (e) => e.author ?? '-',
                            },
                            {
                                key: 'version',
                                header: t('版本', 'Version'),
                                render: (e) => e.version ?? '-',
                            },
                            {
                                key: 'actions',
                                header: '',
                                render: (e) => {
                                    const entryId = e.id ?? e.name;
                                    if (e.installed) {
                                        return (
                                            <button
                                                type="button"
                                                disabled={busyName === entryId}
                                                onClick={() => act(e, 'uninstall')}
                                                className="text-xs text-warning hover:underline disabled:opacity-50"
                                            >
                                                {t('卸载', 'Uninstall')}
                                            </button>
                                        );
                                    }
                                    return (
                                        <button
                                            type="button"
                                            disabled={busyName === entryId}
                                            onClick={() => act(e, 'install')}
                                            className="text-xs text-success hover:underline disabled:opacity-50"
                                        >
                                            {t('安装', 'Install')}
                                        </button>
                                    );
                                },
                            },
                        ]}
                    />
                )}
            </div>
        </div>
    );
}
