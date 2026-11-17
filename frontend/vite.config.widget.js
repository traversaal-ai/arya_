import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// The embeddable widget (iframe app served at /embed/{pk}). Its own build:
// absolute base (the HTML is served from /embed/<key>, so relative asset
// paths would break), its own dist subtree so the builder build never
// invalidates it. Build order matters: `build:app` wipes dist/ first.
const forbidAdminApi = {
  // Hard guard: src/api.js is the ADMIN client (agent CRUD, transcripts,
  // ?api= override). One stray import would ship admin capability into
  // every customer's page — fail the build instead.
  name: "arya-forbid-admin-api",
  load(id) {
    if (id.replace(/\\/g, "/").endsWith("/src/api.js")) {
      throw new Error(
        "widget bundle imported src/api.js (admin surface) — use src/widget/api.js"
      );
    }
    return null;
  },
};

export default defineConfig({
  base: "/widget/",
  plugins: [react(), forbidAdminApi],
  build: {
    outDir: "dist/widget",
    emptyOutDir: true,
    target: "es2020",
    rollupOptions: {
      input: fileURLToPath(new URL("./widget.html", import.meta.url)),
    },
  },
  server: {
    port: 5174,
    proxy: { "/api": process.env.VITE_API_PROXY || "http://localhost:8000" },
  },
});
