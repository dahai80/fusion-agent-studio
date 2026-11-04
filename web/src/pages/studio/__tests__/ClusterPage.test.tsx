// #583: cluster ops panel — stats header, task actions (cancel/degrade/
// migrate) call the daemon proxy, autoscaler config PUT.
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiGet = vi.hoisted(() => vi.fn());
const apiPost = vi.hoisted(() => vi.fn());
const apiPut = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    apiGet: (...args: unknown[]) => apiGet(...args),
    apiPost: (...args: unknown[]) => apiPost(...args),
    apiPut: (...args: unknown[]) => apiPut(...args),
    rpc: vi.fn(),
    api: {},
}));

vi.mock('../../../sdk', () => ({
    sdk: {
        i18n: { lang: () => 'en', subscribe: () => () => {} },
        toast: { show: (...args: unknown[]) => toastMock(...args) },
    },
    isEmbedded: () => false,
    embeddedParam: () => false,
    standaloneApiKey: () => '',
}));

import { ClusterPage } from '../ClusterPage';

describe('ClusterPage (#583)', () => {
    beforeEach(() => {
        apiGet.mockReset();
        apiPost.mockReset();
        apiPut.mockReset();
        toastMock.mockReset();
        apiGet.mockImplementation(async (path: string) => {
            if (path === '/v1/cluster-ops/stats')
                return {
                    cluster: {
                        online_nodes: 2,
                        total_nodes: 2,
                        active_tasks: 1,
                        total_memory_gb: 64,
                        available_memory_gb: 32,
                        utilization: 0.5,
                    },
                    tasks: { total: 1, completed: 8, failed: 0 },
                };
            if (path === '/v1/cluster-ops/tasks')
                return {
                    total: 1,
                    tasks: [
                        {
                            task_id: 't1',
                            name: 'infer',
                            status: 'running',
                            model_name: 'qwen',
                            assigned_nodes: ['n1'],
                        },
                    ],
                };
            if (path === '/v1/cluster-ops/nodes')
                return {
                    nodes: [{ node_id: 'n1', status: 'online', active_tasks: 1, max_tasks: 4 }],
                };
            if (path === '/v1/cluster-ops/autoscaler/config')
                return { enabled: true, min_nodes: 1 };
            if (path === '/v1/cluster-ops/routing/summary') return { strategy: 'score' };
            throw new Error(`unexpected GET ${path}`);
        });
        apiPost.mockResolvedValue({ status: 'cancelled' });
        apiPut.mockResolvedValue({ enabled: false });
    });

    it('renders cluster stats in the header', async () => {
        render(<ClusterPage />);
        const { waitFor } = await import('@testing-library/react');
        await waitFor(() => {
            expect(document.body.textContent).toContain('2/2 nodes · 1 running tasks');
        });
    });

    it('lists tasks and fires migrate with the typed target node', async () => {
        const user = userEvent.setup();
        render(<ClusterPage />);
        await screen.findByTestId('cluster-tasks');
        expect(await screen.findByText('t1')).toBeTruthy();
        const inputs = screen.getAllByPlaceholderText('target node');
        await user.type(inputs[0], 'n2');
        await user.click(screen.getByRole('button', { name: 'Migrate' }));
        expect(apiPost).toHaveBeenCalledWith('/v1/cluster-ops/tasks/t1/migrate', {
            target_node_id: 'n2',
        });
        expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ level: 'success' }));
    });

    it('fires degrade with the typed target model', async () => {
        const user = userEvent.setup();
        render(<ClusterPage />);
        await screen.findByTestId('cluster-tasks');
        const inputs = screen.getAllByPlaceholderText('target model');
        await user.type(inputs[0], 'qwen-mini');
        await user.click(screen.getByRole('button', { name: 'Degrade' }));
        expect(apiPost).toHaveBeenCalledWith('/v1/cluster-ops/tasks/t1/degrade', {
            target_model: 'qwen-mini',
        });
    });

    it('switches to nodes and autoscaler tabs', async () => {
        const user = userEvent.setup();
        render(<ClusterPage />);
        await user.click(await screen.findByRole('button', { name: 'Nodes' }));
        expect(await screen.findByTestId('cluster-nodes')).toBeTruthy();
        expect(screen.getByText('n1')).toBeTruthy();
        await user.click(screen.getByRole('button', { name: 'Autoscaler' }));
        expect(await screen.findByTestId('cluster-autoscaler')).toBeTruthy();
    });

    it('saves autoscaler config via PUT', async () => {
        const user = userEvent.setup();
        render(<ClusterPage />);
        await user.click(await screen.findByRole('button', { name: 'Autoscaler' }));
        const box = await screen.findByPlaceholderText('override config fields as JSON');
        const { fireEvent } = await import('@testing-library/react');
        fireEvent.change(box, { target: { value: '{"enabled":false}' } });
        await user.click(screen.getByRole('button', { name: 'Save config' }));
        expect(apiPut).toHaveBeenCalledWith('/v1/cluster-ops/autoscaler/config', {
            enabled: false,
        });
    });

    it('surfaces cluster-unreachable 503 as an error state', async () => {
        apiGet.mockReset();
        apiGet.mockRejectedValue(new Error('request failed: 503'));
        const { waitFor } = await import('@testing-library/react');
        render(<ClusterPage />);
        // useAutoRetry exhausts 3 retries (~7s backoff) before the error state
        await waitFor(
            () => {
                expect(document.body.textContent).toContain('request failed: 503');
            },
            { timeout: 12000 }
        );
    }, 15000);
});
