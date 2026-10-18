import { createPoller } from '@fusion/web-sdk';
import {
    EmptyState,
    ErrorState,
    FormField,
    SkeletonLines,
    SubmitButton,
    TextInput,
} from '@fusion/web-ui';
import { useEffect, useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Tabs } from '../../components/ui/Tabs';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Safety console (SafetyView/SafetyTab equivalent): Check / Evaluate / Pending
// approvals / Add policy — safety.* RPC. Pending actions poll every 5s.
interface SafetyAction {
    action_id: string;
    category?: string;
    content?: string;
    level?: string;
    status?: string;
    created_at?: number;
    card_type?: string;
    card?: {
        card_type?: string;
        diff?: string;
        original?: string;
        proposed?: string;
        requires_diff?: boolean;
    };
}

// N9 (#348): Permission Cards — four approval card classes.
const CARD_META: Record<string, { zh: string; en: string; accent: string }> = {
    folder: { zh: '文件夹', en: 'Folder', accent: 'border-warning text-warning' },
    code: { zh: '代码', en: 'Code', accent: 'border-brand text-brand' },
    network: { zh: '网络', en: 'Network', accent: 'border-running text-running' },
    remote: { zh: '远程', en: 'Remote', accent: 'border-error text-error' },
};

const CARD_ORDER = ['folder', 'code', 'network', 'remote'];

interface SafetyVerdict {
    level?: string;
    approved?: boolean;
    violations?: unknown[];
}

const TABS = ['check', 'pending', 'policy'] as const;

export function SafetyPage() {
    const t = useT();
    const [tab, setTab] = useState<string>('check');

    return (
        <div>
            <PageHeader title={t('安全', 'Safety')} />
            <Tabs
                tabs={TABS.map((k) => ({
                    key: k,
                    label:
                        k === 'check'
                            ? t('检查', 'Check')
                            : k === 'pending'
                              ? t('待审动作', 'Pending')
                              : t('策略', 'Policy'),
                }))}
                active={tab}
                onChange={setTab}
            />
            <div className="pt-3">
                {tab === 'check' ? <CheckSection /> : null}
                {tab === 'pending' ? <PendingSection /> : null}
                {tab === 'policy' ? <PolicySection /> : null}
            </div>
        </div>
    );
}

function CheckSection() {
    const t = useT();
    const [content, setContent] = useState('');
    const [context, setContext] = useState('');
    const [verdict, setVerdict] = useState<SafetyVerdict | null>(null);
    const [busy, setBusy] = useState(false);

    const run = async () => {
        if (!content.trim()) return;
        setBusy(true);
        setVerdict(null);
        try {
            const res = await rpc<{ verdict: SafetyVerdict }>('safety.check', {
                content: content.trim(),
                context: context.trim(),
            });
            setVerdict(res.verdict);
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="flex flex-col gap-3 px-4">
            <FormField label={t('内容', 'Content')} required>
                <Textarea value={content} onChange={setContent} rows={4} />
            </FormField>
            <FormField label={t('上下文', 'Context')}>
                <Textarea value={context} onChange={setContext} rows={2} />
            </FormField>
            <div>
                <SubmitButton label={t('安全检查', 'Run check')} onAction={run} disabled={busy} />
            </div>
            {verdict ? (
                <div className="rounded-card border border-border bg-bg-elevated p-4 text-sm">
                    <div className="flex items-center gap-2">
                        <span className="text-text-secondary">{t('级别', 'Level')}:</span>
                        <span className="font-medium">{verdict.level ?? '-'}</span>
                        <span
                            className={`ml-2 text-xs ${
                                verdict.approved ? 'text-success' : 'text-error'
                            }`}
                        >
                            {verdict.approved ? t('通过', 'approved') : t('未通过', 'blocked')}
                        </span>
                    </div>
                    {verdict.violations && verdict.violations.length > 0 ? (
                        <pre className="mt-2 overflow-x-auto font-mono text-xs text-text-secondary">
                            {JSON.stringify(verdict.violations, null, 2)}
                        </pre>
                    ) : null}
                </div>
            ) : null}
        </div>
    );
}

function PendingSection() {
    const t = useT();
    const [actions, setActions] = useState<SafetyAction[]>([]);
    const [pollError, setPollError] = useState('');
    const [loaded, setLoaded] = useState(false);
    const [cardFilter, setCardFilter] = useState('');

    const refresh = async () => {
        try {
            const res = await rpc<{ actions: SafetyAction[] }>('safety.get_pending_actions', {});
            setActions(res.actions ?? []);
            setPollError('');
            setLoaded(true);
        } catch (e) {
            setPollError((e as Error).message);
        }
    };

    // biome-ignore lint/correctness/useExhaustiveDependencies: intentional: poller starts once, refresh stable
    useEffect(() => {
        const poller = createPoller({ intervalMs: 5000, onTick: refresh });
        poller.start(true);
        return () => poller.stop();
    }, []);

    const act = async (actionId: string, approve: boolean) => {
        try {
            await rpc(approve ? 'safety.approve_action' : 'safety.reject_action', {
                action_id: actionId,
            });
            sdk.toast.show({
                level: 'success',
                message: approve ? t('已批准', 'approved') : t('已拒绝', 'rejected'),
            });
            refresh();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const counts = CARD_ORDER.reduce<Record<string, number>>((acc, key) => {
        acc[key] = actions.filter((a) => (a.card_type ?? a.card?.card_type) === key).length;
        return acc;
    }, {});
    const visible = cardFilter
        ? actions.filter((a) => (a.card_type ?? a.card?.card_type) === cardFilter)
        : actions;

    return (
        <div className="flex flex-col gap-3 px-4">
            <div className="flex flex-wrap gap-2">
                <button
                    type="button"
                    onClick={() => setCardFilter('')}
                    className={`rounded-md border px-2.5 py-1 text-xs ${
                        cardFilter === ''
                            ? 'border-brand text-brand'
                            : 'border-border text-text-secondary'
                    }`}
                >
                    {t('全部', 'All')} ({actions.length})
                </button>
                {CARD_ORDER.map((key) => (
                    <button
                        key={key}
                        type="button"
                        onClick={() => setCardFilter(key)}
                        className={`rounded-md border px-2.5 py-1 text-xs ${
                            cardFilter === key
                                ? `${CARD_META[key].accent}`
                                : 'border-border text-text-secondary'
                        }`}
                    >
                        {t(CARD_META[key].zh, CARD_META[key].en)} ({counts[key] ?? 0})
                    </button>
                ))}
            </div>
            {pollError ? (
                <ErrorState message={pollError} retryLabel={t('重试', 'Retry')} onRetry={refresh} />
            ) : !loaded ? (
                <SkeletonLines rows={3} />
            ) : visible.length === 0 ? (
                <EmptyState title={t('无待审动作', 'no pending actions')} />
            ) : (
                <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
                    {visible.map((a) => {
                        const cardType = a.card_type ?? a.card?.card_type ?? 'code';
                        const meta = CARD_META[cardType] ?? CARD_META.code;
                        const diff = a.card?.diff;
                        return (
                            <div
                                key={a.action_id}
                                className={`flex flex-col gap-2 rounded-card border-l-4 border border-border bg-bg-elevated p-3 ${meta.accent.split(' ')[0]}`}
                            >
                                <div className="flex items-center justify-between">
                                    <span
                                        className={`text-xs font-medium ${meta.accent.split(' ')[1]}`}
                                    >
                                        {t(meta.zh, meta.en)}
                                    </span>
                                    <span className="rounded bg-bg px-1.5 py-0.5 text-[10px] text-text-secondary">
                                        {a.level ?? '-'}
                                    </span>
                                </div>
                                <div className="text-xs text-text-secondary">
                                    {t('类别', 'Category')}:{' '}
                                    <span className="font-mono">{a.category ?? '-'}</span>
                                </div>
                                <div className="line-clamp-4 whitespace-pre-wrap text-sm text-text-primary">
                                    {a.content || '-'}
                                </div>
                                <div className="font-mono text-[10px] text-text-muted">
                                    {a.action_id}
                                </div>
                                {diff ? (
                                    <details>
                                        <summary className="cursor-pointer text-xs text-text-secondary">
                                            {t('查看差异', 'View diff')}
                                        </summary>
                                        <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-bg p-2 font-mono text-[11px] text-text-secondary">
                                            {diff}
                                        </pre>
                                    </details>
                                ) : null}
                                <div className="mt-auto flex gap-2">
                                    <button
                                        type="button"
                                        onClick={() => act(a.action_id, true)}
                                        className="rounded border border-success/40 px-2.5 py-1 text-xs text-success hover:bg-success/10"
                                    >
                                        {t('批准', 'Approve')}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => act(a.action_id, false)}
                                        className="rounded border border-error/40 px-2.5 py-1 text-xs text-error hover:bg-error/10"
                                    >
                                        {t('拒绝', 'Reject')}
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

function PolicySection() {
    const t = useT();
    const [category, setCategory] = useState('');
    const [description, setDescription] = useState('');
    const [level, setLevel] = useState('L1');

    const add = async () => {
        if (!category.trim()) return;
        try {
            await rpc('safety.add_policy', {
                category: category.trim(),
                description: description.trim(),
                level,
            });
            sdk.toast.show({ level: 'success', message: t('策略已添加', 'policy added') });
            setCategory('');
            setDescription('');
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    return (
        <div className="flex max-w-md flex-col gap-3 px-4">
            <FormField label={t('类别', 'Category')} required>
                <TextInput value={category} onChange={setCategory} />
            </FormField>
            <FormField label={t('描述', 'Description')}>
                <TextInput value={description} onChange={setDescription} />
            </FormField>
            <FormField label={t('级别', 'Level')}>
                <div className="flex gap-2">
                    {['L1', 'L2', 'L3'].map((l) => (
                        <button
                            key={l}
                            type="button"
                            onClick={() => setLevel(l)}
                            className={`rounded-md border px-3 py-1.5 text-sm ${
                                level === l
                                    ? 'border-brand text-brand'
                                    : 'border-border text-text-secondary'
                            }`}
                        >
                            {l}
                        </button>
                    ))}
                </div>
            </FormField>
            <SubmitButton label={t('添加策略', 'Add policy')} onAction={add} />
        </div>
    );
}
