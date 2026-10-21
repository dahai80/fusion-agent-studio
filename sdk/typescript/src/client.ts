import { readSseStream } from './sse';
import type {
    GraphItem,
    Paginated,
    RpcResult,
    RunDetail,
    RunEvents,
    RunList,
    RunSubmitInput,
    RunSubmitResult,
    SessionList,
    SessionMessages,
    SessionResumeResult,
    StreamEvent,
} from './types';

export const DEFAULT_BASE_URL = 'http://127.0.0.1:11442';
export const RETRY_STATUS = new Set([502, 503, 504]);

export type JsonRequest = (
    method: 'GET' | 'POST' | 'PATCH' | 'DELETE',
    path: string,
    body?: unknown
) => Promise<unknown>;

export interface AgentStudioClientOptions {
    baseUrl?: string;
    apiKey?: string;
    retries?: number;
    timeoutMs?: number;
    fetchImpl?: typeof fetch;
    request?: JsonRequest;
    streamRequest?: (path: string) => Promise<ReadableStream<Uint8Array>>;
}

export class AgentStudioError extends Error {
    status: number;
    body: unknown;

    constructor(status: number, body: unknown, message: string) {
        super(message);
        this.name = 'AgentStudioError';
        this.status = status;
        this.body = body;
    }
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

export class AgentStudioClient {
    readonly baseUrl: string;
    readonly apiKey: string;
    private readonly retries: number;
    private readonly timeoutMs: number;
    private readonly fetchImpl: typeof fetch;
    private readonly customRequest?: JsonRequest;
    private readonly customStream?: (path: string) => Promise<ReadableStream<Uint8Array>>;

    constructor(options: AgentStudioClientOptions = {}) {
        this.baseUrl = (options.baseUrl ?? DEFAULT_BASE_URL).replace(/\/$/, '');
        this.apiKey = options.apiKey ?? '';
        this.retries = Math.max(0, options.retries ?? 2);
        this.timeoutMs = options.timeoutMs ?? 60_000;
        this.fetchImpl = options.fetchImpl ?? globalThis.fetch;
        this.customRequest = options.request;
        this.customStream = options.streamRequest;
    }

    private url(path: string, withQueryKey = false): string {
        const url = `${this.baseUrl}${path}`;
        if (!withQueryKey || !this.apiKey) return url;
        const sep = path.includes('?') ? '&' : '?';
        return `${url}${sep}api_key=${encodeURIComponent(this.apiKey)}`;
    }

    private headers(stream = false): Record<string, string> {
        const headers: Record<string, string> = {
            accept: stream ? 'text/event-stream' : 'application/json',
        };
        if (this.apiKey) headers['x-api-key'] = this.apiKey;
        return headers;
    }

    async request<T>(
        method: 'GET' | 'POST' | 'PATCH' | 'DELETE',
        path: string,
        body?: unknown
    ): Promise<T> {
        if (this.customRequest) {
            return (await this.customRequest(method, path, body)) as T;
        }
        let lastError = '';
        for (let attempt = 0; attempt <= this.retries; attempt += 1) {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), this.timeoutMs);
            try {
                const init: RequestInit = {
                    method,
                    headers: this.headers(),
                    signal: controller.signal,
                };
                if (body !== undefined) {
                    (init.headers as Record<string, string>)['content-type'] = 'application/json';
                    init.body = JSON.stringify(body);
                }
                const res = await this.fetchImpl(this.url(path), init);
                if (RETRY_STATUS.has(res.status) && attempt < this.retries) {
                    lastError = `HTTP ${res.status}`;
                    await sleep(200 * (attempt + 1));
                    continue;
                }
                const text = await res.text();
                const data = text ? JSON.parse(text) : {};
                if (!res.ok) {
                    throw new AgentStudioError(res.status, data, `HTTP ${res.status} ${path}`);
                }
                return data as T;
            } catch (error) {
                if (error instanceof AgentStudioError) throw error;
                lastError = error instanceof Error ? error.message : String(error);
                if (attempt >= this.retries) break;
                await sleep(200 * (attempt + 1));
            } finally {
                clearTimeout(timer);
            }
        }
        throw new Error(`request ${method} ${path} failed after ${this.retries + 1} attempts: ${lastError}`);
    }

    async *stream(path: string): AsyncGenerator<StreamEvent> {
        if (this.customStream) {
            yield* readSseStream(await this.customStream(path));
            return;
        }
        const res = await this.fetchImpl(this.url(path, true), {
            headers: this.headers(true),
        });
        if (!res.ok || !res.body) {
            throw new AgentStudioError(res.status, null, `stream ${path} -> HTTP ${res.status}`);
        }
        yield* readSseStream(res.body);
    }

    rpc<T = RpcResult>(method: string, params: Record<string, unknown> = {}): Promise<T> {
        return this.request<T>('POST', '/v1/rpc', { method, params });
    }

    listAgents(options: { page?: number; limit?: number; status?: string; keyword?: string } = {}): Promise<
        Paginated<Record<string, unknown>>
    > {
        const params = new URLSearchParams();
        params.set('page', String(options.page ?? 1));
        params.set('limit', String(options.limit ?? 20));
        if (options.status) params.set('status', options.status);
        if (options.keyword) params.set('keyword', options.keyword);
        return this.request('GET', `/v1/agents?${params}`);
    }

    listGraphs(options: { page?: number; limit?: number } = {}): Promise<Paginated<GraphItem>> {
        const params = new URLSearchParams();
        params.set('page', String(options.page ?? 1));
        params.set('limit', String(options.limit ?? 20));
        return this.request('GET', `/v1/graphs?${params}`);
    }

    createGraph(graph: Record<string, unknown>): Promise<Record<string, unknown>> {
        return this.request('POST', '/v1/graphs', graph);
    }

    deleteGraph(graphId: string): Promise<Record<string, unknown>> {
        return this.request('DELETE', `/v1/graphs/${encodeURIComponent(graphId)}`);
    }

    executeGraph(
        graphId: string,
        input: string,
        options: { sessionId?: string; variables?: Record<string, unknown> } = {}
    ): Promise<Record<string, unknown>> {
        return this.request('POST', `/v1/graphs/${encodeURIComponent(graphId)}/execute`, {
            input,
            ...options,
        });
    }

    streamGraph(
        graphId: string,
        input: string,
        options: { onlyNode?: string; fromNode?: string; thinking?: boolean } = {}
    ): AsyncGenerator<StreamEvent> {
        const params = new URLSearchParams({ input });
        if (options.onlyNode) params.set('only_node', options.onlyNode);
        if (options.fromNode) params.set('from_node', options.fromNode);
        if (options.thinking === false) params.set('thinking', 'false');
        return this.stream(`/v1/graphs/${encodeURIComponent(graphId)}/execute/stream?${params}`);
    }

    streamAgent(agentId: string, input: string): AsyncGenerator<StreamEvent> {
        const params = new URLSearchParams({ input });
        return this.stream(`/v1/agents/${encodeURIComponent(agentId)}/execute/stream?${params}`);
    }

    submitRun(input: RunSubmitInput): Promise<RunSubmitResult> {
        return this.request('POST', '/v1/runs', input);
    }

    getRun(runId: string): Promise<RunDetail> {
        return this.request('GET', `/v1/runs/${encodeURIComponent(runId)}`);
    }

    runEvents(runId: string): Promise<RunEvents> {
        return this.request('GET', `/v1/runs/${encodeURIComponent(runId)}/events`);
    }

    listRuns(options: { status?: string; limit?: number } = {}): Promise<RunList> {
        const params = new URLSearchParams();
        if (options.status) params.set('status', options.status);
        params.set('limit', String(options.limit ?? 50));
        return this.request('GET', `/v1/runs?${params}`);
    }

    cancelRun(runId: string): Promise<Record<string, unknown>> {
        return this.request('POST', `/v1/runs/${encodeURIComponent(runId)}/cancel`, {});
    }

    streamRun(runId: string): AsyncGenerator<StreamEvent> {
        return this.stream(`/v1/runs/${encodeURIComponent(runId)}/stream`);
    }

    listSessions(options: { limit?: number; offset?: number; tag?: string } = {}): Promise<SessionList> {
        const params = new URLSearchParams();
        params.set('limit', String(options.limit ?? 50));
        params.set('offset', String(options.offset ?? 0));
        if (options.tag) params.set('tag', options.tag);
        return this.request('GET', `/v1/sessions?${params}`);
    }

    getSession(sessionId: string): Promise<Record<string, unknown>> {
        return this.request('GET', `/v1/sessions/${encodeURIComponent(sessionId)}`);
    }

    sessionMessages(sessionId: string, limit = 200): Promise<SessionMessages> {
        return this.request(
            'GET',
            `/v1/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`
        );
    }

    resumeSession(sessionId: string, input = ''): Promise<SessionResumeResult> {
        return this.request('POST', `/v1/sessions/${encodeURIComponent(sessionId)}/resume`, {
            input,
        });
    }

    forkSession(sessionId: string, title = ''): Promise<Record<string, unknown>> {
        return this.request('POST', `/v1/sessions/${encodeURIComponent(sessionId)}/fork`, {
            title,
        });
    }

    renameSession(
        sessionId: string,
        patch: { title?: string; tag?: string; summary?: string }
    ): Promise<Record<string, unknown>> {
        return this.request('PATCH', `/v1/sessions/${encodeURIComponent(sessionId)}`, patch);
    }

    injectInput(sessionId: string, input: string): Promise<Record<string, unknown>> {
        return this.request('POST', `/v1/sessions/${encodeURIComponent(sessionId)}/inject`, {
            input,
        });
    }
}
