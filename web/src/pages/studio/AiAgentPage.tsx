import {
    EmptyState,
    ErrorState,
    FormField,
    PermissionCards,
    SelectInput,
    SkeletonLines,
    SubmitButton,
    useAutoRetry,
} from '@fusion/web-ui';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, apiBase, rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Switch } from '../../components/ui/Switch';
import { Tabs } from '../../components/ui/Tabs';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// #330: per-token SSE chat with a selected agent. GET
// /v1/agents/{agent_id}/execute/stream (auth via gateway-injected Bearer in
// embedded mode, ?api_key= standalone) streamed through fetch getReader and
// split into `data: {...}` frames; a debug tab set keeps raw logs and
// non-token events for troubleshooting.
interface AgentRow {
    id?: string;
    agent_id?: string;
    name?: string;
    tools?: string[];
}

// #416: @ reference targets — graphs, sibling agents, knowledge entries.
interface RefItem {
    kind: 'graph' | 'agent' | 'knowledge';
    name: string;
}

interface ChatMessage {
    role: 'user' | 'assistant';
    content: string;
}

// #430: markdown skill package slash commands with schema-driven forms.
interface SkillParamSpec {
    type?: string;
    description?: string;
    enum?: unknown[];
    default?: unknown;
}

interface SkillCommandRow {
    command: string;
    name: string;
    description?: string;
    parameters?: {
        type?: string;
        properties?: Record<string, SkillParamSpec>;
        required?: string[];
    };
}

interface SseFrame {
    type?: string;
    token?: string;
    delta?: string;
    content?: string;
    message?: string;
    data?: { token?: string };
}

const MAX_LOG_LINES = 200;

// #416: map an agent's tool names onto the four permission card classes
// shared with the Safety console (vendor PermissionCards).
function classifyTools(tools: string[]): Record<string, boolean> {
    const names = tools.map((tool) => tool.toLowerCase());
    const has = (...needles: string[]) => names.some((n) => needles.some((w) => n.includes(w)));
    return {
        folder: has('file', 'read', 'write', 'artifact'),
        code: has('terminal', 'code', 'sandbox', 'exec', 'git'),
        network: has('browser', 'search', 'http', 'knowledge', 'rag', 'web'),
        remote: has('deploy', 'remote', 'cowork', 'skill', 'mcp'),
    };
}

function frameDelta(obj: SseFrame): string {
    if (typeof obj.token === 'string' && obj.token) return obj.token;
    if (typeof obj.delta === 'string' && obj.delta) return obj.delta;
    if (typeof obj.content === 'string' && obj.content) return obj.content;
    if (typeof obj.data?.token === 'string' && obj.data.token) return obj.data.token;
    return '';
}

export function AiAgentPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(async () => {
        // #416: agent list plus mention sources. Mentions degrade gracefully —
        // a failed source just drops out of the @ dropdown.
        const [agents, commands, knowledge, graphs] = await Promise.all([
            rpc<{ agents: AgentRow[] }>('agent.list', {}),
            rpc<{ commands?: SkillCommandRow[] }>('skills.commands', {}).catch(
                () => ({}) as { commands?: SkillCommandRow[] }
            ),
            rpc<{ entries?: Array<{ id?: string; content?: string }> }>('knowledge.list', {}).catch(
                () => ({}) as { entries?: Array<{ id?: string; content?: string }> }
            ),
            api.graphs().catch(() => null),
        ]);
        return { agents: agents.agents ?? [], commands, knowledge, graphs };
    }, []);
    const [tab, setTab] = useState('chat');
    // #416: forkable sessions — index 0 is the root conversation, fork
    // snapshots the current thread into a new one.
    const [sessions, setSessions] = useState<ChatMessage[][]>([[]]);
    const [activeSession, setActiveSession] = useState(0);
    const [pendingText, setPendingText] = useState('');
    const [preflightOpen, setPreflightOpen] = useState(false);
    const messages = sessions[activeSession] ?? [];
    const [input, setInput] = useState('');
    const [streaming, setStreaming] = useState(false);
    const [showThinking, setShowThinking] = useState(true);
    const [thinkingText, setThinkingText] = useState('');
    const [logs, setLogs] = useState<string[]>([]);
    const [events, setEvents] = useState<SseFrame[]>([]);
    const abortRef = useRef<AbortController | null>(null);
    const chatRef = useRef<HTMLDivElement | null>(null);

    const agents = data?.agents ?? [];
    const agentOptions = agents.map((a) => ({
        value: a.id ?? a.agent_id ?? '',
        label: a.name || a.id || a.agent_id || '',
    }));
    const [agentId, setAgentId] = useState('');
    const effectiveAgentId = agentId || agents[0]?.id || agents[0]?.agent_id || '';
    const selectedAgent = agents.find((a) => (a.id ?? a.agent_id) === effectiveAgentId);
    const allowedClasses = useMemo(
        () => classifyTools(selectedAgent?.tools ?? []),
        [selectedAgent]
    );

    const refItems = useMemo<RefItem[]>(() => {
        const items: RefItem[] = [];
        const seen = new Set<string>();
        const push = (kind: RefItem['kind'], name: string) => {
            if (!name || seen.has(`${kind}:${name}`)) return;
            seen.add(`${kind}:${name}`);
            items.push({ kind, name });
        };
        for (const g of data?.graphs?.data ?? []) {
            push('graph', g.name || g.graph_id);
        }
        for (const a of agents) {
            push('agent', a.name || a.id || a.agent_id || '');
        }
        for (const k of data?.knowledge?.entries ?? []) {
            push('knowledge', k.id || (k.content ?? '').slice(0, 24));
        }
        return items;
    }, [data, agents]);
    const commands = useMemo(() => data?.commands?.commands ?? [], [data]);

    // #416: trailing @ref / /skill token in the input drives the dropdown.
    const mention = useMemo(() => {
        const m = input.match(/(^|\s)([@/])([^\s@/]*)$/);
        if (!m) return null;
        return { sig: m[2], frag: m[3] };
    }, [input]);
    const refMatches =
        mention?.sig === '@'
            ? refItems
                  .filter((r) => r.name.toLowerCase().includes(mention.frag.toLowerCase()))
                  .slice(0, 6)
            : [];
    const skillMatches =
        mention?.sig === '/'
            ? commands
                  .filter((c) =>
                      `${c.name} ${c.command}`.toLowerCase().includes(mention.frag.toLowerCase())
                  )
                  .slice(0, 6)
            : [];
    const insertMention = (label: string) => {
        setInput((prev) =>
            prev.replace(
                /(^|\s)([@/])([^\s@/]*)$/,
                (_s, pre: string, sig: string) => `${pre}${sig}${label} `
            )
        );
    };

    // #430: selecting a slash command opens its schema-driven form; submit
    // renders the skill prompt via skills.invoke then streams it with the
    // selected agent (end-to-end local execution).
    const [skillForm, setSkillForm] = useState<SkillCommandRow | null>(null);
    const [skillValues, setSkillValues] = useState<Record<string, string>>({});
    const [skillErrors, setSkillErrors] = useState<string[]>([]);

    const openSkill = (row: SkillCommandRow) => {
        const initial: Record<string, string> = {};
        for (const [key, spec] of Object.entries(row.parameters?.properties ?? {})) {
            initial[key] = spec.default === undefined ? '' : String(spec.default);
        }
        setSkillForm(row);
        setSkillValues(initial);
        setSkillErrors([]);
        setInput((prev) => prev.replace(/(^|\s)\/([^\s@/]*)$/, '$1'));
    };

    const runSkill = async () => {
        if (!skillForm) return;
        try {
            const res = await rpc<{
                status?: string;
                prompt?: string;
                errors?: string[];
                message?: string;
            }>('skills.invoke', { name: skillForm.name, params: skillValues });
            if (res.status === 'error' || !res.prompt) {
                setSkillErrors(res.errors?.length ? res.errors : [res.message ?? 'invoke failed']);
                return;
            }
            setSkillForm(null);
            setSkillErrors([]);
            await doStream(res.prompt);
        } catch (e) {
            setSkillErrors([(e as Error).message]);
        }
    };

    // biome-ignore lint/correctness/useExhaustiveDependencies: intentional: autoscroll on message/tab change
    useEffect(() => {
        const el = chatRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [messages, tab]);

    const appendAssistant = (delta: string) => {
        if (!delta) return;
        setSessions((prev) => {
            const next = prev.map((s, i) => (i === activeSession ? [...s] : s));
            const arr = next[activeSession];
            const last = arr[arr.length - 1];
            if (last && last.role === 'assistant' && last.content.endsWith('…')) {
                arr[arr.length - 1] = {
                    role: 'assistant',
                    content: last.content.slice(0, -1) + delta,
                };
            } else if (last && last.role === 'assistant') {
                arr[arr.length - 1] = { role: 'assistant', content: last.content + delta };
            } else {
                arr.push({ role: 'assistant', content: delta });
            }
            return next;
        });
    };

    // #416: fork — snapshot the active thread into a new session.
    const forkSession = () => {
        setSessions((prev) => [...prev, [...(prev[activeSession] ?? [])]]);
        setActiveSession((i) => i + 1);
    };

    // #416: send opens the permission pre-flight card first; doStream runs
    // after the user confirms (or immediately when they cancel the gate).
    const send = async () => {
        const text = input.trim();
        if (!text || streaming) return;
        if (!effectiveAgentId) {
            sdk.toast.show({
                level: 'error',
                message: t('请先选择 Agent', 'select an agent first'),
            });
            return;
        }
        setPendingText(text);
        setPreflightOpen(true);
    };

    const doStream = async (text: string) => {
        setPreflightOpen(false);
        setPendingText('');
        setInput('');
        setSessions((prev) => {
            const next = prev.map((s, i) => (i === activeSession ? [...s] : s));
            next[activeSession].push({ role: 'user', content: text });
            return next;
        });
        setThinkingText('');
        setStreaming(true);
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        const qs = new URLSearchParams({ input: text, thinking: showThinking ? '1' : '0' });
        const cfgKey = import.meta.env.VITE_API_KEY as string | undefined;
        if (cfgKey) qs.set('api_key', cfgKey);
        try {
            const res = await fetch(
                `${apiBase()}/v1/agents/${encodeURIComponent(effectiveAgentId)}/execute/stream?${qs.toString()}`,
                { headers: { accept: 'text/event-stream' }, signal: ctrl.signal }
            );
            if (!res.ok || !res.body) {
                throw new Error(`stream request failed: ${res.status}`);
            }
            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let done = false;
            while (!done) {
                const { value, done: rdDone } = await reader.read();
                if (rdDone) break;
                buffer += decoder.decode(value, { stream: true });
                let idx = buffer.indexOf('\n\n');
                while (idx >= 0) {
                    const raw = buffer.slice(0, idx);
                    buffer = buffer.slice(idx + 2);
                    idx = buffer.indexOf('\n\n');
                    for (const line of raw.split('\n')) {
                        if (!line.startsWith('data: ')) continue;
                        let obj: SseFrame;
                        try {
                            obj = JSON.parse(line.slice(6)) as SseFrame;
                        } catch {
                            console.warn('[web] unparseable SSE frame, dropping');
                            continue;
                        }
                        const type = obj.type ?? 'unknown';
                        setLogs((prev) =>
                            [...prev, `[${type}] ${line.slice(6).slice(0, 200)}`].slice(
                                -MAX_LOG_LINES
                            )
                        );
                        if (type !== 'token') setEvents((prev) => [...prev, obj]);
                        if (type === 'done') {
                            done = true;
                        } else if (type === 'error') {
                            throw new Error(obj.message ?? obj.content ?? 'stream error');
                        } else if (type === 'token') {
                            appendAssistant(frameDelta(obj));
                        } else if (type === 'thinking_token') {
                            setThinkingText((prev) => prev + frameDelta(obj));
                        }
                        if (done) break;
                    }
                }
            }
        } catch (e) {
            const msg =
                (e as Error).name === 'AbortError' ? t('已停止', 'stopped') : (e as Error).message;
            setSessions((prev) => {
                const next = prev.map((s, i) => (i === activeSession ? [...s] : s));
                next[activeSession].push({
                    role: 'assistant',
                    content: `${t('错误', 'error')}: ${msg}`,
                });
                return next;
            });
            if ((e as Error).name !== 'AbortError') {
                sdk.toast.show({ level: 'error', message: msg });
            }
        } finally {
            abortRef.current = null;
            setStreaming(false);
        }
    };

    const stop = () => abortRef.current?.abort();

    return (
        <div>
            <PageHeader title={t('Composer', 'Composer')} />
            <div className="px-4 pt-3">
                {loading && attempt === 0 && agents.length === 0 ? (
                    <SkeletonLines rows={3} />
                ) : error ? (
                    <ErrorState
                        message={(error as Error).message}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                ) : agents.length === 0 ? (
                    <EmptyState title={t('暂无 Agent', 'no agents')} />
                ) : null}
                {agents.length > 0 ? (
                    <div className="mt-3 max-w-lg">
                        <FormField label={t('Agent', 'Agent')} required>
                            <SelectInput
                                value={effectiveAgentId}
                                onChange={setAgentId}
                                options={agentOptions}
                            />
                        </FormField>
                    </div>
                ) : null}
            </div>
            <Tabs
                tabs={[
                    { key: 'chat', label: t('对话', 'Chat') },
                    { key: 'logs', label: t('日志', 'Logs') },
                    { key: 'events', label: t('事件', 'Events') },
                ]}
                active={tab}
                onChange={setTab}
            />
            <div className="px-4 pt-3">
                {tab === 'chat' ? (
                    <div className="flex flex-col gap-3">
                        <div className="flex items-center gap-2 text-xs">
                            <span className="text-text-secondary">
                                {t('会话', 'Session')} {activeSession + 1}/{sessions.length}
                            </span>
                            {sessions.length > 1 ? (
                                <select
                                    value={activeSession}
                                    onChange={(e) => setActiveSession(Number(e.target.value))}
                                    className="rounded-md border border-border bg-bg-elevated px-1.5 py-0.5 text-xs"
                                >
                                    {sessions.map((_, i) => (
                                        <option key={i} value={i}>
                                            #{i + 1}
                                        </option>
                                    ))}
                                </select>
                            ) : null}
                            <button
                                type="button"
                                onClick={forkSession}
                                className="rounded-md border border-border px-2 py-0.5 text-text-secondary hover:text-text-primary"
                            >
                                {t('分叉会话', 'Fork session')}
                            </button>
                            <div className="ml-auto">
                                <Switch
                                    checked={showThinking}
                                    onChange={setShowThinking}
                                    label={t('显示思考', 'Show thinking')}
                                />
                            </div>
                        </div>
                        {showThinking && thinkingText ? (
                            <details
                                open
                                className="rounded-md border border-border bg-bg-muted p-3 text-xs text-text-secondary"
                            >
                                <summary className="cursor-pointer">
                                    {t('思考通道', 'Thinking channel')}
                                </summary>
                                <pre className="mt-2 max-h-40 overflow-y-auto whitespace-pre-wrap font-mono">
                                    {thinkingText}
                                </pre>
                            </details>
                        ) : null}
                        <div
                            ref={chatRef}
                            className="h-80 overflow-y-auto rounded-md border border-border bg-bg p-3"
                        >
                            {messages.length === 0 ? (
                                <div className="py-10 text-center text-xs text-text-muted">
                                    {t('输入消息开始对话', 'type a message to start')}
                                </div>
                            ) : (
                                messages.map((m, i) => (
                                    <div key={i} className="mb-2 flex">
                                        <div
                                            className={`max-w-[80%] whitespace-pre-wrap rounded-md px-3 py-2 text-sm ${
                                                m.role === 'user'
                                                    ? 'ml-auto bg-brand/10 text-text-primary'
                                                    : 'bg-bg-muted text-text-primary'
                                            }`}
                                        >
                                            {m.content}
                                        </div>
                                    </div>
                                ))
                            )}
                        </div>
                        {mention && mention.sig === '@' && refMatches.length > 0 ? (
                            <div className="flex flex-wrap gap-1 rounded-md border border-border bg-bg-elevated p-1.5 shadow-sm">
                                {refMatches.map((r) => (
                                    <button
                                        key={`${r.kind}-${r.name}`}
                                        type="button"
                                        onClick={() => insertMention(r.name)}
                                        className="rounded bg-bg-muted px-2 py-1 text-xs text-text-secondary hover:text-text-primary"
                                    >
                                        <span className="mr-1 text-[10px] uppercase text-text-muted">
                                            {r.kind}
                                        </span>
                                        {r.name}
                                    </button>
                                ))}
                            </div>
                        ) : null}
                        {mention && mention.sig === '/' && skillMatches.length > 0 ? (
                            <div className="flex flex-wrap gap-1 rounded-md border border-border bg-bg-elevated p-1.5 shadow-sm">
                                {skillMatches.map((s) => (
                                    <button
                                        key={s.name}
                                        type="button"
                                        onClick={() => openSkill(s)}
                                        className="rounded bg-bg-muted px-2 py-1 text-xs text-text-secondary hover:text-text-primary"
                                    >
                                        <span className="mr-1 text-[10px] uppercase text-text-muted">
                                            {s.command}
                                        </span>
                                        {s.name}
                                    </button>
                                ))}
                            </div>
                        ) : null}
                        {skillForm ? (
                            <div className="rounded-md border border-border bg-bg-elevated p-3">
                                <div className="mb-2 flex items-center gap-2 text-xs">
                                    <span className="font-mono text-brand">
                                        {skillForm.command}
                                    </span>
                                    <span className="text-text-secondary">
                                        {skillForm.description ?? skillForm.name}
                                    </span>
                                </div>
                                {(skillForm.parameters?.properties ?? {}) &&
                                Object.keys(skillForm.parameters?.properties ?? {}).length > 0 ? (
                                    <div className="flex flex-col gap-2">
                                        {Object.entries(skillForm.parameters?.properties ?? {}).map(
                                            ([key, spec]) => {
                                                const required = (
                                                    skillForm.parameters?.required ?? []
                                                ).includes(key);
                                                const value = skillValues[key] ?? '';
                                                return (
                                                    <div
                                                        key={key}
                                                        className="flex flex-col gap-1 text-xs"
                                                    >
                                                        <span className="text-text-secondary">
                                                            {spec.description ?? key}
                                                            {required ? ' *' : ''}
                                                        </span>
                                                        {Array.isArray(spec.enum) ? (
                                                            <select
                                                                aria-label={spec.description ?? key}
                                                                value={value}
                                                                onChange={(e) =>
                                                                    setSkillValues((prev) => ({
                                                                        ...prev,
                                                                        [key]: e.target.value,
                                                                    }))
                                                                }
                                                                className="rounded-md border border-border bg-bg px-2 py-1"
                                                            >
                                                                <option value="">—</option>
                                                                {spec.enum.map((opt) => (
                                                                    <option
                                                                        key={String(opt)}
                                                                        value={String(opt)}
                                                                    >
                                                                        {String(opt)}
                                                                    </option>
                                                                ))}
                                                            </select>
                                                        ) : (
                                                            <input
                                                                aria-label={spec.description ?? key}
                                                                value={value}
                                                                onChange={(e) =>
                                                                    setSkillValues((prev) => ({
                                                                        ...prev,
                                                                        [key]: e.target.value,
                                                                    }))
                                                                }
                                                                className="rounded-md border border-border bg-bg px-2 py-1"
                                                            />
                                                        )}
                                                    </div>
                                                );
                                            }
                                        )}
                                    </div>
                                ) : (
                                    <div className="text-xs text-text-muted">
                                        {t('该技能无需参数', 'this skill takes no parameters')}
                                    </div>
                                )}
                                {skillErrors.length > 0 ? (
                                    <div className="mt-2 text-xs text-error">
                                        {skillErrors.join('; ')}
                                    </div>
                                ) : null}
                                <div className="mt-3 flex gap-2">
                                    <button
                                        type="button"
                                        onClick={runSkill}
                                        disabled={streaming}
                                        className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90 disabled:opacity-50"
                                    >
                                        {t('运行技能', 'Run skill')}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setSkillForm(null);
                                            setSkillErrors([]);
                                        }}
                                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
                                    >
                                        {t('取消', 'Cancel')}
                                    </button>
                                </div>
                            </div>
                        ) : null}
                        <Textarea
                            value={input}
                            onChange={setInput}
                            rows={3}
                            placeholder={t(
                                '输入消息… @ 引用 / 技能',
                                'type a message… @ref /skill'
                            )}
                        />
                        {preflightOpen ? (
                            <div className="rounded-md border border-border bg-bg-elevated p-3">
                                <div className="mb-2 text-xs font-medium text-text-primary">
                                    {t('发送前权限检查', 'Pre-run permission check')}
                                </div>
                                <PermissionCards allowed={allowedClasses} />
                                <div className="mt-3 flex gap-2">
                                    <button
                                        type="button"
                                        onClick={() => doStream(pendingText)}
                                        disabled={streaming}
                                        className="rounded-md bg-brand px-3 py-1.5 text-xs text-white hover:opacity-90 disabled:opacity-50"
                                    >
                                        {t('确认发送', 'Confirm send')}
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setPreflightOpen(false);
                                            setPendingText('');
                                        }}
                                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:text-text-primary"
                                    >
                                        {t('取消', 'Cancel')}
                                    </button>
                                </div>
                            </div>
                        ) : null}
                        <div className="flex gap-2">
                            <SubmitButton
                                label={t('发送', 'Send')}
                                onAction={send}
                                disabled={streaming}
                            />
                            {streaming ? (
                                <button
                                    type="button"
                                    onClick={stop}
                                    className="rounded-md border border-error/60 px-3 py-1.5 text-xs text-error hover:bg-error/10"
                                >
                                    {t('停止', 'Stop')}
                                </button>
                            ) : null}
                        </div>
                    </div>
                ) : null}
                {tab === 'logs' ? (
                    <pre className="h-96 overflow-y-auto rounded-md bg-bg-muted p-3 font-mono text-xs">
                        {logs.length === 0
                            ? t('暂无日志', 'no logs yet')
                            : logs.map((line, i) => `${i + 1}\t${line}`).join('\n')}
                    </pre>
                ) : null}
                {tab === 'events' ? (
                    <div className="h-96 overflow-y-auto rounded-md border border-border bg-bg p-3">
                        {events.length === 0 ? (
                            <div className="text-xs text-text-muted">
                                {t('暂无事件', 'no events yet')}
                            </div>
                        ) : (
                            events.map((ev, i) => (
                                <div key={i} className="mb-1 font-mono text-xs text-text-secondary">
                                    [{ev.type ?? 'unknown'}] {ev.content ?? ev.message ?? ''}
                                </div>
                            ))
                        )}
                    </div>
                ) : null}
            </div>
        </div>
    );
}
