// #584: sdk-level key attach — standalone mode attaches the runtime key as
// x-api-key on every http call (localStorage, falling back to the build-time
// VITE_API_KEY); embedded mode never produces a key.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@fusion/web-sdk', () => ({
    HttpError: class HttpError extends Error {
        status: number;
        constructor(status: number) {
            super(`http ${status}`);
            this.status = status;
        }
    },
    createShellSdk: () => ({
        http: {
            request: () => {
                throw new Error('host bridge unavailable in tests');
            },
        },
        toast: { show: () => {} },
    }),
}));

import { STANDALONE_KEY_STORAGE, setStandaloneApiKey, standaloneApiKey } from '../sdk';

describe('standaloneApiKey (#584)', () => {
    beforeEach(() => {
        localStorage.removeItem(STANDALONE_KEY_STORAGE);
    });
    afterEach(() => {
        localStorage.removeItem(STANDALONE_KEY_STORAGE);
    });

    it('defaults to empty without a stored key or build-time key', () => {
        expect(standaloneApiKey(false)).toBe('');
    });

    it('returns the stored key in standalone mode', () => {
        setStandaloneApiKey('  fk-runtime-key  ');
        expect(localStorage.getItem(STANDALONE_KEY_STORAGE)).toBe('fk-runtime-key');
        expect(standaloneApiKey(false)).toBe('fk-runtime-key');
    });

    it('clears the key on empty input', () => {
        setStandaloneApiKey('fk-runtime-key');
        setStandaloneApiKey('');
        expect(localStorage.getItem(STANDALONE_KEY_STORAGE)).toBeNull();
        expect(standaloneApiKey(false)).toBe('');
    });

    it('never returns a key in embedded mode', () => {
        setStandaloneApiKey('fk-runtime-key');
        expect(standaloneApiKey(true)).toBe('');
    });

    it('survives unavailable storage', () => {
        const orig = Storage.prototype.setItem;
        Storage.prototype.setItem = () => {
            throw new Error('quota');
        };
        expect(() => setStandaloneApiKey('fk-x')).not.toThrow();
        Storage.prototype.setItem = orig;
    });
});

describe('keyed http attach (#584)', () => {
    it('attaches the runtime key as x-api-key on every call', async () => {
        setStandaloneApiKey('fk-runtime-key');
        const fetchMock = vi.fn(async (_path: string, _init?: RequestInit) => ({
            ok: true,
            status: 200,
            json: async () => ({ ok: 1 }),
        }));
        vi.stubGlobal('fetch', fetchMock);
        try {
            const { sdk } = await import('../sdk');
            await sdk.http.request('/v1/agent-platform/approvals', {
                method: 'POST',
                body: { decided_by: 'owner-a' },
            });
            const init = fetchMock.mock.calls[0]?.[1] as RequestInit;
            const headers = init.headers as Record<string, string>;
            expect(headers['x-api-key']).toBe('fk-runtime-key');
        } finally {
            vi.unstubAllGlobals();
            setStandaloneApiKey('');
        }
    });
});
