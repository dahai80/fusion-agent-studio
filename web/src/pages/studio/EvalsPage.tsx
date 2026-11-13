import {
    EmptyState,
    ErrorState,
    FormField,
    SkeletonLines,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { useState } from 'react';
import { inspectorApi } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// E2 web surface: eval datasets (eval.listDatasets) + run a dataset against
// a graph (eval.run) with a pass-rate report. Datasets live at
// studio_home()/evals/*.yaml; trace-replay datasets come from eval.fromTrace.

interface DatasetRow {
    name?: string;
    path?: string;
    cases?: number;
    file?: string;
}

interface EvalCaseReport {
    case_id?: string;
    passed?: boolean;
    score?: number;
    error?: string;
}

export function EvalsPage() {
    const t = useT();
    return (
        <div>
            <PageHeader title={t('评测', 'Evals')} />
            <div className="pt-3">
                <EvalRunSection />
            </div>
        </div>
    );
}

function EvalRunSection() {
    const t = useT();
    const { data, error, loading, manualRetry } = useAutoRetry(() => inspectorApi.evalList(), []);
    const [graphId, setGraphId] = useState('');
    const [limit, setLimit] = useState('');
    const [busy, setBusy] = useState(false);
    const [runError, setRunError] = useState('');
    const [report, setReport] = useState<Record<string, unknown> | null>(null);

    const datasets = (data?.datasets ?? []) as DatasetRow[];

    const runEval = async (datasetPath: string) => {
        if (!graphId.trim()) {
            setRunError(t('需要 graph_id', 'graph_id required'));
            return;
        }
        setBusy(true);
        setRunError('');
        setReport(null);
        try {
            const res = await inspectorApi.evalRun({
                dataset_path: datasetPath,
                graph_id: graphId.trim(),
                limit: limit.trim() ? Number(limit.trim()) : undefined,
            });
            setReport(res);
            sdk.toast.show({ level: 'success', message: t('评测完成', 'eval finished') });
        } catch (e) {
            setRunError((e as Error).message || 'eval failed');
        } finally {
            setBusy(false);
        }
    };

    if (loading) {
        return (
            <div className="px-4">
                <SkeletonLines rows={5} />
            </div>
        );
    }
    if (error) {
        return (
            <div className="px-4">
                <ErrorState
                    message={(error as Error).message || 'load failed'}
                    retryLabel={t('重试', 'Retry')}
                    onRetry={manualRetry}
                />
            </div>
        );
    }
    if (!datasets.length) {
        return (
            <div className="px-4">
                <EmptyState
                    title={t('暂无评测数据集', 'no eval datasets')}
                    description={t(
                        '在 studio_home()/evals/ 放置 *.yaml 数据集，或从评审包生成',
                        'drop *.yaml datasets into studio_home()/evals/ or derive one from a reviewer pack'
                    )}
                />
            </div>
        );
    }
    const cases = (report?.cases ?? []) as EvalCaseReport[];
    const passedCount = cases.filter((c) => c.passed).length;
    return (
        <div className="flex flex-col gap-3 px-4">
            <div className="flex flex-wrap items-end gap-2">
                <div className="w-64">
                    <FormField label={t('Graph ID', 'Graph ID')}>
                        <TextInput value={graphId} onChange={setGraphId} placeholder="graph-uuid" />
                    </FormField>
                </div>
                <div className="w-32">
                    <FormField label={t('用例上限', 'Case limit')}>
                        <TextInput value={limit} onChange={setLimit} placeholder="20" />
                    </FormField>
                </div>
            </div>
            <div className="overflow-x-auto">
                <table className="w-full text-left text-xs">
                    <thead className="text-text-secondary">
                        <tr>
                            <th className="py-1.5 pr-3">{t('数据集', 'Dataset')}</th>
                            <th className="py-1.5 pr-3">{t('用例数', 'Cases')}</th>
                            <th className="py-1.5" />
                        </tr>
                    </thead>
                    <tbody>
                        {datasets.map((d, i) => {
                            const path = d.path || d.file || '';
                            return (
                                <tr key={path || i} className="border-t border-border/60">
                                    <td className="py-1.5 pr-3 font-mono text-text-primary">
                                        {d.name || path}
                                    </td>
                                    <td className="py-1.5 pr-3">{d.cases ?? '-'}</td>
                                    <td className="py-1.5">
                                        <button
                                            type="button"
                                            data-testid={`eval-run-${i}`}
                                            disabled={busy}
                                            onClick={() => void runEval(path)}
                                            className="rounded-md border border-border px-3 py-1 text-text-primary hover:bg-bg-muted disabled:opacity-50"
                                        >
                                            {busy ? t('运行中…', 'running…') : t('运行', 'Run')}
                                        </button>
                                    </td>
                                </tr>
                            );
                        })}
                    </tbody>
                </table>
            </div>
            {runError ? <div className="text-xs text-error">{runError}</div> : null}
            {report ? (
                <div
                    data-testid="eval-report"
                    className="rounded-lg border border-border/70 p-3 text-xs"
                >
                    <div className="mb-2 flex items-center gap-3">
                        <span className="font-medium text-text-primary">
                            {t('通过率', 'Pass rate')}
                        </span>
                        <span data-testid="eval-pass-rate" className="font-mono">
                            {passedCount}/{cases.length}
                        </span>
                    </div>
                    <div className="flex flex-col gap-1">
                        {cases.map((c, i) => (
                            <div key={c.case_id || i} className="flex items-center gap-2">
                                <span className={c.passed ? 'text-success' : 'text-error'}>
                                    {c.passed ? '✓' : '✗'}
                                </span>
                                <span className="font-mono text-text-primary">
                                    {c.case_id || i}
                                </span>
                                {c.error ? (
                                    <span className="text-text-secondary">{c.error}</span>
                                ) : null}
                            </div>
                        ))}
                    </div>
                </div>
            ) : null}
        </div>
    );
}
