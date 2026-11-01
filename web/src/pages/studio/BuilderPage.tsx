import { ErrorState, SkeletonLines, SubmitButton, useAutoRetry } from '@fusion/web-ui';
import { useMemo, useState } from 'react';
import { apiBase, apiGet, apiPost, apiPut } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// #528/#529: conversational agent builder. The right-hand config-draft card
// updates live per turn (compose over the agents-platform LLM gateway);
// capabilities suggested from the catalog install inline; test runs inside
// the conversation; [部署] lands the agent on the Dashboard as ready-to-use
// (and, via the #531 marketplace API, in Cowork's Sub-agents tab).
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

    const [ask, setAsk] = useState('');
    const [doc, setDoc] = useState<DraftDoc | null>(null);
    const [agentId, setAgentId] = useState('');
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState('');
    const [deployed, setDeployed] = useState(false);

    const catalog = useMemo(() => data?.catalog ?? [], [data]);
    const catalogById = useMemo(() => {
        const m: Record<string, CatalogCapability> = {};
        for (const c of catalog) m[c.id] = c;
        return m;
    }, [catalog]);

    // Suggestions: catalog entries not yet in the draft's tool set.
    const suggestions = useMemo(() => {
        if (!doc) return [];
        const picked = new Set(doc.tools ?? []);
        return catalog.filter((c) => !picked.has(c.id)).slice(0, 4);
    }, [doc, catalog]);

    const compose = async (instruction: string) => {
        if (!instruction.trim() || busy) return;
        setBusy(true);
        setMsg('');
        try {
            const r = await apiPost<ComposeResponse>('/v1/agents-platform/compose', {
                ask: instruction.trim(),
                ...(doc ? { current_doc: doc } : {}),
            });
            if (!r.ok || !r.doc) {
                setMsg(r.error || t('生成失败', 'compose failed'));
                return;
            }
            setDoc(r.doc);
            setDeployed(false);
        } catch (e) {
            setMsg((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const saveDraft = async () => {
        if (!doc || busy) return;
        setBusy(true);
        setMsg('');
        try {
            if (agentId) {
                await apiPut(`/v1/agents-platform/agents/${encodeURIComponent(agentId)}`, {
                    definition_doc: doc,
                });
                sdk.toast.show({ level: 'success', message: t('草稿已更新', 'draft updated') });
            } else {
                const created = await apiPost<{ agent_id?: string }>('/v1/agents-platform/agents', {
                    definition_doc: doc,
                });
                if (!created.agent_id) {
                    setMsg(t('草稿保存失败', 'draft save failed'));
                    return;
                }
                setAgentId(created.agent_id);
                sdk.toast.show({ level: 'success', message: t('草稿已保存', 'draft saved') });
            }
        } catch (e) {
            setMsg((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const deploy = async () => {
        if (!agentId || busy) return;
        setBusy(true);
        setMsg('');
        try {
            await apiPost(`/v1/agents-platform/agents/${encodeURIComponent(agentId)}/publish`);
            setDeployed(true);
            sdk.toast.show({
                level: 'success',
                message: t('已部署 — Dashboard 与 Cowork 市场可见', 'deployed'),
            });
        } catch (e) {
            setMsg((e as Error).message);
        } finally {
            setBusy(false);
        }
    };

    const addCapability = (id: string) => {
        setDoc((prev) => (prev ? { ...prev, tools: [...(prev.tools ?? []), id] } : prev));
    };

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
                                        onClick={() => {
                                            setAsk(s.ask);
                                            compose(s.ask);
                                        }}
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
                    <Textarea
                        value={ask}
                        onChange={setAsk}
                        rows={3}
                        placeholder={t(
                            '描述你想要的智能体，或继续调整当前草稿…',
                            'Describe the agent you want, or refine the current draft…'
                        )}
                    />
                    <div className="flex items-center gap-2">
                        <SubmitButton
                            label={
                                busy
                                    ? t('生成中…', 'Generating…')
                                    : t('生成/调整草稿', 'Generate / refine draft')
                            }
                            onAction={() => compose(ask)}
                            disabled={busy}
                        />
                        {msg ? <span className="text-xs text-error">{msg}</span> : null}
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
                                        className="rounded-md border border-border px-2 py-1 text-xs text-text-secondary hover:border-brand hover:text-brand"
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
                                <SubmitButton
                                    label={
                                        agentId
                                            ? t('保存草稿更新', 'Save draft update')
                                            : t('保存草稿', 'Save draft')
                                    }
                                    onAction={saveDraft}
                                    disabled={busy}
                                />
                                {agentId ? (
                                    <>
                                        <button
                                            type="button"
                                            onClick={deploy}
                                            disabled={busy || deployed}
                                            className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90 disabled:opacity-50"
                                        >
                                            {deployed
                                                ? t('已部署 ✓', 'Deployed ✓')
                                                : t('部署', 'Deploy')}
                                        </button>
                                        <a
                                            href={`${apiBase()}/console/agents.html?agent=${encodeURIComponent(agentId)}`}
                                            className="text-center text-xs text-text-secondary hover:text-text-primary"
                                        >
                                            {t(
                                                '在表单编辑器中打开（高级）',
                                                'Open in form editor (advanced)'
                                            )}
                                        </a>
                                    </>
                                ) : null}
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    );
}
