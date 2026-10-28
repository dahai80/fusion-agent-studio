// #489: component tests for the governance-critical surfaces —
// GovernancePage tabs render + mutate RPCs fire + RPC failure surfaces
// an error state instead of a silent blank.
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const rpcMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    rpc: (...args: unknown[]) => rpcMock(...args),
    api: {},
    apiBase: () => '',
    apiGet: vi.fn(),
    apiPost: vi.fn(),
    statusKind: () => 'ok',
}));

vi.mock('../../../sdk', () => ({
    sdk: {
        i18n: { lang: () => 'en', subscribe: () => () => {} },
        toast: { show: (...args: unknown[]) => toastMock(...args) },
    },
    isEmbedded: () => false,
    embeddedParam: () => false,
}));

import { GovernancePage } from '../GovernancePage';

function httpError(status: number, message: string): Error {
    return Object.assign(new Error(message), { status });
}

describe('GovernancePage (#489)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        toastMock.mockReset();
    });

    it('renders budget status and fires budget.set on submit', async () => {
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'budget.status')
                return { max_tokens: 1000, spent_tokens: 250, exceeded: false };
            if (method === 'budget.set') return { status: 'ok' };
            throw new Error(`unexpected rpc: ${method}`);
        });
        const user = userEvent.setup();

        render(<GovernancePage />);
        expect(await screen.findByText('250 / 1000')).toBeInTheDocument();

        await user.type(screen.getByPlaceholderText('0'), '500');
        await user.click(screen.getByRole('button', { name: 'Set budget' }));

        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('budget.set', { max_tokens: 500 })
        );
        expect(toastMock).toHaveBeenCalledWith(
            expect.objectContaining({ level: 'success', message: 'Budget set' })
        );
    });

    it('renders permissions tab and fires permission.update on toggle', async () => {
        rpcMock.mockImplementation(async (method: string, params: Record<string, unknown>) => {
            if (method === 'agent.list') return { agents: [{ agent_id: 'a1', name: 'Agent One' }] };
            if (method === 'permission.list' && params.agent_id === 'a1')
                return { permissions: [], denied_tools: ['bash'], tools: ['read_file', 'bash'] };
            if (method === 'permission.update') return { status: 'ok' };
            throw new Error(`unexpected rpc: ${method}`);
        });
        const user = userEvent.setup();

        render(<GovernancePage />);
        await user.click(screen.getByRole('button', { name: 'Permissions' }));
        expect((await screen.findAllByText('read_file')).length).toBeGreaterThan(0);
        expect(screen.getAllByText('bash').length).toBeGreaterThan(0);

        // bash is denied → its action button offers Allow; read_file offers
        // Deny. DataTable renders a desktop table and a mobile card view,
        // so each action button exists twice — take the first.
        const denyButtons = screen.getAllByRole('button', { name: 'Deny' });
        expect(denyButtons.length).toBeGreaterThan(0);
        await user.click(denyButtons[0]);
        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('permission.update', {
                agent_id: 'a1',
                tool: 'read_file',
                level: 'deny',
            })
        );
    });

    it('renders events tab SLO cards from telemetry RPCs', async () => {
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'telemetry.session_slo')
                return {
                    sessions_completed: 12,
                    compactions_total: 3,
                    approvals_total: 4,
                    avg_approval_latency_ms: 90,
                    p99_approval_latency_ms: 210,
                    avg_session_duration_ms: 1500,
                    p99_session_duration_ms: 3000,
                };
            if (method === 'telemetry.metrics')
                return { counters: { tokens_prompt: 42 }, latencies: {} };
            throw new Error(`unexpected rpc: ${method}`);
        });
        const user = userEvent.setup();

        render(<GovernancePage />);
        await user.click(screen.getByRole('button', { name: 'Events' }));
        expect(await screen.findByText('Sessions completed')).toBeInTheDocument();
        expect(screen.getByText('P99 approval latency')).toBeInTheDocument();
        expect(await screen.findByText('tokens_prompt: 42')).toBeInTheDocument();
    });

    it('renders artifacts tab and opens a run-artifact triple', async () => {
        rpcMock.mockImplementation(async (method: string, params: Record<string, unknown>) => {
            if (method === 'run_artifact.list')
                return {
                    artifacts: [
                        {
                            artifact_id: 'ar1',
                            name: 'demo graph',
                            run_id: 'r1',
                            version: 2,
                            created_at: 1760000000,
                            message_count: 5,
                            graph_nodes: 3,
                            graph_edges: 2,
                        },
                    ],
                };
            if (method === 'run_artifact.get' && params.artifact_id === 'ar1')
                return {
                    artifact_id: 'ar1',
                    name: 'demo graph',
                    run_id: 'r1',
                    version: 2,
                    created_at: 1760000000,
                    message_count: 5,
                    graph_nodes: 3,
                    graph_edges: 2,
                    graph: { nodes: [] },
                    environment: { cwd: '/tmp' },
                    conversation: { count: 1, messages: [{ role: 'user' }] },
                };
            throw new Error(`unexpected rpc: ${method}`);
        });
        const user = userEvent.setup();

        render(<GovernancePage />);
        await user.click(screen.getByRole('button', { name: 'Artifacts' }));
        expect((await screen.findAllByText('demo graph')).length).toBeGreaterThan(0);

        await user.click(screen.getByRole('button', { name: 'Open triple' }));
        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('run_artifact.get', { artifact_id: 'ar1' })
        );
        expect(await screen.findByText('Graph definition')).toBeInTheDocument();
        expect(screen.getByText('Environment snapshot')).toBeInTheDocument();
        expect(screen.getByText('Conversation archive')).toBeInTheDocument();
    });

    it('surfaces an error state when budget.status fails (not a silent blank)', async () => {
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'budget.status') throw httpError(401, 'unauthorized');
            throw new Error(`unexpected rpc: ${method}`);
        });

        render(<GovernancePage />);
        const errorBox = await screen.findByText(/unauthorized/i);
        expect(errorBox).toBeInTheDocument();
        // Retry control exists — the user is not stuck on a blank panel.
        expect(screen.getByRole('button', { name: /重试|retry/i })).toBeInTheDocument();
    });

    it('shows a toast error (not a silent no-op) when budget.set fails', async () => {
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'budget.status')
                return { max_tokens: 1000, spent_tokens: 0, exceeded: false };
            if (method === 'budget.set') throw new Error('budget write rejected');
            throw new Error(`unexpected rpc: ${method}`);
        });
        const user = userEvent.setup();

        render(<GovernancePage />);
        await screen.findByText('0 / 1000');

        await user.type(screen.getByPlaceholderText('0'), '7');
        await user.click(screen.getByRole('button', { name: 'Set budget' }));

        await waitFor(() =>
            expect(toastMock).toHaveBeenCalledWith(
                expect.objectContaining({ level: 'error', message: 'budget write rejected' })
            )
        );
        // The tab still renders its data — no silent blank after failure.
        expect(screen.getByText('0 / 1000')).toBeInTheDocument();
    });
});
