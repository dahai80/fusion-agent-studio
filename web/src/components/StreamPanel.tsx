import { StreamingMarkdown } from '@fusion/web-ui';
import { formatDuration, formatTokenCount } from '@fusion/web-ui/format';
import { useEffect, useRef, useState } from 'react';
import { apiBase } from '../api';
import { useT } from '../i18n';

// Spec §7.1 true streaming: SSE per-token TOKEN events, follow-scroll lock
// (inside StreamingMarkdown), a stop button, and live token/elapsed readout.
// Auth: EventSource cannot set headers — embedded mode the gateway /shell
// proxy injects the module Bearer; standalone mode the Vite/daemon host is
// same-origin and the query api_key (when configured) is appended.
export interface StreamEvent {
    type: string;
    content?: string;
    node_id?: string;
    name?: string;
    message?: string;
    metadata?: Record<string, unknown>;
}

interface StreamStats {
    tokens: number;
    startedAt: number;
}

export function StreamPanel({
    graphId,
    input,
    onDone,
    onActiveChange,
}: {
    graphId: string;
    input: string;
    onDone?: (finalText: string, error: string) => void;
    onActiveChange?: (active: boolean) => void;
}) {
    const t = useT();
    const [events, setEvents] = useState<StreamEvent[]>([]);
    const [text, setText] = useState('');
    const [streaming, setStreaming] = useState(false);
    const [error, setError] = useState('');
    const [stats, setStats] = useState<StreamStats | null>(null);
    const [elapsed, setElapsed] = useState(0);
    const esRef = useRef<EventSource | null>(null);
    // Remount-driven: bump key to restart, stable across StrictMode effects.
    const [tick, setTick] = useState(0);

    // Remount-driven: restart is driven by [tick]; onDone/onActiveChange are
    // invoked, not reactive — listing them would restart the stream on every
    // parent re-render.
    // biome-ignore lint/correctness/useExhaustiveDependencies: intentional partial deps
    useEffect(() => {
        const qs = new URLSearchParams({ input });
        const cfgKey = import.meta.env.VITE_API_KEY as string | undefined;
        if (cfgKey) qs.set('api_key', cfgKey);
        const es = new EventSource(
            `${apiBase()}/v1/graphs/${encodeURIComponent(graphId)}/execute/stream?${qs.toString()}`
        );
        esRef.current = es;
        setEvents([]);
        setText('');
        setError('');
        setStats({ tokens: 0, startedAt: Date.now() });
        setElapsed(0);
        setStreaming(true);
        onActiveChange?.(true);
        let finalText = '';
        let streamError = '';

        es.onmessage = (ev) => {
            let msg: StreamEvent;
            try {
                msg = JSON.parse(ev.data) as StreamEvent;
            } catch {
                console.warn('[web] unparseable SSE line, dropping');
                return;
            }
            if (msg.type === 'token' || msg.type === 'thinking_token') {
                setText((prev) => prev + (msg.content ?? ''));
                setStats((prev) => (prev ? { ...prev, tokens: prev.tokens + 1 } : prev));
            } else if (msg.type === 'result') {
                finalText = msg.content ?? '';
                setText(finalText || text);
            } else if (msg.type === 'error') {
                streamError = msg.message ?? msg.content ?? 'stream error';
                setError(streamError);
            } else if (msg.type === 'done') {
                es.close();
                setStreaming(false);
                onActiveChange?.(false);
                onDone?.(finalText, streamError);
                return;
            }
            setEvents((prev) => [...prev, msg]);
        };
        es.onerror = () => {
            // 'done' closes the stream; EventSource retries otherwise — if
            // we already saw done this is the expected close, else surface it.
            es.close();
            setStreaming(false);
            onActiveChange?.(false);
            setError((prev) => prev || t('连接中断', 'stream disconnected'));
            onDone?.(finalText, streamError || 'disconnected');
        };
        return () => {
            es.close();
            esRef.current = null;
        };
        // eslint-disable-next-line react-hooks/exhaustive-deps -- remount via tick; graphId/input are start-time params
    }, [graphId, input, tick]);

    useEffect(() => {
        if (!stats || !streaming) return;
        const timer = setInterval(() => setElapsed(Date.now() - stats.startedAt), 500);
        return () => clearInterval(timer);
    }, [stats, streaming]);

    const stop = () => {
        esRef.current?.close();
        setStreaming(false);
        onActiveChange?.(false);
    };

    const restart = () => setTick((v) => v + 1);

    const eventCount = events.length;
    return (
        <div className="flex flex-col gap-2 rounded-card border border-border bg-bg-elevated p-4">
            <div className="flex items-center justify-between gap-2">
                <span className="text-sm text-text-primary">
                    {t('流式输出', 'Streaming output')}
                    <span className="ml-2 font-mono text-xs tabular-nums text-text-muted">
                        {formatTokenCount(stats?.tokens ?? 0)} tok ·{' '}
                        {formatDuration(elapsed || (stats ? Date.now() - stats.startedAt : 0))} ·{' '}
                        {eventCount} {t('事件', 'events')}
                    </span>
                </span>
                {streaming ? (
                    <button
                        type="button"
                        onClick={stop}
                        className="rounded-md border border-error/60 px-3 py-1.5 text-xs text-error hover:bg-error/10"
                    >
                        {t('停止', 'Stop')}
                    </button>
                ) : (
                    <button
                        type="button"
                        onClick={restart}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-muted"
                    >
                        {t('重新运行', 'Rerun')}
                    </button>
                )}
            </div>
            {error ? (
                <div className="rounded-md bg-error/10 px-3 py-2 text-xs text-error" role="alert">
                    {error}
                </div>
            ) : null}
            {text ? (
                <StreamingMarkdown text={text} className="rounded-md bg-bg p-3" />
            ) : (
                <div className="px-3 py-6 text-center text-xs text-text-muted">
                    {streaming ? t('等待首个 token…', 'waiting for first token…') : ''}
                </div>
            )}
        </div>
    );
}
