import { describe, expect, it } from 'vitest';
import { NODE_DEFINITIONS, NodeRegistry, WORKFLOW_NODE_KEYS, nodeRegistry } from '../nodeRegistry';
import taxonomy from './fixtures/node-taxonomy-17.json';

const KEYS_17 = [
    'start',
    'end',
    'llm',
    'agent',
    'tool',
    'mcp_tool',
    'condition',
    'question_classifier',
    'loop',
    'iteration',
    'variable_aggregator',
    'knowledge_retrieval',
    'code',
    'template',
    'http_request',
    'human_input',
    'sub_flow',
];

describe('NodeRegistry (#551)', () => {
    it('registers the full 17-type unified set, keys identical to studio#427', () => {
        expect(WORKFLOW_NODE_KEYS).toHaveLength(17);
        expect([...WORKFLOW_NODE_KEYS].sort()).toEqual([...KEYS_17].sort());
        for (const k of KEYS_17) {
            expect(nodeRegistry.has(k)).toBe(true);
        }
    });

    it('taxonomy fixture keys resolve both ways (cross-surface contract)', () => {
        const fixtureKeys = (taxonomy.nodeTypes as { key: string }[]).map((t) => t.key);
        for (const k of fixtureKeys) expect(nodeRegistry.get(k)).toBeDefined();
        for (const d of NODE_DEFINITIONS) {
            expect(fixtureKeys).toContain(d.key);
        }
    });

    it('every definition carries palette metadata + non-empty configSchema', () => {
        for (const d of nodeRegistry.all()) {
            expect(d.displayName).toBeTruthy();
            expect(d.icon).toBeTruthy();
            expect(d.category).toBeTruthy();
            expect(d.configSchema.length).toBeGreaterThan(0);
            expect(d.description).toBeTruthy();
        }
    });

    it('configSchema drives the inspector without per-node bespoke forms', () => {
        const llm = nodeRegistry.get('llm');
        expect(llm?.configSchema.some((f) => f.key === 'model' && f.widget === 'model')).toBe(true);
        const cond = nodeRegistry.get('condition');
        expect(cond?.configSchema.some((f) => f.key === 'condition_expr' && f.required)).toBe(true);
        const http = nodeRegistry.get('http_request');
        expect(http?.configSchema.find((f) => f.key === 'method')?.options).toContain('GET');
    });

    it('groups by category for palette sections', () => {
        const byCat = nodeRegistry.byCategory();
        expect(byCat.get('flow')?.map((d) => d.key)).toEqual(['start', 'end']);
        expect(byCat.get('control')?.length).toBe(7);
    });

    it('ports follow the plan §6.3 table', () => {
        expect(nodeRegistry.get('start')?.inputs).toHaveLength(0);
        expect(nodeRegistry.get('end')?.outputs).toHaveLength(0);
        expect(nodeRegistry.get('llm')?.inputs[0].dataType).toBe('message');
        expect(nodeRegistry.get('knowledge_retrieval')?.inputs[0].dataType).toBe('message');
        expect(nodeRegistry.get('knowledge_retrieval')?.outputs[0].dataType).toBe('data');
        expect(nodeRegistry.get('iteration')?.inputs[0].dataType).toBe('array');
    });

    it('register() rejects duplicate keys', () => {
        const reg = new NodeRegistry([]);
        reg.register(NODE_DEFINITIONS[0]);
        expect(() => reg.register(NODE_DEFINITIONS[0])).toThrow('already registered');
    });
});
