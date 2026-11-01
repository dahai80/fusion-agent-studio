// #534: FCS-1 composer (architecture/fusion-composer-standard-1101.md).
// Five zones: ① context bar ② attachment chips ③ auto-grow textarea
// (1→8 rows) ④ tool row (📎 ＠ left · ⚙ params + ⬆ send right) ⑤ hint row.
// Send is icon-only ⬆ with three states — empty=disabled, content=enabled,
// generating=⏹ stop (Esc equivalent). It NEVER switches label.
// Palette rides the shared fusion CSS vars (web-ui S1: no hardcoded hex
// outside tokens.css) so both themes pass without dark: variants; the
// structural tokens (rounded-2xl, ring-2, 36×36 send, 11px hint) are FCS
// literals. ⌘K is left un-intercepted for the shell host's global search.
import { ArrowUp, AtSign, Paperclip, Square } from 'lucide-react';
import { type ReactNode, useRef, useState } from 'react';
import { useT } from '../i18n';

export interface FcsAttachment {
    id: string;
    name: string;
}

export function FcsComposer(props: {
    value: string;
    onChange: (value: string) => void;
    onSend: () => void;
    streaming: boolean;
    onStop: () => void;
    attachments: FcsAttachment[];
    onPickFiles: (files: File[]) => void;
    onRemoveAttachment: (id: string) => void;
    contextBar?: ReactNode;
    paramsPopup?: ReactNode;
    placeholder: string;
}) {
    const t = useT();
    const {
        value,
        onChange,
        onSend,
        streaming,
        onStop,
        attachments,
        onPickFiles,
        onRemoveAttachment,
        contextBar,
        paramsPopup,
        placeholder,
    } = props;
    const [paramsOpen, setParamsOpen] = useState(false);
    const textRef = useRef<HTMLTextAreaElement | null>(null);
    const fileRef = useRef<HTMLInputElement | null>(null);

    const canSend = value.trim().length > 0 && !streaming;
    const rows = Math.min(8, Math.max(1, value.split('\n').length));

    const insertAt = () => {
        onChange(`${value}@`);
        textRef.current?.focus();
    };

    const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
        if (e.key === 'Escape') {
            if (streaming) {
                e.preventDefault();
                onStop();
            } else if (paramsOpen) {
                e.preventDefault();
                setParamsOpen(false);
            }
            return;
        }
        if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            if (canSend) onSend();
        }
    };

    const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
        const files = Array.from(e.clipboardData?.files ?? []).filter((f) =>
            f.type.startsWith('image/')
        );
        if (files.length > 0) {
            e.preventDefault();
            onPickFiles(files);
        }
    };

    const sendLabel = t('发送', 'Send');
    const stopLabel = t('停止', 'Stop');

    return (
        <div className="relative">
            <div className="rounded-2xl border border-border bg-bg-elevated shadow-sm focus-within:ring-2 focus-within:ring-brand/30">
                {contextBar ? (
                    <div className="flex flex-wrap items-center gap-2 px-4 pt-3 text-xs">
                        {contextBar}
                    </div>
                ) : null}
                {attachments.length > 0 ? (
                    <div className="flex flex-wrap gap-1.5 px-4 pt-2">
                        {attachments.map((a) => (
                            <span
                                key={a.id}
                                className="flex items-center gap-1 rounded-lg bg-bg-muted px-2 py-1 text-xs text-text-secondary"
                            >
                                {a.name}
                                <button
                                    type="button"
                                    aria-label={t('移除附件', 'remove attachment')}
                                    onClick={() => onRemoveAttachment(a.id)}
                                    className="text-text-muted hover:text-text-primary"
                                >
                                    ×
                                </button>
                            </span>
                        ))}
                    </div>
                ) : null}
                <textarea
                    ref={textRef}
                    value={value}
                    rows={rows}
                    placeholder={placeholder}
                    aria-label={placeholder}
                    onChange={(e) => onChange(e.target.value)}
                    onKeyDown={onKeyDown}
                    onPaste={onPaste}
                    className="w-full resize-none border-none bg-transparent px-4 pt-3 pb-2 text-sm text-text-primary outline-none placeholder:text-text-muted"
                />
                <div className="flex items-center gap-1 px-3 pb-1">
                    <button
                        type="button"
                        aria-label={t('附件', 'Attach')}
                        onClick={() => fileRef.current?.click()}
                        className="flex h-8 w-8 items-center justify-center rounded-lg text-text-secondary hover:bg-bg-muted"
                    >
                        <Paperclip size={16} />
                    </button>
                    <input
                        ref={fileRef}
                        type="file"
                        multiple
                        className="hidden"
                        onChange={(e) => {
                            const files = Array.from(e.target.files ?? []);
                            if (files.length > 0) onPickFiles(files);
                            e.target.value = '';
                        }}
                    />
                    <button
                        type="button"
                        aria-label={t('引用', 'Reference')}
                        onClick={insertAt}
                        className="flex h-8 w-8 items-center justify-center rounded-lg text-text-secondary hover:bg-bg-muted"
                    >
                        <AtSign size={16} />
                    </button>
                    <div className="flex-1" />
                    {paramsPopup ? (
                        <div className="relative">
                            <button
                                type="button"
                                aria-label={t('高级参数', 'Advanced parameters')}
                                onClick={() => setParamsOpen((v) => !v)}
                                className="flex h-8 w-8 items-center justify-center rounded-lg text-text-secondary hover:bg-bg-muted"
                            >
                                ⚙
                            </button>
                            {paramsOpen ? (
                                <>
                                    <button
                                        type="button"
                                        aria-label={t('关闭参数', 'close parameters')}
                                        className="fixed inset-0 z-10 cursor-default"
                                        onClick={() => setParamsOpen(false)}
                                    />
                                    <div className="absolute bottom-10 right-0 z-20 w-56 rounded-2xl border border-border bg-bg-elevated p-3 shadow-overlay">
                                        {paramsPopup}
                                    </div>
                                </>
                            ) : null}
                        </div>
                    ) : null}
                    <button
                        type="button"
                        aria-label={streaming ? stopLabel : sendLabel}
                        onClick={() => (streaming ? onStop() : onSend())}
                        disabled={!streaming && !canSend}
                        className="ml-1 flex h-9 w-9 items-center justify-center rounded-xl bg-brand text-white transition-opacity hover:opacity-90 disabled:bg-bg-muted disabled:text-text-muted"
                    >
                        {streaming ? <Square size={14} /> : <ArrowUp size={16} />}
                    </button>
                </div>
                <div className="px-4 pb-2 text-[11px] text-text-muted">
                    {t('Enter 发送 · Shift+Enter 换行', 'Enter to send · Shift+Enter for newline')}
                    {streaming ? ` · ${t('Esc 停止', 'Esc to stop')}` : ''}
                </div>
            </div>
        </div>
    );
}
