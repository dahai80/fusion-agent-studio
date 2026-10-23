import { sdk } from './sdk';

// #444: follow the shell theme toggle when embedded in the union-web shell.
// The shell posts {type:"event", event:"theme", payload:"dark"|"light"} to
// the iframe; @fusion/web-sdk routes it to sdk.theme.subscribe. The palette
// is class-based (tokens.css): html.light switches the --fs-* tokens, dark
// is the default. Every applied mode is persisted under the shell's own
// storage key so the standalone console inherits the last shell theme.
const STORAGE_KEY = 'fusion.shell.theme';

export function applyTheme(mode: 'dark' | 'light'): void {
    const root = document.documentElement;
    root.classList.toggle('light', mode === 'light');
    root.classList.toggle('dark', mode === 'dark');
    try {
        localStorage.setItem(STORAGE_KEY, mode);
    } catch (e) {
        console.warn('[theme] persist failed', e);
    }
}

// Subscribe once at module load. Embedded: every host push (initial + each
// toggle) re-applies. Standalone: sdk.theme tracks prefers-color-scheme, so
// OS-level changes still flip the console.
export function initTheme(): void {
    sdk.theme.subscribe((mode) => {
        if (mode === 'dark' || mode === 'light') {
            applyTheme(mode);
        }
    });
    // Embedded: the re-push on iframe load may arrive before the SDK
    // finishes wiring listeners (about:blank loses the first postMessage);
    // seed from the shell's stored value so the first paint already matches.
    try {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (saved === 'dark' || saved === 'light') {
            applyTheme(saved);
        }
    } catch (e) {
        console.warn('[theme] storage unavailable', e);
    }
}
