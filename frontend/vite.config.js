import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev mode: `npm run dev` on :5173, API proxied to the FastAPI backend.
// Override the target with VITE_API_PROXY if the backend runs elsewhere.
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    proxy: {
      "/api": process.env.VITE_API_PROXY || "http://localhost:8000",
    },
  },
});
