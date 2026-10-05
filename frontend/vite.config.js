import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The backend base URL is the ONLY environment knob:
//   dev  — Vite proxies /api -> the local gateway (see server.proxy below)
//   prod — set VITE_API_BASE at build time to the Render API origin
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    strictPort: false,
    // Bind 0.0.0.0, not localhost: hosted/preview environments reach the
    // dev server from outside the container, and a localhost-bound socket
    // is invisible to them.
    host: true,
    // Preview/CI hosts are dynamic (e.g. 5173-<id>.e2b.app). Vite's host
    // allowlist would reject them with "Blocked request"; this is a dev
    // server only — `vite build` output is unaffected.
    allowedHosts: true,
    // Same-origin /api -> gateway. The browser never talks cross-origin,
    // so the console works behind any proxy without CORS changes, and the
    // X-API-Key auth header rides the proxy unchanged.
    proxy: {
      "/api": {
        target: process.env.VITE_API_PROXY || "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
