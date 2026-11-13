import { ErrorState, FormField, SkeletonLines, SubmitButton, TextInput } from '@fusion/web-ui';
import { useState } from 'react';
import { inspectorApi } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';

// E6 web surface: prompt playground — edit system/prompt/params, replay
// against the local model, and diff the output against a baseline (from a
// trace replay or the previous replay). Local-only inference, zero egress.

interface ReplayResult {
    output: string;
    duration_ms?: number;
    baseline_output?: string;
    diff?: { changed: boolean; unified: string };
    trace_id?: string;
    error?: string;
    status?: string;
    message?: string;
}

export function PlaygroundPage() {
    const t = useT();
    return (
        <div>
            <PageHeader
                title={t('Playground', 'Playground')}
                subtitle={t('本地模型提示词试验场', 'local-model prompt lab')}
            />
            <div className="pt-3">
                <PlaygroundSection />
            </div>
        </div>
    );
}

function PlaygroundSection() {
    const t = useT();
    const [mode, setMode] = useState<'direct' | 'trace'>('direct');
    const [traceId, setTraceId] = useState('');
    const [prompt, setPrompt] = useState('');
    const [system, setSystem] = useState('');
    const [temperature, setTemperature] = useState('0.7');
    const [maxTokens, setMaxTokens] = useState('1024');
    const [busy, setBusy] = useState(false);
    const [error, setError] = useState('');
    const [result, setResult] = useState<ReplayResult | null>(null);
    const [baseline, setBaseline] = useState('');

    const run = async () => {
        setBusy(true);
        setError('');
        try {
            let res: ReplayResult;
            if (mode === 'trace') {
                if (!traceId.trim()) {
                    setError(t('需要 trace_id', 'trace_id required'));
                    setBusy(false);
                    return;
                }
                res = await inspectorApi.playgroundFromTrace({
                    trace_id: traceId.trim(),
                    system,
                    prompt: prompt,
                    params: {
                        temperature: Number(temperature) || 0.7,
                        max_tokens: Number(maxTokens) || 1024,
                    },
                });
            } else {
                if (!prompt.trim()) {
                    setError(t('需要提示词', 'prompt required'));
                    setBusy(false);
                    return;
                }
                res = await inspectorApi.playgroundReplay({
                    messages: [{ role: 'user', content: prompt }],
                    system,
                    params: {
                        temperature: Number(temperature) || 0.7,
                        max_tokens: Number(maxTokens) || 1024,
                    },
                });
            }
            if (res.error || res.status === 'error') {
                setError(res.error || res.message || 'replay failed');
            } else {
                setResult(res);
                if (res.baseline_output) setBaseline(res.baseline_output);
            }
        } catch (e) {
            setError((e as Error).message || 'replay failed');
        } finally {
            setBusy(false);
        }
    };

    const diffText =
        result?.diff?.unified ||
        (result && baseline && baseline !== result.output
            ? t('输出与基线不同（无 diff 详情）', 'output differs from baseline (no diff detail)')
            : '');

    return (
        <div className="flex flex-col gap-3 px-4">
            <div className="flex items-center gap-2 text-xs">
                <button
                    type="button"
                    data-testid="mode-direct"
                    onClick={() => setMode('direct')}
                    className={`rounded-md px-3 py-1.5 ${mode === 'direct' ? 'bg-bg-muted font-medium text-text-primary' : 'text-text-secondary hover:bg-bg-muted/60'}`}
                >
                    {t('直接提示词', 'Direct prompt')}
                </button>
                <button
                    type="button"
                    data-testid="mode-trace"
                    onClick={() => setMode('trace')}
                    className={`rounded-md px-3 py-1.5 ${mode === 'trace' ? 'bg-bg-muted font-medium text-text-primary' : 'text-text-secondary hover:bg-bg-muted/60'}`}
                >
                    {t('从 Trace 重放', 'From trace')}
                </button>
            </div>
            {mode === 'trace' ? (
                <div className="w-80">
                    <FormField label={t('Trace ID', 'Trace ID')}>
                        <TextInput value={traceId} onChange={setTraceId} placeholder="trace-…" />
                    </FormField>
                </div>
            ) : null}
            <FormField label={t('System', 'System')}>
                <Textarea value={system} onChange={setSystem} rows={3} />
            </FormField>
            <FormField
                label={
                    mode === 'trace'
                        ? t('改写最新用户输入（可选）', 'Override last user input (optional)')
                        : t('提示词', 'Prompt')
                }
            >
                <Textarea value={prompt} onChange={setPrompt} rows={5} />
            </FormField>
            <div className="flex items-end gap-2">
                <div className="w-32">
                    <FormField label={t('温度', 'Temperature')}>
                        <TextInput value={temperature} onChange={setTemperature} />
                    </FormField>
                </div>
                <div className="w-32">
                    <FormField label={t('最大 Token', 'Max tokens')}>
                        <TextInput value={maxTokens} onChange={setMaxTokens} />
                    </FormField>
                </div>
                <SubmitButton
                    label={t('重放', 'Replay')}
                    busyLabel={t('重放中…', 'replaying…')}
                    disabled={busy}
                    onAction={() => run()}
                />
            </div>
            {error ? (
                <ErrorState
                    message={error}
                    retryLabel={t('重试', 'Retry')}
                    onRetry={() => void run()}
                />
            ) : null}
            {busy && !result ? <SkeletonLines rows={3} /> : null}
            {result ? (
                <div data-testid="playground-result" className="flex flex-col gap-2 text-xs">
                    <div className="flex items-center gap-3">
                        <span className="font-medium text-text-primary">{t('输出', 'Output')}</span>
                        {result.duration_ms ? (
                            <span className="text-text-secondary">{result.duration_ms}ms</span>
                        ) : null}
                        {result.diff ? (
                            <span
                                data-testid="playground-diff-flag"
                                className={result.diff.changed ? 'text-warning' : 'text-success'}
                            >
                                {result.diff.changed
                                    ? t('与基线不同', 'differs from baseline')
                                    : t('与基线一致', 'matches baseline')}
                            </span>
                        ) : null}
                    </div>
                    <pre className="max-h-72 overflow-auto rounded-lg border border-border/70 bg-bg-elevated p-3 whitespace-pre-wrap text-text-primary">
                        {result.output}
                    </pre>
                    {baseline ? (
                        <div>
                            <div className="mb-1 text-text-secondary">{t('基线', 'Baseline')}</div>
                            <pre className="max-h-40 overflow-auto rounded-lg border border-border/70 bg-bg-elevated p-3 whitespace-pre-wrap text-text-secondary">
                                {baseline}
                            </pre>
                        </div>
                    ) : null}
                    {diffText ? (
                        <pre
                            data-testid="playground-diff"
                            className="max-h-48 overflow-auto rounded-lg border border-border/70 bg-bg-elevated p-3 font-mono whitespace-pre-wrap"
                        >
                            {diffText}
                        </pre>
                    ) : null}
                </div>
            ) : null}
        </div>
    );
}
