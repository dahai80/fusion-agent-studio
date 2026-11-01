// #528: AI Agent Dashboard — sections render from agent.list + usage +
// /v1/runs, published agents show [使用]/[编辑], drafts strip only for
// non-published, runs strip links to the run detail.
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const rpcMock = vi.hoisted(() => vi.fn());
const apiRunsMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    rpc: (...args: unknown[]) => rpcMock(...args),
    api: { runs: (...args: unknown[]) => apiRunsMock(...args) },
    apiBase: () => '',
    statusKind: () => 'success',
}));

vi.mock('../../../sdk', () => ({
    sdk: {
        i18n: { lang: () => 'en', subscribe: () => () => {} },
        toast: { show: vi.fn() },
    },
    isEmbedded: () => false,
    embeddedParam: () => false,
}));

import { AgentHomePage } from '../AgentHomePage';

describe('AgentHomePage (#528)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        apiRunsMock.mockReset();
    });

    it('renders ready-to-use + drafts + recent runs sections', async () => {
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'agent.list')
                return {
                    agents: [
                        {
                            id: 'ag-ready',
                            name: 'Reconciler',
                            description: 'reconciles',
                            status: 'published',
                            tools: ['agent.list'],
                        },
                        { id: 'ag-draft', name: 'WIP', status: 'draft' },
                    ],
                };
            if (method === 'analytics.agent_usage')
                return { agents: [{ agent_id: 'ag-ready', runs: 7 }] };
            throw new Error(`unexpected rpc: ${method}`);
        });
        apiRunsMock.mockResolvedValue({
            runs: [{ task_id: 'r1', title: 'nightly', status: 'completed', updated_at: 100 }],
        });

        render(<AgentHomePage />);

        expect(await screen.findByText('Reconciler')).toBeInTheDocument();
        expect(screen.getByText('WIP')).toBeInTheDocument();
        expect(screen.getByText('nightly')).toBeInTheDocument();
        expect(screen.getByText('7 runs')).toBeInTheDocument();
        // [使用] deep-links to the composer with the agent preselected
        expect(screen.getByRole('link', { name: /Use/i }).getAttribute('href')).toBe(
            '#/aiagent?agent=ag-ready'
        );
        // drafts strip [继续构建] opens the console editor on the same config
        expect(screen.getByRole('link', { name: /Continue building/i }).getAttribute('href')).toBe(
            '/console/agents.html?agent=ag-draft'
        );
    });

    it('shows an empty hint when nothing is published', async () => {
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'agent.list') return { agents: [] };
            if (method === 'analytics.agent_usage') return { agents: [] };
            throw new Error(`unexpected rpc: ${method}`);
        });
        apiRunsMock.mockResolvedValue({ runs: [] });

        render(<AgentHomePage />);

        expect(await screen.findByText(/none yet/i)).toBeInTheDocument();
    });
});
