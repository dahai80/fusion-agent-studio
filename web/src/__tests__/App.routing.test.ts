// #528 route regression: the legacy workflows page keeps its own hash
// route (top-nav link, manifest page list, e2e smoke) while unknown
// hashes fall to the AI Agent Dashboard entry — the CI e2e failure this
// pins was `#/workflows` silently becoming the home tab.
import { afterEach, describe, expect, it } from 'vitest';

import { parseHash } from '../App';

function withHash(hash: string) {
    window.location.hash = hash;
    return parseHash();
}

describe('parseHash route contract', () => {
    afterEach(() => {
        window.location.hash = '';
    });

    it('keeps #/workflows on the legacy workflows page', () => {
        expect(withHash('#/workflows')).toEqual({ name: 'workflows' });
    });

    it('falls back unknown hashes to the AI Agent Dashboard (#528)', () => {
        expect(withHash('#/')).toEqual({ name: 'studio', tab: 'home' });
        expect(withHash('#/nope')).toEqual({ name: 'studio', tab: 'home' });
    });

    it('strips deep-link queries before routing (#528)', () => {
        expect(withHash('#/aiagent?agent=ag-1')).toEqual({
            name: 'studio',
            tab: 'aiagent',
        });
    });

    it('keeps run and agent detail routes', () => {
        expect(withHash('#/runs/r-1')).toEqual({ name: 'run', id: 'r-1' });
        expect(withHash('#/agents/ag-1')).toEqual({ name: 'agent', id: 'ag-1' });
    });
});
