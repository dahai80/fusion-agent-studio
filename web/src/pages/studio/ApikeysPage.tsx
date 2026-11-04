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
import { isEmbedded, sdk, setStandaloneApiKey, standaloneApiKey } from '../../sdk';

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
    // #584: standalone serving (daemon static host) enforces auth once any
    // key exists, but the embedded shell invariant keeps keys out of the UI.
    // In standalone the operator pastes an admin key here (from
    // `fusion-agent apikeys recover` or the daemon bootstrap log); it is
    // stored in localStorage and attached by sdk.ts on every call.
    const standalone = !isEmbedded();
    const [accessKey, setAccessKey] = useState<string>(() =>
        standalone ? standaloneApiKey() : ''
    );

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
            {standalone ? (
                <div
                    data-testid="standalone-access-key"
                    className="mx-4 mt-2 rounded-md border border-border bg-bg-card px-3 py-2 text-xs"
                >
                    <div className="text-text-primary">
                        {t('本机直连访问密钥', 'Standalone access key')}
                    </div>
                    <div className="mt-1 text-text-secondary">
                        {t(
                            '独立模式（daemon 静态托管）下，写操作需要访问密钥。粘贴管理员密钥（来自 fusion-agent apikeys recover 或 daemon 首次启动日志），保存后本浏览器所有请求自动携带。',
                            'Standalone mode (daemon static host) enforces auth on writes. Paste an admin key (from fusion-agent apikeys recover or the daemon bootstrap log); after saving, every request from this browser carries it.'
                        )}
                    </div>
                    <div className="mt-2 flex items-center gap-2">
                        <input
                            data-testid="standalone-key-input"
                            type="password"
                            value={accessKey}
                            placeholder={t('粘贴访问密钥', 'paste access key')}
                            onChange={(e) => setAccessKey(e.target.value)}
                            className="w-72 rounded-md border border-border bg-bg-elevated px-2 py-1 font-mono text-xs text-text-primary"
                        />
                        <button
                            type="button"
                            data-testid="standalone-key-save"
                            onClick={() => {
                                setStandaloneApiKey(accessKey);
                                setAccessKey(standaloneApiKey());
                                sdk.toast.show({
                                    level: 'success',
                                    message: t(
                                        '访问密钥已保存到本浏览器',
                                        'access key saved to this browser'
                                    ),
                                });
                            }}
                            className="rounded-md border border-border px-3 py-1 text-xs text-text-primary hover:bg-bg-muted"
                        >
                            {t('保存', 'Save')}
                        </button>
                        <button
                            type="button"
                            data-testid="standalone-key-clear"
                            onClick={() => {
                                setStandaloneApiKey('');
                                setAccessKey('');
                                sdk.toast.show({
                                    level: 'success',
                                    message: t('访问密钥已清除', 'access key cleared'),
                                });
                            }}
                            className="rounded-md border border-border px-3 py-1 text-xs text-text-secondary hover:bg-bg-muted"
                        >
                            {t('清除', 'Clear')}
                        </button>
                        <span data-testid="standalone-key-state" className="text-text-muted">
                            {standaloneApiKey()
                                ? t('已配置', 'configured')
                                : t('未配置', 'not configured')}
                        </span>
                    </div>
                </div>
            ) : null}
            {/* #543: bootstrap guidance — the list is masked to the key id
                prefix, which is right, but a first-run user needs the path
                from "I see a prefix" to "I have a working key". */}
            <div
                data-testid="key-bootstrap-hint"
                className="mx-4 mt-2 rounded-md border border-border bg-bg-card px-3 py-2 text-xs text-text-secondary"
            >
                <div className="text-text-primary">
                    {t('如何拿到可用密钥', 'How to get a working key')}
                </div>
                <ol className="mt-1 list-decimal space-y-0.5 pl-4">
                    <li>
                        {t(
                            '点右上角「新建密钥」签发 —— 完整密钥只在创建/轮换时显示一次，请立即保存。',
                            'Create a key above — the full secret is shown once at create/rotate time; save it immediately.'
                        )}
                    </li>
                    <li>
                        {t(
                            '列表里只显示前缀与 ID，用于辨认密钥；丢失的密钥无法找回，只能轮换或重建。',
                            'The list only shows the prefix and id for identification; a lost secret cannot be recovered — rotate or recreate instead.'
                        )}
                    </li>
                    <li>
                        {t(
                            '轮换会立即签发新密钥并吊销旧密钥；吊销前的调用不受影响。',
                            'Rotating issues a new secret and revokes the old one immediately; calls made before the rotation are unaffected.'
                        )}
                    </li>
                    <li>
                        {t(
                            '密钥通过 Authorization: Bearer 调用已发布的 Agent（见下方创建后的调用示例）。',
                            'Call published agents with Authorization: Bearer (see the usage example shown after creation).'
                        )}
                    </li>
                </ol>
            </div>
            {created ? (
                <div className="mx-4 mt-2 rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-xs">
                    <div className="text-text-primary">
                        {t('新密钥（仅显示一次）', 'new key (shown once)')}:
                    </div>
                    <code className="break-all font-mono text-text-secondary">{created}</code>
                    {/* #530: Claude-console-parity usage example — the key
                        authorizes external calls against deployed agents. */}
                    <div className="mt-2 text-text-secondary">
                        {t('外部调用示例', 'external call example')}:
                    </div>
                    <pre className="mt-1 overflow-x-auto rounded bg-bg-muted p-2 font-mono text-[10px] text-text-secondary">
                        {`curl -X POST ${window.location.origin}/v1/agents-platform/agents/<agent_id>/test \\
  -H "Authorization: Bearer ${created}" \\
  -H "content-type: application/json" \\
  -d '{"message": "hello"}'`}
                    </pre>
                    <div className="mt-1 text-text-muted">
                        {t(
                            '轮换后旧密钥立即失效。',
                            'rotating a key invalidates the old secret immediately.'
                        )}
                    </div>
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
