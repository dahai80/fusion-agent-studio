import {
    DataTable,
    EmptyState,
    ErrorState,
    FormField,
    SkeletonLines,
    SubmitButton,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { parseJson } from '../../components/ui/JsonEditor';
import { Modal } from '../../components/ui/Modal';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Hooks (HookTabView equivalent): list / register / test — hooks.* RPC.
interface HookItem {
    event: string;
    matcher?: string;
    type?: string;
    command?: string;
    timeout?: number;
}

export function HooksPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ hooks: HookItem[] }>('hooks.list', {}),
        []
    );
    const [showCreate, setShowCreate] = useState(false);
    const [event, setEvent] = useState('tool.before');
    const [matcher, setMatcher] = useState('');
    const [command, setCommand] = useState('');
    const [timeout, setTimeout_] = useState('30');
    const [testing, setTesting] = useState<HookItem | null>(null);
    const [testPayload, setTestPayload] = useState('{\n  \n}');
    const [testResult, setTestResult] = useState<string>('');
    const [testBusy, setTestBusy] = useState(false);

    const register = async () => {
        if (!event.trim() || !command.trim()) return;
        try {
            await rpc('hooks.register', {
                event: event.trim(),
                matcher: matcher.trim() || undefined,
                type: 'command',
                command: command.trim(),
                timeout: Number(timeout) || 30,
            });
            sdk.toast.show({ level: 'success', message: t('已注册', 'registered') });
            setShowCreate(false);
            setCommand('');
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const runTest = async () => {
        if (!testing) return;
        const parsed = parseJson(testPayload);
        if (!parsed.ok) {
            sdk.toast.show({ level: 'error', message: `JSON: ${parsed.error}` });
            return;
        }
        setTestBusy(true);
        setTestResult('');
        try {
            const res = await rpc<{ result: unknown }>('hooks.test', {
                event: testing.event,
                payload: parsed.data,
                tool_name: testing.matcher || undefined,
            });
            setTestResult(JSON.stringify(res.result ?? res, null, 2));
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setTestBusy(false);
        }
    };

    const rows = data?.hooks ?? [];

    return (
        <div>
            <PageHeader
                title={t('钩子', 'Hooks')}
                subtitle={t(`${rows.length} 个钩子`, `${rows.length} hooks`)}
                actions={
                    <button
                        type="button"
                        onClick={() => setShowCreate(true)}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        {t('注册钩子', 'Register hook')}
                    </button>
                }
            />
            <div className="px-4 pt-3">
                {loading && attempt === 0 && rows.length === 0 ? (
                    <SkeletonLines rows={3} />
                ) : error ? (
                    <ErrorState
                        message={(error as Error).message}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                ) : rows.length === 0 ? (
                    <EmptyState
                        title={t('暂无钩子', 'no hooks')}
                        description={t('注册第一个钩子', 'register your first hook')}
                    />
                ) : (
                    <DataTable<HookItem>
                        rows={rows}
                        rowKey={(h) => `${h.event}-${h.matcher ?? ''}`}
                        pageSize={15}
                        columns={[
                            { key: 'event', header: t('事件', 'Event'), render: (h) => h.event },
                            {
                                key: 'matcher',
                                header: t('匹配器', 'Matcher'),
                                render: (h) => h.matcher || '-',
                            },
                            {
                                key: 'type',
                                header: t('类型', 'Type'),
                                render: (h) => h.type || '-',
                            },
                            {
                                key: 'command',
                                header: t('命令', 'Command'),
                                render: (h) => (
                                    <span className="line-clamp-1 font-mono text-xs">
                                        {h.command ?? '-'}
                                    </span>
                                ),
                            },
                            {
                                key: 'actions',
                                header: '',
                                render: (h) => (
                                    <button
                                        type="button"
                                        onClick={() => {
                                            setTesting(h);
                                            setTestResult('');
                                        }}
                                        className="text-xs text-text-secondary hover:underline"
                                    >
                                        {t('测试', 'Test')}
                                    </button>
                                ),
                            },
                        ]}
                    />
                )}
            </div>
            <Modal
                open={testing !== null}
                title={t('测试钩子', 'Test hook')}
                onClose={() => setTesting(null)}
                footer={
                    <SubmitButton
                        label={t('执行测试', 'Run test')}
                        onAction={runTest}
                        disabled={testBusy}
                    />
                }
            >
                <FormField label={t('事件', 'Event')}>
                    <TextInput value={testing?.event ?? ''} onChange={() => undefined} />
                </FormField>
                <FormField label={t('负载 (JSON)', 'Payload (JSON)')}>
                    <Textarea value={testPayload} onChange={setTestPayload} rows={4} mono />
                </FormField>
                {testResult ? (
                    <pre className="mt-2 max-h-40 overflow-auto rounded-md bg-bg-muted p-2 font-mono text-xs">
                        {testResult}
                    </pre>
                ) : null}
            </Modal>
            <Modal
                open={showCreate}
                title={t('注册钩子', 'Register hook')}
                onClose={() => setShowCreate(false)}
                footer={<SubmitButton label={t('注册', 'Register')} onAction={register} />}
            >
                <FormField label={t('事件', 'Event')} required>
                    <TextInput value={event} onChange={setEvent} />
                </FormField>
                <FormField label={t('匹配器', 'Matcher')}>
                    <TextInput value={matcher} onChange={setMatcher} />
                </FormField>
                <FormField label={t('命令', 'Command')} required>
                    <TextInput value={command} onChange={setCommand} />
                </FormField>
                <FormField label={t('超时 (秒)', 'Timeout (s)')}>
                    <TextInput value={timeout} onChange={setTimeout_} type="number" />
                </FormField>
            </Modal>
        </div>
    );
}
