export interface RunSubmitInput {
    goal: string;
    agent_id?: string;
    graph_id?: string;
    sandbox?: string;
    title?: string;
    idempotency_key?: string;
}

export interface RunSubmitResult {
    run_id: string;
    status: string;
    deduped?: boolean;
    sandbox?: {
        ok: boolean;
        node: string;
        cluster_task_id?: string;
        assigned_nodes?: string[];
    };
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

export interface RunSpan {
    node_id: string;
    start: number;
    end: number;
    status: string;
    tool_calls: number;
    token_events: number;
    tokens?: number;
    input?: string;
    output?: string;
    events?: number;
    error: string;
}

export interface RunEvents {
    run_status: string;
    stop_reason: string;
    run_start: number;
    run_end: number;
    nodes: RunSpan[];
    execution_id?: string;
}

export interface RunList {
    runs: Array<Record<string, unknown>>;
    total: number;
}

export interface Paginated<T> {
    data: T[];
    total: number;
    page: number;
    limit: number;
}

export interface GraphItem {
    graph_id: string;
    name: string;
    description?: string;
    graph_data?: {
        id?: string;
        nodes?: Record<string, { type?: string; label?: string }>;
        edges?: unknown[];
    };
}

export interface SessionSummary {
    id: string;
    title: string;
    mode: string;
    graph_id: string;
    summary: string;
    tags: string[];
    cwd: string;
    git_branch: string;
    tokens: number;
    created_at: number;
    updated_at: number;
}

export interface SessionList {
    status: string;
    sessions: SessionSummary[];
    total: number;
    offset: number;
}

export interface SessionMessage {
    role: string;
    content: unknown;
    [key: string]: unknown;
}

export interface SessionMessages {
    status: string;
    session_id: string;
    messages: SessionMessage[];
}

export interface SessionResumeResult {
    status: string;
    session_id: string;
    background_session_id: string;
    fork_mode: string;
    state: string;
}

export interface StreamEvent {
    type: string;
    [key: string]: unknown;
}

export interface RpcResult {
    status?: string;
    message?: string;
    [key: string]: unknown;
}
