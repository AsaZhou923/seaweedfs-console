import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

type Fixture = {
  url: string;
  official_admin_url?: string;
  admin_password: string;
  bucket: string;
  project_id: string;
  scope_id: string;
  output_scope_id?: string;
  restricted_scope_id?: string;
  management_id: string;
  source_keys: string[];
  object_ids: string[];
};

const fixturePath = process.env.SWC_E2E_FIXTURE || "output/e2e-2026-10-06/live/live-fixture.json";
const fixture = JSON.parse(await readFile(fixturePath, "utf8")) as Fixture;
const baseUrl = process.env.SWC_E2E_BASE_URL || fixture.url || "http://127.0.0.1:18775";
const here = dirname(fileURLToPath(import.meta.url));
const e2eRepo = process.env.SWC_E2E_REPO || resolve(here, "../../../../useful_repo/e2e");
const { test } = await import(pathToFileURL(resolve(e2eRepo, "packages/web/dist/index.js")).href);
const { expect, credentials } = await import(pathToFileURL(resolve(e2eRepo, "packages/e2e/dist/index.js")).href);

function firstPresent<T>(values: (T | undefined | null)[]): T {
  const value = values.find((item): item is T => item != null && item !== "");
  if (value == null) throw new Error("Live e2e fixture is missing a required value.");
  return value;
}

async function loginThroughUi(app: any, browser: any) {
  const admin = credentials.user("admin");
  await app.clearState();
  await app.open("/");
  await expect(browser.locator(".loginBox")).toBeVisible();
  await browser.locator('input[autocomplete="username"]').fill(admin.username);
  await browser.locator('input[autocomplete="current-password"]').fill(admin.password);
  await browser.locator(".loginBox button").click();
  await expect(browser.locator(".shell")).toBeVisible({ timeout: 20000 });
}

async function openAuthenticated(app: any, browser: any, path: string) {
  await app.open(path);
  await expect(browser.locator(".shell")).toBeVisible({ timeout: 20000 });
}

async function assertNoTopLevelError(browser: any) {
  await expect(browser.locator(".topbar + .notice.error")).toHaveCount(0, { timeout: 1000 });
}

async function selectTestImagesScope(browser: any) {
  await expect(browser.locator(".contextBar")).toBeVisible({ timeout: 20000 });
  await browser.locator(".contextBar select").nth(1).selectOption("Test images");
  await expect.poll(async () => browser.evaluate(() => {
    const select = document.querySelectorAll(".contextBar select")[1] as HTMLSelectElement | undefined;
    return select?.value || "";
  }), { timeout: 20000, interval: 250, message: "Test images Scope should remain selected" }).toBe(fixture.scope_id);
}

async function apiGet(browser: any, path: string) {
  return browser.evaluate(async (requestPath) => {
    const response = await fetch(`/api/v1${requestPath}`, { credentials: "same-origin" });
    const text = await response.text();
    let data: any = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = { raw: text };
    }
    return { status: response.status, ok: response.ok, data };
  }, path);
}

async function apiFetch(browser: any, path: string, options: { method?: string; body?: unknown } = {}) {
  return browser.evaluate(async ({ requestPath, method, body }) => {
    const response = await fetch(`/api/v1${requestPath}`, {
      method: method || "GET",
      credentials: "same-origin",
      headers: body == null ? undefined : { "Content-Type": "application/json" },
      body: body == null ? undefined : JSON.stringify(body),
    });
    const contentType = response.headers.get("content-type") || "";
    const bytes = await response.arrayBuffer();
    let data: any = {};
    if (contentType.includes("json")) {
      try {
        data = JSON.parse(new TextDecoder().decode(bytes));
      } catch {
        data = {};
      }
    }
    return { status: response.status, ok: response.ok, contentType, byteLength: bytes.byteLength, data };
  }, { requestPath: path, method: options.method || "GET", body: options.body ?? null });
}

test.setup("authenticate admin through the real login form", { sessions: ["admin"] }, async ({ app, browser, session }) => {
  await loginThroughUi(app, browser);
  await session.save("admin");
});

test("rejects invalid login without opening the console", async ({ app, browser }) => {
  await app.clearState();
  await app.open("/");
  await expect(browser.locator(".loginBox")).toBeVisible();
  await app.screenshot("login-form-before-invalid-password");
  await browser.locator('input[autocomplete="username"]').fill("admin");
  await browser.locator('input[autocomplete="current-password"]').fill("wrong-password-e2e");
  await browser.locator(".loginBox button").click();
  await expect(browser.locator(".formError")).toContainText(/INVALID|401|credential|密码|认证/i);
  await expect(browser.locator(".shell")).toHaveCount(0);
});

test("covers asset search, preview detail, compare, reports, and narrow layout", { session: "admin" }, async ({ app, browser }) => {
  await openAuthenticated(app, browser, "/#assets");
  await selectTestImagesScope(browser);
  await assertNoTopLevelError(browser);
  await expect(browser.locator(".asset")).toHaveCount(9, { timeout: 30000 });
  await expect(browser.locator("main")).toContainText(firstPresent([fixture.bucket]));
  await expect(browser.locator("main")).toContainText(/corrupt\.jpg|resource_limited|corrupt|unsupported/i);

  const specialKey = fixture.source_keys.find((key) => key.includes("literal%_image")) || "literal%_image";
  await browser.locator(".searchInput").fill("literal%_image");
  await expect(browser.locator(".asset")).toHaveCount(1, { timeout: 15000 });
  await expect(browser.locator("main")).toContainText(specialKey);

  await browser.locator(".searchInput").clear();
  await expect(browser.locator(".asset")).toHaveCount(9, { timeout: 15000 });
  await browser.locator('.asset input[type="checkbox"]').nth(0).check();
  await browser.locator('.asset input[type="checkbox"]').nth(1).check();
  await expect(browser.locator(".comparePanel")).toBeVisible();

  await browser.locator(".asset").first().click();
  await expect(browser.locator(".drawer")).toBeVisible();
  await expect(browser.locator(".drawer")).toContainText(/Object ID|对象 ID/i);
  await expect(browser.locator(".drawer")).toContainText(/Version ID|版本下载/i);
});

test("clears asset state when switching scopes", { session: "admin" }, async ({ app, browser }) => {
  await openAuthenticated(app, browser, "/#assets");
  await selectTestImagesScope(browser);
  await expect(browser.locator(".asset")).toHaveCount(9, { timeout: 30000 });
  await browser.locator(".contextBar select").nth(1).selectOption("Derived outputs");
  await expect(browser.locator(".asset")).toHaveCount(0, { timeout: 30000 });
  await expect(browser.locator(".drawer")).toHaveCount(0);
});

test("verifies preview bytes and restricted scope denies preview/download", { session: "admin" }, async ({ app, browser }) => {
  await openAuthenticated(app, browser, "/#assets");
  await selectTestImagesScope(browser);
  const assets = await apiGet(browser, `/scopes/${fixture.scope_id}/objects?limit=60`);
  const sourceAsset = assets.data.items?.find((item: any) => String(item.key || "").includes("sample-01"));
  const sourceId = firstPresent([sourceAsset?.id]);
  const previewPixels = await browser.evaluate(async ({ scopeId, objectId }) => {
    const image = new Image();
    image.src = `/api/v1/scopes/${scopeId}/objects/${objectId}/preview`;
    await new Promise<void>((resolve, reject) => {
      image.onload = () => resolve();
      image.onerror = () => reject(new Error("preview image failed to load"));
    });
    return { width: image.naturalWidth, height: image.naturalHeight };
  }, { scopeId: fixture.scope_id, objectId: sourceId });
  expect(previewPixels.width).toBeGreaterThan(0);
  expect(previewPixels.height).toBeGreaterThan(0);
  const download = await apiFetch(browser, `/scopes/${fixture.scope_id}/objects/${sourceId}/download`);
  expect(download.status).toBe(200);
  expect(download.byteLength).toBeGreaterThan(100);

  const restrictedObjects = await apiGet(browser, `/scopes/${fixture.restricted_scope_id}/objects?limit=20`);
  const restricted = restrictedObjects.data.items?.find((item: any) => String(item.key || "").includes("literal%_image")) || restrictedObjects.data.items?.[0];
  const restrictedId = firstPresent([restricted?.id]);
  const restrictedPreview = await apiFetch(browser, `/scopes/${fixture.restricted_scope_id}/objects/${restrictedId}/preview`);
  const restrictedDownload = await apiFetch(browser, `/scopes/${fixture.restricted_scope_id}/objects/${restrictedId}/download`);
  expect(restrictedPreview.status).toBe(403);
  expect(restrictedDownload.status).toBe(403);
});

test("submits owned derived-image work and reads diagnostics/capacity evidence", { session: "admin" }, async ({ app, browser }) => {
  await openAuthenticated(app, browser, "/#presets");
  await selectTestImagesScope(browser);
  await assertNoTopLevelError(browser);
  await expect(browser.locator("main")).toContainText(/Image presets|图片规格|规格库/i);
  const assets = await apiGet(browser, `/scopes/${fixture.scope_id}/objects?limit=60`);
  const sourceAsset = assets.data.items?.find((item: any) => String(item.key || "").includes("sample-01"));
  const objectId = firstPresent([sourceAsset?.id]);
  const presetName = `e2e-thumb-${Date.now()}`;
  await browser.locator("label.field").filter({ hasText: /Name|名称/ }).first().getByRole("textbox").fill(presetName);
  await browser.locator("button").filter({ hasText: /Create immutable preset version|创建不可变规格版本/i }).click();
  await expect(browser.locator("main")).toContainText(presetName, { timeout: 20000 });
  const presetReadback = await apiGet(browser, `/projects/${fixture.project_id}/presets`);
  const presetItems = Array.isArray(presetReadback.data) ? presetReadback.data : presetReadback.data.items || [];
  const preset = presetItems.find((item: any) => item.name === presetName);
  const presetId = firstPresent([preset?.id]);
  await browser.locator("label.field").filter({ hasText: /^Object ID$|^对象 ID$/ }).first().getByRole("textbox").fill(objectId);
  await browser.locator("label.field").filter({ hasText: /Preset/ }).first().getByRole("combobox").selectOption({ value: presetId });
  const outputKey = `derived/e2e-${Date.now()}.webp`;
  await browser.locator("label.field").filter({ hasText: /Output Key|输出 Key/ }).first().getByRole("textbox").fill(outputKey);
  await browser.locator("label.field").filter({ hasText: /Output Scope ID|输出 Scope ID/ }).first().getByRole("textbox").fill(firstPresent([fixture.output_scope_id]));
  await browser.locator("button").filter({ hasText: /^Submit variant job$|^提交派生任务$/ }).click();
  await expect(browser.locator("main")).toContainText("variant.submit", { timeout: 30000 });
  await expect.poll(async () => {
    const result = await apiGet(browser, `/scopes/${fixture.scope_id}/derived-variants`);
    const variant = result.data.items?.find((item: any) => item.output_key === outputKey);
    return variant?.status || "missing";
  }, { timeout: 90000, interval: 1000, message: "derived variant should complete on the live worker" }).toBe("succeeded");
  const readback = await apiGet(browser, `/scopes/${fixture.scope_id}/derived-variants`);
  const completed = readback.data.items?.find((item: any) => item.output_key === outputKey);
  const variantId = firstPresent([completed?.id]);
  await expect(browser.locator("main")).toContainText(variantId, { timeout: 30000 });
  expect(Number(completed?.output_size ?? 0)).toBeGreaterThan(0);
  expect(String(completed?.output_sha256 || "")).toMatch(/^[a-f0-9]{64}$/);

  await app.open("/#diagnostics");
  await selectTestImagesScope(browser);
  await expect(browser.locator("main")).toContainText(/诊断证据|Diagnostics/i);
  await browser.locator('input').first().fill(objectId);
  await browser.locator("button").filter({ hasText: /运行 HEAD\/GET\/预览诊断|diagnostic/i }).click();
  await expect(browser.locator("main")).toContainText(/诊断原始证据|preview|head|evidence|status/i, { timeout: 30000 });

  await app.open("/#operations");
  await selectTestImagesScope(browser);
  await expect(browser.locator("main")).toContainText(/受控操作|Controlled/i);
  await expect(browser.locator("button").filter({ hasText: /Audits \/ Cache \/ Capacity|审计\/缓存\/容量/i })).toBeVisible();
  const trends = await apiGet(browser, `/scopes/${fixture.scope_id}/capacity-trends`);
  expect(trends.status).toBe(200);
  expect(Array.isArray(trends.data.items || trends.data.trends || [])).toBe(true);
});

test("checks official Admin availability, management API readbacks, and readonly inventory", { session: "admin" }, async ({ app, browser }) => {
  if (fixture.official_admin_url) {
    await browser.goto(fixture.official_admin_url, { waitUntil: "domcontentloaded", timeout: 30000 });
    await expect(browser.locator("body")).toContainText(/SeaweedFS|Master|Volume|Filer|S3/i, { timeout: 30000 });
  }

  await openAuthenticated(app, browser, "/#dashboard");
  const management = fixture.management_id;
  const overview = await apiGet(browser, `/management/${management}/overview`);
  expect(overview.status).toBe(200);
  expect(overview.data.protocol_baseline).toBe("4.48");
  expect(String(overview.data.source || "")).toContain("official");
  const topology = await apiGet(browser, `/management/${management}/topology`);
  expect(topology.status).toBe(200);
  expect(Array.isArray(topology.data.topology?.volume_servers)).toBe(true);
  const services = await apiGet(browser, `/management/${management}/services`);
  expect(services.status).toBe(200);
  expect((services.data.volume_servers || []).length).toBeGreaterThan(0);
  const health = await apiGet(browser, `/management/${management}/services/health`);
  expect(health.status).toBe(200);
  expect(health.data.services?.master?.status).toBe("healthy");
  expect(health.data.services?.s3?.status).toBe("healthy");
  const volumes = await apiGet(browser, `/management/${management}/volumes`);
  expect(volumes.status).toBe(200);
  expect((volumes.data.items || []).length).toBeGreaterThan(0);
  const buckets = await apiGet(browser, `/management/${management}/buckets`);
  expect(buckets.status).toBe(200);
  expect((buckets.data.items || []).some((item: any) => item.name === fixture.bucket)).toBe(true);
  const objects = await apiGet(browser, `/management/${management}/objects?scope_id=${fixture.scope_id}&limit=5`);
  expect(objects.status).toBe(200);
  expect((objects.data.items || []).some((item: any) => item.key === "gallery/sample-01.jpg")).toBe(true);
  const iamUsers = await apiGet(browser, `/management/${management}/iam/users`);
  expect(iamUsers.status).toBe(200);
  expect(Array.isArray(iamUsers.data.items)).toBe(true);

  await assertNoTopLevelError(browser);
  for (const route of ["dashboard", "topology", "storage", "buckets", "files", "objects", "iam", "maintenance", "services"]) {
    await app.open(`/#${route}`);
    await expect(browser.locator("main")).toBeVisible();
    await expect(browser.locator("main")).toContainText(/Official|官方|Cluster|Topology|Bucket|IAM|Worker|Service|Volume|Filer|对象/i, { timeout: 30000 });
    await assertNoTopLevelError(browser);
  }
  await expect(browser.locator("main")).toContainText(/healthy|ok|configured|Master|Filer|S3|Volume|服务健康|已配置/i, { timeout: 30000 });
});
