import {
    DataTable,
    EmptyState,
    ErrorState,
    FormField,
    SelectInput,
    SkeletonLines,
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

// Tools (tool registry): list + dynamic register/unregister — tool.* RPC.
interface ToolInfo {
    name: string;
    description?: string;
    parameters?: Record<string, unknown>;
    category?: string;
    enabled?: boolean;
}

const EMPTY_SCHEMA = '{\n  "type": "object",\n  "properties": {},\n  "required": []\n}';

export function ToolsPage() {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ tools: ToolInfo[] }>('tool.list', {}),
        []
    );
    const [showRegister, setShowRegister] = useState(false);
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [category, setCategory] = useState('custom');
    const [schemaJson, setSchemaJson] = useState(EMPTY_SCHEMA);

    const register = async () => {
        const parsed = parseJson(schemaJson);
        if (!parsed.ok) {
            sdk.toast.show({ level: 'error', message: `JSON: ${parsed.error}` });
            return;
        }
        try {
            await rpc('tool.dynamic_register', {
                name: name.trim(),
                description: description.trim(),
                parameters: parsed.data,
                category,
            });
            sdk.toast.show({ level: 'success', message: t('已注册', 'registered') });
            setShowRegister(false);
            setName('');
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const unregister = async (toolName: string) => {
        try {
            await rpc('tool.dynamic_unregister', { name: toolName });
            sdk.toast.show({ level: 'success', message: t('已注销', 'unregistered') });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const rows = data?.tools ?? [];
    const categoryOptions = ['custom', 'dynamic', 'builtin'].map((v) => ({
        value: v,
        label: v,
    }));

    return (
        <div>
            <PageHeader
                title={t('工具', 'Tools')}
                subtitle={t(`${rows.length} 个工具`, `${rows.length} tools`)}
                actions={
                    <button
                        type="button"
                        onClick={() => setShowRegister(true)}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        {t('注册工具', 'Register tool')}
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
                        title={t('暂无工具', 'no tools')}
                        description={t('注册第一个工具', 'register your first tool')}
                    />
                ) : (
                    <DataTable<ToolInfo>
                        rows={rows}
                        rowKey={(tool) => tool.name}
                        pageSize={15}
                        columns={[
                            { key: 'name', header: t('名称', 'Name'), render: (tool) => tool.name },
                            {
                                key: 'description',
                                header: t('描述', 'Description'),
                                render: (tool) => (
                                    <span className="line-clamp-2">{tool.description ?? '-'}</span>
                                ),
                            },
                            {
                                key: 'category',
                                header: t('分类', 'Category'),
                                render: (tool) => tool.category ?? '-',
                            },
                            {
                                key: 'enabled',
                                header: t('启用', 'Enabled'),
                                render: (tool) => (tool.enabled ? '✓' : '✗'),
                            },
                            {
                                key: 'actions',
                                header: '',
                                render: (tool) => (
                                    <div className="flex gap-2 text-xs">
                                        <button
                                            type="button"
                                            onClick={() => unregister(tool.name)}
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
            <Modal
                open={showRegister}
                title={t('注册工具', 'Register tool')}
                onClose={() => setShowRegister(false)}
                footer={<SubmitButton label={t('注册', 'Register')} onAction={register} />}
            >
                <FormField label={t('名称', 'Name')} required>
                    <TextInput value={name} onChange={setName} />
                </FormField>
                <FormField label={t('描述', 'Description')}>
                    <TextInput value={description} onChange={setDescription} />
                </FormField>
                <FormField label={t('分类', 'Category')}>
                    <SelectInput
                        value={category}
                        onChange={setCategory}
                        options={categoryOptions}
                    />
                </FormField>
                <FormField label={t('参数模式 (JSON)', 'Parameters schema (JSON)')}>
                    <JsonEditor value={schemaJson} onChange={setSchemaJson} rows={6} />
                </FormField>
            </Modal>
        </div>
    );
}
