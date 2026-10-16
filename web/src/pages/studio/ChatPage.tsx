import {
    EmptyState,
    ErrorState,
    SkeletonLines,
    SubmitButton,
    TextInput,
    useAutoRetry,
} from '@fusion/web-ui';
import { formatRelativeTime } from '@fusion/web-ui/format';
import { useCallback, useEffect, useState } from 'react';
import { rpc } from '../../api';
import { PageHeader } from '../../components/PageHeader';
import { Modal } from '../../components/ui/Modal';
import { Textarea } from '../../components/ui/Textarea';
import { useT } from '../../i18n';
import { sdk } from '../../sdk';

// Chat console (macOS ChatView equivalent): sessions list + sync-RPC thread.
// Backend SSOT via chat.* RPC — no streaming here, send is synchronous.
// Contract notes (agent_runtime/dispatchers/chat.py + chat_engine.py):
// - chat.list -> {sessions} (ChatSession.to_dict: id/title/updated_at)
// - chat.history -> get_message_tree: {nodes|messages, active_branch}
// - chat.send -> {events, content}; image-only vision error carries {status:'error'}
interface SessionItem {
    session_id?: string;
    id?: string;
    title?: string;
    updated_at?: number;
}

interface ThreadMessage {
    role?: string;
    content?: string;
    created_at?: number;
}

interface HistoryResponse {
    messages?: Array<ThreadMessage & { content_preview?: string }>;
    nodes?: Array<ThreadMessage & { content_preview?: string }>;
}

export function ChatPage() {
    const t = useT();
    const [selected, setSelected] = useState('');
    const [messages, setMessages] = useState<ThreadMessage[]>([]);
    const [threadLoading, setThreadLoading] = useState(false);
    const [threadError, setThreadError] = useState<string | null>(null);
    const [draft, setDraft] = useState('');
    const [sending, setSending] = useState(false);
    const [showCreate, setShowCreate] = useState(false);
    const [newTitle, setNewTitle] = useState('');

    const { data, error, loading, attempt, manualRetry } = useAutoRetry(
        () => rpc<{ sessions?: SessionItem[] }>('chat.list'),
        []
    );

    const sessions = data?.sessions ?? [];
    const current = sessions.find((s) => (s.session_id ?? s.id ?? '') === selected);

    const loadHistory = useCallback(async (sessionId: string) => {
        if (!sessionId) {
            setMessages([]);
            setThreadError(null);
            return;
        }
        setThreadLoading(true);
        setThreadError(null);
        try {
            const res = await rpc<HistoryResponse>('chat.history', {
                session_id: sessionId,
                limit: 50,
            });
            const raw = res.messages ?? res.nodes ?? [];
            setMessages(
                raw.map((m) => ({
                    role: m.role,
                    content: m.content ?? m.content_preview ?? '',
                    created_at: m.created_at,
                }))
            );
        } catch (e) {
            setMessages([]);
            setThreadError((e as Error).message);
        } finally {
            setThreadLoading(false);
        }
    }, []);

    useEffect(() => {
        if (selected) loadHistory(selected);
    }, [selected, loadHistory]);

    const send = async () => {
        const content = draft.trim();
        if (!content || !selected || sending) return;
        setSending(true);
        try {
            await rpc('chat.send', { session_id: selected, content });
            setDraft('');
            await loadHistory(selected);
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        } finally {
            setSending(false);
        }
    };

    const createSession = async () => {
        try {
            const session = await rpc<{ session_id?: string; id?: string }>('chat.create', {
                title: newTitle.trim(),
            });
            setShowCreate(false);
            setNewTitle('');
            const id = session.session_id ?? session.id ?? '';
            await manualRetry();
            if (id) setSelected(id);
            sdk.toast.show({ level: 'success', message: t('会话已创建', 'session created') });
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const removeSession = async (sessionId: string) => {
        if (!window.confirm(t('确认删除该会话？', 'Delete this session?'))) return;
        try {
            await rpc('chat.delete', { session_id: sessionId });
            if (selected === sessionId) {
                setSelected('');
                setMessages([]);
            }
            sdk.toast.show({ level: 'success', message: t('已删除', 'deleted') });
            manualRetry();
        } catch (e) {
            sdk.toast.show({ level: 'error', message: (e as Error).message });
        }
    };

    const sessionLabel = (s: SessionItem) => s.title?.trim() || s.session_id || s.id || '-';

    return (
        <div>
            <PageHeader
                title={t('对话', 'Chat')}
                subtitle={t(`${sessions.length} 个会话`, `${sessions.length} sessions`)}
                actions={
                    <button
                        type="button"
                        onClick={() => setShowCreate(true)}
                        className="rounded-md border border-border px-3 py-1.5 text-xs text-text-primary hover:bg-bg-muted"
                    >
                        {t('新建对话', 'New chat')}
                    </button>
                }
            />
            <div className="flex items-start gap-4 px-4 pt-3">
                <div className="w-1/3 shrink-0">
                    {loading && attempt === 0 && sessions.length === 0 ? (
                        <SkeletonLines rows={4} />
                    ) : error ? (
                        <ErrorState
                            message={(error as Error).message}
                            retryLabel={t('重试', 'Retry')}
                            onRetry={manualRetry}
                            retrying={attempt}
                        />
                    ) : sessions.length === 0 ? (
                        <EmptyState
                            title={t('暂无会话', 'no sessions yet')}
                            description={t('新建一个对话', 'create your first chat')}
                        />
                    ) : (
                        <ul className="flex flex-col gap-1">
                            {sessions.map((s) => {
                                const id = s.session_id ?? s.id ?? '';
                                const active = id === selected;
                                return (
                                    <li key={id} className="flex items-center gap-1">
                                        <button
                                            type="button"
                                            onClick={() => setSelected(id)}
                                            className={`flex-1 truncate rounded-md border px-3 py-2 text-left text-sm hover:bg-bg-muted ${
                                                active
                                                    ? 'border-brand text-text-primary'
                                                    : 'border-transparent text-text-secondary'
                                            }`}
                                        >
                                            <span className="block truncate">
                                                {sessionLabel(s)}
                                            </span>
                                            {s.updated_at ? (
                                                <span className="text-xs text-text-secondary">
                                                    {formatRelativeTime(s.updated_at * 1000)}
                                                </span>
                                            ) : null}
                                        </button>
                                        <button
                                            type="button"
                                            onClick={() => removeSession(id)}
                                            className="px-2 py-2 text-xs text-error hover:underline"
                                        >
                                            {t('删除', 'Delete')}
                                        </button>
                                    </li>
                                );
                            })}
                        </ul>
                    )}
                </div>
                <div className="flex min-h-[60vh] flex-1 flex-col rounded-lg border border-border">
                    {selected ? (
                        <>
                            <div className="border-b border-border px-4 py-2 text-sm text-text-primary">
                                {sessionLabel(current ?? { session_id: selected })}
                            </div>
                            <div className="flex-1 flex-col gap-3 overflow-y-auto p-4">
                                {threadLoading ? (
                                    <SkeletonLines rows={3} />
                                ) : threadError ? (
                                    <ErrorState
                                        message={threadError}
                                        retryLabel={t('重试', 'Retry')}
                                        onRetry={() => loadHistory(selected)}
                                        retrying={0}
                                    />
                                ) : messages.length === 0 ? (
                                    <EmptyState
                                        title={t('暂无消息', 'no messages yet')}
                                        description={t('发送第一条消息', 'send your first message')}
                                    />
                                ) : (
                                    messages.map((m, i) => (
                                        <div
                                            key={m.created_at ? `${m.created_at}-${i}` : `msg-${i}`}
                                            className="rounded-lg border border-border bg-bg-elevated p-3"
                                        >
                                            <div className="text-xs font-bold text-text-secondary">
                                                {m.role ?? 'unknown'}
                                                {m.created_at ? (
                                                    <span className="ml-2 font-normal">
                                                        {formatRelativeTime(m.created_at * 1000)}
                                                    </span>
                                                ) : null}
                                            </div>
                                            <div className="mt-1 whitespace-pre-wrap text-sm text-text-primary">
                                                {m.content || '-'}
                                            </div>
                                        </div>
                                    ))
                                )}
                            </div>
                            <div className="flex items-end gap-2 border-t border-border p-3">
                                <Textarea
                                    value={draft}
                                    onChange={setDraft}
                                    rows={2}
                                    placeholder={t('输入消息…', 'type a message…')}
                                />
                                <SubmitButton
                                    label={sending ? t('发送中…', 'sending…') : t('发送', 'Send')}
                                    onAction={send}
                                    disabled={sending || !draft.trim()}
                                />
                            </div>
                        </>
                    ) : (
                        <div className="flex flex-1 items-center justify-center p-8">
                            <EmptyState
                                title={t('选择左侧会话', 'select a session')}
                                description={t('或新建一个对话', 'or start a new chat')}
                            />
                        </div>
                    )}
                </div>
            </div>
            <Modal
                open={showCreate}
                title={t('新建对话', 'New chat')}
                onClose={() => setShowCreate(false)}
                footer={<SubmitButton label={t('创建', 'Create')} onAction={createSession} />}
            >
                <TextInput
                    value={newTitle}
                    onChange={setNewTitle}
                    placeholder={t('会话标题', 'session title')}
                />
            </Modal>
        </div>
    );
}
