import { useT } from '../i18n';

// Studio module nav — mirrors the macOS AgentStudio FusionTabBar (step-1
// scope: Agents/Workflows/Dashboard/Team/Memory/Safety/Connectors/Apikeys/
// Cron/Hooks/Tools/Skills/Marketplace/Chat + the AIAgent console). Rendered
// in BOTH topologies; embedded mode only hides the host-owned bottom bar.
export const STUDIO_TABS: Array<{ key: string; label: string; labelEn: string }> = [
    { key: 'agents', label: 'Agents', labelEn: 'Agents' },
    { key: 'workflows', label: '工作流', labelEn: 'Workflows' },
    { key: 'canvas', label: '画布', labelEn: 'Canvas' },
    { key: 'governance', label: '治理', labelEn: 'Governance' },
    { key: 'dashboard', label: '仪表盘', labelEn: 'Dashboard' },
    { key: 'team', label: '团队', labelEn: 'Team' },
    { key: 'memory', label: '记忆', labelEn: 'Memory' },
    { key: 'safety', label: '安全', labelEn: 'Safety' },
    { key: 'connectors', label: '连接器', labelEn: 'Connectors' },
    { key: 'apikeys', label: '密钥', labelEn: 'API Keys' },
    { key: 'cron', label: '定时', labelEn: 'Cron' },
    { key: 'hooks', label: '钩子', labelEn: 'Hooks' },
    { key: 'tools', label: '工具', labelEn: 'Tools' },
    { key: 'skills', label: '技能', labelEn: 'Skills' },
    { key: 'marketplace', label: '市场', labelEn: 'Marketplace' },
    { key: 'chat', label: '聊天', labelEn: 'Chat' },
    { key: 'aiagent', label: 'AI 调试', labelEn: 'AI Console' },
];

export function StudioNav(props: { active: string }) {
    const { active } = props;
    const t = useT();
    return (
        <nav className="flex gap-1 overflow-x-auto border-b border-border bg-bg-elevated px-2 py-1">
            {STUDIO_TABS.map((tab) => (
                <a
                    key={tab.key}
                    href={`#/${tab.key}`}
                    className={`whitespace-nowrap rounded-md px-2.5 py-1.5 text-xs ${
                        active === tab.key
                            ? 'bg-bg-muted text-brand'
                            : 'text-text-secondary hover:text-text-primary'
                    }`}
                >
                    {t(tab.label, tab.labelEn)}
                </a>
            ))}
        </nav>
    );
}
