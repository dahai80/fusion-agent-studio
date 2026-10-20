import { useEffect, useState } from 'react';
import { ErrorBoundary } from './components/ErrorBoundary';
import { StudioNav } from './components/StudioNav';
import { useT } from './i18n';
import { RunDetailPage } from './pages/RunDetailPage';
import { RunsPage } from './pages/RunsPage';
import { WorkflowsPage } from './pages/WorkflowsPage';
import { AgentDetailPage } from './pages/studio/AgentDetailPage';
import { AgentsPage } from './pages/studio/AgentsPage';
import { AiAgentPage } from './pages/studio/AiAgentPage';
import { ApikeysPage } from './pages/studio/ApikeysPage';
import { ChatPage } from './pages/studio/ChatPage';
import { ConnectorsPage } from './pages/studio/ConnectorsPage';
import { CronPage } from './pages/studio/CronPage';
import { DashboardPage } from './pages/studio/DashboardPage';
import { GovernancePage } from './pages/studio/GovernancePage';
import { HooksPage } from './pages/studio/HooksPage';
import { MarketplacePage } from './pages/studio/MarketplacePage';
import { MemoryPage } from './pages/studio/MemoryPage';
import { SafetyPage } from './pages/studio/SafetyPage';
import { SkillsPage } from './pages/studio/SkillsPage';
import { TeamPage } from './pages/studio/TeamPage';
import { ToolsPage } from './pages/studio/ToolsPage';
import { WorkflowCanvasPage } from './pages/studio/WorkflowCanvasPage';
import { embeddedParam, isEmbedded } from './sdk';

// Hash router (no router dep). Studio pages (#330/#331 step-1 migration from
// the macOS AgentStudio/AIAgent surface) + the original workflows/runs pages.
// Standalone shows a bottom tab bar; when the shell embeds us (?embedded=1 or
// iframe) the shell owns the bottom bar — StudioNav always renders.
type StudioRoute =
    | 'agents'
    | 'dashboard'
    | 'team'
    | 'memory'
    | 'safety'
    | 'connectors'
    | 'apikeys'
    | 'cron'
    | 'hooks'
    | 'tools'
    | 'skills'
    | 'marketplace'
    | 'chat'
    | 'aiagent'
    | 'canvas'
    | 'governance';

type Route =
    | { name: 'workflows' }
    | { name: 'runs' }
    | { name: 'run'; id: string }
    | { name: 'agent'; id: string }
    | { name: 'studio'; tab: StudioRoute };

const STUDIO_ROUTES: ReadonlySet<string> = new Set<StudioRoute>([
    'agents',
    'dashboard',
    'team',
    'memory',
    'safety',
    'connectors',
    'apikeys',
    'cron',
    'hooks',
    'tools',
    'skills',
    'marketplace',
    'chat',
    'aiagent',
    'canvas',
    'governance',
]);

function parseHash(): Route {
    const h = window.location.hash.replace(/^#\/?/, '');
    const parts = h.split('/').filter(Boolean);
    if (parts[0] === 'runs' && parts[1]) return { name: 'run', id: decodeURIComponent(parts[1]) };
    if (parts[0] === 'runs') return { name: 'runs' };
    if (parts[0] === 'agents' && parts[1])
        return { name: 'agent', id: decodeURIComponent(parts[1]) };
    if (parts[0] && STUDIO_ROUTES.has(parts[0]))
        return { name: 'studio', tab: parts[0] as StudioRoute };
    return { name: 'workflows' };
}

function StudioPage(props: { tab: StudioRoute }) {
    switch (props.tab) {
        case 'agents':
            return <AgentsPage />;
        case 'dashboard':
            return <DashboardPage />;
        case 'team':
            return <TeamPage />;
        case 'memory':
            return <MemoryPage />;
        case 'safety':
            return <SafetyPage />;
        case 'connectors':
            return <ConnectorsPage />;
        case 'apikeys':
            return <ApikeysPage />;
        case 'cron':
            return <CronPage />;
        case 'hooks':
            return <HooksPage />;
        case 'tools':
            return <ToolsPage />;
        case 'skills':
            return <SkillsPage />;
        case 'marketplace':
            return <MarketplacePage />;
        case 'chat':
            return <ChatPage />;
        case 'canvas':
            return <WorkflowCanvasPage />;
        case 'governance':
            return <GovernancePage />;
        case 'aiagent':
            return <AiAgentPage />;
        default:
            return <WorkflowsPage />;
    }
}

// #413: remount the boundary on every route change so a page that crashed
// does not stay crashed when the user navigates elsewhere.
function routeKey(route: Route): string {
    if (route.name === 'studio') return `studio:${route.tab}`;
    if (route.name === 'run') return `run:${route.id}`;
    if (route.name === 'agent') return `agent:${route.id}`;
    return route.name;
}

export function App() {
    const t = useT();
    const [route, setRoute] = useState<Route>(parseHash);
    useEffect(() => {
        const fn = () => setRoute(parseHash());
        window.addEventListener('hashchange', fn);
        return () => window.removeEventListener('hashchange', fn);
    }, []);
    const hideNav = embeddedParam() || isEmbedded();

    return (
        <div className="min-h-screen bg-bg text-text-primary">
            <StudioNav
                active={
                    route.name === 'studio'
                        ? route.tab
                        : route.name === 'run'
                          ? 'runs'
                          : route.name === 'agent'
                            ? 'agents'
                            : route.name
                }
            />
            <main className={hideNav ? 'pb-4' : 'pb-20'}>
                <ErrorBoundary key={routeKey(route)}>
                    {route.name === 'workflows' ? <WorkflowsPage /> : null}
                    {route.name === 'runs' ? <RunsPage /> : null}
                    {route.name === 'run' ? <RunDetailPage runId={route.id} /> : null}
                    {route.name === 'agent' ? <AgentDetailPage agentId={route.id} /> : null}
                    {route.name === 'studio' ? <StudioPage tab={route.tab} /> : null}
                </ErrorBoundary>
            </main>
            {hideNav ? null : (
                <nav className="fixed inset-x-0 bottom-0 z-10 flex justify-around border-t border-border bg-bg-elevated py-2">
                    <a
                        href="#/workflows"
                        className={`rounded-md px-4 py-1 text-sm ${
                            route.name === 'workflows' ? 'text-brand' : 'text-text-secondary'
                        }`}
                    >
                        {t('工作流', 'Workflows')}
                    </a>
                    <a
                        href="#/runs"
                        className={`rounded-md px-4 py-1 text-sm ${
                            route.name === 'runs' || route.name === 'run'
                                ? 'text-brand'
                                : 'text-text-secondary'
                        }`}
                    >
                        {t('运行', 'Runs')}
                    </a>
                </nav>
            )}
        </div>
    );
}
