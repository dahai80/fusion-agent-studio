// #416: four-class permission card, shared between the Safety console and
// the Composer pre-flight. Single source so every module renders the same
// card anatomy (cowork #138 approval cards use the same vocabulary:
// folder / code / network / remote).
export const PERMISSION_CARD_META: Record<
    string,
    { zh: string; en: string; accent: string; dot: string }
> = {
    folder: {
        zh: '文件夹',
        en: 'Folder',
        accent: 'border-warning text-warning',
        dot: 'bg-warning',
    },
    code: { zh: '代码', en: 'Code', accent: 'border-brand text-brand', dot: 'bg-brand' },
    network: {
        zh: '网络',
        en: 'Network',
        accent: 'border-running text-running',
        dot: 'bg-running',
    },
    remote: { zh: '远程', en: 'Remote', accent: 'border-error text-error', dot: 'bg-error' },
};

export const PERMISSION_CARD_ORDER = ['folder', 'code', 'network', 'remote'] as const;

export function PermissionCards({
    allowed,
    toolHint,
}: {
    /** Class keys present/allowed for this run; absent keys render dimmed. */
    allowed?: Record<string, boolean>;
    /** Optional per-class explanation line. */
    toolHint?: Record<string, string>;
}) {
    return (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {PERMISSION_CARD_ORDER.map((cls) => {
                const meta = PERMISSION_CARD_META[cls];
                const on = allowed ? allowed[cls] === true : true;
                return (
                    <div
                        key={cls}
                        className={`rounded-md border p-2 text-xs ${on ? meta.accent : 'border-border text-text-muted'}`}
                    >
                        <div className="flex items-center gap-1.5 font-medium">
                            <span className={`inline-block h-1.5 w-1.5 rounded-full ${on ? meta.dot : 'bg-border'}`} />
                            {meta.zh} / {meta.en}
                        </div>
                        <div className="mt-1 text-[10px] opacity-80">
                            {toolHint?.[cls] ?? (on ? '允许' : '未涉及')}
                        </div>
                    </div>
                );
            })}
        </div>
    );
}
