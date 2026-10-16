import {
    EmptyState,
    ErrorState,
    FormField,
    SelectInput,
    SkeletonLines,
    SubmitButton,
    useAutoRetry,
} from '@fusion/web-ui';
import { useEffect, useRef, useState } from 'react';
import { apiBase, rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
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
}

interface ChatMessage {
    role: 'user' | 'assistant';
    content: string;
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

function frameDelta(obj: SseFrame): string {
    if (typeof obj.token === 'string' && obj.token) return obj.token;
    if (typeof obj.delta === 'string' && obj.delta) return obj.delta;
    if (typeof obj.content === 'string' && obj.content) return obj.content;
    if (typeof obj.data?.token === 'string' && obj.data.token) return obj.data.token;
    return '';
}

export function AiAgentPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ agents: AgentRow[] }>('agent.list', {}),
        []
    );
    const [tab, setTab] = useState('chat');
    const [messages, setMessages] = useState<ChatMessage[]>([]);
    const [input, setInput] = useState('');
    const [streaming, setStreaming] = useState(false);
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

    // biome-ignore lint/correctness/useExhaustiveDependencies: intentional: autoscroll on message/tab change
    useEffect(() => {
        const el = chatRef.current;
        if (el) el.scrollTop = el.scrollHeight;
    }, [messages, tab]);

    const appendAssistant = (delta: string) => {
        if (!delta) return;
        setMessages((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last && last.role === 'assistant' && last.content.endsWith('…')) {
                next[next.length - 1] = {
                    role: 'assistant',
                    content: last.content.slice(0, -1) + delta,
                };
            } else if (last && last.role === 'assistant') {
                next[next.length - 1] = { role: 'assistant', content: last.content + delta };
            } else {
                next.push({ role: 'assistant', content: delta });
            }
            return next;
        });
    };

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
        setInput('');
        setMessages((prev) => [...prev, { role: 'user', content: text }]);
        setStreaming(true);
        const ctrl = new AbortController();
        abortRef.current = ctrl;
        const qs = new URLSearchParams({ input: text });
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
                        }
                        if (done) break;
                    }
                }
            }
        } catch (e) {
            const msg =
                (e as Error).name === 'AbortError' ? t('已停止', 'stopped') : (e as Error).message;
            setMessages((prev) => [
                ...prev,
                { role: 'assistant', content: `${t('错误', 'error')}: ${msg}` },
            ]);
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
            <PageHeader title={t('Agent 对话', 'Agent Chat')} />
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
                        <Textarea
                            value={input}
                            onChange={setInput}
                            rows={3}
                            placeholder={t('输入消息…', 'type a message…')}
                        />
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
