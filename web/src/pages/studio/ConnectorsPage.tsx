import {
    DataTable,
    EmptyState,
    ErrorState,
    FormField,
    SelectInput,
    SkeletonLines,
    StatusBadge,
    SubmitButton,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { JsonEditor, parseJson } from '../../components/ui/JsonEditor';
import { Modal } from '../../components/ui/Modal';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Connectors (ConnectorTabView equivalent): list + create/test/connect/
// disconnect/delete — connector.* RPC.
interface Connector {
    id: string;
    name: string;
    type: string;
    status: string;
    error_message?: string;
}

const STATUS_KIND: Record<string, 'success' | 'error' | 'offline'> = {
    connected: 'success',
    disconnected: 'offline',
    error: 'error',
    failed: 'error',
};

export function ConnectorsPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ connectors: Connector[] }>('connector.list', {}),
        []
    );
    const [showCreate, setShowCreate] = useState(false);
    const [name, setName] = useState('');
    const [ctype, setCtype] = useState('api_key');
    const [authJson, setAuthJson] = useState('{\n  \n}');

    const create = async () => {
        const parsed = parseJson(authJson);
        if (!parsed.ok) {
            sdk.toast.show({ level: 'error', message: `JSON: ${parsed.error}` });
            return;
        }
        try {
            await rpc('connector.create', {
                name: name.trim(),
                type: ctype,
                auth_config: parsed.data,
            });
            // i18n: user-facing
            sdk.toast.show({ level: 'success', message: t('已创建', 'created') });
            setShowCreate(false);
            setName('');
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const act = async (connectorId: string, verb: string, label: string) => {
        try {
            await rpc(`connector.${verb}`, { connector_id: connectorId });
            sdk.toast.show({ level: 'success', message: label });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const rows = data?.connectors ?? [];
    const typeOptions = ['api_key', 'oauth', 'basic_auth', 'bearer'].map((v) => ({
        value: v,
        label: v,
    }));

    return (
        <div>
            <PageHeader
                // i18n: user-facing
                title={t('连接器', 'Connectors')}
                // i18n: user-facing
                subtitle={t(`${rows.length} 个连接器`, `${rows.length} connectors`)}
                actions={
                    <button
                        type="button"
                        onClick={() => setShowCreate(true)}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        // i18n: user-facing
                        {t('新增', 'Add')}
                    </button>
                }
            />
            <div className="px-4 pt-3">
                {loading && attempt === 0 && rows.length === 0 ? (
                    <SkeletonLines rows={3} />
                ) : error ? (
                    <ErrorState
                        message={(error as Error).message}
                        // i18n: user-facing
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                ) : rows.length === 0 ? (
                    <EmptyState
                        // i18n: user-facing
                        title={t('暂无连接器', 'no connectors')}
                        // i18n: user-facing
                        description={t('添加第一个连接器', 'add your first connector')}
                    />
                ) : (
                    <DataTable<Connector>
                        rows={rows}
                        rowKey={(c) => c.id}
                        pageSize={15}
                        columns={[
                            // i18n: user-facing
                            { key: 'name', header: t('名称', 'Name'), render: (c) => c.name },
                            // i18n: user-facing
                            { key: 'type', header: t('类型', 'Type'), render: (c) => c.type },
                            {
                                key: 'status',
                                // i18n: user-facing
                                header: t('状态', 'Status'),
                                render: (c) => (
                                    <StatusBadge
                                        status={STATUS_KIND[c.status] ?? 'offline'}
                                        label={c.status}
                                    />
                                ),
                            },
                            {
                                key: 'actions',
                                header: '',
                                render: (c) => (
                                    <div className="flex gap-2 text-xs">
                                        <button
                                            type="button"
                                            onClick={() =>
                                                // i18n: user-facing
                                                act(c.id, 'test', t('测试通过', 'test ok'))
                                            }
                                            className="text-text-secondary hover:underline"
                                        >
                                            // i18n: user-facing
                                            {t('测试', 'Test')}
                                        </button>
                                        {c.status === 'connected' ? (
                                            <button
                                                type="button"
                                                onClick={() =>
                                                    act(
                                                        c.id,
                                                        'disconnect',
                                                        // i18n: user-facing
                                                        t('已断开', 'disconnected')
                                                    )
                                                }
                                                className="text-warning hover:underline"
                                            >
                                                // i18n: user-facing
                                                {t('断开', 'Disconnect')}
                                            </button>
                                        ) : (
                                            <button
                                                type="button"
                                                onClick={() =>
                                                    // i18n: user-facing
                                                    act(c.id, 'connect', t('已连接', 'connected'))
                                                }
                                                className="text-success hover:underline"
                                            >
                                                // i18n: user-facing
                                                {t('连接', 'Connect')}
                                            </button>
                                        )}
                                        <button
                                            type="button"
                                            onClick={() =>
                                                // i18n: user-facing
                                                act(c.id, 'delete', t('已删除', 'deleted'))
                                            }
                                            className="text-error hover:underline"
                                        >
                                            // i18n: user-facing
                                            {t('删除', 'Delete')}
                                        </button>
                                    </div>
                                ),
                            },
                        ]}
                    />
                )}
            </div>
            <Modal
                open={showCreate}
                // i18n: user-facing
                title={t('新增连接器', 'Add connector')}
                onClose={() => setShowCreate(false)}
                // i18n: user-facing
                footer={<SubmitButton label={t('创建', 'Create')} onAction={create} />}
            >
                // i18n: user-facing
                <FormField label={t('名称', 'Name')} required>
                    <TextInput value={name} onChange={setName} />
                </FormField>
                // i18n: user-facing
                <FormField label={t('类型', 'Type')}>
                    <SelectInput value={ctype} onChange={setCtype} options={typeOptions} />
                </FormField>
                // i18n: user-facing
                <FormField label={t('鉴权配置 (JSON)', 'Auth config (JSON)')}>
                    <JsonEditor value={authJson} onChange={setAuthJson} rows={4} />
                </FormField>
            </Modal>
        </div>
    );
}
