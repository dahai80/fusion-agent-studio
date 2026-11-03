import { EmptyState, ErrorState, SkeletonLines, SubmitButton } from '@fusion/web-ui';
import { useCallback, useEffect, useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';

// #549: reviewer packs — installable role-based review templates. Select
// packs, paste an artifact, run them in parallel; findings render grouped by
// pack with severity badges. Blind-review toggle controls author-notes
// injection (default on = author reasoning excluded).

interface PackDef {
    key: string;
    name: string;
    description: string;
    blind_review: boolean;
    default_params: Record<string, unknown>;
}

interface Finding {
    severity: string;
    location: string;
    rationale: string;
    recommendation: string;
    pack: string;
}

interface RunResult {
    status: string;
    findings: Finding[];
    per_pack: Record<string, { status: string; finding_count: number; error?: string }>;
    total: number;
}

const SEVERITY_STYLES: Record<string, string> = {
    P0: 'bg-red-100 text-red-700 dark:bg-red-500/15 dark:text-red-400',
    P1: 'bg-orange-100 text-orange-700 dark:bg-orange-500/15 dark:text-orange-400',
    P2: 'bg-amber-100 text-amber-700 dark:bg-amber-500/15 dark:text-amber-400',
    P3: 'bg-zinc-100 text-zinc-600 dark:bg-zinc-500/15 dark:text-zinc-300',
};

export function ReviewPage() {
    const t = useT();
    const [packs, setPacks] = useState<PackDef[] | null>(null);
    const [loadError, setLoadError] = useState('');
    const [selected, setSelected] = useState<Set<string>>(new Set());
    const [artifact, setArtifact] = useState('');
    const [request, setRequest] = useState('');
    const [authorNotes, setAuthorNotes] = useState('');
    const [blind, setBlind] = useState(true);
    const [runError, setRunError] = useState('');
    const [result, setResult] = useState<RunResult | null>(null);

    useEffect(() => {
        rpc<{ packs: PackDef[] }>('reviewer.packs', {})
            .then((res) => {
                setPacks(res.packs);
                setSelected(new Set(res.packs.map((p) => p.key)));
            })
            .catch((e: unknown) => setLoadError(String(e)));
    }, []);

    const run = useCallback(async () => {
        setRunError('');
        setResult(null);
        try {
            const res = await rpc<RunResult>('reviewer.run', {
                packs: [...selected],
                artifact,
                request,
                author_notes: blind ? '' : authorNotes,
                blind,
            });
            setResult(res);
        } catch (e: unknown) {
            setRunError(String(e));
        } finally {
        }
    }, [selected, artifact, request, authorNotes, blind]);

    const toggle = (key: string) => {
        setSelected((prev) => {
            const next = new Set(prev);
            if (next.has(key)) next.delete(key);
            else next.add(key);
            return next;
        });
    };

    return (
        <div>
            <PageHeader title={t('评审包', 'Reviewer Packs')} />
            <div className="pt-3 space-y-3">
                {loadError ? (
                    <ErrorState message={loadError} />
                ) : packs === null ? (
                    <SkeletonLines rows={4} />
                ) : (
                    <div className="rounded-xl border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                        <div className="text-sm font-medium mb-2">
                            {t('选择评审包', 'Select reviewer packs')}
                        </div>
                        <div className="grid gap-2 sm:grid-cols-2" data-testid="pack-list">
                            {packs.map((p) => (
                                <label
                                    key={p.key}
                                    className="flex items-start gap-2 rounded-lg border border-zinc-200/80 p-3 cursor-pointer hover:bg-zinc-50 dark:border-zinc-800 dark:hover:bg-zinc-800/50"
                                >
                                    <input
                                        type="checkbox"
                                        className="mt-1"
                                        checked={selected.has(p.key)}
                                        onChange={() => toggle(p.key)}
                                        data-testid={`pack-${p.key}`}
                                    />
                                    <span>
                                        <span className="block text-sm font-medium">{p.name}</span>
                                        <span className="block text-xs text-zinc-500 dark:text-zinc-400">
                                            {p.description}
                                        </span>
                                    </span>
                                </label>
                            ))}
                        </div>
                        <label
                            className="mt-3 flex items-center gap-2 text-sm"
                            data-testid="blind-toggle"
                        >
                            <input
                                type="checkbox"
                                checked={blind}
                                onChange={(e) => setBlind(e.target.checked)}
                            />
                            {t('盲审模式（排除作者说明）', 'Blind review (exclude author notes)')}
                        </label>
                    </div>
                )}

                <div className="rounded-xl border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900">
                    <div className="space-y-3">
                        <Textarea
                            value={request}
                            onChange={(v) => setRequest(v)}
                            placeholder={t(
                                '原始请求（评审对象的目标）',
                                'Original request (goal of the artifact)'
                            )}
                            rows={3}
                            dataTestId="review-request"
                        />
                        <Textarea
                            value={artifact}
                            onChange={(v) => setArtifact(v)}
                            placeholder={t(
                                '粘贴待评审产物（文档/设计/代码）',
                                'Paste the artifact to review (doc/design/code)'
                            )}
                            rows={10}
                            dataTestId="review-artifact"
                        />
                        {!blind && (
                            <Textarea
                                value={authorNotes}
                                onChange={(v) => setAuthorNotes(v)}
                                placeholder={t(
                                    '作者说明（仅非盲审模式使用）',
                                    'Author notes (non-blind mode only)'
                                )}
                                rows={3}
                                dataTestId="review-author-notes"
                            />
                        )}
                        <SubmitButton
                            onAction={run}
                            disabled={!artifact.trim() || selected.size === 0}
                            label={t('并行运行选中的评审包', 'Run selected packs in parallel')}
                            busyLabel={t('评审中…', 'Reviewing…')}
                        />
                        {runError && <ErrorState message={runError} />}
                    </div>
                </div>

                {result && (
                    <div
                        className="rounded-xl border border-zinc-200/80 bg-white p-4 dark:border-zinc-800 dark:bg-zinc-900"
                        data-testid="review-result"
                    >
                        <div className="text-sm font-medium mb-2">
                            {t('发现', 'Findings')} · {result.total}
                        </div>
                        {result.total === 0 ? (
                            <EmptyState
                                title={t('无发现', 'No findings')}
                                description={t(
                                    '所有选中的评审包均未报告问题。',
                                    'All selected packs reported no issues.'
                                )}
                            />
                        ) : (
                            <div className="space-y-2">
                                {result.findings.map((f, i) => (
                                    <div
                                        key={i}
                                        className="rounded-lg border border-zinc-200/80 p-3 dark:border-zinc-800"
                                        data-testid="finding-row"
                                    >
                                        <div className="flex items-center gap-2">
                                            <span
                                                className={`rounded px-1.5 py-0.5 text-xs font-semibold ${SEVERITY_STYLES[f.severity] ?? SEVERITY_STYLES.P3}`}
                                            >
                                                {f.severity}
                                            </span>
                                            <span className="text-xs text-zinc-500 dark:text-zinc-400">
                                                {f.pack}
                                            </span>
                                            <span className="text-xs text-zinc-400 dark:text-zinc-500 truncate">
                                                {f.location}
                                            </span>
                                        </div>
                                        <div className="mt-1 text-sm">{f.rationale}</div>
                                        <div className="mt-1 text-sm text-zinc-600 dark:text-zinc-300">
                                            {t('建议', 'Recommendation')}: {f.recommendation}
                                        </div>
                                    </div>
                                ))}
                            </div>
                        )}
                        <div
                            className="mt-3 text-xs text-zinc-500 dark:text-zinc-400"
                            data-testid="per-pack-summary"
                        >
                            {Object.entries(result.per_pack)
                                .map(
                                    ([pack, s]) =>
                                        `${pack}: ${s.status === 'ok' ? `${s.finding_count}` : `error: ${s.error}`}`
                                )
                                .join(' · ')}
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
