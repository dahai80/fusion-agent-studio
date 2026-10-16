import { type ReactNode, useEffect } from 'react';

// Minimal modal (web-ui only ships ConfirmDialog; studio pages need generic
// forms). Backdrop click + Escape close; content scrolls.
export function Modal(props: {
    open: boolean;
    title: string;
    onClose: () => void;
    children: ReactNode;
    footer?: ReactNode;
}) {
    const { open, title, onClose, children, footer } = props;
    useEffect(() => {
        if (!open) return undefined;
        const fn = (e: KeyboardEvent) => {
            if (e.key === 'Escape') onClose();
        };
        window.addEventListener('keydown', fn);
        return () => window.removeEventListener('keydown', fn);
    }, [open, onClose]);
    if (!open) return null;
    return (
        <div
            className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
            onMouseDown={onClose}
        >
            <div
                className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-card border border-border bg-bg-elevated p-4"
                onMouseDown={(e) => e.stopPropagation()}
            >
                <div className="flex items-center justify-between">
                    <h3 className="text-sm font-medium text-text-primary">{title}</h3>
                    <button
                        type="button"
                        onClick={onClose}
                        className="px-1 text-text-secondary hover:text-text-primary"
                    >
                        ×
                    </button>
                </div>
                <div className="mt-3 flex flex-col gap-3">{children}</div>
                {footer ? <div className="mt-3">{footer}</div> : null}
            </div>
        </div>
    );
}
