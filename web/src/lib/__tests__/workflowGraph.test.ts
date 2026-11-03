import { describe, expect, it } from 'vitest';
import { validateGraphClient } from '../graphSchema';
import { NODE_DEFINITIONS } from '../nodeRegistry';
import {
    WORKFLOW_SCHEMA_VERSION,
    canConnectPorts,
    fromAgentStudioGraph,
    parseWorkflowGraph,
    toAgentStudioGraph,
} from '../workflowGraph';
import taxonomy from './fixtures/node-taxonomy-17.json';
import studioFlow from './fixtures/studio-flow-fixture.json';

describe('parseWorkflowGraph (#550)', () => {
    it('parses a valid graph', () => {
        const { graph, issues } = parseWorkflowGraph(studioFlow);
        expect(issues).toEqual([]);
        expect(graph.schemaVersion).toBe(1);
        expect(graph.nodes).toHaveLength(4);
        expect(graph.edges).toHaveLength(3);
        expect(graph.flowStateSchema).toHaveLength(2);
    });

    it('defaults schemaVersion with a warning when missing', () => {
        const { graph, issues } = parseWorkflowGraph({ id: 'x', name: 'n', nodes: [], edges: [] });
        expect(graph.schemaVersion).toBe(WORKFLOW_SCHEMA_VERSION);
        expect(
            issues.some((i) => i.level === 'warning' && i.message.includes('schemaVersion'))
        ).toBe(true);
    });

    it('reports duplicate node ids and dangling edges', () => {
        const raw = {
            id: 'x',
            name: 'n',
            nodes: [
                { id: 'a', type: 'start', position: { x: 0, y: 0 }, config: {} },
                { id: 'a', type: 'end', position: { x: 1, y: 0 }, config: {} },
            ],
            edges: [
                {
                    id: 'e1',
                    sourceNode: 'a',
                    sourcePort: 'out',
                    targetNode: 'ghost',
                    targetPort: 'in',
                },
            ],
        };
        const { issues } = parseWorkflowGraph(raw);
        expect(issues.some((i) => i.message.includes('duplicate node id a'))).toBe(true);
        expect(issues.some((i) => i.message.includes('ghost not in nodes'))).toBe(true);
    });
});

describe('port compatibility (#550, plan §6.2)', () => {
    it('same type connects', () => {
        expect(canConnectPorts('message', 'message').ok).toBe(true);
        expect(canConnectPorts('data', 'data').ok).toBe(true);
    });

    it('any is escape hatch with warning', () => {
        const r = canConnectPorts('any', 'data');
        expect(r.ok).toBe(true);
        expect(r.warning).toBeTruthy();
    });

    it('data→json migration mapping applies', () => {
        const r = canConnectPorts('data', 'json');
        expect(r.ok).toBe(true);
        expect(r.warning).toContain('migration');
    });

    it('incompatible types reject with reason', () => {
        const r = canConnectPorts('message', 'tool');
        expect(r.ok).toBe(false);
        expect(r.reason).toContain('incompatible');
    });
});

describe('GraphCodec round-trip (#550)', () => {
    it('core-typed graph round-trips losslessly', () => {
        const { graph } = parseWorkflowGraph(studioFlow);
        const { server, issues } = toAgentStudioGraph(graph);
        expect(issues).toEqual([]);
        expect(server.start_node_id).toBe('n-start');
        const back = fromAgentStudioGraph(server);
        expect(back.issues).toEqual([]);
        expect(back.graph.nodes.map((n) => [n.id, n.type])).toEqual(
            graph.nodes.map((n) => [n.id, n.type])
        );
        expect(back.graph.edges.map((e) => [e.sourceNode, e.targetNode])).toEqual(
            graph.edges.map((e) => [e.sourceNode, e.targetNode])
        );
        expect(back.graph.nodes.find((n) => n.id === 'n-llm')?.config.model).toBe(
            'mlx-community--Qwen3-0.6B-4bit'
        );
    });

    it('server-only types map forward: rag→knowledge_retrieval, parallel→variable_aggregator', () => {
        const { graph, issues } = fromAgentStudioGraph({
            id: 'g',
            name: 'n',
            nodes: {
                s: { type: 'start', x: 0, y: 0 },
                p: { type: 'parallel', x: 1, y: 1 },
                r: { type: 'rag', x: 2, y: 2 },
            },
            edges: [],
            start_node_id: 's',
        });
        expect(issues).toEqual([]);
        expect(graph.nodes.find((n) => n.id === 'r')?.type).toBe('knowledge_retrieval');
        expect(graph.nodes.find((n) => n.id === 'p')?.type).toBe('variable_aggregator');
    });

    it('unified-only types are reported as dropped, not silently lost', () => {
        const { graph } = parseWorkflowGraph({
            id: 'g',
            name: 'n',
            nodes: [
                { id: 's', type: 'start', position: { x: 0, y: 0 }, config: {} },
                { id: 'a', type: 'agent', position: { x: 1, y: 1 }, config: { agent_id: 'ag1' } },
            ],
            edges: [],
        });
        const { server, issues } = toAgentStudioGraph(graph);
        expect(server.nodes && 'a' in server.nodes).toBe(false);
        expect(issues).toHaveLength(1);
        expect(issues[0]).toMatchObject({ nodeId: 'a', nodeType: 'agent' });
        expect(issues[0].reason).toContain('no agent-studio counterpart');
    });
});

describe('golden-file cross-surface contract (#550)', () => {
    it('studio-authored flow loads, validates, and converts to a graph the server schema accepts', () => {
        const { graph, issues } = parseWorkflowGraph(studioFlow);
        expect(issues).toEqual([]);
        const { server, issues: codecIssues } = toAgentStudioGraph(graph);
        expect(codecIssues).toEqual([]);
        const serverIssues = validateGraphClient(server as never);
        const errors = serverIssues.filter((i) => i.level === 'error');
        expect(errors).toEqual([]);
    });

    it('taxonomy fixture keys resolve in the web registry and vice versa (#551)', () => {
        const fixtureKeys = (taxonomy.nodeTypes as { key: string }[]).map((t) => t.key);
        const defKeys = NODE_DEFINITIONS.map((d) => d.key);
        expect(defKeys).toHaveLength(17);
        for (const k of fixtureKeys) expect(defKeys).toContain(k);
        for (const k of defKeys) expect(fixtureKeys).toContain(k);
    });
});
