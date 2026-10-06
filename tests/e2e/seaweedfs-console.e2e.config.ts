import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const fixturePath = process.env.SWC_E2E_FIXTURE || "output/e2e-2026-10-06/live/live-fixture.json";
const fixture = JSON.parse(await import("node:fs/promises").then((fs) => fs.readFile(fixturePath, "utf8")));
const appUrl = process.env.SWC_E2E_BASE_URL || fixture.url || "http://127.0.0.1:18775";
const here = dirname(fileURLToPath(import.meta.url));
const e2eRepo = process.env.SWC_E2E_REPO || resolve(here, "../../../../useful_repo/e2e");
const { web } = await import(pathToFileURL(resolve(e2eRepo, "packages/web/dist/index.js")).href);

export default {
  projectId: "seaweedfs-console-live",
  tests: "tests/e2e/**/*.e2e.ts",
  output: process.env.SWC_E2E_OUTPUT || "output/e2e-2026-10-06/runner",
  timeout: 180000,
  launchTimeout: 90000,
  actionTimeout: 20000,
  assertionTimeout: 15000,
  cleanupTimeout: 45000,
  workers: 1,
  retries: 0,
  cache: "off",
  trace: "off",
  video: "retain-on-failure",
  reporters: ["list", "markdown"],
  credentials: {
    admin: {
      username: "admin",
      password: () => fixture.admin_password,
    },
  },
  targets: [
    {
      name: "desktop",
      engine: web({ viewport: { width: 1280, height: 900 } }),
      app: {
        url: appUrl,
        environment: "test",
        identity: "seaweedfs-console-live-2026-10-06",
      },
    },
    {
      name: "narrow",
      engine: web({ viewport: { width: 595, height: 900 } }),
      app: {
        url: appUrl,
        environment: "test",
        identity: "seaweedfs-console-live-2026-10-06",
      },
    },
  ],
};
