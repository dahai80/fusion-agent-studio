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
import { type RunDetail, type RunResult, api, statusKind } from '../api';
import { PageHeader } from '../components/PageHeader';
import { StreamPanel } from '../components/StreamPanel';
import { useT } from '../i18n';
import { sdk } from '../sdk';

// Run detail: TaskProgress (spec §7.2) + hasActiveTask tab declaration
// (spec S: sdk.tab.setActiveTask while running), result KVPanel, cancel
// with ConfirmDialog, optional live SSE test when a graph is bound.
const ACTIVE_STATUSES = new Set(['pending', 'submitted', 'running']);

export function RunDetailPage({ runId }: { runId: string }) {
    const t = useT();
    const [run, setRun] = useState<RunDetail | null>(null);
    const [result, setResult] = useState<RunResult | null>(null);
    const [loadError, setLoadError] = useState('');
    const [cancelOpen, setCancelOpen] = useState(false);
    const [streaming, setStreaming] = useState(false);
    const [replayInput, setReplayInput] = useState('');
    const runRef = useRef<RunDetail | null>(null);
    runRef.current = run;

    const refresh = useCallback(async () => {
        try {
            const detail = await api.run(runId);
            setRun(detail);
            setLoadError('');
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
                {run.graph_id ? (
                    <div className="flex flex-col gap-2">
                        <div className="flex items-center justify-between gap-2">
                            <span className="text-sm text-text-primary">
                                {t('流式重放', 'Live replay')}
                            </span>
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
                                <StreamPanel graphId={run.graph_id} input={replayInput} />
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
