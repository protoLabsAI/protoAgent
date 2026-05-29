import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:7870",
      "/a2a": "http://127.0.0.1:7870",
      "/v1": "http://127.0.0.1:7870",
    },
  },
});
