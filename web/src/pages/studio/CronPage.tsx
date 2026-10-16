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
import { formatRelativeTime } from '@fusion/web-ui/format';
import { useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Modal } from '../../components/ui/Modal';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Cron jobs (CronTabView equivalent): list / register / unregister /
// executions — cron.* RPC.
interface CronJob {
    id: string;
    name?: string;
    expression: string;
    graph_id?: string;
    enabled?: boolean;
    next_run?: number;
    last_run?: number;
}

interface CronExecution {
    job_id?: string;
    status?: string;
    started_at?: number;
    finished_at?: number;
    error?: string;
}

export function CronPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ jobs: CronJob[] }>('cron.list', {}),
        []
    );
    const [showCreate, setShowCreate] = useState(false);
    const [name, setName] = useState('');
    const [expression, setExpression] = useState('*/5 * * * *');
    const [graphId, setGraphId] = useState('');
    const [execsFor, setExecsFor] = useState<string | null>(null);
    const [execs, setExecs] = useState<CronExecution[] | null>(null);

    const register = async () => {
        try {
            await rpc('cron.register', {
                name: name.trim(),
                expression: expression.trim(),
                graph_id: graphId.trim(),
                enabled: true,
            });
            sdk.toast.show({ level: 'success', message: t('已注册', 'registered') });
            setShowCreate(false);
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const unregister = async (jobId: string) => {
        try {
            await rpc('cron.unregister', { id: jobId });
            sdk.toast.show({ level: 'success', message: t('已注销', 'unregistered') });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const showExecs = async (jobId: string) => {
        setExecsFor(jobId);
        try {
            const res = await rpc<{ executions: CronExecution[] }>('cron.list_executions', {
                job_id: jobId,
                limit: 20,
            });
            setExecs(res.executions ?? []);
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const rows = data?.jobs ?? [];

    return (
        <div>
            <PageHeader
                title={t('定时任务', 'Cron')}
                subtitle={t(`${rows.length} 个任务`, `${rows.length} jobs`)}
                actions={
                    <button
                        type="button"
                        onClick={() => setShowCreate(true)}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        {t('新建', 'New')}
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
                        title={t('暂无定时任务', 'no cron jobs')}
                        description={t('注册第一个定时任务', 'register your first cron job')}
                    />
                ) : (
                    <DataTable<CronJob>
                        rows={rows}
                        rowKey={(j) => j.id}
                        pageSize={15}
                        columns={[
                            {
                                key: 'name',
                                header: t('名称', 'Name'),
                                render: (j) => j.name || j.id,
                            },
                            {
                                key: 'expression',
                                header: t('表达式', 'Expression'),
                                render: (j) => (
                                    <span className="font-mono text-xs">{j.expression}</span>
                                ),
                            },
                            {
                                key: 'graph_id',
                                header: t('工作流', 'Graph'),
                                render: (j) => j.graph_id || '-',
                            },
                            {
                                key: 'enabled',
                                header: t('启用', 'Enabled'),
                                render: (j) => (j.enabled ? '✓' : '✗'),
                            },
                            {
                                key: 'actions',
                                header: '',
                                render: (j) => (
                                    <div className="flex gap-2 text-xs">
                                        <button
                                            type="button"
                                            onClick={() => showExecs(j.id)}
                                            className="text-text-secondary hover:underline"
                                        >
                                            {t('执行历史', 'Executions')}
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => unregister(j.id)}
                                            className="text-error hover:underline"
                                        >
                                            {t('注销', 'Unregister')}
                                        </button>
                                    </div>
                                ),
                            },
                        ]}
                    />
                )}
            </div>
            {execsFor ? (
                <div className="mx-4 mt-2 rounded-card border border-border bg-bg-elevated p-4">
                    <div className="flex items-center justify-between">
                        <span className="text-sm font-medium">
                            {t('执行历史', 'Executions')}: {execsFor}
                        </span>
                        <button
                            type="button"
                            onClick={() => {
                                setExecsFor(null);
                                setExecs(null);
                            }}
                            className="text-xs text-text-secondary hover:text-text-primary"
                        >
                            ×
                        </button>
                    </div>
                    <div className="mt-2">
                        {execs === null ? (
                            <SkeletonLines rows={2} />
                        ) : execs.length === 0 ? (
                            <EmptyState title={t('暂无执行记录', 'no executions')} />
                        ) : (
                            <DataTable<CronExecution>
                                rows={execs}
                                rowKey={(e) => `${e.job_id ?? ''}-${e.started_at ?? 0}`}
                                pageSize={10}
                                columns={[
                                    {
                                        key: 'status',
                                        header: t('状态', 'Status'),
                                        render: (e) => e.status ?? '-',
                                    },
                                    {
                                        key: 'started_at',
                                        header: t('开始', 'Started'),
                                        render: (e) =>
                                            e.started_at
                                                ? formatRelativeTime(e.started_at * 1000)
                                                : '-',
                                    },
                                    {
                                        key: 'error',
                                        header: t('错误', 'Error'),
                                        render: (e) => (
                                            <span className="line-clamp-1 text-xs text-error">
                                                {e.error ?? '-'}
                                            </span>
                                        ),
                                    },
                                ]}
                            />
                        )}
                    </div>
                </div>
            ) : null}
            <Modal
                open={showCreate}
                title={t('新建定时任务', 'New cron job')}
                onClose={() => setShowCreate(false)}
                footer={<SubmitButton label={t('注册', 'Register')} onAction={register} />}
            >
                <FormField label={t('名称', 'Name')}>
                    <TextInput value={name} onChange={setName} />
                </FormField>
                <FormField label={t('Cron 表达式', 'Cron expression')} required>
                    <TextInput value={expression} onChange={setExpression} />
                </FormField>
                <FormField label={t('工作流 ID', 'Graph ID')}>
                    <TextInput value={graphId} onChange={setGraphId} />
                </FormField>
            </Modal>
        </div>
    );
}
