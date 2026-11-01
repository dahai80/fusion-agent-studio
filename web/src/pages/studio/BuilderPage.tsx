import { ErrorState, SkeletonLines, SubmitButton, useAutoRetry } from '@fusion/web-ui';
import { useEffect, useMemo, useRef, useState } from 'react';
import { apiBase, apiGet, apiPost, apiPut } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// #528/#529: conversational agent builder. The build conversation on the
// left composes/refines the draft through the agents-platform LLM gateway
// (`current_doc` refinement turns); the right-hand config-draft card
// updates live per turn; capability suggestions install inline; the test
// run executes inside the conversation (trajectory + result + event count);
// [部署] lands the agent on the Dashboard as ready-to-use (and, via the
// #531 marketplace API, in Cowork's Sub-agents tab). The form editor stays
// the [编辑] advanced view of the same draft document.
interface DraftDoc {
    name?: string;
    description?: string;
    model?: { id?: string; effort?: string };
    system?: string;
    tools?: string[];
    metadata?: Record<string, unknown>;
}

interface ComposeResponse {
    ok: boolean;
    doc?: DraftDoc;
    yaml?: string;
    error?: string;
    warnings?: string[];
}

interface CatalogCapability {
    id: string;
    title: string;
    description: string;
    mutation?: boolean;
}

interface StarterTemplate {
    key: string;
    name: string;
    description: string;
    ask: string;
    tools: string[];
}

interface TestResponse {
    status?: string;
    events?: Array<{ type?: string; content?: string }>;
}

// Conversation turns — `test` renders a compact sample-execution receipt.
type Turn = { kind: 'user' | 'assistant' | 'test'; text: string };

export function BuilderPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(async () => {
        const [catalog, starters] = await Promise.all([
            apiGet<{ capabilities?: CatalogCapability[] }>(
                '/v1/agents-platform/capability-catalog'
            ).catch(() => ({}) as { capabilities?: CatalogCapability[] }),
            apiGet<{ starters?: StarterTemplate[] }>('/v1/agents-platform/starters').catch(
                () => ({}) as { starters?: StarterTemplate[] }
            ),
        ]);
        return { catalog: catalog.capabilities ?? [], starters: starters.starters ?? [] };
    }, []);

    const [turns, setTurns] = useState<Turn[]>([]);
    const [ask, setAsk] = useState('');
    const [doc, setDoc] = useState<DraftDoc | null>(null);
    const [agentId, setAgentId] = useState('');
    const [busy, setBusy] = useState(false);
    const [deployed, setDeployed] = useState(false);
    const [testInput, setTestInput] = useState('');
    const turnsRef = useRef<HTMLDivElement | null>(null);

    const catalog = useMemo(() => data?.catalog ?? [], [data]);
    const catalogById = useMemo(() => {
        const m: Record<string, CatalogCapability> = {};
        for (const c of catalog) m[c.id] = c;
        return m;
    }, [catalog]);

    // Suggestions: catalog entries not yet in the draft's tool set — the
    // inline capability-assembly chips (Skills/Connectors/Plugins classes
    // all surface through the same catalog; mutations marked ⚡).
    const suggestions = useMemo(() => {
        if (!doc) return [];
        const picked = new Set(doc.tools ?? []);
        return catalog.filter((c) => !picked.has(c.id)).slice(0, 4);
    }, [doc, catalog]);

    const pushTurn = (turn: Turn) => setTurns((prev) => [...prev, turn]);

    // Auto-save keeps the persisted draft in lockstep with the card so the
    // in-conversation test and deploy always act on the latest config.
    const autosave = async (nextDoc: DraftDoc): Promise<string> => {
        if (agentId) {
            await apiPut(`/v1/agents-platform/agents/${encodeURIComponent(agentId)}`, {
                definition_doc: nextDoc,
            });
            return agentId;
        }
        const created = await apiPost<{ agent_id?: string }>('/v1/agents-platform/agents', {
            definition_doc: nextDoc,
        });
        if (!created.agent_id) throw new Error(t('草稿保存失败', 'draft save failed'));
        setAgentId(created.agent_id);
        return created.agent_id;
    };

    const compose = async (instruction: string) => {
        if (!instruction.trim() || busy) return;
        setBusy(true);
        pushTurn({ kind: 'user', text: instruction.trim() });
        setAsk('');
        try {
            const r = await apiPost<ComposeResponse>('/v1/agents-platform/compose', {
                ask: instruction.trim(),
                ...(doc ? { current_doc: doc } : {}),
            });
            if (!r.ok || !r.doc) {
                pushTurn({
                    kind: 'assistant',
                    text: r.error || t('生成失败', 'compose failed'),
                });
                return;
            }
            setDoc(r.doc);
            setDeployed(false);
            await autosave(r.doc);
            pushTurn({
                kind: 'assistant',
                text: t(
                    `草案已更新：${r.doc.name ?? '-'} · 能力 ${(r.doc.tools ?? []).length} 个${
                        r.warnings?.length ? ` · 警告 ${r.warnings.length}` : ''
                    }`,
                    `draft updated: ${r.doc.name ?? '-'} · ${(r.doc.tools ?? []).length} capabilities${
                        r.warnings?.length ? ` · ${r.warnings.length} warnings` : ''
                    }`
                ),
            });
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
            pushTurn({ kind: 'assistant', text: (e as Error).message });
        } finally {
            setBusy(false);
        }
    };

    // In-conversation test run — same engine path as the editor's Test tab
    // (#441 semantics), rendered back into the conversation.
    const runTest = async () => {
        if (!agentId || !testInput.trim() || busy) return;
        setBusy(true);
        try {
            const r = await apiPost<TestResponse>(
                `/v1/agents-platform/agents/${encodeURIComponent(agentId)}/test`,
                { message: testInput.trim() }
            );
            let output = '';
            for (let i = (r.events ?? []).length - 1; i >= 0; i--) {
                const c = r.events?.[i]?.content;
                if (typeof c === 'string' && c.trim()) {
                    output = c;
                    break;
                }
            }
            pushTurn({
                kind: 'test',
                text: t(
                    `试跑 ${r.status ?? '-'} · 事件 ${(r.events ?? []).length} 个\n${output || '-'}`,
                    `test ${r.status ?? '-'} · ${(r.events ?? []).length} events\n${output || '-'}`
                ),
            });
        } catch (e) {
            pushTurn({ kind: 'test', text: `${t('错误', 'error')}: ${(e as Error).message}` });
        } finally {
            setTestInput('');
            setBusy(false);
        }
    };

    const deploy = async () => {
        if (!agentId || busy) return;
        setBusy(true);
        try {
            await apiPost(`/v1/agents-platform/agents/${encodeURIComponent(agentId)}/publish`);
            setDeployed(true);
            pushTurn({
                kind: 'assistant',
                text: t(
                    '已部署 — Dashboard「可直接使用」可见，Cowork 能力市场 Sub-agents 同步收录',
                    'deployed — visible on the Dashboard and in Cowork Sub-agents'
                ),
            });
            sdk.toast.show({ level: 'success', message: t('已部署', 'deployed') });
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setBusy(false);
        }
    };

    const addCapability = (id: string) => {
        setDoc((prev) => {
            if (!prev) return prev;
            const next = { ...prev, tools: [...(prev.tools ?? []), id] };
            autosave(next).catch((e) =>
                sdk.toast.show({ level: 'error', message: (e as Error).message })
            );
            return next;
        });
        setDeployed(false);
    };

    // biome-ignore lint/correctness/useExhaustiveDependencies: intentional: autoscroll on new turns
    useEffect(() => {
        const el = turnsRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [turns]);

    return (
        <div>
            <PageHeader title={t('构建智能体', 'Build agent')} />
            <div className="grid gap-4 px-4 pt-3 pb-6 lg:grid-cols-[1fr_20rem]">
                <div className="flex flex-col gap-3">
                    {loading && attempt === 0 ? <SkeletonLines rows={3} /> : null}
                    {error ? (
                        <ErrorState
                            message={(error as Error).message}
                            retryLabel={t('重试', 'Retry')}
                            onRetry={manualRetry}
                            retrying={attempt}
                        />
                    ) : null}
                    {!doc && (data?.starters ?? []).length > 0 ? (
                        <div className="rounded-card border border-border bg-bg-elevated p-3">
                            <div className="mb-2 text-xs font-medium text-text-secondary">
                                {t('从场景开始', 'Start from a use case')}
                            </div>
                            <div className="grid gap-2 sm:grid-cols-2">
                                {(data?.starters ?? []).map((s) => (
                                    <button
                                        key={s.key}
                                        type="button"
                                        onClick={() => compose(s.ask)}
                                        disabled={busy}
                                        className="flex flex-col gap-1 rounded-card border border-border p-3 text-left hover:border-brand disabled:opacity-50"
                                    >
                                        <span className="text-sm font-medium text-text-primary">
                                            {s.name}
                                        </span>
                                        <span className="text-xs text-text-secondary">
                                            {s.description}
                                        </span>
                                    </button>
                                ))}
                            </div>
                        </div>
                    ) : null}
                    <div
                        ref={turnsRef}
                        className="flex h-80 flex-col gap-2 overflow-y-auto rounded-card border border-border bg-bg p-3"
                    >
                        {turns.length === 0 ? (
                            <div className="py-10 text-center text-xs text-text-muted">
                                {t(
                                    '描述你想要的智能体，配置草案将随对话实时更新',
                                    'describe the agent — the draft updates as you talk'
                                )}
                            </div>
                        ) : (
                            turns.map((turn, i) => (
                                <div key={i} className="flex">
                                    <div
                                        className={`max-w-[85%] whitespace-pre-wrap rounded-md px-3 py-2 text-sm ${
                                            turn.kind === 'user'
                                                ? 'ml-auto bg-brand/10 text-text-primary'
                                                : turn.kind === 'test'
                                                  ? 'w-full bg-success/10 font-mono text-xs text-text-secondary'
                                                  : 'bg-bg-muted text-text-primary'
                                        }`}
                                    >
                                        {turn.kind === 'test' ? `🧪 ${turn.text}` : turn.text}
                                    </div>
                                </div>
                            ))
                        )}
                        {busy ? (
                            <div className="text-xs text-text-muted">
                                {t('思考中…', 'thinking…')}
                            </div>
                        ) : null}
                    </div>
                    <Textarea
                        value={ask}
                        onChange={setAsk}
                        rows={3}
                        placeholder={t(
                            '描述用途，或继续调整当前草稿…',
                            'describe the agent, or refine the current draft…'
                        )}
                    />
                    <div className="flex items-center gap-2">
                        <SubmitButton
                            label={busy ? t('生成中…', 'Generating…') : t('发送', 'Send')}
                            onAction={() => compose(ask)}
                            disabled={busy}
                        />
                    </div>
                    {suggestions.length > 0 ? (
                        <div className="rounded-card border border-border bg-bg-elevated p-3">
                            <div className="mb-2 text-xs font-medium text-text-secondary">
                                {t('建议装配的能力', 'Suggested capabilities')}
                            </div>
                            <div className="flex flex-wrap gap-2">
                                {suggestions.map((c) => (
                                    <button
                                        key={c.id}
                                        type="button"
                                        onClick={() => addCapability(c.id)}
                                        disabled={busy}
                                        className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:border-brand hover:text-brand disabled:opacity-50"
                                        title={c.description}
                                    >
                                        {c.mutation ? '⚡' : '◇'} {c.title}
                                        <span className="ml-1 text-[10px] text-brand">
                                            {t('加入', 'add')}
                                        </span>
                                    </button>
                                ))}
                            </div>
                        </div>
                    ) : null}
                    {agentId && !deployed ? (
                        <div className="flex flex-wrap items-end gap-2 rounded-card border border-border bg-bg-elevated p-3">
                            <div className="min-w-48 flex-1">
                                <Textarea
                                    value={testInput}
                                    onChange={setTestInput}
                                    rows={2}
                                    placeholder={t('试跑样例输入…', 'sample input for a test run…')}
                                />
                            </div>
                            <SubmitButton
                                label={t('试跑一下？', 'Test run')}
                                onAction={runTest}
                                disabled={busy}
                            />
                        </div>
                    ) : null}
                </div>
                {/* config-draft card — updates live per turn */}
                <div className="h-fit rounded-card border border-border bg-bg-elevated p-4">
                    <div className="mb-3 text-sm font-medium text-text-primary">
                        {t('配置草案', 'Config draft')}
                    </div>
                    {!doc ? (
                        <p className="text-xs text-text-muted">
                            {t(
                                '描述用途后，草案将在此实时更新',
                                'the draft updates here as you describe the agent'
                            )}
                        </p>
                    ) : (
                        <div className="flex flex-col gap-2 text-xs">
                            <div>
                                <span className="text-text-muted">{t('名称', 'Name')}: </span>
                                <span className="text-text-primary">{doc.name ?? '-'}</span>
                            </div>
                            <div>
                                <span className="text-text-muted">
                                    {t('描述', 'Description')}:{' '}
                                </span>
                                <span className="text-text-primary">{doc.description ?? '-'}</span>
                            </div>
                            <div>
                                <span className="text-text-muted">{t('模型', 'Model')}: </span>
                                <span className="font-mono text-text-primary">
                                    {doc.model?.id ?? 'base'}
                                    {doc.model?.effort ? ` · ${doc.model.effort}` : ''}
                                </span>
                            </div>
                            <div>
                                <span className="text-text-muted">
                                    {t('指令', 'Instructions')}:
                                </span>
                                <pre className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap rounded-md bg-bg-muted p-2 font-mono text-[10px] text-text-secondary">
                                    {doc.system ?? '-'}
                                </pre>
                            </div>
                            <div>
                                <span className="text-text-muted">
                                    {t('能力', 'Capabilities')}:
                                </span>
                                <div className="mt-1 flex flex-wrap gap-1">
                                    {(doc.tools ?? []).length === 0 ? (
                                        <span className="text-text-muted">-</span>
                                    ) : (
                                        (doc.tools ?? []).map((id) => (
                                            <span
                                                key={id}
                                                className="rounded bg-bg-muted px-1.5 py-0.5 font-mono text-[10px] text-text-secondary"
                                            >
                                                {catalogById[id]?.title ?? id}
                                            </span>
                                        ))
                                    )}
                                </div>
                            </div>
                            <div className="mt-2 flex flex-col gap-2">
                                <button
                                    type="button"
                                    onClick={deploy}
                                    disabled={busy || deployed}
                                    className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90 disabled:opacity-50"
                                >
                                    {deployed ? t('已部署 ✓', 'Deployed ✓') : t('部署', 'Deploy')}
                                </button>
                                {agentId ? (
                                    <a
                                        href={`${apiBase()}/console/agents.html?agent=${encodeURIComponent(agentId)}`}
                                        className="text-center text-xs text-text-secondary hover:text-text-primary"
                                    >
                                        {t(
                                            '在表单编辑器中打开（高级）',
                                            'Open in form editor (advanced)'
                                        )}
                                    </a>
                                ) : null}
                                {deployed ? (
                                    <a
                                        href="#/home"
                                        className="text-center text-xs text-brand hover:underline"
                                    >
                                        {t('回到 Dashboard →', 'Back to Dashboard →')}
                                    </a>
                                ) : null}
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
