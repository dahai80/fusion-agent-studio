import type { ReactNode } from 'react';

export function PageHeader({
    title,
    subtitle,
    actions,
}: {
    title: string;
    subtitle?: string;
    actions?: ReactNode;
}) {
    return (
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-4">
            <div>
                <h1 className="text-lg text-text-primary">{title}</h1>
                {subtitle ? <div className="text-xs text-text-muted">{subtitle}</div> : null}
            </div>
            {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
        </div>
    );
}
