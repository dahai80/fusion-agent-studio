import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '../i18n';

// Studio module nav — mirrors the macOS AgentStudio FusionTabBar. #406 IA
// simplification + #416: task-centric top-level entries; #528 (DC-2): the
// AI Agent Dashboard is the module home, the builder + composer + agents
// stay primary, every other console collapses into "More".
export const STUDIO_TABS: Array<{ key: string; label: string; labelEn: string; href: string }> = [
    { key: 'home', label: '首页', labelEn: 'Home', href: '#/home' },
    { key: 'builder', label: '构建', labelEn: 'Build', href: '#/builder' },
    { key: 'aiagent', label: '对话', labelEn: 'Composer', href: '#/aiagent' },
    { key: 'agents', label: 'Agents', labelEn: 'Agents', href: '#/agents' },
    { key: 'runs', label: '运行', labelEn: 'Runs', href: '#/runs' },
    { key: 'canvas', label: '画布', labelEn: 'Canvas', href: '#/canvas' },
    { key: 'workflows', label: '工作流', labelEn: 'Workflows', href: '#/workflows' },
    { key: 'governance', label: '治理', labelEn: 'Governance', href: '#/governance' },
    { key: 'cluster', label: '集群', labelEn: 'Cluster', href: '#/cluster' },
    { key: 'observability', label: '可观测', labelEn: 'Observability', href: '#/observability' },
    { key: 'evals', label: '评测', labelEn: 'Evals', href: '#/evals' },
    { key: 'playground', label: '试验场', labelEn: 'Playground', href: '#/playground' },
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
    { key: 'review', label: '评审包', labelEn: 'Review', href: '#/review' },
    { key: 'chat', label: '聊天', labelEn: 'Chat', href: '#/chat' },
];

const PRIMARY_KEYS = new Set(['home', 'builder', 'aiagent', 'agents', 'runs', 'canvas']);

export function StudioNav(props: { active: string }) {
    const { active } = props;
    const t = useT();
    const [moreOpen, setMoreOpen] = useState(false);
    const [morePos, setMorePos] = useState<{ left: number; top: number } | null>(null);
    const btnRef = useRef<HTMLButtonElement | null>(null);
    const panelRef = useRef<HTMLDivElement | null>(null);
    const primary = STUDIO_TABS.filter((tab) => PRIMARY_KEYS.has(tab.key));
    const secondary = STUDIO_TABS.filter((tab) => !PRIMARY_KEYS.has(tab.key));
    const secondaryActive = secondary.some((tab) => tab.key === active);

    const toggleMore = () => {
        if (!moreOpen && btnRef.current) {
            // The nav scrolls horizontally, so an absolutely-positioned panel
            // inside it gets clipped (overflow-x auto implies overflow-y
            // auto). Portal the panel to <body> at the button's viewport
            // position instead.
            const r = btnRef.current.getBoundingClientRect();
            setMorePos({ left: r.left, top: r.bottom + 4 });
        }
        setMoreOpen((v) => !v);
    };

    useEffect(() => {
        if (!moreOpen) return;
        const onDown = (ev: MouseEvent) => {
            const target = ev.target as Node;
            if (btnRef.current?.contains(target) || panelRef.current?.contains(target)) return;
            setMoreOpen(false);
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
            <button
                ref={btnRef}
                type="button"
                onClick={toggleMore}
                aria-expanded={moreOpen}
                className={`whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs ${
                    secondaryActive
                        ? 'bg-bg-muted text-brand'
                        : 'text-text-secondary hover:text-text-primary'
                }`}
            >
                {t('更多', 'More')} {moreOpen ? '▴' : '▾'}
            </button>
            {moreOpen && morePos
                ? createPortal(
                      <div
                          ref={panelRef}
                          style={{ position: 'fixed', left: morePos.left, top: morePos.top }}
                          className="z-50 grid w-64 grid-cols-2 gap-1 rounded-card border border-border bg-bg-elevated p-2 shadow-lg"
                      >
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
                      </div>,
                      document.body
                  )
                : null}
        </nav>
    );
}
