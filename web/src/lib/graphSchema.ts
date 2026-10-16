// N13 (#352): single graph JSON schema adapter — server schema (agent_runtime/graph.py)
// is the only source of truth; this module converts to/from React Flow view state.
// Server node dict carries x/y canvas positions directly (no view fork).

export interface ServerNode {
    type?: string;
    label?: string;
    model?: string;
    system_prompt?: string;
    tool_name?: string;
    condition_expr?: string;
    x?: number;
    y?: number;
    [k: string]: unknown;
}

export interface ServerEdge {
    source_id: string;
    target_id: string;
    label?: string;
}

export interface GraphJson {
    id?: string;
    name?: string;
    description?: string;
    nodes?: Record<string, ServerNode>;
    edges?: ServerEdge[];
    start_node_id?: string;
    version?: number;
    agent_id?: string;
    plan_mode?: boolean;
    stop_on_tool_error?: boolean;
}

export const NODE_TYPES = [
    'start',
    'end',
    'llm',
    'tool',
    'condition',
    'loop',
    'parallel',
    'error_handler',
    'rag',
    'planner',
    'verify',
] as const;

export type PortType = 'flow' | 'branch' | 'fan';

// Typed ports per execution semantics (_runtime_nodes walk):
//   start has no inlet; end has no outlet; condition emits branch ports
//   (multiple successors); parallel fans out; everything else is flow.
export function outPortType(nodeType: string): PortType | null {
    if (nodeType === 'end') return null;
    if (nodeType === 'condition') return 'branch';
    if (nodeType === 'parallel') return 'fan';
    return 'flow';
}

export function inPortType(nodeType: string): PortType | null {
    if (nodeType === 'start') return null;
    return 'flow';
}

// Single-successor semantics: start/llm/tool/loop/error_handler/rag/planner/verify
// walk to exactly one next node; condition/parallel may have several; end none.
export function maxOutEdges(nodeType: string): number {
    if (nodeType === 'end') return 0;
    if (nodeType === 'condition' || nodeType === 'parallel') return Number.POSITIVE_INFINITY;
    return 1;
}

export interface ConnectCheck {
    ok: boolean;
    reason: string;
}

export function canConnect(
    graph: GraphJson,
    sourceId: string,
    targetId: string,
    existingEdges: ServerEdge[]
): ConnectCheck {
    if (sourceId === targetId) {
        return { ok: false, reason: 'Self-connection is not allowed' };
    }
    const nodes = graph.nodes || {};
    const source = nodes[sourceId];
    const target = nodes[targetId];
    if (!source || !target) {
        return { ok: false, reason: 'Unknown node' };
    }
    if (!outPortType(source.type || '')) {
        return { ok: false, reason: `${source.type || 'node'} has no outlet (typed port)` };
    }
    if (!inPortType(target.type || '')) {
        return { ok: false, reason: `${target.type || 'node'} has no inlet (typed port)` };
    }
    if (existingEdges.some((e) => e.source_id === sourceId && e.target_id === targetId)) {
        return { ok: false, reason: 'Edge already exists' };
    }
    const cap = maxOutEdges(source.type || '');
    const outgoing = existingEdges.filter((e) => e.source_id === sourceId).length;
    if (outgoing >= cap) {
        return {
            ok: false,
            reason: `${source.type || 'node'} allows at most ${cap} outgoing edge(s)`,
        };
    }
    return { ok: true, reason: '' };
}

export interface RfNode {
    id: string;
    type: string;
    position: { x: number; y: number };
    data: { label: string; nodeType: string; config: ServerNode };
}

export interface RfEdge {
    id: string;
    source: string;
    target: string;
    label?: string;
    sourceHandle?: string;
}

export function toRfNodes(graph: GraphJson): RfNode[] {
    const out: RfNode[] = [];
    for (const [id, cfg] of Object.entries(graph.nodes || {})) {
        out.push({
            id,
            type: 'fusionNode',
            position: {
                x: typeof cfg.x === 'number' ? cfg.x : 0,
                y: typeof cfg.y === 'number' ? cfg.y : 0,
            },
            data: { label: cfg.label || id, nodeType: cfg.type || 'llm', config: cfg },
        });
    }
    return out;
}

export function toRfEdges(graph: GraphJson): RfEdge[] {
    return (graph.edges || []).map((e, i) => ({
        id: `e${i}`,
        source: e.source_id,
        target: e.target_id,
        label: e.label || undefined,
    }));
}

export function fromRf(graph: GraphJson, nodes: RfNode[], edges: RfEdge[]): GraphJson {
    const outNodes: Record<string, ServerNode> = {};
    for (const n of nodes) {
        outNodes[n.id] = {
            ...(graph.nodes?.[n.id] || {}),
            ...n.data.config,
            type: n.data.nodeType,
            label: n.data.label,
            x: n.position.x,
            y: n.position.y,
        };
    }
    const outEdges: ServerEdge[] = edges.map((e) => ({
        source_id: e.source,
        target_id: e.target,
        ...(e.label ? { label: e.label } : {}),
    }));
    return { ...graph, nodes: outNodes, edges: outEdges };
}
