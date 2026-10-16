import { defineConfig, devices } from "@playwright/test";

const useDevServer = process.env.MEDIA_E2E_DEV === "1";

export default defineConfig({
  testDir: "./tests/e2e",
  globalSetup: "./tests/global-setup.ts",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  workers: 2,
  reporter: "list",
  use: { baseURL: "http://127.0.0.1:5189", trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: useDevServer
      ? "npm run dev -- --port 5189 --strictPort --force"
      : "npm run build && npm run preview -- --port 5189 --strictPort",
    url: "http://127.0.0.1:5189",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
