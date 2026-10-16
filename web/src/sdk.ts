import { type FusionShellSdk, HttpError, type HttpResult, createShellSdk } from '@fusion/web-sdk';

// Single SDK instance for the module. Embedded (inside union-web shell
// iframe): http goes through the host postMessage bridge — the host injects
// the module api_key, the UI holds no keys (spec S: no auth logic in UI).
// Standalone (own Vite server / daemon static host): same-origin direct
// fetch. Standalone + daemon auth configured: VITE_API_KEY (build-time env,
// dev convenience only) is attached as x-api-key on every http call; in
// embedded mode no key ever exists here.
const DEV_API_KEY: string | undefined = import.meta.env.VITE_API_KEY;

export const isEmbedded = (): boolean => typeof window !== 'undefined' && window.parent !== window;

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
    body?: unknown
): Promise<HttpResult<T>> {
    const res = await fetch(path, {
        method,
        headers: { 'content-type': 'application/json', 'x-api-key': DEV_API_KEY ?? '' },
        body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = (await res.json().catch(() => null)) as T;
    if (!res.ok) {
        throw new HttpError(res.status, 'http_error', `request failed: ${res.status}`);
    }
    return { status: res.status, ok: true, data };
}

function keyedHttp() {
    return {
        request: <T>(path: string) => keyedRequest<T>(path, 'GET'),
        get: <T>(path: string) => keyedRequest<T>(path, 'GET'),
        post: <T>(path: string, body?: unknown) => keyedRequest<T>(path, 'POST', body),
        put: <T>(path: string, body?: unknown) => keyedRequest<T>(path, 'PUT', body),
        delete: <T>(path: string) => keyedRequest<T>(path, 'DELETE'),
    };
}

export const sdk: FusionShellSdk = createShellSdk();

if (!isEmbedded() && DEV_API_KEY) {
    // Standalone against an auth-configured daemon: layer the dev key onto
    // the plain same-origin http. Embedded mode never enters this branch.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (sdk as any).http = keyedHttp();
}
