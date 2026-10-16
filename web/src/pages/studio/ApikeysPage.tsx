import {
    DataTable,
    EmptyState,
    ErrorState,
    FormField,
    SkeletonLines,
    StatusBadge,
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

// API keys (ApikeyTabView equivalent): list / create / rotate / revoke —
// apikey.* RPC.
interface ApiKeyItem {
    id?: string;
    key_id?: string;
    name: string;
    status?: string;
    permissions?: string[];
    created_at?: number;
    expires_at?: number | string;
}

export function ApikeysPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ keys: ApiKeyItem[] }>('apikey.list', {}),
        []
    );
    const [showCreate, setShowCreate] = useState(false);
    const [name, setName] = useState('');
    const [created, setCreated] = useState<string>('');

    const create = async () => {
        if (!name.trim()) return;
        try {
            const res = await rpc<Record<string, unknown>>('apikey.create', {
                name: name.trim(),
            });
            const key = res.key as { secret?: string } | undefined;
            const secret = key?.secret ?? (res.secret as string | undefined) ?? JSON.stringify(res);
            setCreated(secret);
            setShowCreate(false);
            setName('');
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const rotate = async (keyId: string) => {
        try {
            const res = await rpc<Record<string, unknown>>('apikey.rotate', { key_id: keyId });
            const key = res.key as { secret?: string } | undefined;
            const secret = key?.secret ?? (res.secret as string | undefined) ?? JSON.stringify(res);
            setCreated(secret);
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const revoke = async (keyId: string) => {
        try {
            await rpc('apikey.revoke', { key_id: keyId });
            sdk.toast.show({ level: 'success', message: t('已吊销', 'revoked') });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const rows = data?.keys ?? [];

    return (
        <div>
            <PageHeader
                title={t('API 密钥', 'API Keys')}
                subtitle={t(`${rows.length} 个密钥`, `${rows.length} keys`)}
                actions={
                    <button
                        type="button"
                        onClick={() => setShowCreate(true)}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        {t('新建密钥', 'Create key')}
                    </button>
                }
            />
            {created ? (
                <div className="mx-4 mt-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
                    <div className="text-text-primary">
                        {t('新密钥（仅显示一次）', 'new key (shown once)')}:
                    </div>
                    <code className="break-all font-mono text-text-secondary">{created}</code>
                </div>
            ) : null}
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
                        title={t('暂无密钥', 'no keys')}
                        description={t('创建第一个 API 密钥', 'create your first API key')}
                    />
                ) : (
                    <DataTable<ApiKeyItem>
                        rows={rows}
                        rowKey={(k) => k.id ?? k.key_id ?? ''}
                        pageSize={15}
                        columns={[
                            { key: 'name', header: t('名称', 'Name'), render: (k) => k.name },
                            {
                                key: 'key_id',
                                header: t('ID', 'ID'),
                                render: (k) => (
                                    <span className="font-mono text-xs">
                                        {k.id ?? k.key_id ?? ''}
                                    </span>
                                ),
                            },
                            {
                                key: 'status',
                                header: t('状态', 'Status'),
                                render: (k) => (
                                    <StatusBadge
                                        status={k.status === 'revoked' ? 'error' : 'success'}
                                        label={k.status ?? 'active'}
                                    />
                                ),
                            },
                            {
                                key: 'created_at',
                                header: t('创建时间', 'Created'),
                                render: (k) =>
                                    k.created_at ? formatRelativeTime(k.created_at * 1000) : '-',
                            },
                            {
                                key: 'actions',
                                header: '',
                                render: (k) => (
                                    <div className="flex gap-2 text-xs">
                                        <button
                                            type="button"
                                            onClick={() => rotate(k.id ?? k.key_id ?? '')}
                                            className="text-text-secondary hover:underline"
                                        >
                                            {t('轮换', 'Rotate')}
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => revoke(k.id ?? k.key_id ?? '')}
                                            className="text-error hover:underline"
                                        >
                                            {t('吊销', 'Revoke')}
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
                title={t('新建密钥', 'Create API key')}
                onClose={() => setShowCreate(false)}
                footer={<SubmitButton label={t('创建', 'Create')} onAction={create} />}
            >
                <FormField label={t('名称', 'Name')} required>
                    <TextInput value={name} onChange={setName} />
                </FormField>
            </Modal>
        </div>
    );
}
