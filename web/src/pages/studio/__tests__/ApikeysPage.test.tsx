// #537-1: the created-key usage example must target the deployed-agent
// test route (/v1/agents-platform/agents/<id>/test); the old
// /v1/agents/<id>/test path 404s for platform agents when copied verbatim.
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const rpcMock = vi.hoisted(() => vi.fn());
const toastMock = vi.hoisted(() => vi.fn());

vi.mock('../../../api', () => ({
    rpc: (...args: unknown[]) => rpcMock(...args),
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
    setStandaloneApiKey: () => {},
}));

import { ApikeysPage } from '../ApikeysPage';

describe('ApikeysPage example path (#537-1)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        toastMock.mockReset();
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'apikey.list') return { keys: [] };
            if (method === 'apikey.create') return { key: { secret: 'fk-example' } };
            throw new Error(`unexpected rpc ${method}`);
        });
    });

    it('new key example calls the agents-platform test route', async () => {
        const user = userEvent.setup();
        render(<ApikeysPage />);
        await user.click(await screen.findByRole('button', { name: /Create key/i }));
        await user.type(await screen.findByRole('textbox'), 'ops');
        await user.click(screen.getByRole('button', { name: /^Create$/i }));

        const example = await screen.findByText(/curl -X POST/);
        expect(example.textContent).toContain('/v1/agents-platform/agents/<agent_id>/test');
        expect(example.textContent).not.toMatch(/\/v1\/agents\/<agent_id>\/test/);
        expect(example.textContent).toContain('Bearer fk-example');
    });
});

describe('ApikeysPage bootstrap guidance (#543)', () => {
    beforeEach(() => {
        rpcMock.mockReset();
        toastMock.mockReset();
        rpcMock.mockImplementation(async (method: string) => {
            if (method === 'apikey.list') return { keys: [] };
            throw new Error(`unexpected rpc ${method}`);
        });
    });

    it('shows bootstrap guidance with rotation flow on first run', async () => {
        render(<ApikeysPage />);
        const hint = await screen.findByTestId('key-bootstrap-hint');
        expect(hint.textContent).toContain('How to get a working key');
        expect(hint.textContent).toContain('shown once');
        expect(hint.textContent).toContain('cannot be recovered');
        expect(hint.textContent).toContain('Rotating');
        expect(hint.textContent).toContain('Authorization: Bearer');
    });
});
