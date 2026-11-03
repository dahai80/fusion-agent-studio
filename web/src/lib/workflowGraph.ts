// W1-1 (#550): unified WorkflowGraph TS model — JSON-compatible with the
// fusion-studio SwiftUI surface (studio#421, plan §6.2). The studio model is
// the definition source; this file mirrors it. GraphCodec round-trips to the
// server AgentGraphModel shape (GraphJson in graphSchema.ts) in-process.

export const WORKFLOW_SCHEMA_VERSION = 1;

export type PortDataType = 'message' | 'data' | 'tool' | 'json' | 'array' | 'any';
export type PortDirection = 'input' | 'output';

export const PORT_DATA_TYPES: readonly PortDataType[] = [
    'message',
    'data',
    'tool',
    'json',
    'array',
    'any',
];

export interface Port {
    key: string;
    label: string;
    dataType: PortDataType;
    direction: PortDirection;
    allowsLoop?: boolean;
}

export interface Viewport {
    offsetX: number;
    offsetY: number;
    scale: number;
}

export interface StateKey {
    key: string;
    valueType: 'string' | 'number' | 'boolean' | 'json';
    defaultValue?: unknown;
}

export interface NodeConfig {
    [k: string]: unknown;
}

export interface WorkflowNode {
    id: string;
    type: string;
    position: { x: number; y: number };
    size?: { width: number; height: number };
    config: NodeConfig;
    parentNodeId?: string | null;
    frozen?: boolean;
    label?: string;
}

export interface WorkflowEdge {
    id: string;
    sourceNode: string;
    sourcePort: string;
    targetNode: string;
    targetPort: string;
    condition?: string | null;
    isLoop?: boolean;
}

export interface WorkflowGraph {
    schemaVersion: number;
    id: string;
    name: string;
    nodes: WorkflowNode[];
    edges: WorkflowEdge[];
    viewport?: Viewport;
    flowStateSchema?: StateKey[];
}

// ── parse / validate ──

export interface GraphIssue {
    level: 'error' | 'warning';
    message: string;
    nodeId?: string;
    edgeId?: string;
}

export function parseWorkflowGraph(raw: unknown): { graph: WorkflowGraph; issues: GraphIssue[] } {
    const issues: GraphIssue[] = [];
    const obj = (raw ?? {}) as Record<string, unknown>;
    if (typeof obj.schemaVersion !== 'number') {
        issues.push({ level: 'warning', message: 'missing schemaVersion — assuming 1' });
    }
    const nodes = Array.isArray(obj.nodes) ? (obj.nodes as WorkflowNode[]) : [];
    const edges = Array.isArray(obj.edges) ? (obj.edges as WorkflowEdge[]) : [];
    const ids = new Set<string>();
    for (const n of nodes) {
        if (!n || typeof n.id !== 'string' || !n.id) {
            issues.push({ level: 'error', message: 'node missing id' });
            continue;
        }
        if (ids.has(n.id))
            issues.push({ level: 'error', message: `duplicate node id ${n.id}`, nodeId: n.id });
        ids.add(n.id);
        if (typeof n.type !== 'string' || !n.type) {
            issues.push({ level: 'error', message: `node ${n.id} missing type`, nodeId: n.id });
        }
    }
    for (const e of edges) {
        if (!e || typeof e.id !== 'string' || !e.id) {
            issues.push({ level: 'error', message: 'edge missing id' });
            continue;
        }
        if (!ids.has(e.sourceNode)) {
            issues.push({
                level: 'error',
                message: `edge ${e.id} source ${e.sourceNode} not in nodes`,
                edgeId: e.id,
            });
        }
        if (!ids.has(e.targetNode)) {
            issues.push({
                level: 'error',
                message: `edge ${e.id} target ${e.targetNode} not in nodes`,
                edgeId: e.id,
            });
        }
    }
    const graph: WorkflowGraph = {
        schemaVersion:
            typeof obj.schemaVersion === 'number' ? obj.schemaVersion : WORKFLOW_SCHEMA_VERSION,
        id: typeof obj.id === 'string' ? obj.id : crypto.randomUUID(),
        name: typeof obj.name === 'string' ? obj.name : 'Untitled flow',
        nodes,
        edges,
        viewport: (obj.viewport as Viewport) ?? undefined,
        flowStateSchema: (obj.flowStateSchema as StateKey[]) ?? undefined,
    };
    return { graph, issues };
}

// ── port compatibility (plan §6.2) ──

export interface PortConnectCheck {
    ok: boolean;
    reason?: string;
    warning?: string;
}

const PORT_MIGRATIONS: Readonly<Record<PortDataType, PortDataType[]>> = {
    message: [],
    data: ['json'],
    tool: [],
    json: [],
    array: [],
    any: [],
};

export function canConnectPorts(output: PortDataType, input: PortDataType): PortConnectCheck {
    if (output === 'any' || input === 'any') {
        if (output !== input) {
            return { ok: true, warning: 'any-typed port used — prefer a concrete type' };
        }
        return { ok: true };
    }
    if (output === input) return { ok: true };
    if (PORT_MIGRATIONS[output].includes(input)) {
        return { ok: true, warning: `migration mapping applied: ${output} → ${input}` };
    }
    return { ok: false, reason: `incompatible port types: ${output} output → ${input} input` };
}

// ── GraphCodec: WorkflowGraph <-> server AgentGraphModel (GraphJson) ──
//
// Lossless both ways for the shared set: start, end, llm, tool, condition,
// loop + knowledge_retrieval <-> rag. Types with no server counterpart are
// reported as dropped (not silently lost — caller gets CodecIssue per node).
// Studio-side unification (studio#421) is the contract for the JSON shape.

export interface CodecIssue {
    nodeId: string;
    nodeType: string;
    reason: string;
}

const UNIFIED_TO_SERVER: Readonly<Record<string, string>> = {
    start: 'start',
    end: 'end',
    llm: 'llm',
    tool: 'tool',
    condition: 'condition',
    loop: 'loop',
    knowledge_retrieval: 'rag',
};

const SERVER_TO_UNIFIED: Readonly<Record<string, string>> = {
    start: 'start',
    end: 'end',
    llm: 'llm',
    tool: 'tool',
    condition: 'condition',
    loop: 'loop',
    rag: 'knowledge_retrieval',
    parallel: 'variable_aggregator',
    planner: 'agent',
    verify: 'condition',
    error_handler: 'condition',
};

const CODEC_CONFIG_KEYS = ['model', 'system_prompt', 'tool_name', 'condition_expr', 'label'];

export function toAgentStudioGraph(graph: WorkflowGraph): {
    server: GraphJsonLike;
    issues: CodecIssue[];
} {
    const issues: CodecIssue[] = [];
    const nodes: Record<string, Record<string, unknown>> = {};
    const kept = new Set<string>();
    for (const n of graph.nodes) {
        const serverType = UNIFIED_TO_SERVER[n.type];
        if (!serverType) {
            issues.push({
                nodeId: n.id,
                nodeType: n.type,
                reason: `node type "${n.type}" has no agent-studio counterpart — dropped from server graph`,
            });
            continue;
        }
        kept.add(n.id);
        const server: Record<string, unknown> = { type: serverType };
        for (const k of CODEC_CONFIG_KEYS) {
            if (n.config[k] !== undefined) server[k] = n.config[k];
        }
        if (n.label) server.label = n.label;
        server.x = n.position.x;
        server.y = n.position.y;
        nodes[n.id] = server;
    }
    const start = graph.nodes.find((n) => kept.has(n.id) && n.type === 'start');
    const edges = graph.edges
        .filter((e) => kept.has(e.sourceNode) && kept.has(e.targetNode))
        .map((e) => ({
            source_id: e.sourceNode,
            target_id: e.targetNode,
            ...(e.condition ? { label: e.condition } : {}),
        }));
    const server: GraphJsonLike = {
        id: graph.id,
        name: graph.name,
        nodes,
        edges,
        ...(start ? { start_node_id: start.id } : {}),
    };
    return { server, issues };
}

export interface GraphJsonLike {
    id?: string;
    name?: string;
    nodes?: Record<string, Record<string, unknown>>;
    edges?: { source_id: string; target_id: string; label?: string }[];
    start_node_id?: string;
}

export function fromAgentStudioGraph(server: GraphJsonLike): {
    graph: WorkflowGraph;
    issues: CodecIssue[];
} {
    const issues: CodecIssue[] = [];
    const nodes: WorkflowNode[] = [];
    const kept = new Set<string>();
    const entries = Object.entries(server.nodes ?? {});
    for (const [id, sn] of entries) {
        const type = SERVER_TO_UNIFIED[String(sn.type)];
        if (!type) {
            issues.push({
                nodeId: id,
                nodeType: String(sn.type ?? ''),
                reason: `server node type "${String(sn.type)}" has no unified counterpart — dropped`,
            });
            continue;
        }
        kept.add(id);
        const config: NodeConfig = {};
        for (const k of CODEC_CONFIG_KEYS) {
            if (sn[k] !== undefined && k !== 'label') config[k] = sn[k];
        }
        nodes.push({
            id,
            type,
            position: {
                x: typeof sn.x === 'number' ? sn.x : 0,
                y: typeof sn.y === 'number' ? sn.y : 0,
            },
            config,
            label: typeof sn.label === 'string' ? sn.label : undefined,
        });
    }
    const edges = (server.edges ?? [])
        .filter((e) => kept.has(e.source_id) && kept.has(e.target_id))
        .map((e) => ({
            id: `${e.source_id}->${e.target_id}`,
            sourceNode: e.source_id,
            sourcePort: 'out',
            targetNode: e.target_id,
            targetPort: 'in',
            condition: e.label ?? null,
        }));
    const graph: WorkflowGraph = {
        schemaVersion: WORKFLOW_SCHEMA_VERSION,
        id: server.id ?? crypto.randomUUID(),
        name: server.name ?? 'Untitled flow',
        nodes,
        edges,
    };
    return { graph, issues };
}
