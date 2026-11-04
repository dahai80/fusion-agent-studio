// #583: observability page — alerts, suggestions, log export button.
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const apiGet = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());
const fetchMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    apiGet: (...args: unknown[]) => apiGet(...args),
    apiBase: () => '',
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

import { ObservabilityPage } from '../ObservabilityPage';

describe('ObservabilityPage (#583)', () => {
    beforeEach(() => {
        apiGet.mockReset();
        toastMock.mockReset();
        fetchMock.mockReset();
        apiGet.mockImplementation(async (path: string) => {
            if (path === '/v1/cluster-ops/observability/alerts')
                return { alerts: [{ level: 'warn', message: 'node n1 slow', node_id: 'n1' }] };
            if (path === '/v1/cluster-ops/observability/suggestions')
                return { suggestions: [{ title: 'scale up', detail: 'cpu high' }], error: null };
            throw new Error(`unexpected GET ${path}`);
        });
    });

    it('renders alerts and suggestions', async () => {
        render(<ObservabilityPage />);
        expect(await screen.findByTestId('obs-alerts')).toBeTruthy();
        expect(screen.getByText('node n1 slow')).toBeTruthy();
        expect(await screen.findByTestId('obs-suggestions')).toBeTruthy();
        expect(screen.getByText('scale up')).toBeTruthy();
    });

    it('exports logs via the proxy endpoint', async () => {
        const userEvent = (await import('@testing-library/user-event')).default;
        const user = userEvent.setup();
        fetchMock.mockResolvedValue({
            ok: true,
            status: 200,
            blob: async () => new Blob(['{"task_id": "t1"}']),
        });
        vi.stubGlobal('fetch', fetchMock);
        const revoke = vi.fn();
        const createUrl = vi.fn(() => 'blob:x');
        vi.stubGlobal(
            'URL',
            Object.assign(URL, { createObjectURL: createUrl, revokeObjectURL: revoke })
        );
        try {
            render(<ObservabilityPage />);
            await user.click(await screen.findByTestId('obs-export'));
            expect(fetchMock).toHaveBeenCalledWith('/v1/cluster-ops/observability/logs/export');
            expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ level: 'success' }));
        } finally {
            vi.unstubAllGlobals();
        }
    });

    it('surfaces proxy errors on both lists', async () => {
        apiGet.mockReset();
        apiGet.mockRejectedValue(new Error('request failed: 503'));
        const { waitFor } = await import('@testing-library/react');
        render(<ObservabilityPage />);
        // useAutoRetry exhausts 3 retries (~7s backoff) before the error state
        await waitFor(
            () => {
                expect(document.body.textContent?.match(/request failed: 503/g)?.length).toBe(2);
            },
            { timeout: 12000 }
        );
    }, 15000);
});
