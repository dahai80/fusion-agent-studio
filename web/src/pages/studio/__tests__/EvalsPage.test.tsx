// E2 web surface: EvalsPage — dataset list + run with pass-rate report.
import { render, screen, fireEvent } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const evalListMock = vi.hoisted(() => vi.fn());
const evalRunMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    api: {},
    apiGet: vi.fn(),
    apiPost: vi.fn(),
    rpc: vi.fn(),
    apiBase: () => '',
    statusKind: vi.fn(),
    inspectorApi: {
        evalList: (...a: unknown[]) => evalListMock(...a),
        evalRun: (...a: unknown[]) => evalRunMock(...a),
    },
}));

vi.mock('../../../sdk', () => ({
    sdk: {
        i18n: { lang: () => 'en', subscribe: () => () => {} },
        toast: { show: (...args: unknown[]) => toastMock(...args) },
        http: { request: vi.fn() },
    },
    isEmbedded: () => false,
}));

import { EvalsPage } from '../EvalsPage';

beforeEach(() => {
    evalListMock.mockReset().mockResolvedValue({
        datasets: [{ name: 'smoke.yaml', path: '/evals/smoke.yaml', cases: 2 }],
    });
    evalRunMock.mockReset().mockResolvedValue({
        cases: [
            { case_id: 'c1', passed: true },
            { case_id: 'c2', passed: false, error: 'mismatch' },
        ],
    });
});

describe('EvalsPage (E2)', () => {
    it('lists datasets and runs one with pass-rate report', async () => {
        render(<EvalsPage />);
        const row = await screen.findByTestId('eval-run-0');
        expect(screen.getByText('smoke.yaml')).toBeTruthy();
        const graphInput = screen.getByPlaceholderText('graph-uuid');
        fireEvent.input(graphInput, { target: { value: 'g1' } });
        fireEvent.click(row);
        const report = await screen.findByTestId('eval-report');
        expect(screen.getByTestId('eval-pass-rate').textContent).toBe('1/2');
        expect(report.textContent).toContain('c2');
        expect(evalRunMock).toHaveBeenCalledWith(
            expect.objectContaining({ dataset_path: '/evals/smoke.yaml', graph_id: 'g1' })
        );
    });

    it('empty state when no datasets', async () => {
        evalListMock.mockResolvedValue({ datasets: [] });
        render(<EvalsPage />);
        await screen.findByText('no eval datasets');
    });
});
