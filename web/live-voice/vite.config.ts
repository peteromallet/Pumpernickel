import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// https://vitejs.dev/config/
const BACKEND_TARGET = process.env.VITE_BACKEND_URL ?? "http://localhost:8000";

export default defineConfig({
  base: "/live/",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target: BACKEND_TARGET, changeOrigin: true },
      "/ws": { target: BACKEND_TARGET, changeOrigin: true, ws: true },
    },
  },
});
