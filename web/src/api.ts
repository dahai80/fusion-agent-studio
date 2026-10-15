import { sdk } from './sdk';

// Typed client over the daemon HTTP surface (port 11442 direct, or through
// the gateway /shell/fusion-agent-studio/ proxy when embedded). All calls go
// through sdk.http so auth is handled by the host bridge / same-origin
// gateway injection — this module holds no keys.

export interface DashboardData {
    today_requests: number;
    total_token_consumption: number;
    active_agents: number;
    error_count: number;
    telemetry?: Record<string, unknown>;
}

export interface GraphItem {
    graph_id: string;
    name: string;
    description: string;
    graph_data: {
        id?: string;
        nodes?: Record<string, { type?: string; label?: string }>;
        edges?: unknown[];
    };
}

export interface RunItem {
    task_id: string;
    title: string;
    description?: string;
    status: string;
    agent_id?: string;
    graph_id?: string;
    trigger?: string;
    created_at?: number;
    updated_at?: number;
    retry_count?: number;
    artifact_ids?: string[];
    last_error?: string;
}

export interface RunDetail {
    run_id: string;
    status: string;
    progress: {
        events: number;
        artifact_ids: string[];
        retry_count: number;
    };
    error: string;
    agent_id: string;
    graph_id: string;
    created_at: number;
    updated_at: number;
}

export interface RunResult {
    run_id: string;
    status: string;
    steps: Array<{ session_id: string; events: number }>;
    artifacts: string[];
    last_result: Record<string, unknown>;
    last_error: string;
}

// #327: API base. Served under the gateway shell path
// (/shell/fusion-agent-studio/) the bridge http path must carry the module
// prefix so the host fetch lands on the gateway /shell proxy (module api_key
// injection); served standalone (own root) the prefix is empty. Derived from
// the document pathname, not config — one build serves both topologies.
export function apiBase(): string {
    if (typeof window === 'undefined') return '';
    const m = window.location.pathname.match(/^(\/shell\/[^/]+)/);
    return m ? m[1] : '';
}

async function getJson<T>(path: string): Promise<T> {
    const res = await sdk.http.get<T>(path);
    return res.data;
}

async function postJson<T>(path: string, body?: unknown): Promise<T> {
    const res = await sdk.http.post<T>(path, body);
    return res.data;
}

const B = () => apiBase();

export const api = {
    dashboard: () => getJson<DashboardData>(`${B()}/v1/dashboard`),
    graphs: (page = 1, limit = 50) =>
        getJson<{ data: GraphItem[]; total: number; page: number; limit: number }>(
            `${B()}/v1/graphs?page=${page}&limit=${limit}`
        ),
    runs: (status = '', limit = 100) =>
        getJson<{ runs: RunItem[]; total: number }>(
            `${B()}/v1/runs?limit=${limit}${status ? `&status=${encodeURIComponent(status)}` : ''}`
        ),
    run: (runId: string) => getJson<RunDetail>(`${B()}/v1/runs/${encodeURIComponent(runId)}`),
    runResult: (runId: string) =>
        getJson<RunResult>(`${B()}/v1/runs/${encodeURIComponent(runId)}/result`),
    submitRun: (body: { goal: string; graph_id?: string; title?: string }) =>
        postJson<{ run_id: string; status: string; deduped: boolean }>(`${B()}/v1/runs`, body),
    cancelRun: (runId: string) =>
        postJson<{ run_id: string; status: string }>(
            `${B()}/v1/runs/${encodeURIComponent(runId)}/cancel`
        ),
};

// Map task/run status strings to the four-color StatusKind vocabulary
// (spec S2: success / running / warning / error / offline).
export function statusKind(
    status: string
): 'success' | 'running' | 'warning' | 'error' | 'offline' {
    switch (status) {
        case 'completed':
        case 'success':
        case 'published':
            return 'success';
        case 'running':
        case 'in_progress':
        case 'pending':
        case 'submitted':
            return 'running';
        case 'partial':
        case 'needs_review':
        case 'retrying':
            return 'warning';
        case 'failed':
        case 'error':
        case 'canceled':
        case 'cancelled':
            return 'error';
        default:
            return 'offline';
    }
}
