import { createPoller } from '@fusion/web-sdk';
import {
    DataTable,
    EmptyState,
    ErrorState,
    FormField,
    SelectInput,
    SkeletonLines,
    SubmitButton,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { useEffect, useRef, useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Tabs } from '../../components/ui/Tabs';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Team console: swarm agents / plaza chat rooms / orchestration — team.* RPC.
// Plaza messages poll every 5s.
interface SwarmAgent {
    name?: string;
    agent_id?: string;
    graph_id?: string;
    role?: string;
    status?: string;
}

interface PlazaMessage {
    role?: string;
    sender?: string;
    content?: string;
    created_at?: string;
}

interface OrchestrateResult {
    results?: unknown;
    errors?: unknown;
    summary?: string;
    total_duration?: number;
}

const TABS = ['swarm', 'plaza', 'orchestrate'] as const;

const PATTERNS = ['sequential', 'debate', 'consensus', 'relay'];

export function TeamPage() {
    const t = useT();
    const [tab, setTab] = useState<string>('swarm');

    return (
        <div>
            <PageHeader title={t('团队', 'Team')} />
            <Tabs
                tabs={TABS.map((k) => ({
                    key: k,
                    label:
                        k === 'swarm'
                            ? t('蜂群', 'Swarm')
                            : k === 'plaza'
                              ? t('广场', 'Plaza')
                              : t('编排', 'Orchestrate'),
                }))}
                active={tab}
                onChange={setTab}
            />
            <div className="pt-3">
                {tab === 'swarm' ? <SwarmSection /> : null}
                {tab === 'plaza' ? <PlazaSection /> : null}
                {tab === 'orchestrate' ? <OrchestrateSection /> : null}
            </div>
        </div>
    );
}

function SwarmSection() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ agents: SwarmAgent[] }>('team.swarm_agents', {}),
        []
    );

    const rows = data?.agents ?? [];

    return (
        <div className="px-4">
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
                    title={t('暂无蜂群 Agent', 'no swarm agents')}
                    description={t(
                        '创建带 team 角色的 agent 后展示',
                        'create agents with team roles'
                    )}
                />
            ) : (
                <DataTable<SwarmAgent>
                    rows={rows}
                    rowKey={(a) => a.agent_id || a.name || ''}
                    pageSize={15}
                    columns={[
                        {
                            key: 'name',
                            header: t('名称', 'Name'),
                            render: (a) => a.name || a.agent_id || '-',
                        },
                        { key: 'agent_id', header: 'Agent ID', render: (a) => a.agent_id ?? '-' },
                        {
                            key: 'graph_id',
                            header: t('图', 'Graph'),
                            render: (a) => (
                                <span className="font-mono text-xs">{a.graph_id ?? '-'}</span>
                            ),
                        },
                        { key: 'role', header: t('角色', 'Role'), render: (a) => a.role ?? '-' },
                        {
                            key: 'status',
                            header: t('状态', 'Status'),
                            render: (a) => a.status ?? '-',
                        },
                    ]}
                />
            )}
        </div>
    );
}

function PlazaSection() {
    const t = useT();
    const [channels, setChannels] = useState<string[]>([]);
    const [activeChannel, setActiveChannel] = useState<string | null>(null);
    const [messages, setMessages] = useState<PlazaMessage[]>([]);
    const [draft, setDraft] = useState('');
    const [error, setError] = useState('');
    const [loaded, setLoaded] = useState(false);
    const channelRef = useRef<string | null>(null);
    channelRef.current = activeChannel;

    const refresh = async () => {
        const channel = channelRef.current;
        if (!channel) return;
        try {
            const res = await rpc<{ messages: PlazaMessage[] }>('team.plaza_messages', { channel });
            setMessages(res.messages ?? []);
            setError('');
        } catch (e) {
            setError((e as Error).message);
        }
    };

    useEffect(() => {
        let cancelled = false;
        (async () => {
            try {
                const res = await rpc<{ channels: string[] }>('team.plaza_channels', {});
                if (cancelled) return;
                const list = res.channels ?? [];
                setChannels(list);
                if (list.length > 0) setActiveChannel(list[0]);
                setLoaded(true);
            } catch (e) {
                if (!cancelled) setError((e as Error).message);
            }
        })();
        return () => {
            cancelled = true;
        };
    }, []);

    // biome-ignore lint/correctness/useExhaustiveDependencies: intentional: refetch on channel switch
    useEffect(() => {
        if (activeChannel) refresh();
    }, [activeChannel]);

    // biome-ignore lint/correctness/useExhaustiveDependencies: intentional: refetch on channel switch
    useEffect(() => {
        const poller = createPoller({ intervalMs: 5000, onTick: refresh });
        poller.start(true);
        return () => poller.stop();
    }, []);

    const send = async () => {
        if (!activeChannel || !draft.trim()) return;
        try {
            await rpc('team.plaza_break_in', { channel: activeChannel, content: draft.trim() });
            setDraft('');
            refresh();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    return (
        <div className="flex flex-col gap-3 px-4">
            {error ? (
                <ErrorState message={error} retryLabel={t('重试', 'Retry')} onRetry={refresh} />
            ) : !loaded ? (
                <SkeletonLines rows={3} />
            ) : channels.length === 0 ? (
                <EmptyState title={t('暂无频道', 'no channels')} />
            ) : (
                <>
                    <div className="flex flex-wrap gap-2">
                        {channels.map((ch) => (
                            <button
                                key={ch}
                                type="button"
                                onClick={() => setActiveChannel(ch)}
                                className={`rounded-md border px-3 py-1.5 text-sm ${
                                    activeChannel === ch
                                        ? 'border-brand text-brand'
                                        : 'border-border text-text-secondary'
                                }`}
                            >
                                {ch}
                            </button>
                        ))}
                    </div>
                    <div className="flex max-h-96 flex-col gap-2 overflow-y-auto rounded-card border border-border bg-bg-elevated p-4">
                        {messages.length === 0 ? (
                            <span className="text-sm text-text-secondary">
                                {t('暂无消息', 'no messages')}
                            </span>
                        ) : (
                            messages.map((m, i) => (
                                <div key={i} className="text-sm">
                                    <span className="font-medium">{m.sender || m.role || '-'}</span>
                                    <span className="text-text-secondary">: </span>
                                    <span>{m.content ?? ''}</span>
                                </div>
                            ))
                        )}
                    </div>
                    <div className="flex gap-2">
                        <TextInput
                            value={draft}
                            onChange={setDraft}
                            placeholder={t('输入消息…', 'type a message…')}
                        />
                        <SubmitButton
                            label={t('发送', 'Send')}
                            onAction={send}
                            disabled={!activeChannel || !draft.trim()}
                        />
                    </div>
                </>
            )}
        </div>
    );
}

function OrchestrateSection() {
    const t = useT();
    const [objective, setObjective] = useState('');
    const [pattern, setPattern] = useState('sequential');
    const [maxRounds, setMaxRounds] = useState('3');
    const [result, setResult] = useState<OrchestrateResult | null>(null);
    const [busy, setBusy] = useState(false);

    const run = async () => {
        if (!objective.trim()) return;
        setBusy(true);
        setResult(null);
        try {
            const res = await rpc<OrchestrateResult>('team.orchestrate', {
                input: objective.trim(),
                pattern,
                max_rounds: Number(maxRounds) || 3,
            });
            setResult(res);
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setBusy(false);
        }
    };

    const patternOptions = PATTERNS.map((p) => ({ value: p, label: p }));

    return (
        <div className="flex flex-col gap-3 px-4">
            <FormField label={t('目标', 'Objective')} required>
                <Textarea value={objective} onChange={setObjective} rows={4} />
            </FormField>
            <FormField label={t('模式', 'Pattern')}>
                <SelectInput value={pattern} onChange={setPattern} options={patternOptions} />
            </FormField>
            <FormField label={t('最大轮数', 'Max rounds')}>
                <TextInput value={maxRounds} onChange={setMaxRounds} type="number" />
            </FormField>
            <div>
                <SubmitButton
                    label={t('编排', 'Orchestrate')}
                    onAction={run}
                    disabled={busy || !objective.trim()}
                />
            </div>
            {result ? (
                <div className="rounded-card border border-border bg-bg-elevated p-4 text-sm">
                    <div className="flex items-center gap-2">
                        <span className="text-text-secondary">{t('耗时', 'Duration')}:</span>
                        <span className="font-medium">
                            {result.total_duration != null ? `${result.total_duration}s` : '-'}
                        </span>
                    </div>
                    {result.summary ? (
                        <div className="mt-2 whitespace-pre-wrap">{result.summary}</div>
                    ) : null}
                    {result.results ? (
                        <pre className="mt-2 overflow-x-auto font-mono text-xs text-text-secondary">
                            {JSON.stringify(result.results, null, 2)}
                        </pre>
                    ) : null}
                    {result.errors && (result.errors as unknown[]).length > 0 ? (
                        <pre className="mt-2 overflow-x-auto font-mono text-xs text-error">
                            {JSON.stringify(result.errors, null, 2)}
                        </pre>
                    ) : null}
                </div>
            ) : null}
        </div>
    );
}
