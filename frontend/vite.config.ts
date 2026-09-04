import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev-server proxy so `npm run dev` (frontend on :5173) can talk to the
// backend (uvicorn on :8000) without CORS/absolute-URL juggling -- the
// production build (served by FastAPI itself, see the top-level Dockerfile)
// doesn't need this at all since both are the same origin there.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
