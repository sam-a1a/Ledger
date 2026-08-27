import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/**
 * There is deliberately no `/api` proxy here.
 *
 * Vite's proxy buffers server-sent events in both dev and preview: the request
 * streams correctly with curl and then hangs in the browser with no frames
 * delivered. Rather than leave a proxy configured that cannot carry this app's
 * primary response type, the client talks to the API directly in development
 * via VITE_API_BASE, and same-origin through nginx in production.
 */
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  preview: { port: 4173 },
  build: { outDir: "dist", sourcemap: true },
});
