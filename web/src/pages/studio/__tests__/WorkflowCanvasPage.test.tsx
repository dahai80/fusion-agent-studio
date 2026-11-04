// #489: component tests for the canvas safety pending-actions surface —
// approve/reject fire their RPCs, and an approval failure surfaces the
// error toast instead of silently vanishing.
import { render, screen, waitFor, within } from '@testing-library/react';
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

describe('WorkflowCanvasPage palette from node registry (#552)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        graphsMock.mockReset();
        window.sessionStorage.clear();
        window.localStorage.clear();
    });

    it('renders server-executable palette entries enabled and planned ones disabled', async () => {
        defaultRpcs([{ action_id: 'seed', category: 'bash' }]);
        await renderCanvas();

        // server-executable entries are draggable buttons
        const start = screen.getByRole('button', { name: '+ Start' });
        expect(start).toBeEnabled();
        expect(start).toHaveAttribute('draggable', 'true');
        // planned entries (no server executor) are disabled with a * marker
        const code = screen.getByRole('button', { name: '+ Code *' });
        expect(code).toBeDisabled();
        const http = screen.getByRole('button', { name: '+ HTTP Request *' });
        expect(http).toBeDisabled();
    });

    it('keeps server-native error_handler and verify in the palette', async () => {
        defaultRpcs([{ action_id: 'seed', category: 'bash' }]);
        await renderCanvas();

        expect(screen.getByRole('button', { name: '+ Error Handler' })).toBeEnabled();
        expect(screen.getByRole('button', { name: '+ Verify' })).toBeEnabled();
    });
});

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

describe('WorkflowCanvasPage managed runs (#553)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        graphsMock.mockReset();
        window.sessionStorage.clear();
        window.localStorage.clear();
    });

    class FakeEventSource {
        static instances: FakeEventSource[] = [];
        url: string;
        closed = false;
        onmessage: ((ev: { data: string }) => void) | null = null;
        onerror: (() => void) | null = null;
        constructor(url: string) {
            this.url = url;
            FakeEventSource.instances.push(this);
        }
        close() {
            this.closed = true;
        }
    }

    function stubStream(rpcImpl?: (method: string) => Promise<unknown>) {
        FakeEventSource.instances = [];
        vi.stubGlobal('EventSource', FakeEventSource as unknown as typeof EventSource);
        const fetchMock = vi.fn(async () => ({ ok: true, json: async () => ({}) }));
        vi.stubGlobal('fetch', fetchMock);
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'graph.get')
                return {
                    graph: {
                        nodes: {
                            start: { type: 'start', label: 'start' },
                            llm1: { type: 'llm', label: 'LLM' },
                        },
                        edges: [{ source_id: 'start', target_id: 'llm1' }],
                    },
                };
            if (rpcImpl) return rpcImpl(method);
            return defaultRpcsImpl(method);
        });
        graphsMock.mockResolvedValue({ data: [] });
        return fetchMock;
    }

    function defaultRpcsImpl(method: string) {
        if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
        if (method === 'safety.get_pending_actions')
            return { actions: [{ action_id: 'seed', category: 'bash' }] };
        if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
        throw new Error(`unexpected rpc: ${method}`);
    }

    it('Step runs exactly the first non-start node via only_node', async () => {
        stubStream();
        const user = await renderCanvas();

        await user.click(screen.getByRole('button', { name: 'Step' }));
        await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
        const url = FakeEventSource.instances[0].url;
        expect(url).toContain('only_node=llm1');
        expect(url).not.toContain('only_node=start');
        // run started — Cancel appears, Run is disabled
        expect(await screen.findByRole('button', { name: /Cancel run/ })).toBeInTheDocument();
    });

    it('Cancel closes the stream and POSTs the cancel endpoint', async () => {
        const fetchMock = stubStream();
        const user = await renderCanvas();

        await user.click(screen.getByRole('button', { name: 'Step' }));
        await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
        // server sends run_started before cancel is meaningful
        FakeEventSource.instances[0].onmessage?.({
            data: JSON.stringify({ type: 'run_started', run_id: 'run-abc123' }),
        });
        const cancelBtn = await screen.findByRole('button', { name: /Cancel run/ });
        await user.click(cancelBtn);
        await waitFor(() =>
            expect(fetchMock).toHaveBeenCalledWith(
                expect.stringContaining('/execute/stream/run-abc123/cancel'),
                expect.objectContaining({ method: 'POST' })
            )
        );
        expect(FakeEventSource.instances[0].closed).toBe(true);
        expect(screen.queryByRole('button', { name: /Cancel run/ })).not.toBeInTheDocument();
    });
});

describe('WorkflowCanvasPage inspector secrets + card summary (#554)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        graphsMock.mockReset();
        window.sessionStorage.clear();
        window.localStorage.clear();
    });

    it('card shows the most relevant config value as summary', async () => {
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'graph.get')
                return {
                    graph: {
                        nodes: {
                            start: { type: 'start', label: 'start' },
                            a: { type: 'llm', label: 'LLM', model: 'qwen3' },
                        },
                        edges: [{ source_id: 'start', target_id: 'a' }],
                    },
                };
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions')
                return { actions: [{ action_id: 'seed', category: 'bash' }] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            throw new Error(`unexpected rpc: ${method}`);
        });
        graphsMock.mockResolvedValue({ data: [] });
        await renderCanvas();

        expect(await screen.findByText(/qwen3/)).toBeInTheDocument();
    });

    it('Save strips secret-typed keys from the persisted graph payload', async () => {
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'graph.get')
                return {
                    graph: {
                        nodes: {
                            start: { type: 'start', label: 'start' },
                            a: {
                                type: 'http_request',
                                label: 'HTTP',
                                url: 'https://x',
                                api_key: 'sk-leak-1',
                            },
                        },
                        edges: [{ source_id: 'start', target_id: 'a' }],
                        start_node_id: 'start',
                    },
                };
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions')
                return { actions: [{ action_id: 'seed', category: 'bash' }] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            if (method === 'graph.update') return { issues: [] };
            throw new Error(`unexpected rpc: ${method}`);
        });
        graphsMock.mockResolvedValue({ data: [] });
        const user = await renderCanvas();

        await user.click(screen.getByRole('button', { name: 'Save' }));
        // secret strip is a real diff — the #396 confirm dialog appears
        const confirmBtn = await screen.findByRole('button', { name: 'Save changes' });
        await user.click(confirmBtn);
        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('graph.update', expect.anything())
        );
        const payload = JSON.stringify(rpcMock.mock.calls.find((c) => c[0] === 'graph.update'));
        expect(payload).not.toContain('sk-leak-1');
        expect(payload).toContain('https://x');
    });
});

describe('WorkflowCanvasPage variable inspector (#556)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        graphsMock.mockReset();
        window.sessionStorage.clear();
        window.localStorage.clear();
    });

    it('shows FlowState snapshots from run events in the sidebar', async () => {
        class FakeES {
            static instances: FakeES[] = [];
            url: string;
            closed = false;
            onmessage: ((ev: { data: string }) => void) | null = null;
            onerror: (() => void) | null = null;
            constructor(url: string) {
                this.url = url;
                FakeES.instances.push(this);
            }
            close() {
                this.closed = true;
            }
        }
        vi.stubGlobal('EventSource', FakeES as unknown as typeof EventSource);
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'graph.get')
                return {
                    graph: {
                        nodes: {
                            start: { type: 'start', label: 'start' },
                            llm1: { type: 'llm', label: 'LLM', model: 'qwen3' },
                        },
                        edges: [{ source_id: 'start', target_id: 'llm1' }],
                        start_node_id: 'start',
                    },
                };
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions')
                return { actions: [{ action_id: 'seed', category: 'bash' }] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            throw new Error(`unexpected rpc: ${method}`);
        });
        graphsMock.mockResolvedValue({ data: [] });
        const user = await renderCanvas();

        await user.click(screen.getByRole('button', { name: 'Step' }));
        await waitFor(() => expect(FakeES.instances).toHaveLength(1));
        FakeES.instances[0].onmessage?.({
            data: JSON.stringify({
                type: 'result',
                node_id: 'llm1',
                content: 'ok',
                seq: 1,
                metadata: { flow_state: { region: 'cn', items: ['x', 'y'] } },
            }),
        });

        expect(await screen.findByText('region')).toBeInTheDocument();
        expect(screen.getByText('cn')).toBeInTheDocument();
        expect(screen.getByText('items')).toBeInTheDocument();
        expect(screen.getByText('["x","y"]')).toBeInTheDocument();
    });
});

describe('WorkflowCanvasPage human input HiTL (#557)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        graphsMock.mockReset();
        window.sessionStorage.clear();
        window.localStorage.clear();
    });

    it('approval frame with kind=human_input renders the decision card; approve responds via rpc', async () => {
        class FakeES {
            static instances: FakeES[] = [];
            url: string;
            closed = false;
            onmessage: ((ev: { data: string }) => void) | null = null;
            onerror: (() => void) | null = null;
            constructor(url: string) {
                this.url = url;
                FakeES.instances.push(this);
            }
            close() {
                this.closed = true;
            }
        }
        vi.stubGlobal('EventSource', FakeES as unknown as typeof EventSource);
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'graph.get')
                return {
                    graph: {
                        nodes: {
                            start: { type: 'start', label: 'start' },
                            ask: { type: 'human_input', label: 'Ask', prompt: 'Deploy?' },
                        },
                        edges: [{ source_id: 'start', target_id: 'ask' }],
                        start_node_id: 'start',
                    },
                };
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions')
                return { actions: [{ action_id: 'seed', category: 'bash' }] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            if (method === 'human_input.respond') return { status: 'ok' };
            throw new Error(`unexpected rpc: ${method}`);
        });
        graphsMock.mockResolvedValue({ data: [] });
        const user = await renderCanvas();

        await user.click(screen.getByRole('button', { name: 'Step' }));
        await waitFor(() => expect(FakeES.instances).toHaveLength(1));
        FakeES.instances[0].onmessage?.({
            data: JSON.stringify({
                type: 'safety_approval',
                node_id: 'ask',
                content: 'Proceed with deploy?',
                seq: 2,
                metadata: {
                    kind: 'human_input',
                    input_id: 'hi-abc',
                    input_var: 'decision',
                    prompt: 'Proceed with deploy?',
                    fields: [],
                },
            }),
        });

        expect(await screen.findByText('Awaiting human input (1)')).toBeInTheDocument();

        await user.type(
            screen.getByPlaceholderText('Value (reason when rejecting)'),
            'ship it'
        );
        // scope to the human-input card — the seeded bash action card also has an Approve button
        const card = screen
            .getByPlaceholderText('Value (reason when rejecting)')
            .closest('div[class*="border-brand"]') as HTMLElement;
        await user.click(within(card).getByRole('button', { name: 'Approve' }));
        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith(
                'human_input.respond',
                expect.objectContaining({ input_id: 'hi-abc', approved: true, value: 'ship it' })
            )
        );
        // card clears after the decision
        await waitFor(() =>
            expect(screen.queryByText('Awaiting human input (1)')).not.toBeInTheDocument()
        );
    });
});

describe('WorkflowCanvasPage template gallery + run history (#558)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        graphsMock.mockReset();
        window.sessionStorage.clear();
        window.localStorage.clear();
    });

    function templateRpcs() {
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions') return { actions: [] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            if (method === 'graph.get') return { graph: { nodes: [], edges: [] } };
            if (method === 'flow.template_list')
                return {
                    templates: [
                        {
                            name: 'basic-llm-prompt',
                            description: 'Single LLM call',
                            source: 'builtin',
                        },
                        {
                            name: 'my-tpl',
                            description: 'saved template',
                            source: 'tenant',
                            flow_id: 'flow-t1',
                        },
                    ],
                };
            if (method === 'flow.template_instantiate')
                return { graph: { nodes: [], edges: [] }, graph_id: 'newg' };
            if (method === 'graph.create') return { graph_id: 'g-new' };
            throw new Error(`unexpected rpc: ${method}`);
        });
        graphsMock.mockResolvedValue({ data: [] });
    }

    it('empty canvas opens the template gallery and instantiate fires template_instantiate + graph.create', async () => {
        templateRpcs();
        const user = userEvent.setup();
        render(<WorkflowCanvasPage />);

        expect(await screen.findByText('Template gallery', undefined, { timeout: 3000 })).toBeInTheDocument();
        expect(screen.getByText('basic-llm-prompt')).toBeInTheDocument();
        expect(screen.getByText('builtin')).toBeInTheDocument();
        expect(screen.getByText('custom')).toBeInTheDocument();

        await user.click(screen.getByText('my-tpl'));
        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('flow.template_instantiate', { name: '', flow_id: 'flow-t1' })
        );
        await waitFor(() => expect(rpcMock).toHaveBeenCalledWith('graph.create', expect.anything()));
    });

    it('run history panel lists archived runs, replay + diff fire history RPCs', async () => {
        templateRpcs();
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'budget.status') return { remaining: 800, max_tokens: 1000 };
            if (method === 'safety.get_pending_actions') return { actions: [] };
            if (method === 'tool.list') return { tools: [{ name: 'read_file' }] };
            if (method === 'graph.get') return { graph: { nodes: [], edges: [] } };
            if (method === 'run.history_list')
                return {
                    runs: [
                        {
                            run_id: 'r1',
                            status: 'completed',
                            finished_at: 1700000000,
                            node_status: { s: 'done', llm: 'done' },
                        },
                        {
                            run_id: 'r2',
                            status: 'failed',
                            finished_at: 1700000100,
                            node_status: { s: 'done', llm: 'error' },
                        },
                    ],
                };
            if (method === 'run.history_get')
                return {
                    run: {
                        node_status: { s: 'done', llm: 'done' },
                        events: [{ type: 'end', node_id: 'e', content: 'ok', timestamp: 1 }],
                    },
                };
            if (method === 'run.diff')
                return {
                    diff: {
                        nodes: [{ node_id: 'llm', a: 'done', b: 'error', changed: true }],
                        changed_count: 1,
                    },
                };
            throw new Error(`unexpected rpc: ${method}`);
        });
        const user = userEvent.setup();
        window.sessionStorage.setItem('fusion.canvas.graphId', 'g1');
        render(<WorkflowCanvasPage />);
        await screen.findByRole('button', { name: /Run history/ }, { timeout: 3000 });

        await user.click(screen.getByRole('button', { name: /Run history/ }));
        expect(await screen.findByText('completed')).toBeInTheDocument();
        expect(screen.getByText('failed')).toBeInTheDocument();
        expect(screen.getByText(/llm:done/)).toBeInTheDocument();

        const r1Card = screen.getByText('completed').closest('div[class*="rounded"]') as HTMLElement;
        await user.click(within(r1Card).getByRole('button', { name: 'Replay' }));
        await waitFor(() => expect(rpcMock).toHaveBeenCalledWith('run.history_get', { run_id: 'r1' }));

        const boxes = screen.getAllByTitle('Pick two to diff');
        await user.click(boxes[0]);
        await user.click(boxes[1]);
        await user.click(screen.getByRole('button', { name: 'Diff selected runs' }));
        await waitFor(() =>
            expect(rpcMock).toHaveBeenCalledWith('run.diff', { run_a: 'r1', run_b: 'r2' })
        );
        expect(await screen.findByText(/Changed nodes: 1/)).toBeInTheDocument();
        expect(screen.getByText(/llm: done \/ error/)).toBeInTheDocument();
    });
});
