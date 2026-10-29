// #489: component tests for the canvas safety pending-actions surface —
// approve/reject fire their RPCs, and an approval failure surfaces the
// error toast instead of silently vanishing.
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const rpcMock = vi.hoisted(() => vi.fn());
const graphsMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    rpc: (...args: unknown[]) => rpcMock(...args),
    api: {
        graphs: (...args: unknown[]) => graphsMock(...args),
    },
    apiBase: () => '',
    apiGet: vi.fn(),
    apiPost: vi.fn(),
    statusKind: () => 'ok',
}));

vi.mock('../../../sdk', () => ({
    sdk: {
        i18n: { lang: () => 'en', subscribe: () => () => {} },
        toast: { show: vi.fn() },
    },
    isEmbedded: () => false,
    embeddedParam: () => false,
}));

import { WorkflowCanvasPage } from '../WorkflowCanvasPage';

function defaultRpcs(pending: Array<Record<string, unknown>>) {
    rpcMock.mockImplementation(async (method: string) => {
        if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
        if (method === 'safety.get_pending_actions') return { actions: pending };
        if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
        if (method === 'graph.get') return { graph: { nodes: [], edges: [] } };
        throw new Error(`unexpected rpc: ${method}`);
    });
    graphsMock.mockResolvedValue({ data: [] });
}

async function renderCanvas() {
    const user = userEvent.setup();
    // The governance panel (budget + pending approvals) loads on graph
    // select — seed the preselected graph like a canvas "open" navigation.
    window.sessionStorage.setItem('fusion.canvas.graphId', 'g1');
    render(<WorkflowCanvasPage />);
    await screen.findByText(/Pending approvals/, undefined, { timeout: 3000 });
    return user;
}

describe('WorkflowCanvasPage safety pending actions (#489)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        graphsMock.mockReset();
        window.sessionStorage.clear();
        window.localStorage.clear();
    });

    it('renders pending actions and fires safety.approve on Approve', async () => {
        defaultRpcs([{ action_id: 'act-1', category: 'bash' }]);
        const user = await renderCanvas();

        expect(screen.getByText('bash')).toBeInTheDocument();

        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions') return { actions: [] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            if (method === 'safety.approve') return { status: 'ok' };
            throw new Error(`unexpected rpc: ${method}`);
        });
        await user.click(screen.getByRole('button', { name: 'Approve' }));

        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('safety.approve', { action_id: 'act-1' })
        );
        // Approved + refreshed — the pending list empties and the toast confirms.
        expect(await screen.findByText('Approved')).toBeInTheDocument();
        await waitFor(() => expect(screen.queryByText('bash')).not.toBeInTheDocument());
    });

    it('fires safety.reject on Deny', async () => {
        defaultRpcs([{ action_id: 'act-2', tool: 'write_file' }]);
        const user = await renderCanvas();

        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions') return { actions: [] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            if (method === 'safety.reject') return { status: 'ok' };
            throw new Error(`unexpected rpc: ${method}`);
        });
        await user.click(screen.getByRole('button', { name: 'Deny' }));

        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('safety.reject', { action_id: 'act-2' })
        );
        expect(await screen.findByText('Rejected')).toBeInTheDocument();
    });

    it('surfaces an approval failure via the toast (not a silent no-op)', async () => {
        defaultRpcs([{ action_id: 'act-3', category: 'bash' }]);
        const user = await renderCanvas();

        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions')
                return { actions: [{ action_id: 'act-3', category: 'bash' }] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            if (method === 'safety.approve') throw new Error('guard offline');
            throw new Error(`unexpected rpc: ${method}`);
        });
        await user.click(screen.getByRole('button', { name: 'Approve' }));

        expect(await screen.findByText(/Approval failed/)).toBeInTheDocument();
        // The action is still pending — a failed decision did not drop it.
        expect(screen.getByText('bash')).toBeInTheDocument();
    });
});

describe('WorkflowCanvasPage graph versioning (#484)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        graphsMock.mockReset();
        window.sessionStorage.clear();
        window.localStorage.clear();
    });

    it('lists version history on toggle and shows the empty state', async () => {
        defaultRpcs([{ action_id: 'seed', category: 'bash' }]);
        const user = await renderCanvas();

        await user.click(screen.getByRole('button', { name: /Version history/ }));

        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('graph.version_list', { graph_id: 'g1' })
        );
        expect(await screen.findByText('No versions recorded')).toBeInTheDocument();
    });

    it('restores a version via graph.version_restore and reloads the canvas', async () => {
        defaultRpcs([{ action_id: 'seed', category: 'bash' }]);
        const user = await renderCanvas();

        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions') return { actions: [] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            if (method === 'graph.get')
                return {
                    graph: { id: 'g1', nodes: [], edges: [], name: 'v1 graph', description: '' },
                };
            if (method === 'graph.version_list')
                return {
                    versions: [{ version: 1, name: 'v1 graph', created_at: 1700000000 }],
                };
            if (method === 'graph.version_restore') return { graph_id: 'g1', restored_from: 1 };
            throw new Error(`unexpected rpc: ${method}`);
        });
        await user.click(screen.getByRole('button', { name: /Version history/ }));
        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('graph.version_list', { graph_id: 'g1' })
        );

        await user.click(screen.getByRole('button', { name: 'Restore' }));

        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('graph.version_restore', {
                graph_id: 'g1',
                version: 1,
            })
        );
        expect(await screen.findByText('Version restored')).toBeInTheDocument();
    });

    it('renders the test sidebar with canned-input run controls', async () => {
        defaultRpcs([{ action_id: 'seed', category: 'bash' }]);
        await renderCanvas();

        expect(screen.getByText('Canned input')).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /Run whole graph/ })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /Run selected node only/ })).toBeInTheDocument();
        expect(screen.getByRole('button', { name: /Run downstream/ })).toBeInTheDocument();
    });
});
