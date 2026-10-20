import { formatDuration, formatTokenCount } from '@fusion/web-ui/format';
import type { RunEvents } from '../api';
import { useT } from '../i18n';

// #406: run event waterfall (Claude Code task-trace pattern). Each node is a
// span with start offset + duration on a shared time axis; failed spans are
// red-flagged and offer "re-run to this node" (SSE only_node, #398).
const STATUS_COLOR: Record<string, string> = {
    success: 'bg-success',
    failed: 'bg-error',
    retrying: 'bg-warning',
};

export function RunWaterfall({
    events,
    onRerunNode,
}: {
    events: RunEvents | null;
    onRerunNode?: (nodeId: string) => void;
}) {
    const t = useT();
    if (!events) return null;
    const spans = events.nodes ?? [];
    if (!spans.length) {
        return (
            <div className="rounded-card border border-border bg-bg-elevated p-4 text-xs text-text-muted">
                {t('暂无节点事件（运行尚未产生结构事件）', 'no node events yet')}
            </div>
        );
    }
    const runStart = events.run_start || Math.min(...spans.map((s) => s.start));
    const runEnd = events.run_end || Math.max(...spans.map((s) => s.end));
    const totalMs = Math.max(1, (runEnd - runStart) * 1000);
    const tokensOf = (s: { tokens?: number; token_events: number }) => s.tokens ?? s.token_events;
    const totalTokens = spans.reduce((a, s) => a + tokensOf(s), 0);
    const failed = spans.filter((s) => s.status === 'failed').length;

    return (
        <div className="rounded-card border border-border bg-bg-elevated p-4">
            <div className="mb-3 flex items-center justify-between gap-2">
                <span className="text-sm text-text-primary">
                    {t('运行事件瀑布', 'Run event waterfall')}
                    <span className="ml-2 font-mono text-xs tabular-nums text-text-muted">
                        {formatDuration(totalMs)} · {formatTokenCount(totalTokens)} tok ·{' '}
                        {spans.length} {t('节点', 'nodes')}
                        {failed ? ` · ${failed} ${t('失败', 'failed')}` : ''}
                        {events.stop_reason ? ` · ${events.stop_reason}` : ''}
                    </span>
                </span>
                <span className="font-mono text-xs text-text-muted">
                    {events.run_status || '-'}
                </span>
            </div>
            <div className="flex flex-col gap-1.5">
                {spans.map((s) => {
                    const left = ((s.start - runStart) * 1000) / totalMs;
                    const width = Math.max(0.5, ((s.end - s.start) * 1000) / totalMs);
                    return (
                        <div key={s.node_id} className="flex items-center gap-2">
                            <span className="w-40 shrink-0 truncate font-mono text-xs text-text-secondary">
                                {s.node_id}
                            </span>
                            <div className="relative h-4 flex-1 overflow-hidden rounded bg-bg-muted">
                                <div
                                    className={`absolute top-0 h-full rounded ${STATUS_COLOR[s.status] ?? 'bg-info'}`}
                                    style={{ left: `${left}%`, width: `${width}%` }}
                                    title={`${s.node_id} · ${s.status} · ${formatDuration((s.end - s.start) * 1000)}`}
                                />
                            </div>
                            <span className="w-20 shrink-0 text-right font-mono text-xs tabular-nums text-text-muted">
                                {formatDuration((s.end - s.start) * 1000)}
                            </span>
                            <span className="w-16 shrink-0 text-right font-mono text-xs tabular-nums text-text-muted">
                                {formatTokenCount(tokensOf(s))} tok
                            </span>
                            <span className="w-12 shrink-0 text-right font-mono text-xs tabular-nums text-text-muted">
                                {s.tool_calls} {t('调用', 'calls')}
                            </span>
                            {s.status === 'failed' && onRerunNode ? (
                                <button
                                    type="button"
                                    onClick={() => onRerunNode(s.node_id)}
                                    className="shrink-0 rounded border border-error/60 px-2 py-0.5 text-[10px] text-error hover:bg-error/10"
                                >
                                    {t('重跑至该节点', 'Re-run to this node')}
                                </button>
                            ) : null}
                        </div>
                    );
                })}
            </div>
            {spans.some((s) => s.error) ? (
                <div className="mt-2 flex flex-col gap-1">
                    {spans
                        .filter((s) => s.error)
                        .map((s) => (
                            <div
                                key={`${s.node_id}:err`}
                                className="rounded bg-error/10 px-2 py-1 font-mono text-[11px] text-error"
                                role="alert"
                            >
                                {s.node_id}: {s.error}
                            </div>
                        ))}
                </div>
            ) : null}
        </div>
    );
}
