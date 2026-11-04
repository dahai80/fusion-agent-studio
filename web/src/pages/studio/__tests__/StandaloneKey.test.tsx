// #584: standalone GUI auth — runtime access key entry on the API Keys
// page. The card renders only in standalone mode; saving persists via
// setStandaloneApiKey (localStorage in the real sdk) and every subsequent
// request carries it (covered by the sdk-level test in src/__tests__).
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const rpcMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());
const keyState = vi.hoisted(() => ({ embedded: false, key: '' }));

vi.mock('../../../api', () => ({
    rpc: (...args: unknown[]) => rpcMock(...args),
    api: {},
}));

vi.mock('../../../sdk', () => ({
    sdk: {
        i18n: { lang: () => 'en', subscribe: () => () => {} },
        toast: { show: (...args: unknown[]) => toastMock(...args) },
    },
    isEmbedded: () => keyState.embedded,
    embeddedParam: () => false,
    standaloneApiKey: () => (keyState.embedded ? '' : keyState.key),
    setStandaloneApiKey: (value: string) => {
        keyState.key = value.trim();
    },
}));

import { ApikeysPage } from '../ApikeysPage';

describe('Standalone access key card (#584)', () => {
    beforeEach(() => {
        keyState.embedded = false;
        keyState.key = '';
        rpcMock.mockReset();
        toastMock.mockReset();
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'apikey.list') return { keys: [] };
            throw new Error(`unexpected rpc ${method}`);
        });
    });

    it('saves a pasted access key and reports configured', async () => {
        const user = userEvent.setup();
        render(<ApikeysPage />);
        const card = await screen.findByTestId('standalone-access-key');
        expect(card).toBeTruthy();
        expect(screen.getByTestId('standalone-key-state').textContent).toContain('not configured');
        await user.type(screen.getByTestId('standalone-key-input'), 'fk-admin-key-123');
        await user.click(screen.getByTestId('standalone-key-save'));
        expect(keyState.key).toBe('fk-admin-key-123');
        expect(screen.getByTestId('standalone-key-state').textContent).toContain('configured');
        expect(toastMock).toHaveBeenCalledWith(expect.objectContaining({ level: 'success' }));
    });

    it('clears the stored access key', async () => {
        const user = userEvent.setup();
        keyState.key = 'fk-existing';
        render(<ApikeysPage />);
        await screen.findByTestId('standalone-access-key');
        expect(screen.getByTestId('standalone-key-state').textContent).toContain('configured');
        await user.click(screen.getByTestId('standalone-key-clear'));
        expect(keyState.key).toBe('');
        expect(screen.getByTestId('standalone-key-state').textContent).toContain('not configured');
    });

    it('renders no card in embedded mode (no keys in UI invariant)', async () => {
        keyState.embedded = true;
        render(<ApikeysPage />);
        await screen.findByText(/API Keys/i);
        expect(screen.queryByTestId('standalone-access-key')).toBeNull();
    });
});
