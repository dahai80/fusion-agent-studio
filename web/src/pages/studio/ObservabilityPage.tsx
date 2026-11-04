import { ErrorState, SkeletonLines, StatusBadge, useAutoRetry } from '@fusion/web-ui';
import { apiBase, apiGet } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// #583: observability page — cluster alerts + routing suggestions + log
// export, through the daemon's same-origin /v1/cluster-ops/* proxy.
interface AlertRow {
    level?: string;
    message?: string;
    node_id?: string;
    created_at?: number;
    [k: string]: unknown;
}

interface SuggestionRow {
    title?: string;
    detail?: string;
    node_id?: string;
    [k: string]: unknown;
}

export function ObservabilityPage() {
    const t = useT();
    const alerts = useAutoRetry(
        () => apiGet<{ alerts?: AlertRow[] }>('/v1/cluster-ops/observability/alerts'),
        []
    );
    const suggestions = useAutoRetry(
        () =>
            apiGet<{ suggestions?: SuggestionRow[] }>('/v1/cluster-ops/observability/suggestions'),
        []
    );

    const exportLogs = async () => {
        try {
            const res = await fetch(`${apiBase()}/v1/cluster-ops/observability/logs/export`);
            if (!res.ok) throw new Error(`export failed: ${res.status}`);
            const blob = await res.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'cluster-logs.txt';
            a.click();
            URL.revokeObjectURL(url);
            sdk.toast.show({ level: 'success', message: t('已导出', 'exported') });
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    return (
        <div>
            <PageHeader
                title={t('可观测', 'Observability')}
                subtitle={t(
                    '集群告警 / 建议 / 日志导出',
                    'cluster alerts / suggestions / log export'
                )}
                actions={
                    <button
                        type="button"
                        data-testid="obs-export"
                        onClick={exportLogs}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        {t('导出日志', 'Export logs')}
                    </button>
                }
            />
            <div className="mx-4 mt-3 text-xs">
                <div className="text-text-primary">{t('告警', 'Alerts')}</div>
                <AlertsList state={alerts} t={t} />
            </div>
            <div className="mx-4 mt-3 text-xs">
                <div className="text-text-primary">{t('建议', 'Suggestions')}</div>
                <SuggestionsList state={suggestions} t={t} />
            </div>
        </div>
    );
}

function AlertsList(props: {
    state: ReturnType<typeof useAutoRetry<{ alerts?: AlertRow[] }>>;
    t: (zh: string, en: string) => string;
}) {
    const { data, error, loading, attempt, manualRetry } = props.state;
    const t = props.t;
    if (error) {
        return (
            <ErrorState
                message={(error as Error).message}
                onRetry={manualRetry}
                retrying={attempt}
            />
        );
    }
    if (loading && attempt === 0) return <SkeletonLines rows={2} />;
    const rows = data?.alerts ?? [];
    if (rows.length === 0) {
        return <div className="mt-1 text-text-muted">{t('暂无告警', 'no alerts')}</div>;
    }
    return (
        <div data-testid="obs-alerts" className="mt-1 space-y-1.5">
            {rows.map((a, i) => (
                <div
                    key={i}
                    className="flex items-center gap-2 rounded-md border border-border bg-bg-card px-3 py-2"
                >
                    <StatusBadge
                        status={a.level === 'error' ? 'error' : 'warning'}
                        label={a.level ?? 'warn'}
                    />
                    <span className="text-text-primary">{a.message ?? JSON.stringify(a)}</span>
                    {a.node_id ? <span className="text-text-secondary">{a.node_id}</span> : null}
                </div>
            ))}
        </div>
    );
}

function SuggestionsList(props: {
    state: ReturnType<typeof useAutoRetry<{ suggestions?: SuggestionRow[] }>>;
    t: (zh: string, en: string) => string;
}) {
    const { data, error, loading, attempt, manualRetry } = props.state;
    const t = props.t;
    if (error) {
        return (
            <ErrorState
                message={(error as Error).message}
                onRetry={manualRetry}
                retrying={attempt}
            />
        );
    }
    if (loading && attempt === 0) return <SkeletonLines rows={2} />;
    const rows = data?.suggestions ?? [];
    if (rows.length === 0) {
        return <div className="mt-1 text-text-muted">{t('暂无建议', 'no suggestions')}</div>;
    }
    return (
        <div data-testid="obs-suggestions" className="mt-1 space-y-1.5">
            {rows.map((s, i) => (
                <div key={i} className="rounded-md border border-border bg-bg-card px-3 py-2">
                    <div className="text-text-primary">{s.title ?? JSON.stringify(s)}</div>
                    {s.detail ? <div className="mt-0.5 text-text-secondary">{s.detail}</div> : null}
                </div>
            ))}
        </div>
    );
}
