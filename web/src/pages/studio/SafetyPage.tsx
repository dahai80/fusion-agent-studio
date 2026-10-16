import { createPoller } from '@fusion/web-sdk';
import {
    DataTable,
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
}

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

    return (
        <div className="px-4">
            {pollError ? (
                <ErrorState message={pollError} retryLabel={t('重试', 'Retry')} onRetry={refresh} />
            ) : !loaded ? (
                <SkeletonLines rows={3} />
            ) : actions.length === 0 ? (
                <EmptyState title={t('无待审动作', 'no pending actions')} />
            ) : (
                <DataTable<SafetyAction>
                    rows={actions}
                    rowKey={(a) => a.action_id}
                    pageSize={15}
                    columns={[
                        {
                            key: 'action_id',
                            header: t('动作', 'Action'),
                            render: (a) => <span className="font-mono text-xs">{a.action_id}</span>,
                        },
                        {
                            key: 'category',
                            header: t('类别', 'Category'),
                            render: (a) => a.category ?? '-',
                        },
                        {
                            key: 'content',
                            header: t('内容', 'Content'),
                            render: (a) => (
                                <span className="line-clamp-2 text-sm">{a.content ?? '-'}</span>
                            ),
                        },
                        { key: 'level', header: t('级别', 'Level'), render: (a) => a.level ?? '-' },
                        {
                            key: 'actions',
                            header: '',
                            render: (a) => (
                                <div className="flex gap-2">
                                    <button
                                        type="button"
                                        onClick={() => act(a.action_id, true)}
                                        className="text-xs text-success hover:underline"
                                    >
                                        {t('批准', 'Approve')}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => act(a.action_id, false)}
                                        className="text-xs text-error hover:underline"
                                    >
                                        {t('拒绝', 'Reject')}
                                    </button>
                                </div>
                            ),
                        },
                    ]}
                />
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
