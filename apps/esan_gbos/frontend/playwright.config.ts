import { defineConfig, devices } from "@playwright/test";

const liveBaseUrl = process.env.GBOS_E2E_BASE_URL;
const liveStorageState = process.env.GBOS_E2E_STORAGE_STATE;
const harnessBaseUrl =
  process.env.GBOS_E2E_HARNESS_URL ??
  "http://127.0.0.1:4173/assets/esan_gbos/frontend/";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  forbidOnly: true,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: liveBaseUrl
    ? undefined
    : {
        command: "pnpm exec vite preview --host 127.0.0.1 --port 4173",
        url: harnessBaseUrl,
        reuseExistingServer: true,
        timeout: 30_000,
      },
  projects: [
    {
      name: "frontend-harness",
      use: { baseURL: harnessBaseUrl },
    },
    {
      name: "frappe-site",
      use: {
        baseURL: liveBaseUrl ?? "http://127.0.0.1:9",
        storageState: liveStorageState,
      },
    },
  ],
});
