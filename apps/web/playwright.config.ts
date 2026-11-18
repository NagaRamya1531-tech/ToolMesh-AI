import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  use: { baseURL: process.env.TOOLMESH_TEST_WEB ?? "http://127.0.0.1:5173" },
});
