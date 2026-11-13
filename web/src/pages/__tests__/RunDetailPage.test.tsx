// E4: RunDetailPage Inspector tabs — tab switching, tool_stats table,
// thread lanes with ?event= deep-link highlight.
import { render, screen, fireEvent } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const runMock = vi.hoisted(() => vi.fn());
const runEventsMock = vi.hoisted(() => vi.fn());
const runResultMock = vi.fn();
const toolStatsMock = vi.hoisted(() => vi.fn());
const threadLanesMock = vi.hoisted(() => vi.fn());

vi.mock('../../api', () => ({
    api: {
        run: (...a: unknown[]) => runMock(...a),
        runEvents: (...a: unknown[]) => runEventsMock(...a),
        runResult: (...a: unknown[]) => runResultMock(...a),
        cancelRun: vi.fn(),
    },
    apiPost: vi.fn(),
    statusKind: (s: string) => (s === 'completed' ? 'success' : 'running'),
    inspectorApi: {
        toolStats: (...a: unknown[]) => toolStatsMock(...a),
        threadLanes: (...a: unknown[]) => threadLanesMock(...a),
    },
}));

vi.mock('@fusion/web-sdk', () => ({
    createPoller: () => ({ start: vi.fn(), stop: vi.fn() }),
}));

vi.mock('../../sdk', () => ({
    sdk: {
        i18n: { lang: () => 'en', subscribe: () => () => {} },
        toast: { show: vi.fn() },
        tab: { setActiveTask: vi.fn() },
        http: { request: vi.fn() },
    },
    isEmbedded: () => false,
}));

vi.mock('../../components/StreamPanel', () => ({
    StreamPanel: () => <div data-testid="stream-panel" />,
}));

import { RunDetailPage } from '../RunDetailPage';

const RUN = {
    run_id: 'run-x',
    status: 'completed',
    progress: { events: 5, artifact_ids: ['art-1'], retry_count: 0 },
    error: '',
    agent_id: '',
    graph_id: 'g1',
    created_at: 1700000000,
    updated_at: 1700000001,
};

const EVENTS = {
    run_status: 'completed',
    stop_reason: 'end',
    run_start: 1,
    run_end: 2,
    nodes: [
        { node_id: 'n1', start: 1, end: 1.5, status: 'completed', tool_calls: 1, token_events: 2, tokens: 10, error: '' },
    ],
};

beforeEach(() => {
    runMock.mockReset().mockResolvedValue(RUN);
    runEventsMock.mockReset().mockResolvedValue(EVENTS);
    runResultMock.mockReset().mockResolvedValue({ run_id: 'run-x', status: 'completed', steps: [], artifacts: [], last_result: {}, last_error: '' });
    toolStatsMock.mockReset().mockResolvedValue({
        tools: [{ tool: 'read_file', calls: 2, failures: 0, failure_rate: 0, median_ms: 12, p95_ms: 20 }],
    });
    threadLanesMock.mockReset().mockResolvedValue({
        lanes: [
            {
                lane_id: 'e1',
                label: 'start',
                root_type: 'start',
                root_name: 'start',
                event_count: 2,
                max_depth: 1,
                t_start: 1,
                t_end: 2,
                events: [
                    { event_id: 'e1', parent_id: '', type: 'start', name: 'start', node_id: 'n1', timestamp: 1, depth: 0 },
                    { event_id: 'ev-9', parent_id: 'e1', type: 'tool_call', name: 'read_file', node_id: 'n1', timestamp: 1.5, depth: 1 },
                ],
            },
        ],
        lane_count: 1,
    });
    window.location.hash = '#/runs/run-x';
});

describe('RunDetailPage Inspector tabs (E4)', () => {
    it('renders summary by default and switches to Tools tab with stats', async () => {
        render(<RunDetailPage runId="run-x" />);
        await screen.findByTestId('run-detail-tabs');
        expect(screen.getByTestId('tab-summary')).toBeTruthy();
        fireEvent.click(screen.getByTestId('tab-tools'));
        const row = await screen.findByTestId('tool-row-read_file');
        expect(row.textContent).toContain('read_file');
        expect(row.textContent).toContain('12ms');
    });

    it('drills from Tools row into Threads tab', async () => {
        render(<RunDetailPage runId="run-x" />);
        await screen.findByTestId('run-detail-tabs');
        fireEvent.click(screen.getByTestId('tab-tools'));
        fireEvent.click(await screen.findByTestId('tool-row-read_file'));
        const threads = await screen.findByTestId('threads-tab');
        expect(threads.textContent).toContain('read_file');
    });

    it('?event= deep link opens Threads and highlights the event', async () => {
        window.location.hash = '#/runs/run-x?event=ev-9';
        render(<RunDetailPage runId="run-x" />);
        const threads = await screen.findByTestId('threads-tab');
        const hit = screen.getByTestId('lane-event-ev-9');
        expect(hit.className).toContain('bg-warning');
        expect(threads).toBeTruthy();
    });

    it('Events tab lists node spans with rerun', async () => {
        render(<RunDetailPage runId="run-x" />);
        await screen.findByTestId('run-detail-tabs');
        fireEvent.click(screen.getByTestId('tab-events'));
        const tab = await screen.findByTestId('events-tab');
        expect(tab.textContent).toContain('n1');
    });

    it('Resources tab shows artifact ids', async () => {
        render(<RunDetailPage runId="run-x" />);
        await screen.findByTestId('run-detail-tabs');
        fireEvent.click(screen.getByTestId('tab-resources'));
        const tab = await screen.findByTestId('resources-tab');
        expect(tab.textContent).toContain('art-1');
    });
});
