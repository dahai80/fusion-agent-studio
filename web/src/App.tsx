import { useEffect, useState } from 'react';
import { useT } from './i18n';
import { RunDetailPage } from './pages/RunDetailPage';
import { RunsPage } from './pages/RunsPage';
import { WorkflowsPage } from './pages/WorkflowsPage';
import { embeddedParam, isEmbedded } from './sdk';

// Hash router (no router dep): #/workflows | #/runs | #/runs/:id.
// Standalone shows a bottom tab bar; when the shell embeds us
// (?embedded=1 or iframe) the shell owns navigation, so it is hidden.
type Route = { name: 'workflows' } | { name: 'runs' } | { name: 'run'; id: string };

function parseHash(): Route {
    const h = window.location.hash.replace(/^#\/?/, '');
    const parts = h.split('/').filter(Boolean);
    if (parts[0] === 'runs' && parts[1]) return { name: 'run', id: decodeURIComponent(parts[1]) };
    if (parts[0] === 'runs') return { name: 'runs' };
    return { name: 'workflows' };
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
            <main className={hideNav ? 'pb-4' : 'pb-20'}>
                {route.name === 'workflows' ? <WorkflowsPage /> : null}
                {route.name === 'runs' ? <RunsPage /> : null}
                {route.name === 'run' ? <RunDetailPage runId={route.id} /> : null}
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
