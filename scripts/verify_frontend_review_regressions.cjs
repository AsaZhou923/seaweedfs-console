const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { URL } = require("node:url");

const root = path.resolve(__dirname, "..");
const dist = path.join(root, "frontend", "dist");
const requests = [];
let authMode = "auth";

const project = { id: "project-1", project_key: "project-1", display_name: "Mock Project" };
const scopes = [
  { id: "scope-a", project_id: "project-1", connection_id: "s3-1", display_name: "Scope A", bucket: "bucket-a", prefix: "a/", allow_preview: true, allow_original_download: true, writable: true, manage_bucket: true },
  { id: "scope-b", project_id: "project-1", connection_id: "s3-1", display_name: "Scope B", bucket: "bucket-b", prefix: "b/", allow_preview: true, allow_original_download: true, writable: true, manage_bucket: true },
];
const assetA = { id: "obj-a-1", asset_id: "asset-a-1", scope_id: "scope-a", bucket: "bucket-a", key: "a/scope-a-photo.jpg", key_display: "scope-a-photo.jpg", revision: "rev-a", version_id: "v-a-1", size_bytes: 12345, content_type: "image/jpeg", properties: { format: "jpeg", width: 640, height: 480, decode_status: "valid" }, preview_state: "ready", reference_status: "unknown" };

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const json = (res, value, status = 200) => {
  const body = JSON.stringify(value);
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(body);
};
const text = (res, value, status = 200, type = "text/plain; charset=utf-8") => {
  res.writeHead(status, { "Content-Type": type, "Cache-Control": "no-store" });
  res.end(value);
};
const delayedJsonBody = async (res, value, delayMs = 450) => {
  const body = JSON.stringify(value);
  res.writeHead(200, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.flushHeaders();
  await wait(delayMs);
  res.end(body);
};
const writeByTopicName = (name) => requests.filter((item) => {
  if (item.method !== "POST" || !item.pathname.endsWith("/modules/mq/topics")) return false;
  try {
    return JSON.parse(item.body || "{}").name === name;
  } catch {
    return false;
  }
});
const receiptForTopic = (name) => {
  const count = writeByTopicName(name).length;
  return { status: "needs_review", operation_id: `op-${name}-${count}`, result: { confirmed: false }, replayed: false };
};

async function routeApi(req, res, url) {
  const pathname = url.pathname;
  const method = req.method || "GET";
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const body = Buffer.concat(chunks).toString("utf8");
  requests.push({ method, pathname, search: url.search, body, idempotencyKey: req.headers["idempotency-key"] || "", xIdempotencyKey: req.headers["x-idempotency-key"] || "" });

  if (pathname === "/api/v1/auth/me" && authMode !== "auth") return json(res, { error: { code: "UNAUTHENTICATED", message: "Invalid username or password." } }, 401);
  if (pathname === "/api/v1/auth/me") return json(res, { user: { username: "reviewer" }, csrf_token: "csrf", projects: [project] });
  if (pathname === "/api/v1/auth/login" && authMode !== "auth") return json(res, { error: { code: "UNAUTHENTICATED", message: "Invalid username or password." } }, 401);
  if (pathname === "/api/v1/auth/login") return json(res, { user: { username: "reviewer" }, csrf_token: "csrf" });
  if (pathname === "/api/v1/connections") return json(res, { items: [{ id: "s3-1", display_name: "Mock S3", endpoint_url: "http://mock-s3.invalid", secret_ref: "mock-secret" }] });
  if (pathname === "/api/v1/projects/project-1/scopes") return json(res, { items: scopes });
  if (pathname === "/api/v1/management/connections") return json(res, { items: [{ id: "mgmt-1", name: "Mock Admin", admin_url: "http://mock-admin.invalid", s3_connection_id: "s3-1", protocol_baseline: "4.48", management_write_enabled: true, permissions: { "bucket.manage": true, "volume.manage": true, "file.manage": true, "object.manage": true, "table.manage": true, "mq.manage": true } }] });
  if (pathname === "/api/v1/management/connections/mgmt-1/access-policy" && method === "PUT") {
    const payload = body ? JSON.parse(body) : {};
    return json(res, { id: "mgmt-1", name: "Mock Admin", management_write_enabled: payload.management_write_enabled, permissions: payload.permissions || {}, updated: true });
  }
  if (pathname === "/api/v1/management/mgmt-1/buckets" && method === "POST") {
    const payload = body ? JSON.parse(body) : {};
    if (Object.prototype.hasOwnProperty.call(payload, "idempotency_key")) return json(res, { error: { code: "STRICT_SCHEMA", message: "extra inputs are not permitted" } }, 422);
    return json(res, { status: "needs_review", operation_id: `bucket-${requests.filter((item) => item.pathname === pathname && item.method === method).length}`, name: payload.name, result: { confirmed: false }, replayed: false });
  }
  if (pathname === "/api/v1/management/mgmt-1/buckets") return json(res, { items: [{ name: "review-bucket", owner: "owner-a", logical_size: 1024, physical_size: 2048, versioning_status: "enabled", lifecycle_rule_count: 1, policy_statement_count: 1 }] });

  if (pathname === "/api/v1/scopes/scope-a/assets") {
    if (url.searchParams.get("query") === "slow") {
      await wait(350);
      return json(res, { items: [{ ...assetA, id: "obj-a-slow", asset_id: "asset-a-slow", key_display: "slow-stale-photo.jpg" }], next_cursor: null });
    }
    return json(res, { items: [assetA], next_cursor: null });
  }
  if (pathname === "/api/v1/scopes/scope-b/assets") return json(res, { items: [], next_cursor: null });
  if (pathname.includes("/objects/obj-a-1/preview")) return text(res, "mock-image", 200, "image/jpeg");
  if (pathname.includes("/objects/obj-a-1/versions")) return json(res, { items: [{ version_id: "v-a-1", is_latest: true, size: 12345, last_modified: "2026-10-05T00:00:00Z" }] });
  if (pathname === "/api/v1/jobs") {
    if (url.searchParams.get("scope_id") === "scope-a") await wait(300);
    return json(res, { items: url.searchParams.get("scope_id") === "scope-a" ? [{ id: "old-job-a", scope_id: "scope-a", kind: "scan", state: "running", processed: 0, errors: 0, total: null, control_request: "", updated_at: "2026-10-05T00:00:00Z" }] : [] });
  }

  if (pathname.startsWith("/api/v1/management/mgmt-1/services/health")) return json(res, { services: { s3: { status: "healthy", endpoint: "mock", version: "4.48", source: "mock" }, master: { status: "unknown", endpoint: "master:9333", source: "mock" } } });
  if (pathname.startsWith("/api/v1/management/mgmt-1/services")) return json(res, { source: "mock-review", master_nodes: [{ address: "master:9333", is_leader: false }], filer_nodes: [{ address: "filer:8888" }], s3_nodes: [{ address: "s3:8333" }], volume_servers: [{ address: "volume:8080", datacenter: "dc1", rack: "rack1" }], services: {} });
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules/mount-clients")) return json(res, { items: [] });
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules/s3-tables/table-preview")) {
    const payload = body ? JSON.parse(body) : {};
    if (payload.name === "table_a") await wait(1200);
    return json(res, { status: "supported", preview_kind: "raw_file_sample", scope_id: payload.scope_id, table_name: payload.name, columns: [{ name: "marker", type: "string" }], rows: [{ marker: `row-from-${payload.name}-${payload.scope_id}` }], file_list_complete: true, deletes_applied: true, has_delete_files: false, sample_truncated: false, total_rows: 1, source: "mock-review", checked_at: "2026-10-05T00:00:00Z" });
  }
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules/s3-tables/table-details")) {
    const tableName = url.searchParams.get("name") || "unknown";
    const namespace = url.searchParams.get("namespace") || "unknown";
    if (tableName === "table_a") await wait(1200);
    return json(res, { status: "supported", details: { catalog: { version_token: `details-${tableName}-${namespace}` } } });
  }
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules/s3-tables/buckets")) return json(res, { buckets: [{ name: "bucket-a", arn: "arn:seaweed:s3tables:bucket-a", format: "iceberg" }] });
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules/s3-tables/namespaces")) return json(res, { namespaces: [{ name: "ns_a" }] });
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules/s3-tables/tables")) return json(res, { tables: [{ name: "table_a", arn: "arn:seaweed:s3tables:bucket-a/ns_a/table_a", format: "iceberg" }] });
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules/mq/topics") && method === "POST") {
    const payload = body ? JSON.parse(body) : {};
    const name = String(payload.name || "");
    if (name === "receipt-html") return text(res, "<html><body>not json</body></html>", 200, "text/html; charset=utf-8");
    if (name === "receipt-truncated-json") return text(res, "{\"status\":\"confirmed\"", 200, "application/json; charset=utf-8");
    if (name === "receipt-empty") return text(res, "", 200, "application/json; charset=utf-8");
    if (name === "receipt-null") return text(res, "null", 200, "application/json; charset=utf-8");
    if (name === "receipt-empty-object") return json(res, {});
    if (name === "receipt-unknown-status") return json(res, { status: "mystery", operation_id: `op-${name}`, replayed: false });
    if (name === "receipt-status-array") return json(res, { status: ["confirmed"], operation_id: `op-${name}`, replayed: false });
    if (name === "receipt-conflicting-states") return json(res, { status: "confirmed", journal_status: "needs_review", operation_id: `op-${name}`, replayed: false });
    if (name === "receipt-blank-operation") return json(res, { status: "confirmed", operation_id: "   ", replayed: false });
    if (name === "receipt-missing-operation") return json(res, { status: "confirmed", replayed: false });
    if (name === "receipt-missing-replayed") return json(res, { status: "confirmed", operation_id: `op-${name}` });
    if (name === "receipt-confirmed") return json(res, { status: "confirmed", operation_id: `op-${name}-${writeByTopicName(name).length}`, result: { confirmed: true }, replayed: false });
    if (name === "receipt-failed") return json(res, { status: "failed", operation_id: `op-${name}-${writeByTopicName(name).length}`, error_code: "MOCK_FAILED", replayed: false });
    if (name === "receipt-probe-confirmed") return json(res, { status: "confirmed", journal_status: "confirmed", capability_status: "supported", operation_id: `op-${name}-${writeByTopicName(name).length}`, replayed: false });
    return delayedJsonBody(res, receiptForTopic(name));
  }
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules/mq/topics")) return json(res, { status: "supported" });
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules/s3-tables")) return json(res, { status: "supported", modules: { s3_tables: { status: "supported" } } });
  if (pathname.startsWith("/api/v1/management/mgmt-1/modules")) return json(res, { s3_tables: { status: "supported" }, mq: { status: "supported" } });
  if (pathname.startsWith("/api/v1/management/mgmt-1/operations")) return json(res, { items: [] });
  if (pathname.startsWith("/api/v1/management/mgmt-1/overview")) return json(res, { admin: {}, config: {} });
  if (pathname.startsWith("/api/v1/management/mgmt-1/volumes")) return json(res, { items: [{ id: 7, collection: "mock", server: "volume:8080", disk_type: "hdd", size: 2048, file_count: 3, garbage_ratio: 0.01, read_only: false }], next_cursor: "" });
  if (pathname.startsWith("/api/v1/management/mgmt-1/collections")) return json(res, { items: [] });
  if (pathname.startsWith("/api/v1/management/mgmt-1/ec")) return json(res, { items: [] });
  if (pathname.startsWith("/api/v1/management/mgmt-1/files/upload")) return json(res, { status: "needs_review", operation_id: `upload-${requests.filter((item) => item.pathname === pathname && item.method === method).length}`, result: { confirmed: false }, uploaded: true, query_key: url.searchParams.get("idempotency_key"), replayed: false });
  if (pathname.startsWith("/api/v1/management/mgmt-1/files")) return json(res, { items: [{ name: "protected", full_path: "/etc/protected", is_directory: false, protected: true, size: 1, mtime: "2026-10-05T00:00:00Z" }, { name: "plain.txt", full_path: "/plain.txt", is_directory: false, protected: false, size: 2, mtime: "2026-10-05T00:00:00Z" }], next_cursor: "" });
  if (pathname.startsWith("/api/v1/management/mgmt-1/objects")) return json(res, { items: [{ key: "a/scope-a-photo.jpg", size: 12345, etag: "etag-a", version_id: "v-a-1" }], folders: ["a/"], next_cursor: "", total: 1 });
  return json(res, { error: { code: "MOCK_NOT_FOUND", message: `${method} ${pathname}` } }, 404);
}

function startServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    if (url.pathname.startsWith("/api/v1/")) {
      routeApi(req, res, url).catch((error) => json(res, { error: { code: "MOCK_ERROR", message: String(error.message || error) } }, 500));
      return;
    }
    let file = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
    let full = path.normalize(path.join(dist, file));
    if (!full.startsWith(dist) || !fs.existsSync(full) || fs.statSync(full).isDirectory()) full = path.join(dist, "index.html");
    const ext = path.extname(full).toLowerCase();
    const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8" };
    res.writeHead(200, { "Content-Type": types[ext] || "application/octet-stream", "Cache-Control": "no-store" });
    fs.createReadStream(full).pipe(res);
  });
  return new Promise((resolve) => server.listen(0, "127.0.0.1", () => resolve(server)));
}

function browserPath() {
  const candidates = [
    process.env.CHROME_PATH,
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
    "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
    "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  ].filter(Boolean);
  return candidates.find((candidate) => fs.existsSync(candidate));
}

function freePort() {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.listen(0, "127.0.0.1", () => {
      const port = server.address().port;
      server.close(() => resolve(port));
    });
  });
}

async function launchCdp() {
  const exe = browserPath();
  assert.ok(exe, "Chrome or Edge was not found; set CHROME_PATH to run this check.");
  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "seaweedfs-console-cdp-"));
  const proc = spawn(exe, [`--remote-debugging-port=${port}`, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
  for (let i = 0; i < 50; i += 1) {
    try {
      const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
      return { proc, port, profile, browserWSEndpoint: version.webSocketDebuggerUrl };
    } catch {
      await wait(100);
    }
  }
  proc.kill();
  throw new Error("Browser did not expose a DevTools endpoint.");
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (!message.id) return;
      const entry = this.pending.get(message.id);
      if (!entry) return;
      this.pending.delete(message.id);
      if (message.error) entry.reject(new Error(JSON.stringify(message.error)));
      else entry.resolve(message.result);
    };
  }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.onerror = reject;
    });
    return new Cdp(ws);
  }
  send(method, params = {}) {
    const id = this.nextId++;
    const promise = new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
    this.ws.send(JSON.stringify({ id, method, params }));
    return promise;
  }
  async eval(expression) {
    const result = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text || JSON.stringify(result.exceptionDetails));
    return result.result.value;
  }
  close() {
    this.ws.close();
  }
}

async function createPage(browser, url) {
  const browserCdp = await Cdp.connect(browser.browserWSEndpoint);
  const target = await browserCdp.send("Target.createTarget", { url });
  browserCdp.close();
  const targets = await (await fetch(`http://127.0.0.1:${browser.port}/json/list`)).json();
  const pageTarget = targets.find((item) => item.id === target.targetId);
  const page = await Cdp.connect(pageTarget.webSocketDebuggerUrl);
  await page.send("Page.enable");
  await page.send("Runtime.enable");
  return page;
}

const helpers = `
(() => {
  const setControl = (control, value) => {
    const proto = control instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(control, value);
    control.dispatchEvent(new Event("input", { bubbles: true }));
    control.dispatchEvent(new Event("change", { bubbles: true }));
  };
  window.__test = {
    setControl,
    collectAssets(label) {
      return {
        label,
        body: document.body.innerText,
        assetCards: [...document.querySelectorAll("article.asset strong")].map((el) => el.textContent || ""),
        drawerTitle: document.querySelector(".drawer h2")?.textContent || "",
        previewSrc: document.querySelector(".drawer .previewLarge img")?.getAttribute("src") || "",
        downloadReportDisabled: !![...document.querySelectorAll("button")].find((el) => (el.textContent || "").includes("Download report"))?.disabled,
      };
    },
    clickText(text) {
      const match = [...document.querySelectorAll("button,a,article.asset")].find((el) => (el.textContent || "").includes(text));
      if (!match) throw new Error("click target not found: " + text);
      match.click();
    },
    setContextScope(value) {
      const controls = [...document.querySelectorAll(".contextBar select")];
      setControl(controls[controls.length - 1], value);
    },
    setInput(selector, value) {
      const control = document.querySelector(selector);
      if (!control) throw new Error("input not found: " + selector);
      setControl(control, value);
    },
    setLabeled(panelText, labelText, value) {
      const panel = [...document.querySelectorAll(".panel.inlinePanel")].find((node) => (node.textContent || "").includes(panelText)) ||
        [...document.querySelectorAll(".managementPage")].find((node) => (node.textContent || "").includes(panelText));
      if (!panel) throw new Error("panel not found: " + panelText);
      const label = [...panel.querySelectorAll("label")].find((node) => (node.textContent || "").includes(labelText));
      if (!label) throw new Error("label not found: " + labelText + " in " + [...panel.querySelectorAll("label")].map((node) => (node.textContent || "").trim()).join(" | "));
      const control = label.querySelector("input,select,textarea");
      if (!control) throw new Error("control not found: " + labelText);
      setControl(control, value);
    },
    hasLabeled(panelText, labelText) {
      const panel = [...document.querySelectorAll(".panel.inlinePanel")].find((node) => (node.textContent || "").includes(panelText));
      return !!panel && [...panel.querySelectorAll("label")].some((node) => (node.textContent || "").includes(labelText));
    },
    setFileInput(name, content) {
      const input = document.querySelector('input[type="file"]');
      if (!input) throw new Error("file input not found");
      const dt = new DataTransfer();
      dt.items.add(new File([content], name, { type: "text/plain" }));
      input.files = dt.files;
      input.dispatchEvent(new Event("change", { bubbles: true }));
    },
    ownedHanLeaks() {
      const han = /[\\u3400-\\u9fff]/;
      const ignored = new Set(["简体中文"]);
      const leaks = [];
      for (const el of document.querySelectorAll("button,a.buttonLink,[title],[placeholder]")) {
        const values = [el.textContent || "", el.getAttribute("title") || "", el.getAttribute("placeholder") || ""];
        for (const value of values) {
          const text = value.trim();
          if (text && han.test(text) && !ignored.has(text)) leaks.push(text);
        }
      }
      return [...new Set(leaks)];
    },
    clickButton(text) {
      const button = [...document.querySelectorAll("button")].find((el) => (el.textContent || "").includes(text));
      if (!button) throw new Error("button not found: " + text);
      button.click();
    },
  };
})()
`;

async function waitFor(page, expression, timeout = 10000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (await page.eval(expression)) return;
    await wait(100);
  }
  throw new Error(`Timed out waiting for ${expression}`);
}

async function waitUntil(predicate, description, timeout = 10000) {
  const started = Date.now();
  while (Date.now() - started < timeout) {
    if (predicate()) return;
    await wait(100);
  }
  throw new Error(`Timed out waiting for ${description}`);
}

async function main() {
  assert.ok(fs.existsSync(path.join(dist, "index.html")), "Run npm run build --prefix frontend before this regression check.");
  const server = await startServer();
  const browser = await launchCdp();
  let page;
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    authMode = "unauth";
    page = await createPage(browser, `${base}/#assets`);
    await waitFor(page, `document.body.innerText.includes("Sign in")`);
    await page.eval(`document.querySelector('input[type="password"]').value = "secret-password"; document.querySelector('input[type="password"]').dispatchEvent(new Event("input", { bubbles: true })); document.querySelector("button").click()`);
    await waitFor(page, `document.body.innerText.includes("Invalid username or password")`);
    const loginStorage = await page.eval(`sessionStorage.getItem("seaweedfs-console.management-intents") || ""`);
    assert.equal(loginStorage, "", "failed login must not store password-derived management intent fingerprints");
    page.close();
    page = null;
    authMode = "auth";
    page = await createPage(browser, `${base}/#assets`);
    await page.eval(helpers);
    await waitFor(page, `document.querySelectorAll("article.asset").length > 0`);
    await page.eval(`window.__test.clickText("scope-a-photo.jpg")`);
    await waitFor(page, `!!document.querySelector(".drawer")`);
    const initial = await page.eval(`window.__test.collectAssets("initial")`);
    await page.eval(`window.__test.setContextScope("")`);
    await wait(450);
    const empty = await page.eval(`window.__test.collectAssets("empty-scope")`);
    await page.eval(`window.__test.setContextScope("scope-b")`);
    await wait(450);
    const scopeB = await page.eval(`window.__test.collectAssets("scope-b")`);
    assert.ok(initial.assetCards.includes("scope-a-photo.jpg"));
    assert.deepEqual(empty.assetCards, []);
    assert.equal(empty.drawerTitle, "");
    assert.equal(empty.downloadReportDisabled, true, "empty scope must keep export disabled");
    assert.deepEqual(scopeB.assetCards, []);
    assert.equal(scopeB.drawerTitle, "");
    assert.equal(scopeB.previewSrc, "");
    assert.ok(!scopeB.body.includes("old-job-a"), "old scope-a poll result must not leak into scope-b");

    await page.eval(`window.__test.setContextScope("scope-a")`);
    await waitFor(page, `document.querySelectorAll("article.asset").length > 0`);
    await page.eval(`window.__test.setInput("input.searchInput", "slow")`);
    await wait(50);
    await page.eval(`window.__test.setContextScope("scope-b")`);
    await wait(500);
    const afterRace = await page.eval(`window.__test.collectAssets("after-race")`);
    assert.deepEqual(afterRace.assetCards, []);
    assert.ok(!afterRace.body.includes("slow-stale-photo.jpg"), "slow stale assets response must not overwrite newer scope");

    await page.send("Page.navigate", { url: `${base}/#settings` });
    await waitFor(page, `document.body.innerText.includes("Access policy")`);
    await page.eval(helpers);
    await page.eval(`([...document.querySelectorAll("label")].find((node) => (node.textContent || "").includes("I acknowledge"))?.querySelector("input") || (() => { throw new Error("ack checkbox missing"); })()).click()`);
    await page.eval(`window.__test.clickButton("Save access policy")`);
    await waitUntil(() => requests.some((item) => item.method === "PUT" && item.pathname.endsWith("/management/connections/mgmt-1/access-policy")), "access policy save request");
    const policyWrites = requests.filter((item) => item.method === "PUT" && item.pathname.endsWith("/management/connections/mgmt-1/access-policy"));
    const accessPolicyWrite = policyWrites[policyWrites.length - 1];
    assert.equal(accessPolicyWrite.idempotencyKey, "", "local management access policy save must not carry durable Idempotency-Key");
    assert.equal(accessPolicyWrite.xIdempotencyKey, "", "local management access policy save must not carry compatibility idempotency header");
    assert.ok(!Object.prototype.hasOwnProperty.call(JSON.parse(accessPolicyWrite.body), "idempotency_key"), "local management access policy body must not carry durable idempotency_key");
    assert.equal(await page.eval(`sessionStorage.getItem("seaweedfs-console.management-intents") || ""`), "", "local management access policy save must not store durable intent keys");

    await page.send("Page.navigate", { url: `${base}/#services` });
    await waitFor(page, `document.body.innerText.includes("S3 Tables table details / data preview")`);
    await page.eval(helpers);
    await waitFor(page, `window.__test.hasLabeled("S3 Tables buckets", "Bucket ARN")`);
    await page.eval(`window.__test.setLabeled("S3 Tables buckets", "Bucket ARN", "arn:seaweed:s3tables:bucket-a")`);
    await waitFor(page, `window.__test.hasLabeled("S3 Tables namespaces", "Namespace")`);
    await page.eval(`window.__test.setLabeled("S3 Tables namespaces", "Namespace", "ns_a")`);
    await waitFor(page, `window.__test.hasLabeled("S3 Tables tables", "Table name")`);
    await page.eval(`window.__test.setLabeled("S3 Tables tables", "Table name", "table_a")`);
    await page.eval(`window.__test.setLabeled("S3 Tables table details / data preview", "Preview scope", "scope-a")`);
    await page.eval(`window.__test.clickButton("Read table details")`);
    await page.eval(`window.__test.setLabeled("S3 Tables tables", "Table name", "table_b")`);
    await wait(1300);
    let detailText = await page.eval(`document.body.innerText`);
    assert.ok(!detailText.includes("details-table_a-ns_a"), "stale table_a details must stay hidden after inputs change while request is in flight");
    assert.ok(!detailText.includes("details-table_b-ns_a"), "table_b details should not be fabricated before an explicit new details request");
    await page.eval(`window.__test.clickButton("Read table details")`);
    await wait(200);
    detailText = await page.eval(`document.body.innerText`);
    assert.ok(detailText.includes("details-table_b-ns_a"), "newer table_b details should render after explicit request");
    assert.ok(!detailText.includes("details-table_a-ns_a"), "stale table_a details must stay hidden after completion");

    await page.eval(`window.__test.setLabeled("S3 Tables tables", "Table name", "table_a")`);
    await page.eval(`window.__test.setLabeled("S3 Tables table details / data preview", "Preview scope", "scope-a")`);
    await page.eval(`window.__test.clickButton("Read data sample")`);
    await page.eval(`window.__test.setLabeled("S3 Tables tables", "Table name", "table_b")`);
    await page.eval(`window.__test.setLabeled("S3 Tables table details / data preview", "Preview scope", "scope-b")`);
    await page.eval(`window.__test.clickButton("Read data sample")`);
    await wait(200);
    const tableText = await page.eval(`document.querySelector(".panel.inlinePanel:nth-of-type(4)")?.textContent || document.body.innerText`);
    assert.ok(!tableText.includes("row-from-table_a-scope-a"));
    assert.ok(!tableText.includes("row-from-table-a-scope-a"));
    let tableAfterB = await page.eval(`document.body.innerText`);
    assert.ok(tableAfterB.includes("row-from-table_b-scope-b"), "newer fast table_b preview should render while table_a preview is still in flight");
    await wait(1100);
    tableAfterB = await page.eval(`document.body.innerText`);
    assert.ok(tableAfterB.includes("row-from-table_b-scope-b"), "slow older table_a preview must not replace newer table_b preview");
    assert.ok(!tableAfterB.includes("row-from-table_a-scope-a"), "stale table_a preview must stay hidden after completion");
    for (const [label, value] of [["Snapshot ID", "222"], ["File location", "s3://bucket-b/b/file.parquet"], ["Limit", "5"]]) {
      await page.eval(`window.__test.setLabeled("S3 Tables table details / data preview", ${JSON.stringify(label)}, ${JSON.stringify(value)})`);
      await wait(100);
      const changedText = await page.eval(`document.body.innerText`);
      assert.ok(!changedText.includes("row-from-table_b-scope-b"), `${label} change should clear stale table preview`);
      await page.eval(`window.__test.clickButton("Read data sample")`);
      await wait(150);
    }
    await page.eval(`window.__test.setLabeled("S3 Tables buckets", "Format", "lance")`);
    await wait(100);
    const lanceChangedText = await page.eval(`document.body.innerText`);
    assert.ok(!lanceChangedText.includes("row-from-table_b-scope-b"), "format change should clear stale table preview");

    async function prepareServicesPage() {
      await page.send("Page.navigate", { url: `${base}/#services` });
      await waitFor(page, `document.body.innerText.includes("MQ Topics")`);
      await page.eval(helpers);
      await waitFor(page, `window.__test.hasLabeled("MQ Topics", "Topic")`);
    }
    async function submitTopic(name) {
      await page.eval(`window.__test.setLabeled("MQ Topics", "Topic", ${JSON.stringify(name)})`);
      const before = writeByTopicName(name).length;
      await page.eval(`window.__test.clickButton("Create topic")`);
      await waitUntil(() => writeByTopicName(name).length > before, `topic write ${name}`);
      const writes = writeByTopicName(name);
      return writes[writes.length - 1];
    }

    const anomalousReceiptKeys = {};
    const terminalReceiptKeys = {};
    for (const name of ["receipt-html", "receipt-truncated-json", "receipt-empty", "receipt-null", "receipt-empty-object", "receipt-unknown-status", "receipt-status-array", "receipt-conflicting-states", "receipt-blank-operation", "receipt-missing-operation", "receipt-missing-replayed"]) {
      const first = await submitTopic(name);
      await waitFor(page, `document.body.innerText.includes("Management write receipt is uncertain")`);
      const second = await submitTopic(name);
      await waitFor(page, `document.body.innerText.includes("Management write receipt is uncertain")`);
      assert.equal(second.idempotencyKey, first.idempotencyKey, `${name} same-page retry should retain the same key after an uncertain receipt`);
      await prepareServicesPage();
      const afterReload = await submitTopic(name);
      await waitFor(page, `document.body.innerText.includes("Management write receipt is uncertain")`);
      assert.equal(afterReload.idempotencyKey, first.idempotencyKey, `${name} reload retry should retain the same key after an uncertain receipt`);
      anomalousReceiptKeys[name] = [first.idempotencyKey, second.idempotencyKey, afterReload.idempotencyKey];
    }

    const confirmedFirst = await submitTopic("receipt-confirmed");
    await waitFor(page, `document.body.innerText.includes("op-receipt-confirmed")`);
    const confirmedSecond = await submitTopic("receipt-confirmed");
    await waitUntil(() => writeByTopicName("receipt-confirmed").length >= 2, "confirmed retry write");
    assert.notEqual(confirmedSecond.idempotencyKey, confirmedFirst.idempotencyKey, "confirmed terminal receipts should clear the retained key for the next explicit attempt");
    terminalReceiptKeys.confirmed = [confirmedFirst.idempotencyKey, confirmedSecond.idempotencyKey];
    const probeFirst = await submitTopic("receipt-probe-confirmed");
    await waitFor(page, `document.body.innerText.includes("op-receipt-probe-confirmed")`);
    const probeSecond = await submitTopic("receipt-probe-confirmed");
    await waitUntil(() => writeByTopicName("receipt-probe-confirmed").length >= 2, "probe retry write");
    assert.notEqual(probeSecond.idempotencyKey, probeFirst.idempotencyKey, "probe confirmed receipts with matching status and journal_status should clear retained keys");
    terminalReceiptKeys.probeConfirmed = [probeFirst.idempotencyKey, probeSecond.idempotencyKey];
    const failedFirst = await submitTopic("receipt-failed");
    await waitFor(page, `document.body.innerText.includes("MOCK_FAILED")`);
    const failedSecond = await submitTopic("receipt-failed");
    await waitUntil(() => writeByTopicName("receipt-failed").length >= 2, "failed retry write");
    assert.notEqual(failedSecond.idempotencyKey, failedFirst.idempotencyKey, "failed terminal receipts should clear the retained key for the next explicit attempt");
    terminalReceiptKeys.failed = [failedFirst.idempotencyKey, failedSecond.idempotencyKey];

    await page.eval(`window.__test.setLabeled("MQ Topics", "Topic", "demo-topic")`);
    await page.eval(`window.__test.clickButton("Create topic")`);
    await wait(150);
    await page.eval(`window.__test.clickButton("Create topic")`);
    await waitFor(page, `document.body.innerText.includes("Operation returned needs_review")`);
    const firstWrites = writeByTopicName("demo-topic");
    assert.equal(firstWrites.length, 1, "pending gate should prevent duplicate in-flight write dispatch");
    await prepareServicesPage();
    await page.eval(`window.__test.setLabeled("MQ Topics", "Topic", "demo-topic")`);
    await page.eval(`window.__test.clickButton("Create topic")`);
    await waitUntil(() => writeByTopicName("demo-topic").length >= 2, "demo-topic retry write");
    const allWrites = writeByTopicName("demo-topic");
    assert.equal(allWrites.length, 2, "retry after needs_review should dispatch for readback/replay");
    assert.ok(allWrites[0].idempotencyKey, "write should include Idempotency-Key");
    assert.equal(allWrites[0].xIdempotencyKey, allWrites[0].idempotencyKey, "write should include X-Idempotency-Key");
    assert.equal(allWrites[0].idempotencyKey, allWrites[1].idempotencyKey, "same payload after needs_review should reuse the same Idempotency-Key");
    assert.equal(JSON.parse(allWrites[0].body).idempotency_key, allWrites[0].idempotencyKey, "native JSON write body should carry idempotency_key");
    assert.equal(JSON.parse(allWrites[1].body).idempotency_key, allWrites[1].idempotencyKey, "native JSON retry body should carry the same idempotency_key");

    await page.send("Page.navigate", { url: `${base}/#buckets` });
    await waitFor(page, `document.body.innerText.includes("Bucket CRUD / guarded writes")`);
    await page.eval(helpers);
    await waitFor(page, `window.__test.hasLabeled("Bucket CRUD / guarded writes", "Bucket")`);
    await page.eval(`window.__test.setLabeled("Bucket CRUD / guarded writes", "Bucket", "review-created"); window.__test.clickButton("Create bucket")`);
    await waitUntil(() => requests.some((item) => item.method === "POST" && item.pathname.endsWith("/management/mgmt-1/buckets")), "bucket create request");
    const bucketCreates = requests.filter((item) => item.method === "POST" && item.pathname.endsWith("/management/mgmt-1/buckets"));
    assert.equal(bucketCreates.length, 1, "bucket create should dispatch exactly once");
    assert.ok(bucketCreates[0].idempotencyKey, "bucket create should include Idempotency-Key header");
    assert.equal(bucketCreates[0].xIdempotencyKey, bucketCreates[0].idempotencyKey, "bucket create should include matching X-Idempotency-Key header");
    assert.ok(!Object.prototype.hasOwnProperty.call(JSON.parse(bucketCreates[0].body), "idempotency_key"), "bucket create body must not include idempotency_key for strict resource schemas");

    for (const route of ["#storage", "#files", "#objects", "#topology"]) {
      await page.send("Page.navigate", { url: `${base}/${route}` });
      await wait(500);
      await page.eval(helpers);
      const leaks = await page.eval(`window.__test.ownedHanLeaks()`);
      assert.deepEqual(leaks, [], `${route} owned dynamic copy should not leak Han in English locale`);
    }

    await page.send("Page.navigate", { url: `${base}/#files` });
    await waitFor(page, `document.body.innerText.includes("File write guard") || document.body.innerText.includes("文件写入守卫")`);
    await page.eval(helpers);
    await waitFor(page, `window.__test.hasLabeled("File write guard", "New folder name") || window.__test.hasLabeled("文件写入守卫", "新目录名")`);
    await page.eval(`window.__test.setFileInput("upload.txt", "hello"); window.__test.clickButton("Upload to current path")`);
    await wait(400);
    const uploads = requests.filter((item) => item.method === "POST" && item.pathname.endsWith("/files/upload"));
    assert.ok(uploads[0]?.idempotencyKey, "upload should include Idempotency-Key header");
    assert.ok(new URLSearchParams(uploads[0].search).get("idempotency_key"), "upload should include query idempotency_key");
    assert.equal(new URLSearchParams(uploads[0].search).get("idempotency_key"), uploads[0].idempotencyKey, "upload query key should match header key");

    console.log(JSON.stringify({ loginStorageClear: loginStorage === "", accessPolicyLocalNoDurableIntent: !accessPolicyWrite.idempotencyKey && !accessPolicyWrite.xIdempotencyKey && !Object.prototype.hasOwnProperty.call(JSON.parse(accessPolicyWrite.body), "idempotency_key"), assets: { initial, empty, scopeB, afterRace }, tablePreviewCleared: true, anomalousReceiptKeys, terminalReceiptKeys, idempotencyKeys: allWrites.map((item) => item.idempotencyKey), bucketCreateHeaderOnly: true, uploadKey: uploads[0].idempotencyKey, requestCount: requests.length }, null, 2));
  } finally {
    if (page) page.close();
    browser.proc.kill();
    await new Promise((resolve) => browser.proc.once("exit", resolve));
    try {
      const resolvedProfile = path.resolve(browser.profile);
      const resolvedTmp = path.resolve(os.tmpdir());
      const basename = path.basename(resolvedProfile);
      if (!resolvedProfile.startsWith(`${resolvedTmp}${path.sep}`) || !basename.startsWith("seaweedfs-console-cdp-")) {
        throw new Error(`Refusing to remove unexpected CDP profile path: ${resolvedProfile}`);
      }
      fs.rmSync(resolvedProfile, { recursive: true, force: true });
    } catch (error) {
      console.warn(`WARN cleanup skipped: ${String(error.message || error)}`);
    }
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
