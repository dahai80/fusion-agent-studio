// #529: conversational builder — turns drive compose with current_doc on
// refinement, the draft card updates live, in-chat test posts to the
// agents-platform test endpoint, deploy publishes.
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiGetMock = vi.hoisted(() => vi.fn());
const apiPostMock = vi.hoisted(() => vi.fn());
const apiPutMock = vi.hoisted(() => vi.fn());
const rpcMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    rpc: (...args: unknown[]) => rpcMock(...args),
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
        rpcMock.mockReset();
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'connector.list') return { connectors: [] };
            throw new Error(`unexpected rpc ${method}`);
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

    // #537: connector capabilities gate on a live grant — ungranted chips
    // open the auth modal and attach only after connector.connect passes.
    const CONNECTOR_CAP = {
        id: 'connector.inv.list',
        title: 'Inventory list',
        description: 'reads stock',
        source: 'connector',
        route: { kind: 'connector', connector: 'connector.inv' },
    };

    it('ungranted connector chip opens auth modal and attaches after grant', async () => {
        apiGetMock.mockResolvedValue({
            capabilities: [
                { id: 'agent.list', title: 'Agent list', description: 'lists' },
                CONNECTOR_CAP,
            ],
            starters: [],
        });
        apiPostMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agents-platform/compose') return composeOk(DOC);
            if (path === '/v1/agents-platform/agents') return { agent_id: 'ag-4' };
            throw new Error(`unexpected post ${path}`);
        });
        apiPutMock.mockResolvedValue({ updated: true });
        const calls: string[] = [];
        rpcMock.mockImplementation(async (method: string) => {
            calls.push(method);
            if (method === 'connector.list') return { connectors: [] };
            if (method === 'connector.create')
                return { connector_id: 'connector.inv', connector: { id: 'connector.inv' } };
            if (method === 'connector.connect')
                return { connector_id: 'connector.inv', status: 'connected' };
            throw new Error(`unexpected rpc ${method}`);
        });

        const user = userEvent.setup();
        render(<BuilderPage />);
        await user.type(await screen.findByPlaceholderText(/describe the agent/i), 'build one');
        await user.click(screen.getByRole('button', { name: /Send/i }));
        await screen.findByText(/draft updated: Reconciler/i);

        // ungranted chip labels itself authorize, never add
        await user.click(await screen.findByRole('button', { name: /Inventory list.*authorize/i }));
        expect(await screen.findByText(/Authorize connector/i)).toBeInTheDocument();
        // nothing attached before the grant
        expect(apiPutMock).not.toHaveBeenCalledWith(
            expect.anything(),
            expect.objectContaining({
                definition_doc: expect.objectContaining({
                    tools: expect.arrayContaining(['connector.inv.list']),
                }),
            })
        );

        await user.type(screen.getByLabelText(/^Key$/i), 'secret-1');
        await user.click(screen.getByRole('button', { name: /Authorize & add/i }));

        await waitFor(() =>
            expect(apiPutMock).toHaveBeenCalledWith(
                '/v1/agents-platform/agents/ag-4',
                expect.objectContaining({
                    definition_doc: expect.objectContaining({
                        tools: expect.arrayContaining(['connector.inv.list']),
                    }),
                })
            )
        );
        expect(calls).toContain('connector.create');
        expect(calls).toContain('connector.connect');
        expect(calls[calls.indexOf('connector.create') + 1]).toBe('connector.connect');
    });

    // #539 R1: the OAuth2 modal collects an access token — the field the
    // backend probe actually requires — and the grant completes end-to-end.
    it('oauth2 grant submits an access token and connects', async () => {
        apiGetMock.mockResolvedValue({
            capabilities: [
                { id: 'agent.list', title: 'Agent list', description: 'lists' },
                CONNECTOR_CAP,
            ],
            starters: [],
        });
        apiPostMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agents-platform/compose') return composeOk(DOC);
            if (path === '/v1/agents-platform/agents') return { agent_id: 'ag-6' };
            throw new Error(`unexpected post ${path}`);
        });
        apiPutMock.mockResolvedValue({ updated: true });
        const rpcParams: Record<string, Record<string, unknown>> = {};
        rpcMock.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
            rpcParams[method] = params ?? {};
            if (method === 'connector.list') return { connectors: [] };
            if (method === 'connector.create')
                return { connector_id: 'connector.inv', connector: { id: 'connector.inv' } };
            if (method === 'connector.connect')
                return { connector_id: 'connector.inv', status: 'connected' };
            throw new Error(`unexpected rpc ${method}`);
        });

        const user = userEvent.setup();
        render(<BuilderPage />);
        await user.type(await screen.findByPlaceholderText(/describe the agent/i), 'build one');
        await user.click(screen.getByRole('button', { name: /Send/i }));
        await screen.findByText(/draft updated: Reconciler/i);

        await user.click(await screen.findByRole('button', { name: /Inventory list.*authorize/i }));
        await user.selectOptions(screen.getByLabelText(/^Auth type$/i), 'oauth2');
        await user.type(screen.getByLabelText(/^Access token$/i), 'gho-token-1');
        await user.click(screen.getByRole('button', { name: /Authorize & add/i }));

        await waitFor(() =>
            expect(rpcParams['connector.create']).toEqual(
                expect.objectContaining({
                    type: 'oauth2',
                    auth_config: { access_token: 'gho-token-1' },
                })
            )
        );
        expect(rpcParams['connector.connect']).toEqual({ connector_id: 'connector.inv' });
        await waitFor(() =>
            expect(apiPutMock).toHaveBeenCalledWith(
                '/v1/agents-platform/agents/ag-6',
                expect.objectContaining({
                    definition_doc: expect.objectContaining({
                        tools: expect.arrayContaining(['connector.inv.list']),
                    }),
                })
            )
        );
    });

    // #539 R2: sensitive-scope grants require a recorded approval reference.
    const SENSITIVE_CAP = {
        ...CONNECTOR_CAP,
        id: 'connector.erp.billing',
        title: 'ERP billing',
        sensitive_scopes: ['billing.write'],
        route: { kind: 'connector', connector: 'connector.erp' },
    };

    it('sensitive-scope grant blocks submit until approval reference entered', async () => {
        apiGetMock.mockResolvedValue({
            capabilities: [
                { id: 'agent.list', title: 'Agent list', description: 'lists' },
                SENSITIVE_CAP,
            ],
            starters: [],
        });
        apiPostMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agents-platform/compose') return composeOk(DOC);
            if (path === '/v1/agents-platform/agents') return { agent_id: 'ag-7' };
            throw new Error(`unexpected post ${path}`);
        });
        apiPutMock.mockResolvedValue({ updated: true });
        const rpcParams: Record<string, Record<string, unknown>> = {};
        rpcMock.mockImplementation(async (method: string, params?: Record<string, unknown>) => {
            rpcParams[method] = params ?? {};
            if (method === 'connector.list') return { connectors: [] };
            if (method === 'connector.create')
                return { connector_id: 'connector.erp', connector: { id: 'connector.erp' } };
            if (method === 'connector.connect')
                return { connector_id: 'connector.erp', status: 'connected' };
            throw new Error(`unexpected rpc ${method}`);
        });

        const user = userEvent.setup();
        render(<BuilderPage />);
        await user.type(await screen.findByPlaceholderText(/describe the agent/i), 'build one');
        await user.click(screen.getByRole('button', { name: /Send/i }));
        await screen.findByText(/draft updated: Reconciler/i);

        await user.click(await screen.findByRole('button', { name: /ERP billing.*authorize/i }));
        const approvalInput = await screen.findByLabelText(/Approval reference/i);
        await user.type(screen.getByLabelText(/^Key$/i), 'secret-9');

        const submit = screen.getByRole('button', { name: /Authorize & add/i });
        expect(submit).toBeDisabled();
        await user.type(approvalInput, 'APPR-2026-0042');
        expect(submit).toBeEnabled();
        await user.click(submit);

        await waitFor(() =>
            expect(rpcParams['connector.create']).toEqual(
                expect.objectContaining({ sensitive_scopes: ['billing.write'] })
            )
        );
        expect(rpcParams['connector.connect']).toEqual({
            connector_id: 'connector.erp',
            approval_ref: 'APPR-2026-0042',
        });
    });

    it('granted connector chip attaches directly without modal', async () => {
        apiGetMock.mockResolvedValue({
            capabilities: [
                { id: 'agent.list', title: 'Agent list', description: 'lists' },
                CONNECTOR_CAP,
            ],
            starters: [],
        });
        apiPostMock.mockImplementation(async (path: string) => {
            if (path === '/v1/agents-platform/compose') return composeOk(DOC);
            if (path === '/v1/agents-platform/agents') return { agent_id: 'ag-5' };
            throw new Error(`unexpected post ${path}`);
        });
        apiPutMock.mockResolvedValue({ updated: true });
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'connector.list') {
                return {
                    connectors: [
                        { id: 'connector.inv', name: 'Inv', type: 'api_key', status: 'connected' },
                    ],
                };
            }
            throw new Error(`unexpected rpc ${method}`);
        });

        const user = userEvent.setup();
        render(<BuilderPage />);
        await user.type(await screen.findByPlaceholderText(/describe the agent/i), 'build one');
        await user.click(screen.getByRole('button', { name: /Send/i }));
        await screen.findByText(/draft updated: Reconciler/i);

        await user.click(await screen.findByRole('button', { name: /Inventory list.*add/i }));
        expect(screen.queryByText(/Authorize connector/i)).not.toBeInTheDocument();
        await waitFor(() =>
            expect(apiPutMock).toHaveBeenCalledWith(
                '/v1/agents-platform/agents/ag-5',
                expect.objectContaining({
                    definition_doc: expect.objectContaining({
                        tools: expect.arrayContaining(['connector.inv.list']),
                    }),
                })
            )
        );
        expect(rpcMock.mock.calls.map((c) => c[0])).not.toContain('connector.create');
    });
});
