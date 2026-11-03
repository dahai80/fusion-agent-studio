// #554: session-only secret store + persistence stripping.
import { describe, expect, it } from 'vitest';
import { fieldsForNode, nodeCardSummary, stripSessionSecrets } from '../graphSchema';
import { WORKFLOW_NODE_KEYS } from '../nodeRegistry';
import {
    clearAllSecrets,
    clearNodeSecrets,
    getSecret,
    hasSecret,
    isSecretField,
    setSecret,
} from '../secretStore';

describe('secretStore (#554)', () => {
    it('holds secrets per node+key and clears per node', () => {
        clearAllSecrets();
        setSecret('n1', 'api_key', 'sk-test');
        setSecret('n2', 'api_key', 'sk-other');
        expect(getSecret('n1', 'api_key')).toBe('sk-test');
        expect(hasSecret('n1', 'api_key')).toBe(true);
        clearNodeSecrets('n1');
        expect(getSecret('n1', 'api_key')).toBe('');
        expect(getSecret('n2', 'api_key')).toBe('sk-other');
    });

    it('empty set deletes the slot', () => {
        clearAllSecrets();
        setSecret('n1', 'password', 'x');
        setSecret('n1', 'password', '');
        expect(hasSecret('n1', 'password')).toBe(false);
    });

    it('isSecretField covers secret-typed keys', () => {
        expect(isSecretField('api_key')).toBe(true);
        expect(isSecretField('password')).toBe(true);
        expect(isSecretField('model')).toBe(false);
    });
});

describe('inspector schema (#554)', () => {
    it('every unified node type has inline-configurable fields', () => {
        for (const key of WORKFLOW_NODE_KEYS) {
            const fields = fieldsForNode(key, false).concat(fieldsForNode(key, true));
            // start/end only carry label (filtered out) — everything else
            // must expose at least one config field
            if (
                key !== 'start' &&
                key !== 'end' &&
                key !== 'iteration' &&
                key !== 'parallel' &&
                key !== 'variable_aggregator'
            ) {
                expect(fields.length, `${key} has no fields`).toBeGreaterThan(0);
            }
        }
    });

    it('registry-derived secret fields map to the secret widget', () => {
        const adv = fieldsForNode('http_request', true).concat(fieldsForNode('mcp_tool', true));
        const secrets = adv.filter((f) => f.widget === 'secret');
        expect(secrets.map((f) => f.key)).toContain('api_key');
        expect(secrets.map((f) => f.key)).toContain('auth_token');
    });

    it('stripSessionSecrets removes secret keys from persisted JSON', () => {
        const graph = {
            nodes: {
                a: { type: 'http_request', url: 'https://x', api_key: 'sk-leak' },
                b: { type: 'mcp_tool', auth_token: 'tok-leak', tool_name: 't' },
                c: { type: 'llm', model: 'm' },
            },
            edges: [],
            start_node_id: 'a',
        };
        const out = stripSessionSecrets(graph);
        expect(JSON.stringify(out)).not.toContain('sk-leak');
        expect(JSON.stringify(out)).not.toContain('tok-leak');
        expect(out.nodes?.a?.url).toBe('https://x');
        expect(out.nodes?.b?.tool_name).toBe('t');
        expect(out.nodes?.c?.model).toBe('m');
        // input untouched
        expect(graph.nodes?.a?.api_key).toBe('sk-leak');
        // idempotent — no secrets left to strip
        expect(stripSessionSecrets(out)).toBe(out);
    });
});

describe('nodeCardSummary (#554)', () => {
    it('picks the most relevant value by deterministic priority', () => {
        expect(nodeCardSummary({ model: 'gpt', tool_name: 't' })).toBe('gpt');
        expect(nodeCardSummary({ tool_name: 'read_file', language: 'py' })).toBe('read_file');
        expect(nodeCardSummary({ url: 'https://api.x.com', method: 'GET' })).toBe(
            'https://api.x.com'
        );
        expect(nodeCardSummary({})).toBe('');
        expect(nodeCardSummary({ model: '' })).toBe('');
    });

    it('truncates long values and stringifies non-strings', () => {
        expect(nodeCardSummary({ code: 'x'.repeat(60) })).toHaveLength(46);
        expect(nodeCardSummary({ tool_args: { a: 1 } })).toBe('{"a":1}');
    });
});
