import { defineConfig } from "vite";
import { fileURLToPath } from "node:url";

// The loader customers paste: IIFE, STABLE filename (no hash — the URL
// lives in customers' HTML), lands at dist/arya-widget.js so the existing
// StaticFiles mount serves it at /arya-widget.js. Runs after build:app
// (which wipes dist/), hence emptyOutDir: false.
export default defineConfig({
  build: {
    outDir: "dist",
    emptyOutDir: false,
    target: "es2019",
    lib: {
      entry: fileURLToPath(new URL("./src/loader/index.js", import.meta.url)),
      formats: ["iife"],
      name: "AryaWidgetLoader",
      fileName: () => "arya-widget.js",
    },
  },
});
