import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The backend base URL is the ONLY environment knob:
//   dev  — Vite env or http://localhost:8000 (the local gateway)
//   prod — set VITE_API_BASE at build time to the Render API origin
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: false },
  build: { outDir: "dist", sourcemap: false },
});
