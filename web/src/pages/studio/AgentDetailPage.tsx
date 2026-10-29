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
import { Modal } from '../../components/ui/Modal';
import { Tabs } from '../../components/ui/Tabs';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Agent detail (agent.get / soul / skills / versions / test — agent.* RPC).
// Each tab section fetches on mount via useAutoRetry keyed by agentId.
interface AgentInfo {
    agent_id?: string;
    name?: string;
    status?: string;
    description?: string;
    [key: string]: unknown;
}

interface AgentSkill {
    name?: string;
    description?: string;
}

interface AgentVersion {
    version?: string;
    created_at?: number;
    note?: string;
}

const TABS = ['soul', 'skills', 'versions', 'test', 'persona'] as const;

export function AgentDetailPage({ agentId }: { agentId: string }) {
    const t = useT();
    const [tab, setTab] = useState<string>('soul');
    const { data, error, loading, attempt, manualRetry } = useAutoRetry<
        { agent: AgentInfo } | AgentInfo
    >(() => rpc<{ agent: AgentInfo } | AgentInfo>('agent.get', { agent_id: agentId }), [agentId]);
    const a = ((data as { agent?: AgentInfo })?.agent ?? (data as AgentInfo) ?? {}) as AgentInfo;
    const status = typeof a.status === 'string' ? a.status : '';

    const publish = async () => {
        try {
            await rpc(status === 'published' ? 'agent.unpublish' : 'agent.publish', {
                agent_id: agentId,
            });
            sdk.toast.show({
                level: 'success',
                message:
                    status === 'published'
                        ? t('已取消发布', 'unpublished')
                        : t('已发布', 'published'),
            });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    return (
        <div>
            <PageHeader
                title={a.name ?? t('加载中…', 'Loading…')}
                subtitle={agentId}
                actions={
                    <div className="flex gap-2">
                        <button
                            type="button"
                            onClick={() => {
                                window.location.hash = '#/studio/agents';
                            }}
                            className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                        >
                            {t('返回', 'Back')}
                        </button>
                        <button
                            type="button"
                            onClick={publish}
                            className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                        >
                            {status === 'published'
                                ? t('取消发布', 'Unpublish')
                                : t('发布', 'Publish')}
                        </button>
                    </div>
                }
            />
            {loading && attempt === 0 && !a.name ? (
                <div className="px-4 pt-3">
                    <SkeletonLines rows={3} />
                </div>
            ) : error ? (
                <div className="px-4 pt-3">
                    <ErrorState
                        message={error.message}
                        retryLabel={t('重试', 'Retry')}
                        onRetry={manualRetry}
                        retrying={attempt}
                    />
                </div>
            ) : (
                <>
                    <Tabs
                        tabs={TABS.map((k) => ({
                            key: k,
                            label:
                                k === 'soul'
                                    ? t('灵魂', 'Soul')
                                    : k === 'skills'
                                      ? t('技能', 'Skills')
                                      : k === 'versions'
                                        ? t('版本', 'Versions')
                                        : k === 'persona'
                                          ? t('人设', 'Persona')
                                          : t('测试', 'Test'),
                        }))}
                        active={tab}
                        onChange={setTab}
                    />
                    <div className="pt-3">
                        {tab === 'soul' ? <SoulSection agentId={agentId} /> : null}
                        {tab === 'skills' ? <SkillsSection agentId={agentId} /> : null}
                        {tab === 'versions' ? <VersionsSection agentId={agentId} /> : null}
                        {tab === 'test' ? <TestSection agentId={agentId} /> : null}
                        {tab === 'persona' ? <PersonaSection agentId={agentId} /> : null}
                    </div>
                </>
            )}
        </div>
    );
}

function SoulSection({ agentId }: { agentId: string }) {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry<{ soul: string }>(
        () => rpc<{ soul: string }>('agent.get_soul', { agent_id: agentId }),
        [agentId]
    );
    const [soul, setSoul] = useState<string | null>(null);
    const remote = data?.soul ?? '';
    const value = soul ?? remote;

    const save = async () => {
        try {
            await rpc('agent.update_soul', { agent_id: agentId, soul: value });
            sdk.toast.show({ level: 'success', message: t('已保存', 'saved') });
            setSoul(null);
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    if (loading && attempt === 0) {
        return (
            <div className="px-4">
                <SkeletonLines rows={6} />
            </div>
        );
    }
    if (error) {
        return (
            <div className="px-4">
                <ErrorState
                    message={error.message}
                    retryLabel={t('重试', 'Retry')}
                    onRetry={manualRetry}
                    retrying={attempt}
                />
            </div>
        );
    }
    return (
        <div className="flex flex-col gap-3 px-4">
            <FormField label={t('灵魂 (Markdown)', 'Soul (Markdown)')}>
                <Textarea value={value} onChange={setSoul} rows={14} mono />
            </FormField>
            <div>
                <SubmitButton label={t('保存灵魂', 'Save soul')} onAction={save} />
            </div>
        </div>
    );
}

function SkillsSection({ agentId }: { agentId: string }) {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry<{ skills: AgentSkill[] }>(
        () => rpc<{ skills: AgentSkill[] }>('agent.list_skills', { agent_id: agentId }),
        [agentId]
    );
    const [showAdd, setShowAdd] = useState(false);
    const [name, setName] = useState('');
    const [content, setContent] = useState('');
    const rows = data?.skills ?? [];

    const add = async () => {
        if (!name.trim()) return;
        try {
            await rpc('agent.add_skill', { agent_id: agentId, name: name.trim(), content });
            sdk.toast.show({ level: 'success', message: t('技能已添加', 'skill added') });
            setShowAdd(false);
            setName('');
            setContent('');
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const del = async (skillName: string) => {
        try {
            await rpc('agent.delete_skill', { agent_id: agentId, skill_name: skillName });
            sdk.toast.show({ level: 'success', message: t('已删除', 'deleted') });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    return (
        <div className="px-4">
            <div className="mb-2 flex justify-end">
                <button
                    type="button"
                    onClick={() => setShowAdd(true)}
                    className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                >
                    {t('新增技能', 'Add skill')}
                </button>
            </div>
            {loading && attempt === 0 && rows.length === 0 ? (
                <SkeletonLines rows={4} />
            ) : error ? (
                <ErrorState
                    message={error.message}
                    retryLabel={t('重试', 'Retry')}
                    onRetry={manualRetry}
                    retrying={attempt}
                />
            ) : rows.length === 0 ? (
                <EmptyState
                    title={t('暂无技能', 'no skills')}
                    description={t('添加第一个技能', 'add your first skill')}
                />
            ) : (
                <DataTable<AgentSkill>
                    rows={rows}
                    rowKey={(s) => s.name ?? `skill-${rows.indexOf(s)}`}
                    pageSize={15}
                    columns={[
                        {
                            key: 'name',
                            header: t('名称', 'Name'),
                            render: (s) => (
                                <span className="font-mono text-xs">{s.name ?? '-'}</span>
                            ),
                        },
                        {
                            key: 'description',
                            header: t('描述', 'Description'),
                            render: (s) => (
                                <span className="line-clamp-2 text-sm">{s.description ?? '-'}</span>
                            ),
                        },
                        {
                            key: 'actions',
                            header: '',
                            render: (s) =>
                                s.name ? (
                                    <button
                                        type="button"
                                        onClick={() => del(s.name as string)}
                                        className="text-xs text-error hover:underline"
                                    >
                                        {t('删除', 'Delete')}
                                    </button>
                                ) : null,
                        },
                    ]}
                />
            )}
            <Modal
                open={showAdd}
                title={t('新增技能', 'Add skill')}
                onClose={() => setShowAdd(false)}
                footer={<SubmitButton label={t('添加', 'Add')} onAction={add} />}
            >
                <FormField label={t('名称', 'Name')} required>
                    <TextInput value={name} onChange={setName} />
                </FormField>
                <FormField label={t('内容', 'Content')}>
                    <Textarea value={content} onChange={setContent} rows={8} mono />
                </FormField>
            </Modal>
        </div>
    );
}

function VersionsSection({ agentId }: { agentId: string }) {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry<{
        versions: AgentVersion[];
    }>(() => rpc<{ versions: AgentVersion[] }>('agent.versions', { agent_id: agentId }), [agentId]);
    const rows = data?.versions ?? [];

    if (loading && attempt === 0 && rows.length === 0) {
        return (
            <div className="px-4">
                <SkeletonLines rows={4} />
            </div>
        );
    }
    if (error) {
        return (
            <div className="px-4">
                <ErrorState
                    message={error.message}
                    retryLabel={t('重试', 'Retry')}
                    onRetry={manualRetry}
                    retrying={attempt}
                />
            </div>
        );
    }
    if (rows.length === 0) {
        return (
            <div className="px-4">
                <EmptyState title={t('暂无版本', 'no versions')} />
            </div>
        );
    }
    return (
        <div className="px-4">
            <DataTable<AgentVersion>
                rows={rows}
                rowKey={(v) => v.version ?? `v-${rows.indexOf(v)}`}
                pageSize={15}
                columns={[
                    {
                        key: 'version',
                        header: t('版本', 'Version'),
                        render: (v) => (
                            <span className="font-mono text-xs">{v.version ?? '-'}</span>
                        ),
                    },
                    {
                        key: 'created_at',
                        header: t('创建时间', 'Created'),
                        render: (v) =>
                            v.created_at ? new Date(v.created_at * 1000).toLocaleString() : '-',
                    },
                    {
                        key: 'note',
                        header: t('备注', 'Note'),
                        render: (v) => (
                            <span className="line-clamp-2 text-sm">{v.note ?? '-'}</span>
                        ),
                    },
                ]}
            />
        </div>
    );
}

function TestSection({ agentId }: { agentId: string }) {
    const t = useT();
    const [input, setInput] = useState('');
    const [result, setResult] = useState<unknown>(null);
    const [busy, setBusy] = useState(false);

    const run = async () => {
        setBusy(true);
        setResult(null);
        try {
            const res = await rpc<{ result: unknown }>('agent.test', {
                agent_id: agentId,
                input: input,
            });
            setResult(res.result ?? res);
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setBusy(false);
        }
    };

    return (
        <div className="flex flex-col gap-3 px-4">
            <FormField label={t('输入', 'Input')}>
                <Textarea value={input} onChange={setInput} rows={6} />
            </FormField>
            <div>
                <SubmitButton label={t('运行测试', 'Run test')} onAction={run} disabled={busy} />
            </div>
            {result !== null ? (
                <div className="rounded-card border border-border bg-bg-elevated p-4">
                    <pre className="overflow-x-auto font-mono text-xs text-text-secondary">
                        {JSON.stringify(result, null, 2)}
                    </pre>
                </div>
            ) : null}
        </div>
    );
}

interface PersonaResolve {
    layers_applied?: string[];
    conflicts?: { section: string; winner: string; loser: string }[];
    declarations?: { rules: string[]; memory_scope: string; skills: string[] };
}

function PersonaSection({ agentId }: { agentId: string }) {
    const t = useT();
    const { data, error, loading, attempt, manualRetry } = useAutoRetry<PersonaResolve>(
        () => rpc<PersonaResolve>('agent.resolve_persona', { agent_id: agentId }),
        [agentId]
    );
    const [layer, setLayer] = useState<'global' | 'tenant' | 'team'>('global');
    const [name, setName] = useState('');
    const [files, setFiles] = useState<string[]>([]);
    const [selected, setSelected] = useState('');
    const [content, setContent] = useState('');
    const [newPath, setNewPath] = useState('');
    const [saving, setSaving] = useState(false);

    const loadFiles = async (l: string, n: string) => {
        try {
            const res = await rpc<{ files: string[] }>('agent.list_persona_files', {
                layer: l,
                name: n,
            });
            setFiles(res.files ?? []);
            setSelected('');
            setContent('');
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const open = async (p: string) => {
        try {
            const res = await rpc<{ content: string }>('agent.get_persona_file', {
                layer,
                name,
                path: p,
            });
            setSelected(p);
            setContent(res.content ?? '');
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const save = async () => {
        if (!selected) return;
        setSaving(true);
        try {
            await rpc('agent.set_persona_file', { layer, name, path: selected, content });
            sdk.toast.show({ level: 'success', message: t('已保存', 'saved') });
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setSaving(false);
        }
    };

    const createFile = async () => {
        const p = newPath.trim();
        if (!p) return;
        (await rpc('agent.set_persona_file', { layer, name, path: p, content: '' })) as unknown;
        setNewPath('');
        await loadFiles(layer, name);
        await open(p);
    };
    if (loading && attempt === 0) {
        return (
            <div className="px-4">
                <SkeletonLines rows={6} />
            </div>
        );
    }
    if (error) {
        return (
            <div className="px-4">
                <ErrorState
                    message={error.message}
                    retryLabel={t('重试', 'Retry')}
                    onRetry={manualRetry}
                    retrying={attempt}
                />
            </div>
        );
    }
    const decls = data?.declarations ?? { rules: [], memory_scope: '', skills: [] };
    const conflicts = data?.conflicts ?? [];
    return (
        <div className="flex flex-col gap-4 px-4 pb-4">
            <div className="rounded-card border border-border bg-bg-elevated p-4">
                <div className="text-sm font-medium text-text-primary">
                    {t('生效人设', 'Effective persona')}
                </div>
                <div className="mt-1 text-xs text-text-muted">
                    {t('应用层', 'Layers')}: {(data?.layers_applied ?? []).join(' → ') || '-'}
                </div>
                <div className="mt-2 text-xs text-text-muted">
                    {t('规则', 'Rules')}: {decls.rules.length ? decls.rules.join('; ') : '-'}
                </div>
                <div className="mt-1 text-xs text-text-muted">
                    {t('记忆范围', 'Memory scope')}: {decls.memory_scope || '-'}
                </div>
                <div className="mt-1 text-xs text-text-muted">
                    {t('技能', 'Skills')}: {decls.skills.length ? decls.skills.join('; ') : '-'}
                </div>
                <div className="mt-2">
                    {conflicts.length === 0 ? (
                        <span className="text-xs text-text-muted">
                            {t('无冲突', 'No conflicts')}
                        </span>
                    ) : (
                        <ul className="list-disc pl-4 text-xs text-warning">
                            {conflicts.map((c, i) => (
                                <li key={i}>
                                    {c.section}: {c.winner} ← {c.loser}
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            </div>

            <div className="rounded-card border border-border bg-bg-elevated p-4">
                <div className="text-sm font-medium text-text-primary">
                    {t('层级文件', 'Layer files')}
                </div>
                <div className="mt-2 flex flex-wrap items-center gap-2">
                    {(['global', 'tenant', 'team'] as const).map((l) => (
                        <button
                            key={l}
                            type="button"
                            onClick={() => {
                                setLayer(l);
                                loadFiles(l, name);
                            }}
                            className={`rounded-md border px-3 py-1.5 text-xs ${
                                layer === l
                                    ? 'border-brand bg-brand/10 text-text-primary'
                                    : 'border-border text-text-primary hover:bg-bg-muted'
                            }`}
                        >
                            {l}
                        </button>
                    ))}
                    <input
                        value={name}
                        onChange={(e) => setName(e.target.value)}
                        placeholder={t('租户/团队名', 'tenant/team name')}
                        className="w-40 rounded-md border border-border bg-bg px-2 py-1.5 text-xs text-text-primary focus:outline-none"
                    />
                    <button
                        type="button"
                        onClick={() => loadFiles(layer, name)}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        {t('加载', 'Load')}
                    </button>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                    {files.length === 0 ? (
                        <span className="text-xs text-text-muted">-</span>
                    ) : (
                        files.map((f) => (
                            <button
                                key={f}
                                type="button"
                                onClick={() => open(f)}
                                className={`rounded-md border px-2 py-1 font-mono text-xs ${
                                    selected === f
                                        ? 'border-brand bg-brand/10 text-text-primary'
                                        : 'border-border text-text-secondary hover:bg-bg-muted'
                                }`}
                            >
                                {f}
                            </button>
                        ))
                    )}
                </div>
                <div className="mt-3 flex items-center gap-2">
                    <input
                        value={newPath}
                        onChange={(e) => setNewPath(e.target.value)}
                        placeholder={t(
                            '新文件路径 (persona/tone.md)',
                            'new file path (persona/tone.md)'
                        )}
                        className="w-64 rounded-md border border-border bg-bg px-2 py-1.5 text-xs text-text-primary focus:outline-none"
                    />
                    <button
                        type="button"
                        onClick={createFile}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        {t('新建', 'Create')}
                    </button>
                </div>
                {selected ? (
                    <div className="mt-3">
                        <FormField label={selected}>
                            <Textarea value={content} onChange={setContent} rows={12} mono />
                        </FormField>
                        <div className="mt-2">
                            <SubmitButton
                                label={t('保存文件', 'Save file')}
                                onAction={save}
                                disabled={saving}
                            />
                        </div>
                    </div>
                ) : null}
            </div>
        </div>
    );
}
