// #489: jsdom lacks the browser APIs React Flow and the pages rely on.
import '@testing-library/jest-dom/vitest';

class ResizeObserverStub {
    observe() {}
    unobserve() {}
    disconnect() {}
}

if (typeof globalThis.ResizeObserver === 'undefined') {
    globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}

if (!globalThis.DOMMatrixReadOnly) {
    class DOMMatrixReadOnlyStub {
        m22 = 1;
    }
    globalThis.DOMMatrixReadOnly = DOMMatrixReadOnlyStub as unknown as typeof DOMMatrixReadOnly;
}

// jsdom under vitest can start without storage enabled — the canvas reads
// sessionStorage/localStorage at mount, so provide in-memory fallbacks.
class MemoryStorageStub {
    private map = new Map<string, string>();
    get length() {
        return this.map.size;
    }
    key(index: number) {
        return [...this.map.keys()][index] ?? null;
    }
    getItem(key: string) {
        return this.map.has(key) ? (this.map.get(key) as string) : null;
    }
    setItem(key: string, value: string) {
        this.map.set(key, String(value));
    }
    removeItem(key: string) {
        this.map.delete(key);
    }
    clear() {
        this.map.clear();
    }
}

if (typeof window !== 'undefined') {
    if (!window.localStorage) {
        Object.defineProperty(window, 'localStorage', { value: new MemoryStorageStub() });
    }
    if (!window.sessionStorage) {
        Object.defineProperty(window, 'sessionStorage', { value: new MemoryStorageStub() });
    }
}

// React Flow reads element sizes via getBoundingClientRect — jsdom
// provides it (all zeros), which is sufficient for render-only tests.
