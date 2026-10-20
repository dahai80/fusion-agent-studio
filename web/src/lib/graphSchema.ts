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
    parent?: string;
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
    parentId?: string;
    extent?: 'parent';
    hidden?: boolean;
}

export interface RfGroupNode extends RfNode {
    data: { label: string; nodeType: 'group'; config: ServerNode };
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
        if (cfg.type === 'group') {
            out.push({
                id,
                type: 'fusionGroup',
                position: {
                    x: typeof cfg.x === 'number' ? cfg.x : 0,
                    y: typeof cfg.y === 'number' ? cfg.y : 0,
                },
                data: { label: cfg.label || id, nodeType: 'group', config: cfg },
            });
            continue;
        }
        const rf: RfNode = {
            id,
            type: 'fusionNode',
            position: {
                x: typeof cfg.x === 'number' ? cfg.x : 0,
                y: typeof cfg.y === 'number' ? cfg.y : 0,
            },
            data: { label: cfg.label || id, nodeType: cfg.type || 'llm', config: cfg },
        };
        if (cfg.parent) {
            rf.parentId = cfg.parent;
            rf.extent = 'parent';
        }
        out.push(rf);
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
        if (n.data.nodeType === 'group') continue;
        outNodes[n.id] = {
            ...(graph.nodes?.[n.id] || {}),
            ...n.data.config,
            type: n.data.nodeType,
            label: n.data.label,
            x: n.position.x,
            y: n.position.y,
            parent: n.parentId || '',
        };
    }
    const outEdges: ServerEdge[] = edges.map((e) => ({
        source_id: e.source,
        target_id: e.target,
        ...(e.label ? { label: e.label } : {}),
    }));
    return { ...graph, nodes: outNodes, edges: outEdges };
}

// ---- #393 (audit-1020-1): schema-driven node config forms ------------------
// FieldSpec is the single inspector form source of truth, mirroring
// NodeConfig fields in agent_runtime/graph.py. `appliesTo` filters which
// node types show the field; `advanced` collapses into the advanced section.

export type FieldWidget = 'text' | 'textarea' | 'number' | 'select' | 'boolean' | 'tags' | 'kv';

export interface FieldSpec {
    key: string;
    label: string;
    widget: FieldWidget;
    default?: unknown;
    options?: string[];
    min?: number;
    max?: number;
    step?: number;
    appliesTo: string[];
    advanced?: boolean;
    help?: string;
}

export const NODE_FIELDS: FieldSpec[] = [
    // llm
    {
        key: 'model',
        label: 'Model',
        widget: 'text',
        default: '',
        appliesTo: ['llm'],
        help: 'Empty = agent default model',
    },
    {
        key: 'system_prompt',
        label: 'System prompt',
        widget: 'textarea',
        default: '',
        appliesTo: ['llm'],
    },
    {
        key: 'temperature',
        label: 'Temperature',
        widget: 'number',
        default: 0.7,
        min: 0,
        max: 2,
        step: 0.1,
        appliesTo: ['llm'],
        advanced: true,
    },
    {
        key: 'max_tokens',
        label: 'Max tokens',
        widget: 'number',
        default: 4096,
        min: 1,
        appliesTo: ['llm'],
        advanced: true,
    },
    {
        key: 'effort',
        label: 'Effort',
        widget: 'select',
        default: '',
        options: ['', 'low', 'medium', 'high'],
        appliesTo: ['llm'],
        advanced: true,
    },
    {
        key: 'tool_choice',
        label: 'Tool choice',
        widget: 'select',
        default: '',
        options: ['', 'auto', 'none', 'required'],
        appliesTo: ['llm'],
        advanced: true,
    },
    {
        key: 'loop_mode',
        label: 'Loop mode',
        widget: 'select',
        default: '',
        options: ['', 'agent'],
        appliesTo: ['llm'],
        advanced: true,
        help: '"agent" = built-in multi-turn tool loop',
    },
    {
        key: 'max_loop_iterations',
        label: 'Max loop iterations',
        widget: 'number',
        default: 0,
        min: 0,
        appliesTo: ['llm'],
        advanced: true,
    },
    {
        key: 'parallel_tool_calls',
        label: 'Parallel tool calls',
        widget: 'boolean',
        default: false,
        appliesTo: ['llm'],
        advanced: true,
    },
    {
        key: 'retry_on_error',
        label: 'Retry on tool error',
        widget: 'boolean',
        default: false,
        appliesTo: ['llm'],
        advanced: true,
    },
    {
        key: 'disable_tools',
        label: 'Disable tool injection',
        widget: 'boolean',
        default: false,
        appliesTo: ['llm'],
        advanced: true,
    },
    {
        key: 'allow_dynamic_tools',
        label: 'Allow dynamic tools',
        widget: 'boolean',
        default: false,
        appliesTo: ['llm'],
        advanced: true,
    },
    {
        key: 'stop_sequences',
        label: 'Stop sequences',
        widget: 'tags',
        default: [],
        appliesTo: ['llm'],
        advanced: true,
    },
    // tool
    {
        key: 'tool_name',
        label: 'Tool name',
        widget: 'text',
        default: '',
        appliesTo: ['tool'],
    },
    {
        key: 'tool_params',
        label: 'Tool params',
        widget: 'kv',
        default: {},
        appliesTo: ['tool'],
    },
    // condition
    {
        key: 'condition_expr',
        label: 'Condition expression',
        widget: 'textarea',
        default: '',
        appliesTo: ['condition'],
        help: 'e.g. has_tool_calls, ctx.key == "value", not flag, a or b',
    },
    // loop / error_handler
    {
        key: 'max_iterations',
        label: 'Max iterations',
        widget: 'number',
        default: 10,
        min: 1,
        appliesTo: ['loop'],
    },
    {
        key: 'max_retries',
        label: 'Max retries',
        widget: 'number',
        default: 3,
        min: 0,
        appliesTo: ['error_handler'],
    },
    {
        key: 'retry_delay',
        label: 'Retry delay (s)',
        widget: 'number',
        default: 1.0,
        min: 0,
        step: 0.5,
        appliesTo: ['error_handler'],
    },
];

export function fieldsForNode(nodeType: string, advanced: boolean): FieldSpec[] {
    return NODE_FIELDS.filter(
        (f) => f.appliesTo.includes(nodeType) && Boolean(f.advanced) === advanced
    );
}

export function applyFieldDefault(config: ServerNode, field: FieldSpec): ServerNode {
    if (config[field.key] === undefined && field.default !== undefined) {
        return { ...config, [field.key]: field.default };
    }
    return config;
}

// ---- #395 (audit-1020-3): client-side pre-save graph validation ------------

export interface GraphIssue {
    nodeId: string;
    level: 'error' | 'warning';
    message: string;
}

export function validateGraphClient(graph: GraphJson): GraphIssue[] {
    const issues: GraphIssue[] = [];
    const nodes = graph.nodes || {};
    const edges = graph.edges || [];
    const ids = Object.keys(nodes);
    if (ids.length === 0) {
        issues.push({ nodeId: '', level: 'error', message: 'Graph has no nodes' });
        return issues;
    }
    if (!graph.start_node_id) {
        issues.push({ nodeId: '', level: 'error', message: 'Graph has no start node' });
    } else if (!nodes[graph.start_node_id]) {
        issues.push({
            nodeId: graph.start_node_id,
            level: 'error',
            message: `Start node "${graph.start_node_id}" not found`,
        });
    }
    for (const e of edges) {
        if (!nodes[e.source_id]) {
            issues.push({
                nodeId: e.source_id,
                level: 'error',
                message: `Edge source "${e.source_id}" missing`,
            });
        }
        if (!nodes[e.target_id]) {
            issues.push({
                nodeId: e.target_id,
                level: 'error',
                message: `Edge target "${e.target_id}" missing`,
            });
        }
    }
    // Reachability from start (single-start walk over outgoing edges).
    if (graph.start_node_id && nodes[graph.start_node_id]) {
        const seen = new Set<string>([graph.start_node_id]);
        const queue = [graph.start_node_id];
        while (queue.length > 0) {
            const cur = queue.shift() as string;
            for (const e of edges) {
                if (e.source_id === cur && !seen.has(e.target_id)) {
                    seen.add(e.target_id);
                    queue.push(e.target_id);
                }
            }
        }
        for (const id of ids) {
            if (!seen.has(id)) {
                issues.push({
                    nodeId: id,
                    level: 'warning',
                    message: `Node "${id}" unreachable from start`,
                });
            }
        }
    }
    for (const id of ids) {
        const node = nodes[id];
        if (node.type === 'condition' && !node.condition_expr) {
            issues.push({
                nodeId: id,
                level: 'warning',
                message: `Condition node "${id}" has no expression`,
            });
        }
        if (node.type === 'tool' && !node.tool_name) {
            issues.push({
                nodeId: id,
                level: 'warning',
                message: `Tool node "${id}" has no tool_name`,
            });
        }
    }
    return issues;
}

// ---- #398 (audit-1020-6c): visual grouping ----------------------------------

export function groupNodes(nodes: RfNode[], ids: string[]): RfNode | null {
    const members = nodes.filter((n) => ids.includes(n.id));
    if (members.length < 1) return null;
    const minX = Math.min(...members.map((n) => n.position.x)) - 40;
    const minY = Math.min(...members.map((n) => n.position.y)) - 56;
    const id = `group_${Math.random().toString(36).slice(2, 6)}`;
    return {
        id,
        type: 'fusionGroup',
        position: { x: minX, y: minY },
        data: { label: 'group', nodeType: 'group', config: { type: 'group', label: 'group' } },
    };
}
