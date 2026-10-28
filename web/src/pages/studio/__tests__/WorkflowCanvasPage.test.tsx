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
