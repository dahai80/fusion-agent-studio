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
import { useEffect, useMemo, useState } from 'react';
import { api, apiGet, apiPost, apiBase, rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Modal } from '../../components/ui/Modal';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { type AgentModel, modelLabel } from '../../lib/agentModel';
import { sdk } from '../../sdk';

// Agents: list + create/publish/unpublish/clone/archive/delete — agent.* RPC.
// #411: §7.0 Anthropic-style card grid — one card per agent (name, description,
// model, last run, usage count) with a create-card first tile; usage counters
// are aggregated client-side from the existing runs list (no new backend).
// #417: cards gain a color dot + tools count + per-agent token usage; the
// create flow is a 5-step wizard (name → system prompt → tools → model →
// color) and each card has an inline run button (agent.execute modal).
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
    tools?: string[];
    tags?: string[];
}

interface Usage {
    count: number;
    lastRun?: number;
    tokens?: number;
}

// #441: agents-platform template card (GET /v1/agents-platform/templates).
interface TemplateItem {
    key: string;
    name: string;
    description: string;
    tools: string[];
    tool_count: number;
    yaml: string;
}

interface ComposeResponse {
    ok: boolean;
    doc?: Record<string, unknown>;
    yaml?: string;
    error?: string;
    warnings?: string[];
}

const STATUS_KIND: Record<string, 'success' | 'running' | 'offline'> = {
    published: 'success',
    archived: 'offline',
};

// #417: wizard steps — 名称 → 系统提示 → 工具 → 模型 → 颜色.
const WIZARD_STEPS = ['名称/Name', '系统提示/Prompt', '工具/Tools', '模型/Model', '颜色/Color'];

// #417: stable per-agent accent color — color:<hex> tag wins, else hash id.
const AGENT_COLORS = [
    '#6366f1',
    '#0ea5e9',
    '#10b981',
    '#f59e0b',
    '#ef4444',
    '#ec4899',
    '#8b5cf6',
    '#14b8a6',
];

function agentColor(a: Agent): string {
    for (const tag of a.tags ?? []) {
        if (typeof tag === 'string' && tag.startsWith('color:')) return tag.slice(6);
    }
    const id = a.id ?? a.agent_id ?? a.name;
    let h = 0;
    for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) >>> 0;
    return AGENT_COLORS[h % AGENT_COLORS.length];
}

export function AgentsPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(async () => {
        const [agents, runs, usageAgg, tools] = await Promise.all([
            rpc<{ agents: Agent[] }>('agent.list', {}),
            api.runs('', 200).catch(() => ({ runs: [], total: 0 })),
            rpc<{
                agents?: Array<{
                    agent_id?: string;
                    runs?: number;
                    tokens?: number;
                    last_run?: number;
                }>;
            }>('analytics.agent_usage', {}).catch(
                () =>
                    ({}) as {
                        agents?: Array<{
                            agent_id?: string;
                            runs?: number;
                            tokens?: number;
                            last_run?: number;
                        }>;
                    }
            ),
            rpc<{ tools?: Array<{ name: string; description?: string; category?: string }> }>(
                'tool.list',
                {}
            ).catch(
                () =>
                    ({}) as {
                        tools?: Array<{ name: string; description?: string; category?: string }>;
                    }
            ),
        ]);
        return { agents: agents.agents ?? [], runs: runs.runs ?? [], usageAgg, tools };
    }, []);
    const [showCreate, setShowCreate] = useState(false);
    const [step, setStep] = useState(0);
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [systemPrompt, setSystemPrompt] = useState('');
    const [pickedTools, setPickedTools] = useState<string[]>([]);
    const [model, setModel] = useState('');
    const [color, setColor] = useState('');
    // #417: inline run modal state.
    const [runAgent, setRunAgent] = useState<Agent | null>(null);
    const [runInput, setRunInput] = useState('');
    const [runOutput, setRunOutput] = useState('');
    const [runBusy, setRunBusy] = useState(false);
    // #441: conversational creation — one sentence in, editable draft out.
    const [ask, setAsk] = useState('');
    const [composing, setComposing] = useState(false);
    const [composeMsg, setComposeMsg] = useState('');
    const [templates, setTemplates] = useState<TemplateItem[]>([]);

    useEffect(() => {
        apiGet<{ templates?: TemplateItem[] }>('/v1/agents-platform/templates')
            .then((r) => setTemplates(r.templates ?? []))
            .catch(() => setTemplates([]));
    }, []);

    const rows = data?.agents ?? [];
    const toolCatalog = data?.tools?.tools ?? [];
    const stepValid = step !== 0 || name.trim().length > 0;
    const usage = useMemo(() => {
        const map: Record<string, Usage> = {};
        // #417: server-side analytics is primary (graph-scoped task runs are
        // attributed there); the /v1/runs feed only fills agents the
        // analytics window missed.
        for (const agg of data?.usageAgg?.agents ?? []) {
            const id = agg.agent_id ?? '';
            if (!id || id === 'unknown') continue;
            map[id] = {
                count: agg.runs ?? 0,
                tokens: agg.tokens,
                lastRun: agg.last_run,
            };
        }
        for (const run of data?.runs ?? []) {
            const id = run.agent_id ?? '';
            if (!id || map[id]) continue;
            map[id] = { count: 1, lastRun: run.updated_at ?? run.created_at ?? 0 };
        }
        return map;
    }, [data?.runs, data?.usageAgg]);

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
            // #417: 5-step wizard payload — color persists as a color:<hex> tag
            // so the card dot survives reloads without a manifest schema change.
            await rpc('agent.create', {
                name: name.trim(),
                description: description.trim(),
                system_prompt: systemPrompt.trim(),
                tools: pickedTools,
                model: model.trim(),
                tags: color ? [`color:${color}`] : [],
            });
            sdk.toast.show({ level: 'success', message: t('已创建', 'created') });
            setShowCreate(false);
            setStep(0);
            setName('');
            setDescription('');
            setSystemPrompt('');
            setPickedTools([]);
            setModel('');
            setColor('');
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    // #441: compose -> save draft -> open the Form/YAML/Test editor
    // (web/console/agents.html) with the draft loaded. The editor owns
    // steps 3-6 of the f-ecommerce sequence unchanged: compile & sync,
    // capability picker (⚡ approval-gated / ◇ read-only), sandbox test,
    // save draft -> publish.
    const openDraftEditor = (agentId: string) => {
        window.location.href = `${apiBase()}/console/agents.html?agent=${encodeURIComponent(agentId)}`;
    };

    const generateDraft = async () => {
        if (!ask.trim() || composing) return;
        setComposing(true);
        setComposeMsg('');
        try {
            const r = await apiPost<ComposeResponse>('/v1/agents-platform/compose', {
                ask: ask.trim(),
            });
            if (!r.ok || !r.doc) {
                setComposeMsg(r.error || t('生成失败', 'compose failed'));
                return;
            }
            const created = await apiPost<{ agent_id?: string }>('/v1/agents-platform/agents', {
                definition_doc: r.doc,
            });
            if (!created.agent_id) {
                setComposeMsg(t('草稿保存失败', 'draft save failed'));
                return;
            }
            openDraftEditor(created.agent_id);
        } catch (e) {
            setComposeMsg((e as Error).message);
        } finally {
            setComposing(false);
        }
    };

    const useTemplate = async (key: string) => {
        const tpl = templates.find((x) => x.key === key);
        if (!tpl || composing) return;
        setComposing(true);
        setComposeMsg('');
        try {
            const c = await apiPost<ComposeResponse>('/v1/agents-platform/compile', {
                yaml: tpl.yaml,
            });
            if (!c.ok || !c.doc) {
                setComposeMsg(c.error || t('模板编译失败', 'template compile failed'));
                return;
            }
            const created = await apiPost<{ agent_id?: string }>('/v1/agents-platform/agents', {
                definition_doc: c.doc,
            });
            if (!created.agent_id) {
                setComposeMsg(t('草稿保存失败', 'draft save failed'));
                return;
            }
            openDraftEditor(created.agent_id);
        } catch (e) {
            setComposeMsg((e as Error).message);
        } finally {
            setComposing(false);
        }
    };

    const runInline = async () => {
        const agent = runAgent;
        if (!agent || !runInput.trim() || runBusy) return;
        setRunBusy(true);
        setRunOutput('');
        try {
            const res = await rpc<{
                status?: string;
                events?: Array<Record<string, unknown>>;
                session_id?: string;
            }>('agent.execute', {
                agent_id: agent.id ?? agent.agent_id ?? '',
                input: runInput.trim(),
            });
            let output = '';
            for (let i = (res.events ?? []).length - 1; i >= 0; i--) {
                const ev = res.events?.[i] ?? {};
                const d = (ev.data ?? {}) as Record<string, unknown>;
                const cand = [ev.content, d.content, d.output, d.response, d.message].find(
                    (v) => typeof v === 'string' && v.trim()
                );
                if (typeof cand === 'string' && cand.trim()) {
                    output = cand;
                    break;
                }
            }
            setRunOutput(
                output ||
                    `${t('状态', 'status')}: ${res.status ?? '-'} · ${t('事件', 'events')}: ${res.events?.length ?? 0}`
            );
            sdk.toast.show({ level: 'success', message: t('已执行', 'executed') });
            manualRetry();
        } catch (e) {
            setRunOutput(`${t('错误', 'error')}: ${(e as Error).message}`);
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setRunBusy(false);
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
                        {t('快速创建', 'Quick create')}
                    </button>
                }
            />
            {/* #441: conversational creation — the default entry. One
                sentence in, an editable draft YAML out; the draft opens
                in the Form/YAML/Test editor. */}
            <div className="px-4 pt-3">
                <div className="rounded-card border border-border bg-bg-elevated p-4">
                    <Textarea
                        value={ask}
                        onChange={(v) => setAsk(v)}
                        placeholder={t(
                            '用一句话描述你想要的智能体…',
                            'Describe the agent you want…'
                        )}
                        rows={2}
                    />
                    <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                        <span className="text-xs text-text-muted">
                            {t(
                                '对话式创建：一句话输入，产出可编辑的草稿 YAML',
                                'Conversational: one sentence in, an editable draft YAML out'
                            )}
                        </span>
                        <button
                            type="button"
                            disabled={!ask.trim() || composing}
                            onClick={generateDraft}
                            className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90 disabled:opacity-50"
                        >
                            {composing
                                ? t('生成中…', 'Generating…')
                                : t('生成草稿', 'Generate draft')}
                        </button>
                    </div>
                    {composeMsg ? (
                        <div className="mt-2 text-xs text-error">{composeMsg}</div>
                    ) : null}
                    {templates.length > 0 ? (
                        <div className="mt-4">
                            <div className="mb-2 text-xs font-medium text-text-secondary">
                                {t('从样例开始', 'Start from a sample')}
                            </div>
                            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                                {templates.map((tpl) => (
                                    <div
                                        key={tpl.key}
                                        className="flex flex-col gap-1 rounded-card border border-border p-3"
                                    >
                                        <div className="text-sm font-medium text-text-primary">
                                            {tpl.name}
                                        </div>
                                        <div className="text-xs text-text-secondary">
                                            {tpl.description}
                                        </div>
                                        <div className="font-mono text-[10px] text-text-muted">
                                            {tpl.tools.join(', ')}
                                        </div>
                                        <button
                                            type="button"
                                            disabled={composing}
                                            onClick={() => useTemplate(tpl.key)}
                                            className="mt-auto self-start text-xs text-brand hover:underline disabled:opacity-50"
                                        >
                                            {t('使用 →', 'Use →')}
                                        </button>
                                    </div>
                                ))}
                            </div>
                        </div>
                    ) : null}
                </div>
            </div>
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
                            <span className="text-xs">
                                {t('快速创建（名称+描述）', 'Quick create (name + description)')}
                            </span>
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
                                            className="flex items-center gap-1.5 text-left text-sm font-medium text-text-primary hover:underline"
                                        >
                                            <span
                                                className="inline-block h-2 w-2 shrink-0 rounded-full"
                                                style={{ backgroundColor: agentColor(a) }}
                                            />
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
                                        <span className="rounded bg-bg-muted px-1 font-mono text-text-secondary">
                                            {t(
                                                `${a.tools?.length ?? 0} 工具`,
                                                `${a.tools?.length ?? 0} tools`
                                            )}
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
                                                  `${u.count} 次运行${u.tokens ? ` · ${u.tokens} tokens` : ''} · 最近 ${formatRelativeTime((u.lastRun ?? 0) * 1000)}`,
                                                  `${u.count} runs${u.tokens ? ` · ${u.tokens} tokens` : ''} · last ${formatRelativeTime((u.lastRun ?? 0) * 1000)}`
                                              )
                                            : t('暂无运行', 'No runs yet')}
                                    </div>
                                    <div className="mt-auto flex flex-wrap gap-2 text-[11px]">
                                        <button
                                            type="button"
                                            onClick={() => {
                                                setRunAgent(a);
                                                setRunInput('');
                                                setRunOutput('');
                                            }}
                                            className="text-brand hover:underline"
                                        >
                                            {t('运行', 'Run')}
                                        </button>
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
                title={t(
                    `新建智能体 · ${step + 1}/5 ${WIZARD_STEPS[step]}`,
                    `New agent · ${step + 1}/5 ${WIZARD_STEPS[step]}`
                )}
                onClose={() => {
                    setShowCreate(false);
                    setStep(0);
                }}
                footer={
                    <div className="flex gap-2">
                        {step > 0 ? (
                            <button
                                type="button"
                                onClick={() => setStep((s) => s - 1)}
                                className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
                            >
                                {t('上一步', 'Back')}
                            </button>
                        ) : null}
                        {step < 4 ? (
                            <button
                                type="button"
                                disabled={!stepValid}
                                onClick={() => setStep((s) => s + 1)}
                                className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90 disabled:opacity-50"
                            >
                                {t('下一步', 'Next')}
                            </button>
                        ) : (
                            <SubmitButton label={t('创建', 'Create')} onAction={create} />
                        )}
                    </div>
                }
            >
                {step === 0 ? (
                    <>
                        <FormField label={t('名称', 'Name')} required>
                            <TextInput value={name} onChange={setName} />
                        </FormField>
                        <FormField label={t('描述', 'Description')}>
                            <TextInput value={description} onChange={setDescription} />
                        </FormField>
                    </>
                ) : null}
                {step === 1 ? (
                    <FormField label={t('系统提示', 'System prompt')}>
                        <Textarea
                            value={systemPrompt}
                            onChange={setSystemPrompt}
                            rows={6}
                            placeholder={t('You are…', 'You are…')}
                        />
                    </FormField>
                ) : null}
                {step === 2 ? (
                    <FormField
                        label={t(
                            `工具 · 已选 ${pickedTools.length}/${toolCatalog.length}`,
                            `Tools · ${pickedTools.length}/${toolCatalog.length} selected`
                        )}
                    >
                        <div className="max-h-56 overflow-y-auto rounded-md border border-border p-2">
                            {toolCatalog.length === 0 ? (
                                <div className="text-xs text-text-muted">
                                    {t('工具目录为空', 'tool catalog empty')}
                                </div>
                            ) : (
                                toolCatalog.map((tool) => (
                                    <label
                                        key={tool.name}
                                        className="flex cursor-pointer items-start gap-2 rounded px-1 py-1 text-xs hover:bg-bg-muted"
                                    >
                                        <input
                                            type="checkbox"
                                            className="mt-0.5"
                                            checked={pickedTools.includes(tool.name)}
                                            onChange={(e) => {
                                                setPickedTools((prev) =>
                                                    e.target.checked
                                                        ? [...prev, tool.name]
                                                        : prev.filter((n) => n !== tool.name)
                                                );
                                            }}
                                        />
                                        <span>
                                            <span className="font-mono text-text-primary">
                                                {tool.name}
                                            </span>
                                            {tool.description ? (
                                                <span className="ml-1 text-text-muted">
                                                    {tool.description.slice(0, 60)}
                                                </span>
                                            ) : null}
                                        </span>
                                    </label>
                                ))
                            )}
                        </div>
                    </FormField>
                ) : null}
                {step === 3 ? (
                    <FormField label={t('模型', 'Model')}>
                        <TextInput value={model} onChange={setModel} placeholder="qwen3-4b" />
                        <div className="text-[10px] text-text-muted">
                            {t('留空使用引擎默认模型', 'empty = engine default')}
                        </div>
                    </FormField>
                ) : null}
                {step === 4 ? (
                    <FormField label={t('卡片颜色', 'Card color')}>
                        <div className="flex flex-wrap gap-2">
                            <button
                                type="button"
                                onClick={() => setColor('')}
                                className={`h-7 w-7 rounded-full border ${
                                    color === '' ? 'border-brand' : 'border-border'
                                } bg-bg text-[9px] leading-none text-text-muted`}
                            >
                                auto
                            </button>
                            {AGENT_COLORS.map((c) => (
                                <button
                                    key={c}
                                    type="button"
                                    onClick={() => setColor(c)}
                                    aria-label={c}
                                    className={`h-7 w-7 rounded-full border-2 ${
                                        color === c ? 'border-brand' : 'border-transparent'
                                    }`}
                                    style={{ backgroundColor: c }}
                                />
                            ))}
                        </div>
                    </FormField>
                ) : null}
            </Modal>
            <Modal
                open={runAgent !== null}
                title={t(`运行 · ${runAgent?.name ?? ''}`, `Run · ${runAgent?.name ?? ''}`)}
                onClose={() => setRunAgent(null)}
                footer={
                    <SubmitButton
                        label={runBusy ? t('运行中…', 'Running…') : t('运行', 'Run')}
                        onAction={runInline}
                        disabled={runBusy}
                    />
                }
            >
                <FormField label={t('输入', 'Input')} required>
                    <Textarea
                        value={runInput}
                        onChange={setRunInput}
                        rows={3}
                        placeholder={t('给这个 Agent 的任务…', 'task for this agent…')}
                    />
                </FormField>
                {runOutput ? (
                    <pre className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap rounded-md bg-bg-muted p-2 font-mono text-xs text-text-secondary">
                        {runOutput}
                    </pre>
                ) : null}
            </Modal>
        </div>
    );
}
