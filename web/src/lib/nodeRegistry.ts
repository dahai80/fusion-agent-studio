// W1-2 (#551): NodeDefinition + NodeRegistry — 17-type unified taxonomy,
// web mirror of studio#422/#427 (plan §6.3). Metadata + config-schema only
// on this surface: run() stays backend-side (execution is W1-4).

import type { Port } from './workflowGraph';

export type NodeCategory = 'flow' | 'model' | 'action' | 'control' | 'data' | 'logic' | 'io';

export type ConfigWidget =
    | 'text'
    | 'multiline'
    | 'number'
    | 'boolean'
    | 'select'
    | 'secret'
    | 'tags'
    | 'model';

export interface ConfigField {
    key: string;
    label: string;
    widget: ConfigWidget;
    required?: boolean;
    advanced?: boolean;
    options?: string[];
    defaultValue?: unknown;
    placeholder?: string;
}

export interface NodeDefinition {
    key: string;
    displayName: string;
    icon: string;
    category: NodeCategory;
    inputs: Port[];
    outputs: Port[];
    configSchema: ConfigField[];
    description: string;
}

function port(
    key: string,
    label: string,
    dataType: Port['dataType'],
    direction: Port['direction']
): Port {
    return { key, label, dataType, direction };
}

const DATA_IN = () => [port('in', 'Input', 'data', 'input')];
const DATA_OUT = () => [port('out', 'Output', 'data', 'output')];
const MSG_IN = () => [port('in', 'Input', 'message', 'input')];
const MSG_OUT = () => [port('out', 'Output', 'message', 'output')];
const ANY_IN = () => [port('in', 'Input', 'any', 'input')];
const ANY_OUT = () => [port('out', 'Output', 'any', 'output')];

const MODEL_FIELD: ConfigField = {
    key: 'model',
    label: 'Model',
    widget: 'model',
    required: false,
    placeholder: 'fusion-mlx model id',
};

export const NODE_DEFINITIONS: readonly NodeDefinition[] = [
    {
        key: 'start',
        displayName: 'Start',
        icon: 'play',
        category: 'flow',
        description: 'Entry point; declares flow-state keys',
        inputs: [],
        outputs: [
            port('out', 'Output', 'message', 'output'),
            port('data_out', 'Data', 'data', 'output'),
        ],
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'flow_state', label: 'Flow state keys', widget: 'tags', advanced: true },
        ],
    },
    {
        key: 'end',
        displayName: 'End',
        icon: 'square',
        category: 'flow',
        description: 'Terminal; returns result',
        inputs: MSG_IN().concat(DATA_IN()),
        outputs: [],
        configSchema: [{ key: 'label', label: 'Label', widget: 'text' }],
    },
    {
        key: 'llm',
        displayName: 'LLM',
        icon: 'sparkles',
        category: 'model',
        description: 'LLM call via fusion-mlx',
        inputs: MSG_IN(),
        outputs: MSG_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            MODEL_FIELD,
            { key: 'system_prompt', label: 'System prompt', widget: 'multiline', advanced: true },
        ],
    },
    {
        key: 'agent',
        displayName: 'Agent',
        icon: 'bot',
        category: 'model',
        description: 'Autonomous agent with tools + reasoning loop',
        inputs: MSG_IN().concat(port('tool_in', 'Tools', 'tool', 'input')),
        outputs: MSG_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'agent_id', label: 'Agent id', widget: 'text', required: true },
            { key: 'model', label: 'Model', widget: 'model', advanced: true },
            {
                key: 'max_steps',
                label: 'Max steps',
                widget: 'number',
                advanced: true,
                defaultValue: 10,
            },
        ],
    },
    {
        key: 'tool',
        displayName: 'Tool',
        icon: 'wrench',
        category: 'action',
        description: 'Deterministic tool execution',
        inputs: ANY_IN(),
        outputs: ANY_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'tool_name', label: 'Tool name', widget: 'text', required: true },
            { key: 'tool_args', label: 'Tool args (JSON)', widget: 'multiline', advanced: true },
        ],
    },
    {
        key: 'mcp_tool',
        displayName: 'MCP Tool',
        icon: 'plug',
        category: 'action',
        description: 'MCP tool as a workflow node',
        inputs: ANY_IN(),
        outputs: ANY_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'mcp_server', label: 'MCP server', widget: 'text', required: true },
            { key: 'tool_name', label: 'Tool name', widget: 'text', required: true },
            { key: 'auth_token', label: 'Auth token', widget: 'secret', advanced: true },
        ],
    },
    {
        key: 'condition',
        displayName: 'Condition',
        icon: 'git-branch',
        category: 'control',
        description: 'Deterministic branch (if/else)',
        inputs: DATA_IN(),
        outputs: [port('true', 'True', 'data', 'output'), port('false', 'False', 'data', 'output')],
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'condition_expr', label: 'Expression', widget: 'multiline', required: true },
        ],
    },
    {
        key: 'question_classifier',
        displayName: 'Question Classifier',
        icon: 'help-circle',
        category: 'control',
        description: 'LLM-driven routing into classes',
        inputs: MSG_IN(),
        outputs: [port('route', 'Route', 'data', 'output')],
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            MODEL_FIELD,
            { key: 'classes', label: 'Classes', widget: 'tags', required: true },
        ],
    },
    {
        key: 'loop',
        displayName: 'Loop',
        icon: 'repeat',
        category: 'control',
        description: 'Loop body as sub-workflow with max_iterations',
        inputs: DATA_IN(),
        outputs: [port('item', 'Item', 'data', 'output'), port('done', 'Done', 'data', 'output')],
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'max_iterations', label: 'Max iterations', widget: 'number', defaultValue: 10 },
            {
                key: 'break_condition',
                label: 'Break condition',
                widget: 'multiline',
                advanced: true,
            },
        ],
    },
    {
        key: 'iteration',
        displayName: 'Iteration',
        icon: 'list',
        category: 'control',
        description: 'Per-array-element sub-workflow',
        inputs: [port('in', 'Input', 'array', 'input')],
        outputs: [port('items', 'Items', 'array', 'output')],
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            {
                key: 'parallel',
                label: 'Run in parallel',
                widget: 'boolean',
                advanced: true,
                defaultValue: false,
            },
        ],
    },
    {
        key: 'variable_aggregator',
        displayName: 'Variable Aggregator',
        icon: 'merge',
        category: 'control',
        description: 'Merge exclusive parallel branches into one',
        inputs: [port('a', 'A', 'any', 'input'), port('b', 'B', 'any', 'input')],
        outputs: DATA_OUT(),
        configSchema: [{ key: 'label', label: 'Label', widget: 'text' }],
    },
    {
        key: 'knowledge_retrieval',
        displayName: 'Knowledge Retrieval',
        icon: 'book-open',
        category: 'data',
        description: 'RAG retrieval via fusion-rag',
        inputs: MSG_IN(),
        outputs: DATA_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'kb_id', label: 'Knowledge base id', widget: 'text', required: true },
            { key: 'top_k', label: 'Top K', widget: 'number', advanced: true, defaultValue: 5 },
        ],
    },
    {
        key: 'code',
        displayName: 'Code',
        icon: 'code',
        category: 'logic',
        description: 'Sandbox code transform',
        inputs: ANY_IN(),
        outputs: ANY_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            {
                key: 'language',
                label: 'Language',
                widget: 'select',
                options: ['python', 'javascript'],
                defaultValue: 'python',
            },
            { key: 'code', label: 'Code', widget: 'multiline', required: true },
        ],
    },
    {
        key: 'template',
        displayName: 'Template',
        icon: 'file-text',
        category: 'logic',
        description: 'Jinja2-like templating',
        inputs: DATA_IN(),
        outputs: MSG_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'template', label: 'Template', widget: 'multiline', required: true },
        ],
    },
    {
        key: 'http_request',
        displayName: 'HTTP Request',
        icon: 'globe',
        category: 'io',
        description: 'External API call',
        inputs: ANY_IN(),
        outputs: DATA_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            {
                key: 'method',
                label: 'Method',
                widget: 'select',
                options: ['GET', 'POST', 'PUT', 'DELETE'],
                defaultValue: 'GET',
            },
            { key: 'url', label: 'URL', widget: 'text', required: true },
            { key: 'headers', label: 'Headers (JSON)', widget: 'multiline', advanced: true },
            { key: 'body', label: 'Body', widget: 'multiline', advanced: true },
            { key: 'api_key', label: 'API key', widget: 'secret', advanced: true },
        ],
    },
    {
        key: 'human_input',
        displayName: 'Human Input',
        icon: 'user-check',
        category: 'control',
        description: 'Checkpoint HiTL pause',
        inputs: ANY_IN(),
        outputs: ANY_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'prompt', label: 'Prompt to reviewer', widget: 'multiline', required: true },
            { key: 'timeout_seconds', label: 'Timeout (s)', widget: 'number', advanced: true },
        ],
    },
    {
        key: 'sub_flow',
        displayName: 'Sub Flow',
        icon: 'layers',
        category: 'control',
        description: 'Invoke another saved flow',
        inputs: ANY_IN(),
        outputs: ANY_OUT(),
        configSchema: [
            { key: 'label', label: 'Label', widget: 'text' },
            { key: 'flow_id', label: 'Flow id', widget: 'text', required: true },
        ],
    },
];

export const WORKFLOW_NODE_KEYS: readonly string[] = NODE_DEFINITIONS.map((d) => d.key);

export class NodeRegistry {
    private defs: Map<string, NodeDefinition> = new Map();

    constructor(defs: readonly NodeDefinition[] = NODE_DEFINITIONS) {
        for (const d of defs) this.defs.set(d.key, d);
    }

    get(key: string): NodeDefinition | undefined {
        return this.defs.get(key);
    }

    has(key: string): boolean {
        return this.defs.has(key);
    }

    all(): NodeDefinition[] {
        return [...this.defs.values()];
    }

    byCategory(): Map<NodeCategory, NodeDefinition[]> {
        const out = new Map<NodeCategory, NodeDefinition[]>();
        for (const d of this.defs.values()) {
            const list = out.get(d.category) ?? [];
            list.push(d);
            out.set(d.category, list);
        }
        return out;
    }

    register(def: NodeDefinition): void {
        if (this.defs.has(def.key)) {
            throw new Error(`node type already registered: ${def.key}`);
        }
        this.defs.set(def.key, def);
    }
}

export const nodeRegistry = new NodeRegistry();

// W1-3 (#552): palette — registry entries grouped by category, each mapped
// to its agent-studio server node type. Unmapped unified types render as
// planned (disabled) so the palette shows the full 17-type taxonomy without
// letting a non-executable node into the server graph.
export const UNIFIED_TO_SERVER_TYPE: Readonly<Record<string, string>> = {
    start: 'start',
    end: 'end',
    llm: 'llm',
    agent: 'planner',
    tool: 'tool',
    condition: 'condition',
    loop: 'loop',
    variable_aggregator: 'variable_aggregator',
    knowledge_retrieval: 'rag',
    iteration: 'iteration',
};

export interface PaletteEntry {
    type: string;
    displayName: string;
    serverType: string;
    planned: boolean;
}

export interface PaletteGroup {
    title: string;
    entries: PaletteEntry[];
}

// Server-native node types with no dedicated unified key (both collapse to
// condition in the codec). They stay first-class palette entries so existing
// studio graphs remain authorable.
const SERVER_ONLY_PALETTE: ReadonlyArray<{ category: NodeCategory; entry: PaletteEntry }> = [
    {
        category: 'control',
        entry: {
            type: 'error_handler',
            displayName: 'Error Handler',
            serverType: 'error_handler',
            planned: false,
        },
    },
    {
        category: 'control',
        entry: { type: 'verify', displayName: 'Verify', serverType: 'verify', planned: false },
    },
    // #556: variable_aggregator now has its own unified key — keep the
    // server-native parallel type first-class (C5 fan-out/gather graphs).
    {
        category: 'control',
        entry: {
            type: 'parallel',
            displayName: 'Parallel',
            serverType: 'parallel',
            planned: false,
        },
    },
];

export function paletteGroups(): PaletteGroup[] {
    const out: PaletteGroup[] = [];
    for (const [cat, defs] of nodeRegistry.byCategory()) {
        const entries: PaletteEntry[] = defs.map((d) => {
            const serverType = UNIFIED_TO_SERVER_TYPE[d.key] || '';
            return {
                type: d.key,
                displayName: d.displayName,
                serverType: serverType || d.key,
                planned: !serverType,
            };
        });
        for (const extra of SERVER_ONLY_PALETTE) {
            if (extra.category === cat) entries.push(extra.entry);
        }
        out.push({ title: cat, entries });
    }
    return out;
}
