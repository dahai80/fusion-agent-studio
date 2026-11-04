import { type FusionShellSdk, HttpError, type HttpResult, createShellSdk } from '@fusion/web-sdk';

// Single SDK instance for the module. Embedded (inside union-web shell
// iframe): http goes through the host postMessage bridge — the host injects
// the module api_key, the UI holds no keys (spec S: no auth logic in UI).
// Standalone (own Vite server / daemon static host): same-origin direct
// fetch, with the runtime access key (Settings entry on the API Keys page,
// persisted to localStorage — #584) attached as x-api-key on every http
// call; VITE_API_KEY remains a build-time fallback for dev builds. In
// embedded mode no key ever exists here — the host bridge injects it.
const DEV_API_KEY: string | undefined = import.meta.env.VITE_API_KEY;

export const isEmbedded = (): boolean => typeof window !== 'undefined' && window.parent !== window;

// #584: standalone-only runtime key. Never read in embedded mode — the
// shell-host invariant is that the UI holds no keys inside the iframe.
export const STANDALONE_KEY_STORAGE = 'fusion.standalone_api_key';

export function standaloneApiKey(embedded: boolean = isEmbedded()): string {
    if (embedded) return '';
    try {
        return localStorage.getItem(STANDALONE_KEY_STORAGE) ?? DEV_API_KEY ?? '';
    } catch {
        return DEV_API_KEY ?? '';
    }
}

export function setStandaloneApiKey(value: string): void {
    try {
        const trimmed = value.trim();
        if (trimmed) localStorage.setItem(STANDALONE_KEY_STORAGE, trimmed);
        else localStorage.removeItem(STANDALONE_KEY_STORAGE);
    } catch {
        // storage unavailable (private mode etc.) — nothing to persist
    }
}

// ?embedded=1 is added by the shell when loading the module; standalone
// keeps the module's own bottom nav. When embedded the shell owns chrome,
// so the module hides its own navigation.
export const embeddedParam = (): boolean => {
    try {
        return new URLSearchParams(window.location.search).get('embedded') === '1';
    } catch {
        return false;
    }
};

async function keyedRequest<T>(
    path: string,
    method: 'GET' | 'POST' | 'PUT' | 'DELETE',
    body?: unknown,
    extraHeaders?: Record<string, string>
): Promise<HttpResult<T>> {
    const res = await fetch(path, {
        method,
        headers: {
            'content-type': 'application/json',
            'x-api-key': standaloneApiKey(),
            ...(extraHeaders ?? {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = (await res.json().catch(() => null)) as T;
    if (!res.ok) {
        throw new HttpError(res.status, 'http_error', `request failed: ${res.status}`);
    }
    return { status: res.status, ok: true, data };
}

interface KeyedInit {
    method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
    body?: unknown;
    headers?: Record<string, string>;
}

function keyedHttp() {
    return {
        request: <T>(path: string, init?: KeyedInit) =>
            keyedRequest<T>(path, init?.method ?? 'GET', init?.body, init?.headers),
        get: <T>(path: string) => keyedRequest<T>(path, 'GET'),
        post: <T>(path: string, body?: unknown) => keyedRequest<T>(path, 'POST', body),
        put: <T>(path: string, body?: unknown) => keyedRequest<T>(path, 'PUT', body),
        delete: <T>(path: string) => keyedRequest<T>(path, 'DELETE'),
    };
}

export const sdk: FusionShellSdk = createShellSdk();

if (!isEmbedded()) {
    // Standalone: always route through keyedHttp so the runtime key
    // (#584) is attached when present. Embedded mode never enters this
    // branch — the host bridge stays the transport, keys stay out of the UI.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (sdk as any).http = keyedHttp();
}
