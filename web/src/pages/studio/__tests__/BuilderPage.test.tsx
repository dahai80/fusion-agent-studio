// #529: conversational builder — turns drive compose with current_doc on
// refinement, the draft card updates live, in-chat test posts to the
// agents-platform test endpoint, deploy publishes.
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiGetMock = vi.hoisted(() => vi.fn());
const apiPostMock = vi.hoisted(() => vi.fn());
const apiPutMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    rpc: vi.fn(),
    api: {},
    apiBase: () => '',
    apiGet: (...args: unknown[]) => apiGetMock(...args),
    apiPost: (...args: unknown[]) => apiPostMock(...args),
    apiPut: (...args: unknown[]) => apiPutMock(...args),
}));

vi.mock('../../../sdk', () => ({
    sdk: {
        i18n: { lang: () => 'en', subscribe: () => () => {} },
        toast: { show: (...args: unknown[]) => toastMock(...args) },
    },
    isEmbedded: () => false,
    embeddedParam: () => false,
}));

import { BuilderPage } from '../BuilderPage';

const DOC = {
    name: 'Reconciler',
    description: 'reconciles',
    model: { id: 'base', effort: 'low' },
    system: 'You reconcile.',
    tools: ['agent.list'],
};

function composeOk(doc: typeof DOC) {
    return { ok: true, doc, yaml: '', warnings: [] };
}

describe('BuilderPage (#529)', () => {
    beforeEach(() => {
        apiGetMock.mockReset();
        apiPostMock.mockReset();
        apiPutMock.mockReset();
        toastMock.mockReset();
        apiGetMock.mockResolvedValue({
            capabilities: [
                { id: 'agent.list', title: 'Agent list', description: 'lists' },
                { id: 'graph.create', title: 'Graph create', description: 'makes', mutation: true },
            ],
            starters: [],
        });
    });

    it('first turn composes without current_doc, refinement sends it, draft card updates live', async () => {
        apiPostMock.mockImplementation(async (path: string, body?: Record<string, unknown>) => {
            if (path === '/v1/agents-platform/compose') {
                const hasCurrent = 'current_doc' in (body ?? {});
                return composeOk(
                    hasCurrent ? { ...DOC, system: 'You reconcile. Excel export.' } : DOC
                );
            }
            if (path === '/v1/agents-platform/agents') return { agent_id: 'ag-1' };
            throw new Error(`unexpected post ${path}`);
        });
        apiPutMock.mockResolvedValue({ updated: true });

        const user = userEvent.setup();
        render(<BuilderPage />);

        await user.type(
            await screen.findByPlaceholderText(/describe the agent/i),
            'reconcile my ledgers'
        );
        await user.click(screen.getByRole('button', { name: /Send/i }));

        expect(await screen.findByText(/draft updated: Reconciler/i)).toBeInTheDocument();
        expect(apiPostMock).toHaveBeenCalledWith('/v1/agents-platform/compose', {
            ask: 'reconcile my ledgers',
        });
        // draft card shows the live doc
        expect(screen.getByText('Reconciler')).toBeInTheDocument();

        // refinement turn must carry current_doc (no fork of the draft)
        await user.type(screen.getByPlaceholderText(/describe the agent/i), 'add excel export');
        await user.click(screen.getByRole('button', { name: /Send/i }));
        await waitFor(() =>
            expect(apiPostMock).toHaveBeenCalledWith(
                '/v1/agents-platform/compose',
                expect.objectContaining({ ask: 'add excel export', current_doc: DOC })
            )
        );
        // auto-save updated the existing draft via PUT, not a second create
        await waitFor(() =>
            expect(apiPutMock).toHaveBeenCalledWith('/v1/agents-platform/agents/ag-1', {
                definition_doc: expect.objectContaining({
                    system: 'You reconcile. Excel export.',
                }),
            })
        );
    });

    it('in-chat test posts to the test endpoint and renders the receipt', async () => {
        apiPostMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agents-platform/compose') return composeOk(DOC);
            if (path === '/v1/agents-platform/agents') return { agent_id: 'ag-2' };
            if (path?.endsWith('/test'))
                return {
                    status: 'completed',
                    events: [{ type: 'message', content: 'found 3 diffs' }],
                };
            throw new Error(`unexpected post ${path}`);
        });
        apiPutMock.mockResolvedValue({ updated: true });

        const user = userEvent.setup();
        render(<BuilderPage />);
        await user.type(await screen.findByPlaceholderText(/describe the agent/i), 'build one');
        await user.click(screen.getByRole('button', { name: /Send/i }));
        await screen.findByText(/draft updated: Reconciler/i);

        await user.type(await screen.findByPlaceholderText(/sample input/i), 'run on sample.xlsx');
        await user.click(screen.getByRole('button', { name: /Test run/i }));

        expect(await screen.findByText(/test completed · 1 events/i)).toBeInTheDocument();
        expect(screen.getByText(/found 3 diffs/)).toBeInTheDocument();
        expect(apiPostMock).toHaveBeenCalledWith('/v1/agents-platform/agents/ag-2/test', {
            message: 'run on sample.xlsx',
        });
    });

    it('deploy publishes and links back to the dashboard', async () => {
        apiPostMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agents-platform/compose') return composeOk(DOC);
            if (path === '/v1/agents-platform/agents') return { agent_id: 'ag-3' };
            if (path?.endsWith('/publish')) return { agent_id: 'ag-3', status: 'published' };
            throw new Error(`unexpected post ${path}`);
        });
        apiPutMock.mockResolvedValue({ updated: true });

        const user = userEvent.setup();
        render(<BuilderPage />);
        await user.type(await screen.findByPlaceholderText(/describe the agent/i), 'build one');
        await user.click(screen.getByRole('button', { name: /Send/i }));
        await screen.findByText(/draft updated: Reconciler/i);

        await user.click(await screen.findByRole('button', { name: /^Deploy$/i }));
        expect(await screen.findByText(/deployed — visible on the dashboard/i)).toBeInTheDocument();
        expect(screen.getByRole('link', { name: /Back to Dashboard/i }).getAttribute('href')).toBe(
            '#/home'
        );
    });
});
