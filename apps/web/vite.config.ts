import { defineConfig } from "vite";

export default defineConfig({
  server: {
    proxy: Object.fromEntries(["/readyz", "/api"].map((path) => [
      path, process.env.TOOLMESH_API_ORIGIN ?? "http://127.0.0.1:8000",
    ])),
  },
});
