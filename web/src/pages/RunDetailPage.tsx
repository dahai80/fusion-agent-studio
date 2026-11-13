import { createPoller } from '@fusion/web-sdk';
import {
    ConfirmDialog,
    ErrorState,
    KVPanel,
    SkeletonLines,
    StatusBadge,
    TaskProgress,
    TextInput,
} from '@fusion/web-ui';
import { formatRelativeTime } from '@fusion/web-ui/format';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
    type LaneEvent,
    type RunDetail,
    type RunEvents,
    type RunResult,
    type ThreadLane,
    type ToolStat,
    api,
    apiPost,
    inspectorApi,
    statusKind,
} from '../api';
import { PageHeader } from '../components/PageHeader';
import { RunWaterfall } from '../components/RunWaterfall';
import { StreamPanel } from '../components/StreamPanel';
import { useT } from '../i18n';
import { hashQuery } from '../lib/routeQuery';
import { sdk } from '../sdk';

// Run detail: TaskProgress (spec §7.2) + hasActiveTask tab declaration
// (spec S: sdk.tab.setActiveTask while running), result KVPanel, cancel
// with ConfirmDialog, optional live SSE test when a graph is bound.
const ACTIVE_STATUSES = new Set(['pending', 'submitted', 'running']);

// E4: Inspector tabs — Summary / Events / Tools / Threads / Resources.
type DetailTab = 'summary' | 'events' | 'tools' | 'threads' | 'resources';

const DETAIL_TABS: Array<{ key: DetailTab; label: string; labelEn: string }> = [
    { key: 'summary', label: '概览', labelEn: 'Summary' },
    { key: 'events', label: '事件', labelEn: 'Events' },
    { key: 'tools', label: '工具', labelEn: 'Tools' },
    { key: 'threads', label: '线程', labelEn: 'Threads' },
    { key: 'resources', label: '资源', labelEn: 'Resources' },
];

export function RunDetailPage({ runId }: { runId: string }) {
    const t = useT();
    const [run, setRun] = useState<RunDetail | null>(null);
    const [result, setResult] = useState<RunResult | null>(null);
    const [loadError, setLoadError] = useState('');
    const [cancelOpen, setCancelOpen] = useState(false);
    const [streaming, setStreaming] = useState(false);
    const [replayInput, setReplayInput] = useState('');
    // #406: structural span events for the waterfall; rerunNode pins the SSE
    // re-run to one node (only_node, #398).
    const [spans, setSpans] = useState<RunEvents | null>(null);
    const [rerunNode, setRerunNode] = useState('');
    // E4: active tab; a `?event=<id>` deep link opens straight onto the
    // Threads lane that carries the event.
    const [tab, setTab] = useState<DetailTab>(() =>
        hashQuery().get('event') ? 'threads' : 'summary'
    );
    // E4: Tools-row drill-down — the picked tool name filters the Threads lane.
    const [inspectTool, setInspectTool] = useState('');
    const runRef = useRef<RunDetail | null>(null);
    runRef.current = run;

    const refresh = useCallback(async () => {
        try {
            const detail = await api.run(runId);
            setRun(detail);
            setLoadError('');
            try {
                setSpans(await api.runEvents(runId));
            } catch (e) {
                console.warn('[web] run events load failed', (e as Error).message);
            }
            if (!ACTIVE_STATUSES.has(detail.status)) {
                const res = await api.runResult(runId);
                setResult(res);
            }
        } catch (e) {
            setLoadError((e as Error).message || 'load failed');
        }
    }, [runId]);

    // Initial load + poll every 5s (visibility-aware) while the run is
    // active; stop polling once terminal.
    useEffect(() => {
        void refresh();
        const poller = createPoller({
            intervalMs: 5000,
            onTick: async () => {
                await refresh();
            },
        });
        poller.start(false);
        const check = setInterval(() => {
            const st = runRef.current?.status ?? '';
            if (st && !ACTIVE_STATUSES.has(st)) {
                poller.stop();
                clearInterval(check);
            }
        }, 5000);
        return () => {
            poller.stop();
            clearInterval(check);
        };
    }, [refresh]);

    // hasActiveTask: declare the run on the shell tab while active; clear
    // on unmount or terminal state so the host badge stays truthful.
    useEffect(() => {
        const st = run?.status ?? '';
        if (ACTIVE_STATUSES.has(st)) {
            sdk.tab.setActiveTask({ name: runId });
        } else {
            sdk.tab.setActiveTask(null);
        }
        return () => {
            sdk.tab.setActiveTask(null);
        };
    }, [run?.status, runId]);

    const doCancel = async () => {
        try {
            await api.cancelRun(runId);
            sdk.toast.show({ level: 'info', message: t('已请求取消', 'cancel requested') });
            await refresh();
        } catch (e) {
            sdk.toast.show({
                level: 'error',
                message: `${t('取消失败', 'cancel failed')}: ${(e as Error).message}`,
            });
        } finally {
            setCancelOpen(false);
        }
    };

    if (loadError && !run) {
        return (
            <div>
                <PageHeader title={t('运行详情', 'Run detail')} subtitle={runId} />
                <div className="px-4 pt-3">
                    <ErrorState
                        message={`${t('加载失败', 'load failed')}: ${loadError}`}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={() => void refresh()}
                    />
                </div>
            </div>
        );
    }

    if (!run) {
        return (
            <div>
                <PageHeader title={t('运行详情', 'Run detail')} subtitle={runId} />
                <div className="px-4 pt-3">
                    <SkeletonLines rows={6} />
                </div>
            </div>
        );
    }

    const active = ACTIVE_STATUSES.has(run.status);
    const events = run.progress?.events ?? 0;
    const statusLabelMap: Record<string, [string, string]> = {
        pending: ['等待中', 'pending'],
        submitted: ['已提交', 'submitted'],
        running: ['运行中', 'running'],
        completed: ['已完成', 'completed'],
        failed: ['失败', 'failed'],
        canceled: ['已取消', 'canceled'],
    };
    const pair = statusLabelMap[run.status] ?? [run.status, run.status];
    const statusLabel = t(pair[0], pair[1]);

    return (
        <div className="flex flex-col gap-3">
            <PageHeader
                title={t('运行详情', 'Run detail')}
                subtitle={runId}
                actions={
                    <div className="flex items-center gap-2">
                        <StatusBadge status={statusKind(run.status)} label={statusLabel} />
                        {active ? (
                            <button
                                type="button"
                                onClick={() => setCancelOpen(true)}
                                className="rounded-md border border-error/60 px-3 py-1.5 text-xs text-error hover:bg-error/10"
                            >
                                {t('取消运行', 'Cancel run')}
                            </button>
                        ) : null}
                    </div>
                }
            />
            <div className="flex flex-col gap-3 px-4">
                <div
                    data-testid="run-detail-tabs"
                    className="flex flex-wrap items-center gap-1 border-b border-border pb-2"
                >
                    {DETAIL_TABS.map((d) => (
                        <button
                            key={d.key}
                            type="button"
                            data-testid={`tab-${d.key}`}
                            onClick={() => setTab(d.key)}
                            className={`rounded-md px-3 py-1.5 text-xs ${
                                tab === d.key
                                    ? 'bg-bg-muted font-medium text-text-primary'
                                    : 'text-text-secondary hover:bg-bg-muted/60'
                            }`}
                        >
                            {t(d.label, d.labelEn)}
                        </button>
                    ))}
                </div>
                {tab === 'summary' ? (
                    <>
                        <TaskProgress
                            stage={statusLabel}
                            current={active ? 0 : 1}
                            total={1}
                            startedAt={run.created_at ? run.created_at * 1000 : undefined}
                            errorLog={run.error || undefined}
                        />
                        <KVPanel
                            title={t('运行信息', 'Run info')}
                            object={{
                                run_id: run.run_id,
                                graph_id: run.graph_id || '-',
                                agent_id: run.agent_id || '-',
                                created_at: run.created_at
                                    ? formatRelativeTime(run.created_at * 1000)
                                    : '-',
                                updated_at: run.updated_at
                                    ? formatRelativeTime(run.updated_at * 1000)
                                    : '-',
                                events,
                                retry_count: run.progress?.retry_count ?? 0,
                                artifacts: (run.progress?.artifact_ids ?? []).join(', ') || '-',
                            }}
                        />
                        {result ? (
                            <KVPanel
                                title={t('结果', 'Result')}
                                object={{
                                    status: result.status,
                                    session_id: result.steps?.[0]?.session_id ?? '-',
                                    last_error: result.last_error || '-',
                                }}
                            />
                        ) : null}
                        {result?.steps?.[0]?.session_id ? (
                            <SessionOps sessionId={result.steps[0].session_id} />
                        ) : null}
                        <RunWaterfall
                            events={spans}
                            onRerunNode={
                                run.graph_id
                                    ? (nodeId) => {
                                          setRerunNode(nodeId);
                                          setStreaming(true);
                                      }
                                    : undefined
                            }
                        />
                    </>
                ) : null}
                {tab === 'events' && run.graph_id ? (
                    <EventsTab
                        spans={spans}
                        onRerunNode={
                            run.graph_id
                                ? (nodeId) => {
                                      setRerunNode(nodeId);
                                      setStreaming(true);
                                  }
                                : undefined
                        }
                    />
                ) : null}
                {tab === 'tools' ? (
                    <ToolsTab
                        runId={runId}
                        onInspect={(tool) => {
                            setTab('threads');
                            setInspectTool(tool);
                        }}
                    />
                ) : null}
                {tab === 'threads' ? (
                    <ThreadsTab
                        runId={runId}
                        focusTool={inspectTool}
                        deepLinkEvent={hashQuery().get('event') || ''}
                    />
                ) : null}
                {tab === 'resources' ? <ResourcesTab run={run} result={result} /> : null}
                {tab === 'summary' && run.graph_id ? (
                    <div className="flex flex-col gap-2">
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-sm text-text-primary">
                                {t('流式重放', 'Live replay')}
                                {rerunNode ? (
                                    <span className="ml-2 font-mono text-xs text-warning">
                                        {t('单节点重跑', 'rerun')}: {rerunNode}
                                    </span>
                                ) : null}
                            </span>
                            <div className="flex items-center gap-2">
                                {rerunNode ? (
                                    <button
                                        type="button"
                                        onClick={() => setRerunNode('')}
                                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-secondary hover:bg-bg-muted"
                                    >
                                        {t('改为整图重放', 'Full graph replay')}
                                    </button>
                                ) : null}
                                <button
                                    type="button"
                                    onClick={() => setStreaming((v) => !v)}
                                    className="rounded-md border border-border px-4 py-2 text-sm text-text-primary hover:bg-bg-muted"
                                >
                                    {streaming
                                        ? t('关闭', 'Close')
                                        : t('开始流式重放', 'start live replay')}
                                </button>
                            </div>
                        </div>
                        {streaming ? (
                            <>
                                <TextInput
                                    value={replayInput}
                                    onChange={setReplayInput}
                                    placeholder={t(
                                        '重放输入（默认复用运行目标）',
                                        'replay input (defaults to run goal)'
                                    )}
                                />
                                <StreamPanel
                                    key={rerunNode || 'full'}
                                    graphId={run.graph_id}
                                    input={replayInput}
                                    onlyNode={rerunNode || undefined}
                                />
                            </>
                        ) : null}
                    </div>
                ) : null}
            </div>
            <ConfirmDialog
                open={cancelOpen}
                title={t('取消运行', 'Cancel run')}
                description={t(
                    `确定取消 ${runId} 吗？该操作不可撤销。`,
                    `Cancel run ${runId}? This cannot be undone.`
                )}
                confirmLabel={t('取消运行', 'Cancel run')}
                cancelLabel={t('返回', 'Back')}
                danger
                onConfirm={doCancel}
                onCancel={() => setCancelOpen(false)}
            />
        </div>
    );
}

// E4 Events tab: per-node span table with drill-down rerun (same contract
// as the waterfall, laid out as rows for scanning).
function EventsTab({
    spans,
    onRerunNode,
}: {
    spans: RunEvents | null;
    onRerunNode?: (nodeId: string) => void;
}) {
    const t = useT();
    const nodes = spans?.nodes ?? [];
    if (!nodes.length) {
        return <div className="text-xs text-text-secondary">{t('暂无事件', 'no events yet')}</div>;
    }
    return (
        <div data-testid="events-tab" className="overflow-x-auto">
            <table className="w-full text-left text-xs">
                <thead className="text-text-secondary">
                    <tr>
                        <th className="py-1.5 pr-3">{t('节点', 'Node')}</th>
                        <th className="py-1.5 pr-3">{t('状态', 'Status')}</th>
                        <th className="py-1.5 pr-3">{t('工具调用', 'Tool calls')}</th>
                        <th className="py-1.5 pr-3">{t('Token', 'Tokens')}</th>
                        <th className="py-1.5 pr-3">{t('错误', 'Error')}</th>
                        <th className="py-1.5" />
                    </tr>
                </thead>
                <tbody>
                    {nodes.map((n, i) => (
                        <tr key={`${n.node_id}-${i}`} className="border-t border-border/60">
                            <td className="py-1.5 pr-3 font-mono text-text-primary">{n.node_id}</td>
                            <td className="py-1.5 pr-3">
                                <StatusBadge status={statusKind(n.status)} label={n.status} />
                            </td>
                            <td className="py-1.5 pr-3">{n.tool_calls}</td>
                            <td className="py-1.5 pr-3">{n.tokens ?? '-'}</td>
                            <td className="py-1.5 pr-3 text-error">{n.error || '-'}</td>
                            <td className="py-1.5">
                                {onRerunNode ? (
                                    <button
                                        type="button"
                                        onClick={() => onRerunNode(n.node_id)}
                                        className="rounded-md border border-border px-2 py-0.5 text-text-secondary hover:bg-bg-muted"
                                    >
                                        {t('重跑', 'rerun')}
                                    </button>
                                ) : null}
                            </td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

// E4 Tools tab: runs.tool_stats — calls / failure rate / median duration;
// a row click drills into the Threads lane filtered by that tool.
function ToolsTab({
    runId,
    onInspect,
}: {
    runId: string;
    onInspect: (tool: string) => void;
}) {
    const t = useT();
    const [stats, setStats] = useState<ToolStat[]>([]);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let alive = true;
        inspectorApi
            .toolStats(runId)
            .then((r) => {
                if (alive) setStats(r.tools ?? []);
            })
            .catch((e) => {
                if (alive) setError((e as Error).message || 'load failed');
            })
            .finally(() => {
                if (alive) setLoading(false);
            });
        return () => {
            alive = false;
        };
    }, [runId]);

    if (loading) return <SkeletonLines rows={3} />;
    if (error) return <div className="text-xs text-error">{error}</div>;
    if (!stats.length) {
        return (
            <div className="text-xs text-text-secondary">
                {t('该运行没有工具调用', 'no tool calls in this run')}
            </div>
        );
    }
    return (
        <div data-testid="tools-tab" className="overflow-x-auto">
            <table className="w-full text-left text-xs">
                <thead className="text-text-secondary">
                    <tr>
                        <th className="py-1.5 pr-3">{t('工具', 'Tool')}</th>
                        <th className="py-1.5 pr-3">{t('调用', 'Calls')}</th>
                        <th className="py-1.5 pr-3">{t('失败', 'Failures')}</th>
                        <th className="py-1.5 pr-3">{t('失败率', 'Failure rate')}</th>
                        <th className="py-1.5 pr-3">{t('中位时长', 'Median')}</th>
                        <th className="py-1.5">{t('P95', 'P95')}</th>
                    </tr>
                </thead>
                <tbody>
                    {stats.map((s) => (
                        <tr
                            key={s.tool}
                            data-testid={`tool-row-${s.tool}`}
                            onClick={() => onInspect(s.tool)}
                            onKeyDown={(e) => {
                                if (e.key === 'Enter' || e.key === ' ') onInspect(s.tool);
                            }}
                            role="button"
                            tabIndex={0}
                            className="cursor-pointer border-t border-border/60 hover:bg-bg-muted/50"
                        >
                            <td className="py-1.5 pr-3 font-mono text-text-primary">{s.tool}</td>
                            <td className="py-1.5 pr-3">{s.calls}</td>
                            <td className="py-1.5 pr-3">{s.failures}</td>
                            <td className="py-1.5 pr-3">{(s.failure_rate * 100).toFixed(1)}%</td>
                            <td className="py-1.5 pr-3">{s.median_ms.toFixed(0)}ms</td>
                            <td className="py-1.5">{s.p95_ms.toFixed(0)}ms</td>
                        </tr>
                    ))}
                </tbody>
            </table>
        </div>
    );
}

// E4 Threads tab: runs.thread_lanes — per-lane timeline (root event chains,
// subagent spans nested), minimap span bar, ?event= deep-link highlight.
function ThreadsTab({
    runId,
    focusTool,
    deepLinkEvent,
}: {
    runId: string;
    focusTool: string;
    deepLinkEvent: string;
}) {
    const t = useT();
    const [lanes, setLanes] = useState<ThreadLane[]>([]);
    const [error, setError] = useState('');
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        let alive = true;
        inspectorApi
            .threadLanes(runId)
            .then((r) => {
                if (alive) setLanes(r.lanes ?? []);
            })
            .catch((e) => {
                if (alive) setError((e as Error).message || 'load failed');
            })
            .finally(() => {
                if (alive) setLoading(false);
            });
        return () => {
            alive = false;
        };
    }, [runId]);

    if (loading) return <SkeletonLines rows={3} />;
    if (error) return <div className="text-xs text-error">{error}</div>;
    if (!lanes.length) {
        return (
            <div className="text-xs text-text-secondary">
                {t('该运行没有事件线程', 'no event threads in this run')}
            </div>
        );
    }
    const tMin = Math.min(...lanes.map((l) => l.t_start || Number.POSITIVE_INFINITY));
    const tMax = Math.max(...lanes.map((l) => l.t_end || 0));
    const span = Math.max(tMax - tMin, 1e-9);
    return (
        <div data-testid="threads-tab" className="flex flex-col gap-3">
            {lanes.map((lane) => {
                const visible = lane.events.filter((e) => !focusTool || e.name === focusTool);
                return (
                    <div key={lane.lane_id} className="rounded-lg border border-border/70 p-2">
                        <div className="flex items-center justify-between text-xs">
                            <span className="font-mono text-text-primary">{lane.label}</span>
                            <span className="text-text-secondary">
                                {lane.event_count} {t('事件', 'events')} · {t('深度', 'depth')}{' '}
                                {lane.max_depth}
                            </span>
                        </div>
                        <div className="mt-1.5 h-1.5 w-full rounded bg-bg-muted">
                            <div
                                className="h-1.5 rounded bg-[var(--color-accent,#6366f1)]"
                                style={{
                                    marginLeft: `${(((lane.t_start || tMin) - tMin) / span) * 100}%`,
                                    width: `${Math.max(2, ((lane.t_end - lane.t_start) / span) * 100)}%`,
                                }}
                            />
                        </div>
                        <div className="mt-2 flex flex-col gap-0.5">
                            {visible.map((e: LaneEvent, i: number) => (
                                <div
                                    key={e.event_id || i}
                                    data-testid={
                                        e.event_id ? `lane-event-${e.event_id}` : undefined
                                    }
                                    className={`flex items-center gap-2 text-[11px] ${
                                        deepLinkEvent && e.event_id === deepLinkEvent
                                            ? 'rounded bg-warning/15 px-1 py-0.5'
                                            : ''
                                    }`}
                                    style={{ paddingLeft: `${e.depth * 12}px` }}
                                >
                                    <span className="text-text-secondary">{e.type}</span>
                                    {e.name ? (
                                        <span className="font-mono text-text-primary">
                                            {e.name}
                                        </span>
                                    ) : null}
                                    {e.node_id ? (
                                        <span className="text-text-secondary">@{e.node_id}</span>
                                    ) : null}
                                </div>
                            ))}
                        </div>
                    </div>
                );
            })}
        </div>
    );
}

// E4 Resources tab: artifacts + terminal result payload for this run.
function ResourcesTab({ run, result }: { run: RunDetail; result: RunResult | null }) {
    const t = useT();
    const artifactIds = run.progress?.artifact_ids ?? [];
    return (
        <div data-testid="resources-tab" className="flex flex-col gap-2 text-xs">
            <KVPanel
                title={t('产物', 'Artifacts')}
                object={{
                    count: artifactIds.length,
                    ids: artifactIds.join(', ') || '-',
                }}
            />
            {result?.artifacts?.length ? (
                <KVPanel
                    title={t('结果产物', 'Result artifacts')}
                    object={{ ids: result.artifacts.join(', ') }}
                />
            ) : null}
            {result?.last_result && Object.keys(result.last_result).length ? (
                <KVPanel title={t('最终输出', 'Final output')} object={result.last_result} />
            ) : null}
        </div>
    );
}

// #583: session lifecycle ops on the run's session — fork (branch a new
// session from this one) and live-turn inject (queue input for the next
// turn), previously CLI-only (fusion-agent fork/inject).
function SessionOps({ sessionId }: { sessionId: string }) {
    const t = useT();
    const [injectText, setInjectText] = useState('');

    const fork = async () => {
        try {
            const res = await apiPost<{ session_id?: string; id?: string }>(
                `/v1/sessions/${encodeURIComponent(sessionId)}/fork`,
                { title: `fork of ${sessionId}` }
            );
            sdk.toast.show({
                level: 'success',
                message: `${t('已分支', 'forked')}: ${res.session_id ?? res.id ?? '-'}`,
            });
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const inject = async () => {
        if (!injectText.trim()) return;
        try {
            await apiPost(`/v1/sessions/${encodeURIComponent(sessionId)}/inject`, {
                input: injectText.trim(),
            });
            setInjectText('');
            sdk.toast.show({ level: 'success', message: t('已注入下一轮', 'injected') });
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    return (
        <div data-testid="session-ops" className="mt-3 text-xs">
            <div className="text-text-primary">
                {t('会话操作', 'Session ops')}{' '}
                <span className="font-mono text-text-secondary">{sessionId}</span>
            </div>
            <div className="mt-1.5 flex flex-wrap items-center gap-2">
                <button
                    type="button"
                    data-testid="session-fork"
                    onClick={fork}
                    className="rounded-md border border-border px-3 py-1 text-text-primary hover:bg-bg-muted"
                >
                    {t('分支会话', 'Fork session')}
                </button>
                <input
                    type="text"
                    value={injectText}
                    placeholder={t('注入下一轮的输入', 'input to inject for the next turn')}
                    onChange={(e) => setInjectText(e.target.value)}
                    className="w-64 rounded-md border border-border bg-bg-elevated px-2 py-1 text-text-primary"
                />
                <button
                    type="button"
                    data-testid="session-inject"
                    onClick={inject}
                    className="rounded-md border border-border px-3 py-1 text-text-primary hover:bg-bg-muted"
                >
                    {t('注入', 'Inject')}
                </button>
            </div>
        </div>
    );
}
