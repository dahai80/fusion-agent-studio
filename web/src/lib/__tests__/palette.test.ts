// #552: canvas palette derived from the node registry — every server node
// type stays reachable, planned (non-executable) entries are flagged.
import { describe, expect, it } from 'vitest';

import { NODE_DEFINITIONS, nodeRegistry, paletteGroups } from '../nodeRegistry';

describe('paletteGroups (#552)', () => {
    it('groups all 17 registry types by category plus server-only entries', () => {
        const groups = paletteGroups();
        const entries = groups.flatMap((g) => g.entries);
        expect(entries.length).toBe(NODE_DEFINITIONS.length + 2);
        const keys = new Set(entries.map((e) => e.type));
        for (const d of NODE_DEFINITIONS) expect(keys.has(d.key)).toBe(true);
        expect(keys.has('error_handler')).toBe(true);
        expect(keys.has('verify')).toBe(true);
    });

    it('every server node type is reachable as a draggable entry', () => {
        const serverTypes = new Set(
            paletteGroups()
                .flatMap((g) => g.entries)
                .filter((e) => !e.planned)
                .map((e) => e.serverType)
        );
        for (const st of [
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
        ]) {
            expect(serverTypes.has(st), `missing server type ${st}`).toBe(true);
        }
    });

    it('planned entries are exactly the unified types without a server executor', () => {
        const planned = paletteGroups()
            .flatMap((g) => g.entries)
            .filter((e) => e.planned)
            .map((e) => e.type)
            .sort();
        expect(planned).toEqual([
            'code',
            'http_request',
            'human_input',
            'iteration',
            'mcp_tool',
            'question_classifier',
            'sub_flow',
            'template',
        ]);
    });

    it('registry remains the source of truth (duplicate registration throws)', () => {
        expect(() => nodeRegistry.register({ ...NODE_DEFINITIONS[0] })).toThrow(
            /already registered/
        );
    });
});
