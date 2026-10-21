import type { StreamEvent } from './types';

export const SSE_IDLE_TIMEOUT_MS = 0;

export function parseSseChunk(buffer: string): {
    events: StreamEvent[];
    rest: string;
} {
    const events: StreamEvent[] = [];
    let rest = buffer;
    let boundary = rest.indexOf('\n\n');
    while (boundary !== -1) {
        const block = rest.slice(0, boundary);
        rest = rest.slice(boundary + 2);
        const dataLines: string[] = [];
        let eventName = '';
        for (const line of block.split('\n')) {
            if (line.startsWith('event:')) {
                eventName = line.slice('event:'.length).trim();
            } else if (line.startsWith('data:')) {
                dataLines.push(line.slice('data:'.length).trimStart());
            }
        }
        if (dataLines.length > 0) {
            const data = dataLines.join('\n');
            let frame: StreamEvent;
            try {
                frame = JSON.parse(data) as StreamEvent;
            } catch {
                frame = { type: 'raw', data };
            }
            if (eventName && !frame.event) {
                frame.event = eventName;
            }
            events.push(frame);
        }
        boundary = rest.indexOf('\n\n');
    }
    return { events, rest };
}

export async function* readSseStream(
    body: ReadableStream<Uint8Array>
): AsyncGenerator<StreamEvent> {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    try {
        while (true) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const parsed = parseSseChunk(buffer);
            buffer = parsed.rest;
            for (const event of parsed.events) {
                yield event;
            }
        }
        if (buffer.trim()) {
            const parsed = parseSseChunk(`${buffer}\n\n`);
            for (const event of parsed.events) {
                yield event;
            }
        }
    } finally {
        reader.releaseLock();
    }
}
