import { useEffect, useRef, useState } from 'react';
import { useT } from '../i18n';

// Studio module nav — mirrors the macOS AgentStudio FusionTabBar. #406 IA
// simplification + #416: four task-centric top-level entries (agents, runs,
// canvas, composer); every secondary console collapses into "More".
export const STUDIO_TABS: Array<{ key: string; label: string; labelEn: string; href: string }> = [
    { key: 'agents', label: 'Agents', labelEn: 'Agents', href: '#/agents' },
    { key: 'runs', label: '运行', labelEn: 'Runs', href: '#/runs' },
    { key: 'canvas', label: '画布', labelEn: 'Canvas', href: '#/canvas' },
    { key: 'aiagent', label: 'Composer', labelEn: 'Composer', href: '#/aiagent' },
    { key: 'workflows', label: '工作流', labelEn: 'Workflows', href: '#/workflows' },
    { key: 'governance', label: '治理', labelEn: 'Governance', href: '#/governance' },
    { key: 'dashboard', label: '仪表盘', labelEn: 'Dashboard', href: '#/dashboard' },
    { key: 'team', label: '团队', labelEn: 'Team', href: '#/team' },
    { key: 'memory', label: '记忆', labelEn: 'Memory', href: '#/memory' },
    { key: 'safety', label: '安全', labelEn: 'Safety', href: '#/safety' },
    { key: 'connectors', label: '连接器', labelEn: 'Connectors', href: '#/connectors' },
    { key: 'apikeys', label: '密钥', labelEn: 'API Keys', href: '#/apikeys' },
    { key: 'cron', label: '定时', labelEn: 'Cron', href: '#/cron' },
    { key: 'hooks', label: '钩子', labelEn: 'Hooks', href: '#/hooks' },
    { key: 'tools', label: '工具', labelEn: 'Tools', href: '#/tools' },
    { key: 'skills', label: '技能', labelEn: 'Skills', href: '#/skills' },
    { key: 'marketplace', label: '市场', labelEn: 'Marketplace', href: '#/marketplace' },
    { key: 'chat', label: '聊天', labelEn: 'Chat', href: '#/chat' },
];

const PRIMARY_KEYS = new Set(['agents', 'runs', 'canvas', 'aiagent']);

export function StudioNav(props: { active: string }) {
    const { active } = props;
    const t = useT();
    const [moreOpen, setMoreOpen] = useState(false);
    const moreRef = useRef<HTMLDivElement | null>(null);
    const primary = STUDIO_TABS.filter((tab) => PRIMARY_KEYS.has(tab.key));
    const secondary = STUDIO_TABS.filter((tab) => !PRIMARY_KEYS.has(tab.key));
    const secondaryActive = secondary.some((tab) => tab.key === active);

    useEffect(() => {
        if (!moreOpen) return;
        const onDown = (ev: MouseEvent) => {
            if (!moreRef.current?.contains(ev.target as Node)) setMoreOpen(false);
        };
        window.addEventListener('mousedown', onDown);
        return () => window.removeEventListener('mousedown', onDown);
    }, [moreOpen]);

    return (
        <nav className="flex items-center gap-1 overflow-x-auto border-b border-border bg-bg-elevated px-2 py-1">
            {primary.map((tab) => (
                <a
                    key={tab.key}
                    href={tab.href}
                    className={`whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs ${
                        active === tab.key
                            ? 'bg-bg-muted text-brand'
                            : 'text-text-secondary hover:text-text-primary'
                    }`}
                >
                    {t(tab.label, tab.labelEn)}
                </a>
            ))}
            <div className="relative" ref={moreRef}>
                <button
                    type="button"
                    onClick={() => setMoreOpen((v) => !v)}
                    aria-expanded={moreOpen}
                    className={`whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs ${
                        secondaryActive
                            ? 'bg-bg-muted text-brand'
                            : 'text-text-secondary hover:text-text-primary'
                    }`}
                >
                    {t('更多', 'More')} {moreOpen ? '▴' : '▾'}
                </button>
                {moreOpen ? (
                    <div className="absolute left-0 top-full z-20 mt-1 grid w-64 grid-cols-2 gap-1 rounded-card border border-border bg-bg-elevated p-2 shadow-lg">
                        {secondary.map((tab) => (
                            <a
                                key={tab.key}
                                href={tab.href}
                                onClick={() => setMoreOpen(false)}
                                className={`rounded-md px-2 py-1 text-xs ${
                                    active === tab.key
                                        ? 'bg-bg-muted text-brand'
                                        : 'text-text-secondary hover:bg-bg-muted hover:text-text-primary'
                                }`}
                            >
                                {t(tab.label, tab.labelEn)}
                            </a>
                        ))}
                    </div>
                ) : null}
            </div>
        </nav>
    );
}
