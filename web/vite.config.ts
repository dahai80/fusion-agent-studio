import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// Dev server 11460 (architecture/port-registry.yaml). Proxies API paths to
// the daemon HTTP server on 11442 so standalone mode mirrors the embedded
// gateway /shell/fusion-agent-studio/ layout (same-origin fetch, no CORS).
// FUSION_DEV_BACKEND / FUSION_AGENT_STUDIO_PORT override for CI parity.
const backend = process.env.FUSION_DEV_BACKEND ?? 'http://127.0.0.1:11442';
const port = Number(process.env.FUSION_AGENT_STUDIO_PORT ?? 11460);

const apiPaths = ['/v1', '/api', '/health', '/metrics', '/graphs', '/agents', '/ws'];

export default defineConfig({
    plugins: [react()],
    // #327: embedded deployment serves dist under the gateway sub-path
    // /shell/fusion-agent-studio/ — absolute /assets would 404 there.
    // Relative base + hash router (#/...) resolves assets against the module
    // root in BOTH embedded and standalone-served modes.
    base: './',
    server: {
        port,
        strictPort: true,
        proxy: Object.fromEntries(
            apiPaths.map((p) => [p, { target: backend, changeOrigin: true }])
        ),
    },
    preview: {
        port,
        strictPort: true,
        proxy: Object.fromEntries(
            apiPaths.map((p) => [p, { target: backend, changeOrigin: true }])
        ),
    },
    envPrefix: ['VITE_', 'FUSION_'],
});
