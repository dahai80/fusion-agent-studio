// #489: component tests for the governance-critical surfaces —
// GovernancePage tabs render + mutate RPCs fire + RPC failure surfaces
// an error state instead of a silent blank.
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const rpcMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());
const apiGetMock = vi.hoisted(() => vi.fn());
const apiPostMock = vi.hoisted(() => vi.fn());
const apiDeleteMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    rpc: (...args: unknown[]) => rpcMock(...args),
    api: {},
    apiBase: () => '',
    apiGet: (...args: unknown[]) => apiGetMock(...args),
    apiPost: (...args: unknown[]) => apiPostMock(...args),
    apiDelete: (...args: unknown[]) => apiDeleteMock(...args),
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
        apiGetMock.mockReset();
        apiPostMock.mockReset();
        apiDeleteMock.mockReset();
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

const G1_APPROVAL = {
    id: 'ga1',
    title: 'gate:g1 — proposal approval for fusion-agent-studio#573',
    requester: 'pipeline-bot',
    assigned_to: 'owner-a',
    kind: 'external',
    status: 'pending',
    params: { issue: 'fusion-agent-studio#573', impact: 'governance flow' },
    agent_id: 'external',
};

function openApprovalsTab(user: ReturnType<typeof userEvent.setup>) {
    return user.click(screen.getByRole('button', { name: 'Approvals' }));
}

describe('GovernancePage approvals inbox (#573)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        toastMock.mockReset();
        apiGetMock.mockReset();
        apiPostMock.mockReset();
        apiDeleteMock.mockReset();
        rpcMock.mockResolvedValue({});
    });

    it('lists a gate:g1 approval with badge, issue ref and payload', async () => {
        apiGetMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agent-platform/approvers')
                return { approvers: ['owner-a', 'reviewer-b'] };
            if (path.startsWith('/v1/agent-platform/approvals'))
                return { approvals: [G1_APPROVAL] };
            if (path.startsWith('/v1/agent-platform/webhooks')) return { webhooks: [] };
            throw new Error(`unexpected GET ${path}`);
        });
        const user = userEvent.setup();
        render(<GovernancePage />);
        await openApprovalsTab(user);

        expect((await screen.findAllByText(/gate:g1/))[0]).toBeInTheDocument();
        expect(screen.getAllByText('G1')[0]).toBeInTheDocument();
        expect(screen.getAllByText(/fusion-agent-studio#573/)[0]).toBeInTheDocument();
        expect(screen.getAllByText(/governance flow/)[0]).toBeInTheDocument();
    });

    it('assigned principal can approve; decision hits the REST endpoint', async () => {
        apiGetMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agent-platform/approvers')
                return { approvers: ['owner-a', 'reviewer-b'] };
            if (path.startsWith('/v1/agent-platform/approvals'))
                return { approvals: [G1_APPROVAL] };
            throw new Error(`unexpected GET ${path}`);
        });
        apiPostMock.mockResolvedValue({ id: 'ga1', status: 'approved' });
        const user = userEvent.setup();
        render(<GovernancePage />);
        await openApprovalsTab(user);

        await screen.findAllByText(/gate:g1/);
        await user.selectOptions(screen.getByTestId('approver-select'), 'owner-a');
        const approveBtn = screen.getAllByRole('button', { name: 'Approve' })[0];
        expect(approveBtn).not.toBeDisabled();
        await user.click(approveBtn);

        await waitFor(() =>
            expect(apiPostMock).toHaveBeenCalledWith('/v1/agent-platform/approvals/ga1/approve', {
                decided_by: 'owner-a',
            })
        );
        expect(toastMock).toHaveBeenCalledWith(
            expect.objectContaining({ level: 'success', message: 'Approved' })
        );
    });

    it('non-assigned principal is read-only', async () => {
        apiGetMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agent-platform/approvers')
                return { approvers: ['owner-a', 'reviewer-b'] };
            if (path.startsWith('/v1/agent-platform/approvals'))
                return { approvals: [G1_APPROVAL] };
            throw new Error(`unexpected GET ${path}`);
        });
        const user = userEvent.setup();
        render(<GovernancePage />);
        await openApprovalsTab(user);

        await screen.findAllByText(/gate:g1/);
        await user.selectOptions(screen.getByTestId('approver-select'), 'reviewer-b');
        expect(screen.getAllByRole('button', { name: 'Approve' })[0]).toBeDisabled();
        expect(screen.getAllByRole('button', { name: 'Reject' })[0]).toBeDisabled();
        expect(apiPostMock).not.toHaveBeenCalled();
    });

    it('reject sends the reason field', async () => {
        apiGetMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agent-platform/approvers')
                return { approvers: ['owner-a', 'reviewer-b'] };
            if (path.startsWith('/v1/agent-platform/approvals'))
                return { approvals: [G1_APPROVAL] };
            throw new Error(`unexpected GET ${path}`);
        });
        apiPostMock.mockResolvedValue({ id: 'ga1', status: 'rejected' });
        const user = userEvent.setup();
        render(<GovernancePage />);
        await openApprovalsTab(user);

        await screen.findAllByText(/gate:g1/);
        await user.selectOptions(screen.getByTestId('approver-select'), 'owner-a');
        await user.type(screen.getByPlaceholderText('required on reject'), 'impact too broad');
        await user.click(screen.getAllByRole('button', { name: 'Reject' })[0]);

        await waitFor(() =>
            expect(apiPostMock).toHaveBeenCalledWith('/v1/agent-platform/approvals/ga1/reject', {
                decided_by: 'owner-a',
                reason: 'impact too broad',
            })
        );
    });
});

describe('GovernancePage approver identity (#596)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        toastMock.mockReset();
        apiGetMock.mockReset();
        apiPostMock.mockReset();
        apiDeleteMock.mockReset();
        rpcMock.mockResolvedValue({});
    });

    function mockInbox() {
        apiGetMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agent-platform/approvers')
                return { approvers: ['owner-a', 'reviewer-b'], source: 'env' };
            if (path.startsWith('/v1/agent-platform/approvals'))
                return { approvals: [G1_APPROVAL] };
            throw new Error(`unexpected GET ${path}`);
        });
    }

    it('approver identity is a select of configured approvers, never free text', async () => {
        mockInbox();
        const user = userEvent.setup();
        render(<GovernancePage />);
        await openApprovalsTab(user);

        await screen.findAllByText(/gate:g1/);
        const sel = screen.getByTestId('approver-select') as HTMLSelectElement;
        expect(sel.tagName).toBe('SELECT');
        expect(screen.queryByPlaceholderText('principal id')).toBeNull();
        // unselected: decision buttons stay disabled (no unattributed decisions)
        expect(sel.value).toBe('');
        expect(screen.getAllByRole('button', { name: 'Approve' })[0]).toBeDisabled();
        expect(screen.getAllByRole('button', { name: 'Reject' })[0]).toBeDisabled();
        expect(screen.getAllByText('owner-a').length).toBeGreaterThan(0);
        expect(screen.getAllByText('reviewer-b').length).toBeGreaterThan(0);
        expect(apiPostMock).not.toHaveBeenCalled();
    });

    it('stores the selected identity verbatim as decided_by', async () => {
        mockInbox();
        apiPostMock.mockResolvedValue({ id: 'ga1', status: 'approved' });
        const user = userEvent.setup();
        render(<GovernancePage />);
        await openApprovalsTab(user);

        await screen.findAllByText(/gate:g1/);
        await user.selectOptions(screen.getByTestId('approver-select'), 'owner-a');
        await user.click(screen.getAllByRole('button', { name: 'Approve' })[0]);
        await waitFor(() =>
            expect(apiPostMock).toHaveBeenCalledWith('/v1/agent-platform/approvals/ga1/approve', {
                decided_by: 'owner-a',
            })
        );
    });
});

describe('GovernancePage decision-card evidence (#599)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        toastMock.mockReset();
        apiGetMock.mockReset();
        apiPostMock.mockReset();
        apiDeleteMock.mockReset();
        rpcMock.mockResolvedValue({});
    });

    const EVIDENCE_APPROVAL = {
        ...G1_APPROVAL,
        id: 'ga-ev',
        title: 'gate:g5 — rollout sign-off for delivery pipeline',
        params: {
            issue: 'fusion-agent-studio#598',
            pr_url: 'https://github.com/dahai80/fusion-agent-studio/pull/601',
            artifact_ids: ['art-prd-1', 'art-arch-2'],
            items: [
                { reviewer: 'delivery-security-reviewer', verdict: 'pass' },
                { reviewer: 'delivery-privacy-reviewer', verdict: 'blocking' },
            ],
        },
    };

    function mockEvidenceInbox(rows: unknown[]) {
        apiGetMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agent-platform/approvers') return { approvers: ['owner-a'] };
            if (path.startsWith('/v1/agent-platform/approvals')) return { approvals: rows };
            throw new Error(`unexpected GET ${path}`);
        });
    }

    it('renders PR link, artifact preview and reviewer verdict summary', async () => {
        mockEvidenceInbox([EVIDENCE_APPROVAL]);
        const user = userEvent.setup();
        render(<GovernancePage />);
        await openApprovalsTab(user);

        const evidence = (await screen.findAllByTestId('evidence'))[0];
        const link = evidence.querySelector('a');
        expect(link).not.toBeNull();
        expect(link?.getAttribute('href')).toBe(
            'https://github.com/dahai80/fusion-agent-studio/pull/601'
        );
        expect(link?.getAttribute('target')).toBe('_blank');
        expect(evidence.textContent).toContain('art-prd-1, art-arch-2');
        expect(evidence.textContent).toContain('1 blocking / 1 pass');
        expect(evidence.textContent).toContain('delivery-security-reviewer: pass');
        expect(evidence.textContent).toContain('delivery-privacy-reviewer: blocking');
    });

    it('renders no evidence block when params carry none', async () => {
        mockEvidenceInbox([G1_APPROVAL]);
        const user = userEvent.setup();
        render(<GovernancePage />);
        await openApprovalsTab(user);

        await screen.findAllByText(/gate:g1/);
        expect(screen.queryByTestId('evidence')).toBeNull();
    });
});

describe('GovernancePage webhook registry (#573)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        toastMock.mockReset();
        apiGetMock.mockReset();
        apiPostMock.mockReset();
        apiDeleteMock.mockReset();
        rpcMock.mockResolvedValue({});
    });

    it('registers a webhook with url + secret and lists it', async () => {
        apiGetMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agent-platform/approvers')
                return { approvers: ['owner-a', 'reviewer-b'] };
            if (path.startsWith('/v1/agent-platform/webhooks'))
                return {
                    webhooks: [
                        {
                            id: 'wh1',
                            url: 'https://cowork.internal/hook',
                            events: ['approval.decided'],
                            delivery_count: 3,
                            last_status: 200,
                        },
                    ],
                };
            throw new Error(`unexpected GET ${path}`);
        });
        apiPostMock.mockResolvedValue({ id: 'wh1', url: 'https://cowork.internal/hook' });
        const user = userEvent.setup();
        render(<GovernancePage />);
        await user.click(screen.getByRole('button', { name: 'Webhooks' }));

        expect((await screen.findAllByText('https://cowork.internal/hook'))[0]).toBeInTheDocument();
        await user.type(
            screen.getByPlaceholderText('https://example.internal/hook'),
            'https://cowork.internal/hook'
        );
        await user.type(screen.getByPlaceholderText('********'), 's3cret-value-16ch');
        await user.click(screen.getByRole('button', { name: 'Register' }));

        await waitFor(() =>
            expect(apiPostMock).toHaveBeenCalledWith('/v1/agent-platform/webhooks', {
                url: 'https://cowork.internal/hook',
                secret: 's3cret-value-16ch',
            })
        );
        expect(toastMock).toHaveBeenCalledWith(
            expect.objectContaining({ level: 'success', message: 'Registered' })
        );
    });

    it('deletes a webhook', async () => {
        apiGetMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agent-platform/approvers')
                return { approvers: ['owner-a', 'reviewer-b'] };
            if (path.startsWith('/v1/agent-platform/webhooks'))
                return {
                    webhooks: [{ id: 'wh1', url: 'https://cowork.internal/hook', events: [] }],
                };
            throw new Error(`unexpected GET ${path}`);
        });
        apiDeleteMock.mockResolvedValue({ deleted: true });
        const user = userEvent.setup();
        render(<GovernancePage />);
        await user.click(screen.getByRole('button', { name: 'Webhooks' }));

        await screen.findAllByText('https://cowork.internal/hook');
        await user.click(screen.getAllByRole('button', { name: 'Delete' })[0]);

        await waitFor(() =>
            expect(apiDeleteMock).toHaveBeenCalledWith('/v1/agent-platform/webhooks/wh1')
        );
        expect(toastMock).toHaveBeenCalledWith(
            expect.objectContaining({ level: 'success', message: 'Deleted' })
        );
    });
});
