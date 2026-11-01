// #534 (FCS-1): five-zone composer — fixed icon-only ⬆ send with three
// states (never label switching), Enter/Esc shortcuts, hint row,
// attachment chips (⌘V image), ⚙ params popup.
import { fireEvent, render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../i18n', () => ({
    useT: () => (_zh: string, en: string) => en,
}));

import { FcsComposer } from '../FcsComposer';

function setup(over: Partial<Parameters<typeof FcsComposer>[0]> = {}) {
    const onSend = vi.fn();
    const onStop = vi.fn();
    const onChange = vi.fn();
    const onPickFiles = vi.fn();
    const onRemoveAttachment = vi.fn();
    const utils = render(
        <FcsComposer
            value=""
            onChange={onChange}
            onSend={onSend}
            streaming={false}
            onStop={onStop}
            attachments={[]}
            onPickFiles={onPickFiles}
            onRemoveAttachment={onRemoveAttachment}
            placeholder="Type a message, @ reference, / skill"
            {...over}
        />
    );
    return { onSend, onStop, onChange, onPickFiles, onRemoveAttachment, ...utils };
}

describe('FcsComposer (#534, FCS-1)', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    it('send is icon-only: disabled when empty, enabled with content, no text label', () => {
        const empty = setup();
        const btn = empty.getByRole('button', { name: 'Send' });
        expect(btn).toBeDisabled();
        expect(btn.textContent).toBe(''); // FCS §2: never a text label
        empty.unmount();

        const filled = setup({ value: 'hello' });
        expect(filled.getByRole('button', { name: 'Send' })).toBeEnabled();
    });

    it('generating state turns the same button into ⏹ stop; Esc also stops', () => {
        const r = setup({ value: 'hello', streaming: true });
        const btn = r.getByRole('button', { name: 'Stop' });
        expect(btn.textContent).toBe('');
        fireEvent.click(btn);
        expect(r.onStop).toHaveBeenCalledTimes(1);
        expect(r.onSend).not.toHaveBeenCalled();

        fireEvent.keyDown(r.getByPlaceholderText(/Type a message/), {
            key: 'Escape',
        });
        expect(r.onStop).toHaveBeenCalledTimes(2);
    });

    it('Enter sends, Shift+Enter does not', () => {
        const r = setup({ value: 'hello' });
        const ta = r.getByPlaceholderText(/Type a message/);
        fireEvent.keyDown(ta, { key: 'Enter' });
        expect(r.onSend).toHaveBeenCalledTimes(1);
        fireEvent.keyDown(ta, { key: 'Enter', shiftKey: true });
        expect(r.onSend).toHaveBeenCalledTimes(1);
    });

    it('hint row is always present; Esc stop hint appears while generating', () => {
        const idle = setup();
        expect(idle.getByText(/Enter to send · Shift\+Enter for newline/)).toBeInTheDocument();
        expect(idle.queryByText(/Esc to stop/)).toBeNull();

        const busy = setup({ streaming: true });
        expect(busy.getByText(/Esc to stop/)).toBeInTheDocument();
    });

    it('pasted images become attachment chips and can be removed', () => {
        const r = setup({ attachments: [{ id: 'a1', name: 'shot.png' }] });
        expect(r.getByText('shot.png')).toBeInTheDocument();
        fireEvent.click(r.getByRole('button', { name: 'remove attachment' }));
        expect(r.onRemoveAttachment).toHaveBeenCalledWith('a1');

        const file = new File(['x'], 'paste.png', { type: 'image/png' });
        fireEvent.paste(r.getByPlaceholderText(/Type a message/), {
            clipboardData: { files: [file] },
        });
        expect(r.onPickFiles).toHaveBeenCalledWith([file]);
    });

    it('⚙ popup shows the params content (显示思考 lives there, not in the row)', () => {
        const r = setup({
            paramsPopup: <label>show-thinking-switch</label>,
        });
        expect(r.queryByText('show-thinking-switch')).toBeNull();
        fireEvent.click(r.getByRole('button', { name: 'Advanced parameters' }));
        expect(r.getByText('show-thinking-switch')).toBeInTheDocument();
        fireEvent.click(r.getByRole('button', { name: 'close parameters' }));
        expect(r.queryByText('show-thinking-switch')).toBeNull();
    });
});
