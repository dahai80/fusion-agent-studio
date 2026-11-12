// E6 web surface: PlaygroundPage — direct replay + diff flag rendering.
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const replayMock = vi.hoisted(() => vi.fn());
const fromTraceMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    api: {},
    rpc: vi.fn(),
    apiBase: () => '',
    inspectorApi: {
        playgroundReplay: (...a: unknown[]) => replayMock(...a),
        playgroundFromTrace: (...a: unknown[]) => fromTraceMock(...a),
    },
}));

vi.mock('../../../sdk', () => ({
    sdk: {
        i18n: { lang: () => 'en', subscribe: () => () => {} },
        toast: { show: vi.fn() },
        http: { request: vi.fn() },
    },
    isEmbedded: () => false,
}));

import { PlaygroundPage } from '../PlaygroundPage';

beforeEach(() => {
    replayMock.mockReset().mockResolvedValue({
        output: 'replayed answer',
        duration_ms: 42,
    });
    fromTraceMock.mockReset().mockResolvedValue({
        output: 'new answer',
        baseline_output: 'old answer',
        diff: { changed: true, unified: '-old answer\n+new answer' },
    });
});

describe('PlaygroundPage (E6)', () => {
    it('direct replay renders output', async () => {
        render(<PlaygroundPage />);
        fireEvent.click(await screen.findByTestId('mode-direct'));
        const boxes = screen.getAllByRole('textbox');
        fireEvent.input(boxes[1], { target: { value: 'hello' } });
        fireEvent.click(screen.getByText('Replay'));
        const result = await screen.findByTestId('playground-result');
        expect(result.textContent).toContain('replayed answer');
        expect(replayMock).toHaveBeenCalledWith(
            expect.objectContaining({
                messages: [{ role: 'user', content: 'hello' }],
            })
        );
    });

    it('trace mode shows diff vs baseline', async () => {
        render(<PlaygroundPage />);
        fireEvent.click(await screen.findByTestId('mode-trace'));
        fireEvent.input(screen.getByPlaceholderText('trace-…'), { target: { value: 'tr-1' } });
        fireEvent.click(screen.getByText('Replay'));
        await waitFor(async () => {
            expect(screen.getByTestId('playground-diff-flag').textContent).toContain(
                'differs from baseline'
            );
        });
        expect(screen.getByTestId('playground-diff').textContent).toContain('+new answer');
        expect(fromTraceMock).toHaveBeenCalledWith(expect.objectContaining({ trace_id: 'tr-1' }));
    });
});
