import { describe, expect, test } from 'bun:test';
import { AgentStudioClient, parseSseChunk } from '../src/index';

describe('parseSseChunk', () => {
    test('parses data frames and keeps the remainder', () => {
        const parsed = parseSseChunk('data: {"type":"token","token":"hi"}\n\ndata: {"type":"done"');
        expect(parsed.events).toHaveLength(1);
        expect(parsed.events[0].type).toBe('token');
        expect(parsed.rest).toBe('data: {"type":"done"');
    });

    test('carries event names onto frames', () => {
        const parsed = parseSseChunk('event: span\ndata: {"type":"span"}\n\n');
        expect(parsed.events[0].event).toBe('span');
    });

    test('falls back to raw frames for bad JSON', () => {
        const parsed = parseSseChunk('data: not-json\n\n');
        expect(parsed.events[0].type).toBe('raw');
    });
});

describe('AgentStudioClient', () => {
    test('injects api key and posts JSON', async () => {
        const seen: Array<{ url: string; init?: RequestInit }> = [];
        const fetchImpl = (async (url: string, init?: RequestInit) => {
            seen.push({ url, init });
            return new Response(JSON.stringify({ run_id: 'r1', status: 'pending' }), {
                status: 200,
                headers: { 'content-type': 'application/json' },
            });
        }) as unknown as typeof fetch;
        const client = new AgentStudioClient({ baseUrl: 'http://x', apiKey: 'k', fetchImpl });
        const res = await client.submitRun({ goal: 'g', agent_id: 'a' });
        expect(res.run_id).toBe('r1');
        const [call] = seen;
        expect(call.url).toBe('http://x/v1/runs');
        expect((call.init?.headers as Record<string, string>)['x-api-key']).toBe('k');
        expect(JSON.parse(String(call.init?.body))).toMatchObject({ goal: 'g', agent_id: 'a' });
    });

    test('retries 502 then succeeds', async () => {
        let calls = 0;
        const fetchImpl = (async () => {
            calls += 1;
            if (calls === 1) return new Response('bad gateway', { status: 502 });
            return new Response(JSON.stringify({ run_id: 'r2' }), { status: 200 });
        }) as unknown as typeof fetch;
        const client = new AgentStudioClient({ baseUrl: 'http://x', fetchImpl, retries: 1 });
        const res = await client.getRun('r2');
        expect(calls).toBe(2);
        expect(res.run_id).toBe('r2');
    });

    test('custom transport bypasses fetch', async () => {
        const calls: string[] = [];
        const client = new AgentStudioClient({
            request: async (method, path) => {
                calls.push(`${method} ${path}`);
                return { status: 'ok', sessions: [], total: 0, offset: 0 };
            },
        });
        await client.listSessions();
        expect(calls).toEqual(['GET /v1/sessions?limit=50&offset=0']);
    });

    test('rpc posts the method envelope', async () => {
        const bodies: unknown[] = [];
        const client = new AgentStudioClient({
            request: async (_method, _path, body) => {
                bodies.push(body);
                return { status: 'ok' };
            },
        });
        await client.rpc('agent.list', { limit: 2 });
        expect(bodies[0]).toEqual({ method: 'agent.list', params: { limit: 2 } });
    });

    test('streams run events from an injected stream', async () => {
        const body = new ReadableStream<Uint8Array>({
            start(controller) {
                controller.enqueue(
                    new TextEncoder().encode('data: {"type":"status","status":"running"}\n\n')
                );
                controller.enqueue(new TextEncoder().encode('data: {"type":"done"}\n\n'));
                controller.close();
            },
        });
        const client = new AgentStudioClient({
            streamRequest: async (path) => {
                expect(path).toBe('/v1/runs/r9/stream');
                return body;
            },
        });
        const seen: string[] = [];
        for await (const event of client.streamRun('r9')) seen.push(event.type);
        expect(seen).toEqual(['status', 'done']);
    });
});
