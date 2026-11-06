import {
    EmptyState,
    ErrorState,
    SelectInput,
    SkeletonLines,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import {
    Bot,
    Boxes,
    Download,
    FileCode2,
    Package,
    Puzzle,
    Search,
    Sparkles,
    Wrench,
} from 'lucide-react';
import { useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Marketplace browser: search + install/uninstall via marketplace.* RPC.
// #605-feedback: replaced the flat DataTable with a package-card grid —
// icon by entry type, meta badges, hover lift; dark-mode safe neutrals.
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

function typeIcon(type?: string) {
    const key = (type ?? '').toLowerCase();
    if (key.includes('agent')) return Bot;
    if (key.includes('tool')) return Wrench;
    if (key.includes('skill')) return Sparkles;
    if (key.includes('pack')) return Boxes;
    if (key.includes('flow') || key.includes('graph') || key.includes('template')) return FileCode2;
    return Puzzle;
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
                    className="flex items-center gap-1.5 whitespace-nowrap rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-muted disabled:opacity-50"
                >
                    <Search className="h-3.5 w-3.5" />
                    {t('搜索', 'Search')}
                </button>
            </div>
            <div className="px-4 pb-6 pt-3">
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
                    <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
                        {rows.map((e) => {
                            const entryId = e.id ?? e.name;
                            const Icon = typeIcon(e.type);
                            return (
                                <li
                                    key={entryId}
                                    className="group flex flex-col rounded-xl border border-border bg-bg-elevated p-4 shadow-sm transition-shadow hover:shadow-md"
                                >
                                    <div className="flex items-start gap-3">
                                        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-brand/10 text-brand">
                                            <Icon className="h-4 w-4" />
                                        </div>
                                        <div className="min-w-0 flex-1">
                                            <div className="flex items-center gap-2">
                                                <span className="truncate text-sm font-medium text-text-primary">
                                                    {e.name}
                                                </span>
                                                {e.version ? (
                                                    <span className="shrink-0 rounded bg-bg-muted px-1.5 py-0.5 font-mono text-[10px] text-text-secondary">
                                                        v{e.version}
                                                    </span>
                                                ) : null}
                                            </div>
                                            <div className="mt-0.5 flex items-center gap-2 text-[11px] text-text-secondary">
                                                {e.type ? (
                                                    <span className="rounded bg-brand/10 px-1.5 py-0.5 text-brand">
                                                        {e.type}
                                                    </span>
                                                ) : null}
                                                {e.author ? (
                                                    <span className="truncate">{e.author}</span>
                                                ) : null}
                                                {typeof e.downloads === 'number' ? (
                                                    <span className="flex shrink-0 items-center gap-0.5">
                                                        <Download className="h-3 w-3" />
                                                        {e.downloads}
                                                    </span>
                                                ) : null}
                                            </div>
                                        </div>
                                    </div>
                                    <p className="mt-2 line-clamp-2 min-h-[2rem] text-xs leading-5 text-text-secondary">
                                        {e.description ?? '-'}
                                    </p>
                                    <div className="mt-3 flex items-center justify-between border-t border-border pt-2.5">
                                        {e.installed ? (
                                            <span className="flex items-center gap-1 text-[11px] text-success">
                                                <Package className="h-3 w-3" />
                                                {t('已安装', 'installed')}
                                            </span>
                                        ) : (
                                            <span />
                                        )}
                                        {e.installed ? (
                                            <button
                                                type="button"
                                                disabled={busyName === entryId}
                                                onClick={() => act(e, 'uninstall')}
                                                className="rounded-md border border-border px-2.5 py-1 text-xs text-warning hover:bg-bg-muted disabled:opacity-50"
                                            >
                                                {t('卸载', 'Uninstall')}
                                            </button>
                                        ) : (
                                            <button
                                                type="button"
                                                disabled={busyName === entryId}
                                                onClick={() => act(e, 'install')}
                                                className="rounded-md bg-brand px-2.5 py-1 text-xs text-white hover:opacity-90 disabled:opacity-50"
                                            >
                                                {t('安装', 'Install')}
                                            </button>
                                        )}
                                    </div>
                                </li>
                            );
                        })}
                    </ul>
                )}
            </div>
        </div>
    );
}
