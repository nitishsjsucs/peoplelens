import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Built into web/dist and served by the Worker as static assets
// (wrangler.jsonc `assets`, with /api/* routed to the Worker first).
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  test: { include: ["test/**/*.test.ts"], environment: "node" },
});
