// Dependency-free, loopback-only visual check of the built UI. No real service is used.
// Run: node scripts/verify_ui_layout.cjs [--baseline]
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const os = require("node:os");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { createHash } = require("node:crypto");

const root = path.resolve(__dirname, "..");
const dist = path.resolve(process.env.SWC_LAYOUT_DIST || path.join(root, "frontend", "dist"));
const baseline = process.argv.includes("--baseline");
assert.ok(process.argv.slice(2).every((arg) => arg === "--baseline"), "Only --baseline is supported.");
const output = path.join(root, "output", "ui-layout-2026-10-07", baseline ? "before" : "after");
const requests = [];
const unknownRoutes = [];
const writes = [];
const cases = [];
const failures = [];
let buildHashes = [];
const project = { id: "project-1", project_key: "archive", display_name: "Archive Images" };
const scope = { id: "scope-archive", project_id: "project-1", connection_id: "s3-1", display_name: "archive", bucket: "archive", prefix: "", allow_preview: true, allow_original_download: true, writable: false, manage_bucket: false };
const connection = { id: "mgmt-1", name: "SeaweedFS Admin", admin_url: "http://mock-admin.invalid", s3_connection_id: "s3-1", protocol_baseline: "4.48", management_write_enabled: false, permissions: { "bucket.manage": false, "file.manage": false, "object.manage": false } };
const stamp = "2026-10-07T03:00:00Z";
const assets = ["coast.jpg", "forest.jpg", "city.jpg", "studio.jpg"].map((name, index) => ({ id: `object-${index}`, asset_id: `asset-${index}`, scope_id: scope.id, bucket: "archive", key: `photos/${name}`, key_display: name, revision: `revision-${index}`, version_id: `version-${index}`, size_bytes: 512000 + index * 78000, content_type: "image/jpeg", properties: { format: "jpeg", width: 1600, height: 1200, decode_status: "valid" }, preview_state: "ready", reference_status: "unknown", last_modified: stamp }));
const previews = ["#789388", "#779082", "#748896", "#aa9583"].map((color, index) => `<svg xmlns="http://www.w3.org/2000/svg" width="480" height="360"><rect width="480" height="360" fill="${color}"/><path d="M0 300L120 150L240 270L340 110L480 280V360H0Z" fill="#ffffff" opacity=".3"/><circle cx="370" cy="75" r="35" fill="#ffffff" opacity=".45"/><text x="22" y="330" fill="#fff" font-family="sans-serif" font-size="24">${assets[index].key_display}</text></svg>`);
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const json = (res, value, status = 200) => { res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" }); res.end(JSON.stringify(value)); };
const envelope = { connection_id: connection.id, source: "mocked official DTO", checked_at: stamp, protocol_baseline: "4.48" };

async function routeApi(req, res, url) {
  const pathname = url.pathname;
  const method = req.method || "GET";
  requests.push({ method, pathname, search: url.search });
  if (method !== "GET") {
    writes.push({ method, pathname });
    return json(res, { error: { code: "MOCK_READ_ONLY", message: "This visual fixture rejects all writes." } }, 405);
  }
  if (pathname === "/api/v1/auth/me") return json(res, { user: { username: "layout-reviewer" }, csrf_token: "mock-csrf", projects: [project] });
  if (pathname === "/api/v1/connections") return json(res, { items: [{ id: "s3-1", display_name: "Archive S3", endpoint_url: "http://mock-s3.invalid", secret_ref: "mock-reference" }] });
  if (pathname === "/api/v1/management/connections") return json(res, { items: [connection] });
  if (pathname === "/api/v1/projects/project-1/scopes") return json(res, { items: [scope] });
  if (pathname === "/api/v1/management/mgmt-1/buckets") return json(res, { ...envelope, items: [{ name: "archive", owner: "archive-owner", logical_size: 47513600, physical_size: 95027200, versioning_status: "Suspended", lifecycle_rule_count: 1, policy_statement_count: 1 }, { name: "thumbnails", owner: "image-worker", logical_size: 12582912, physical_size: 25165824, versioning_status: "Enabled", lifecycle_rule_count: 0, policy_statement_count: 0 }] });
  if (pathname === "/api/v1/management/mgmt-1/buckets/archive") return json(res, { ...envelope, name: "archive", owner: "archive-owner" });
  if (pathname === "/api/v1/management/mgmt-1/buckets/archive/versioning") return json(res, { ...envelope, status: "Suspended" });
  if (pathname === "/api/v1/management/mgmt-1/buckets/archive/object-lock") return json(res, { ...envelope, object_lock_configuration: { ObjectLockEnabled: "Enabled" } });
  if (pathname === "/api/v1/management/mgmt-1/buckets/archive/lifecycle") return json(res, { Rules: [{ ID: "retain-originals", Status: "Enabled", Filter: { Prefix: "photos/" } }] });
  if (pathname === "/api/v1/management/mgmt-1/buckets/archive/policy") return json(res, { Version: "2012-10-17", Statement: [{ Effect: "Allow", Action: "s3:GetObject", Resource: "arn:aws:s3:::archive/photos/*" }] });
  if (pathname === "/api/v1/management/mgmt-1/buckets/thumbnails") return json(res, { ...envelope, name: "thumbnails", owner: "image-worker" });
  if (pathname === "/api/v1/management/mgmt-1/buckets/thumbnails/versioning") return json(res, { ...envelope, status: "Enabled" });
  if (pathname === "/api/v1/management/mgmt-1/buckets/thumbnails/object-lock") return json(res, { ...envelope, object_lock_configuration: { ObjectLockEnabled: "Enabled" } });
  if (pathname === "/api/v1/management/mgmt-1/buckets/thumbnails/lifecycle") return json(res, { Rules: [] });
  if (pathname === "/api/v1/management/mgmt-1/buckets/thumbnails/policy") return json(res, { Version: "2012-10-17", Statement: [] });
  if (pathname === "/api/v1/management/mgmt-1/files") return json(res, { ...envelope, items: [{ name: "photos", full_path: "/photos", is_directory: true, protected: false, size: null, mtime: stamp }, { name: "coast.jpg", full_path: "/coast.jpg", is_directory: false, protected: false, size: 512000, mtime: stamp }, { name: "catalog.json", full_path: "/catalog.json", is_directory: false, protected: false, size: 3200, mtime: stamp }, { name: "etc", full_path: "/etc", is_directory: true, protected: true, size: null, mtime: stamp }], next_cursor: url.searchParams.has("cursor") ? "" : "next-files-page" });
  if (pathname === "/api/v1/management/mgmt-1/objects") return json(res, { ...envelope, items: assets.slice(0, 2).map((asset) => ({ key: asset.key, size: asset.size_bytes, etag: `etag-${asset.id}`, version_id: asset.version_id })), folders: ["photos/"], next_cursor: "next-objects-page", total: 4 });
  if (pathname === "/api/v1/management/mgmt-1/overview") return json(res, { ...envelope, admin: { master_nodes: [{ address: "master:9333" }], volume_servers: [{ address: "volume:8080", disk_capacity: 107374182400 }], filer_nodes: [{ address: "filer:8888" }], s3_nodes: [{ address: "s3:8333" }], total_size: 1048576000, total_chunks: 300, total_volumes: 8, tier_stats: [{ disk_type: "hdd", total_size: 1048576000, disk_capacity: 107374182400 }] }, config: { config_info: { replication: "001" } } });
  if (pathname === "/api/v1/management/mgmt-1/services") return json(res, { ...envelope, master_nodes: [{ address: "master:9333", is_leader: true }], filer_nodes: [{ address: "filer:8888" }], s3_nodes: [{ address: "s3:8333" }], volume_servers: [{ address: "volume:8080", datacenter: "dc1", rack: "rack1" }], plugin: { data: { worker_count: 1 } } });
  if (pathname === "/api/v1/management/mgmt-1/volumes") return json(res, { ...envelope, items: [{ id: 7, collection: "archive", server: "volume:8080", disk_type: "hdd", size: 1048576000, file_count: 300, read_only: false }], counts: { logical_volumes: 8 } });
  if (pathname === "/api/v1/management/mgmt-1/operations") return json(res, { items: [] });
  if (pathname === "/api/v1/scopes/scope-archive/assets") return json(res, { items: assets, next_cursor: "next-assets-page" });
  if (pathname === "/api/v1/jobs") return json(res, { items: [{ id: "scan-1", scope_id: scope.id, kind: "scan", state: "succeeded", processed: 4, errors: 0, total: 4, updated_at: stamp }] });
  const preview = /^\/api\/v1\/scopes\/scope-archive\/objects\/object-(\d)\/preview$/.exec(pathname);
  if (preview) { res.writeHead(200, { "Content-Type": "image/svg+xml", "Cache-Control": "no-store" }); return res.end(previews[Number(preview[1])]); }
  unknownRoutes.push({ method, pathname });
  return json(res, { error: { code: "MOCK_NOT_FOUND", message: `${method} ${pathname}` } }, 404);
}

async function startServer() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url || "/", "http://127.0.0.1");
    if (url.pathname.startsWith("/api/v1/")) { routeApi(req, res, url).catch((error) => json(res, { error: { code: "MOCK_ERROR", message: error.message } }, 500)); return; }
    if (url.pathname === "/favicon.ico") { res.writeHead(204); return res.end(); }
    const relative = url.pathname === "/" ? "index.html" : decodeURIComponent(url.pathname.slice(1));
    let full = path.resolve(dist, relative);
    if (!full.startsWith(`${dist}${path.sep}`) || !fs.existsSync(full) || fs.statSync(full).isDirectory()) full = path.join(dist, "index.html");
    const types = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml" };
    res.writeHead(200, { "Content-Type": types[path.extname(full)] || "application/octet-stream", "Cache-Control": "no-store" });
    fs.createReadStream(full).pipe(res);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return server;
}

function browserPath() {
  return [process.env.CHROME_PATH, "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe", "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe"].filter(Boolean).find((candidate) => fs.existsSync(candidate));
}
async function freePort() {
  const server = net.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}
async function launchBrowser() {
  const exe = browserPath();
  assert.ok(exe, "Chrome/Edge unavailable; set CHROME_PATH.");
  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "seaweedfs-console-layout-"));
  const proc = spawn(exe, [`--remote-debugging-port=${port}`, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", `--user-data-dir=${profile}`, "about:blank"], { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] });
  const browser = { proc, port, profile, exe };
  let stderr = "";
  proc.stderr.on("data", (chunk) => { stderr = `${stderr}${chunk}`.slice(-2000); });
  try {
    for (let attempt = 0; attempt < 100; attempt++) {
      assert.equal(proc.exitCode, null, `Browser exited: ${stderr}`);
      try {
        const response = await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(1000) });
        browser.endpoint = (await response.json()).webSocketDebuggerUrl;
        return browser;
      } catch { await wait(100); }
    }
    throw new Error(`Browser startup timed out: ${stderr}`);
  } catch (error) { await stopBrowser(browser); throw error; }
}
async function stopBrowser(browser) {
  if (!browser) return;
  if (browser.proc.exitCode === null && browser.proc.signalCode === null) browser.proc.kill();
  for (let attempt = 0; attempt < 40 && browser.proc.exitCode === null && browser.proc.signalCode === null; attempt++) await wait(100);
  const target = path.resolve(browser.profile);
  const tmp = path.resolve(os.tmpdir());
  assert.ok(target.startsWith(`${tmp}${path.sep}`) && path.basename(target).startsWith("seaweedfs-console-layout-"), "Unexpected browser profile cleanup path.");
  try { fs.rmSync(target, { recursive: true, force: true }); } catch (error) { console.warn(`Profile cleanup: ${error.message}`); }
}

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (!message.id) { this.events.push(message); return; }
      const entry = this.pending.get(message.id);
      if (!entry) return;
      clearTimeout(entry.timeout);
      this.pending.delete(message.id);
      if (message.error) entry.reject(new Error(JSON.stringify(message.error))); else entry.resolve(message.result);
    };
  }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
    return new Cdp(ws);
  }
  send(method, params = {}) {
    const id = this.nextId++;
    const promise = new Promise((resolve, reject) => { const timeout = setTimeout(() => { this.pending.delete(id); reject(new Error(`${method} timed out`)); }, 15000); this.pending.set(id, { resolve, reject, timeout }); });
    this.ws.send(JSON.stringify({ id, method, params }));
    return promise;
  }
  async eval(expression) {
    const result = await this.send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true, userGesture: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  }
  close() { this.ws.close(); }
}
async function createPage(browser) {
  const browserCdp = await Cdp.connect(browser.endpoint);
  const target = await browserCdp.send("Target.createTarget", { url: "about:blank" });
  browserCdp.close();
  const targets = await (await fetch(`http://127.0.0.1:${browser.port}/json/list`)).json();
  const page = await Cdp.connect(targets.find((item) => item.id === target.targetId).webSocketDebuggerUrl);
  page.targetId = target.targetId;
  await page.send("Page.enable");
  await page.send("Runtime.enable");
  await page.send("Log.enable");
  return page;
}
async function until(page, expression, label) {
  for (let attempt = 0; attempt < 100; attempt++) { if (await page.eval(expression)) return; await wait(80); }
  throw new Error(`Timed out waiting for ${label}`);
}
async function screenshot(page, filename) {
  await page.eval("new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
  await wait(50);
  const result = await page.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
  fs.writeFileSync(path.join(output, filename), Buffer.from(result.data, "base64"));
  return filename;
}
function check(condition, label, result) { if (!condition) { result.failures.push(label); failures.push(`${result.route}/${result.locale}/${result.width}: ${label}`); } }

const inspectLayout = `(() => {
  const visible = (element) => element.getBoundingClientRect().width > 0 && element.getBoundingClientRect().height > 0 && !element.closest('details:not([open]) > :not(summary)');
  const gap = (element) => {
    const style = getComputedStyle(element);
    const children = [...element.children].filter(visible);
    const verticalGaps = children.slice(1).flatMap((child, index) => {
      const previous = children[index].getBoundingClientRect(), current = child.getBoundingClientRect();
      return current.top >= previous.bottom - 1 ? [Math.round((current.top - previous.bottom) * 100) / 100] : [];
    });
    return { selector: element.className, rowGap: parseFloat(style.rowGap) || 0, columnGap: parseFloat(style.columnGap) || 0, children: element.children.length, verticalGaps };
  };
  const labelGaps = [...document.querySelectorAll('label.field')].filter(visible).map((label) => {
    const control = label.querySelector('input,select,textarea');
    const text = [...label.childNodes].find((node) => node.nodeType === 3 && node.textContent.trim());
    if (!control || !text) return null;
    const range = document.createRange(); range.selectNodeContents(text);
    return { label: text.textContent.trim(), gap: Math.round((control.getBoundingClientRect().top - range.getBoundingClientRect().bottom) * 100) / 100 };
  }).filter(Boolean);
  const pagination = [...document.querySelectorAll('.paginationBar')].filter(visible).map((bar) => {
    const previous = bar.previousElementSibling;
    return { ...gap(bar), tableGap: previous ? Math.round((bar.getBoundingClientRect().top - previous.getBoundingClientRect().bottom) * 100) / 100 : null, buttons: [...bar.querySelectorAll('button')].map((button) => ({ text: button.textContent.trim(), disabled: button.disabled })) };
  });
  const offenders = [...document.querySelectorAll('body *')].filter(visible).filter((element) => element.getBoundingClientRect().right > document.documentElement.clientWidth + 2 && !element.closest('.tableWrap, .tableScroll, .tableFrame, .dataTableWrap')).slice(0, 12).map((element) => ({ tag: element.tagName, class: element.className, right: Math.round(element.getBoundingClientRect().right) }));
  return { documentWidth: document.documentElement.scrollWidth, viewportWidth: innerWidth, documentClientWidth: document.documentElement.clientWidth, horizontalOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 2, offenders, panelContent: [...document.querySelectorAll('.panelContent')].filter(visible).map(gap), formSections: [...document.querySelectorAll('.formSection')].filter(visible).map(gap), contextFacts: [...document.querySelectorAll('.contextFacts')].filter(visible).map(gap), labelGaps, pagination, headings: [...document.querySelectorAll('main h1,main h2,main h3')].filter(visible).map((element) => ({ level: element.tagName, text: element.textContent.trim() })), evidence: [...document.querySelectorAll('details')].filter((details) => /DTO|evidence|证据|来源/.test(details.querySelector('summary')?.textContent || '')).map((details) => ({ title: details.querySelector('summary').textContent.trim(), open: details.open, dataAvailable: !!details.querySelector('pre')?.textContent.trim() })), allButtons: [...document.querySelectorAll('main button')].map((button) => ({ text: button.textContent.trim(), disabled: button.disabled })) };
})()`;

async function runCase(browser, baseUrl, route, locale, width) {
  const page = await createPage(browser);
  const result = { route, locale, width, height: 900, screenshots: [], failures: [] };
  cases.push(result);
  try {
    await page.send("Emulation.setDeviceMetricsOverride", { width, height: 900, deviceScaleFactor: 1, mobile: false });
    await page.send("Page.addScriptToEvaluateOnNewDocument", { source: `localStorage.setItem('seaweedfs-console.locale', ${JSON.stringify(locale)});` });
    await page.send("Page.navigate", { url: `${baseUrl}/#${route}` });
    await until(page, "!!document.querySelector('.shell') && !!document.querySelector('main')", "authenticated UI");
    if (route === "buckets") {
      await until(page, "[...document.querySelectorAll('button.linkButton')].some((node) => node.textContent === 'archive')", "bucket inventory");
      await page.eval("[...document.querySelectorAll('button.linkButton')].find((node) => node.textContent === 'archive').click()");
      await until(page, "!![...document.querySelectorAll('main')].find((node) => /ObjectLockEnabled/.test(node.textContent))", "bucket settings");
    }
    if (route === "files") await until(page, "document.body.textContent.includes('catalog.json')", "populated files");
    if (route === "objects") await until(page, "document.body.textContent.includes('photos/coast.jpg')", "populated objects");
    if (route === "assets") await until(page, "document.querySelectorAll('article.asset').length === 4", "populated assets");
    if (route === "dashboard") await until(page, "!!document.querySelector('.managementMetrics') && document.body.textContent.includes('300')", "dashboard metrics");
    await wait(200);
    await page.eval("document.fonts.ready");
    result.layout = await page.eval(inspectLayout);
    result.errors = page.events.filter((event) => event.method === "Runtime.exceptionThrown" || (event.method === "Log.entryAdded" && event.params.entry.level === "error")).map((event) => event.params.exceptionDetails?.exception?.description || event.params.entry?.text);
    check(result.errors.length === 0, `browser errors: ${result.errors.join(' | ')}`, result);
    if (!baseline) {
      check(!result.layout.horizontalOverflow, `horizontal overflow ${result.layout.documentWidth}px > available ${result.layout.documentClientWidth}px`, result);
      if (route !== "assets") check(result.layout.panelContent.length > 0, "missing spaced panelContent", result);
      for (const panel of result.layout.panelContent) check(panel.rowGap >= 12, `panelContent row gap ${panel.rowGap}px < 12px`, result);
      for (const section of result.layout.formSections) {
        check(section.rowGap >= 10, `formSection row gap ${section.rowGap}px < 10px`, result);
        check(section.verticalGaps.every((gap) => gap >= 8), `formSection rendered separation ${section.verticalGaps.join(', ')}px < 8px`, result);
      }
      for (const label of result.layout.labelGaps) check(label.gap >= 4, `${label.label}: label/control gap ${label.gap}px < 4px`, result);
      if (route === "buckets" || route === "files") check(result.layout.formSections.length >= 2, "missing distinct form sections", result);
      if (route === "files" || route === "objects") {
        check(result.layout.pagination.length > 0, "missing paginationBar", result);
        for (const bar of result.layout.pagination) { check(bar.rowGap >= 8 || bar.columnGap >= 8, "pagination elements have no gap", result); check(bar.tableGap >= 12, `table/pagination gap ${bar.tableGap}px < 12px`, result); }
      }
      if (route === "objects") { check(result.layout.contextFacts.length > 0, "missing contextFacts", result); for (const facts of result.layout.contextFacts) check(facts.rowGap >= 8 || facts.columnGap >= 8, "scope facts have no gap", result); }
    }
    const writeLabels = /^(Save versioning|保存 versioning|保存版本设置|Save object lock|保存 object lock|保存对象锁|保存对象锁配置|Create directory|创建目录|Upload to current path|上传到当前路径|Rename\s*\/\s*move|重命名\/移动|Confirm delete|确认删除|Delete empty Bucket|删除空 Bucket)$/i;
    const writeButtons = result.layout.allButtons.filter((button) => writeLabels.test(button.text));
    if (route === "buckets" || route === "files") { check(writeButtons.length >= 2, "expected readonly write controls absent", result); check(writeButtons.every((button) => button.disabled), "read-only write control enabled", result); }
    if (route === "buckets" || route === "files" || route === "objects") { check(result.layout.evidence.length > 0, "raw evidence unavailable", result); check(result.layout.evidence.every((entry) => !entry.open && entry.dataAvailable), "raw evidence should be collapsed but available", result); }
    result.screenshots.push(await screenshot(page, `${route}-${locale}-${width}-overview.png`));
    if (route === "buckets") {
      const scrolled = await page.eval(`(() => { const target = [...document.querySelectorAll('main h2,main h3')].find((element) => ['Versioning / Object Lock', '版本控制 / Object Lock', '版本控制与对象锁'].includes(element.textContent.trim())) || [...document.querySelectorAll('main label')].find((element) => /Object lock JSON|Object lock JSON（对象锁配置）/.test(element.textContent)); if (!target) return false; (target.closest('.panel') || target).scrollIntoView({block:'start'}); return true; })()`);
      check(scrolled, "versioning/lock editor could not be focused", result);
      result.screenshots.push(await screenshot(page, `${route}-${locale}-${width}-settings.png`));
    }
    if (route === "files") {
      await page.eval("(document.querySelector('.paginationBar') || [...document.querySelectorAll('.toolbar')].find((element) => /File cursor|文件游标/.test(element.textContent)))?.scrollIntoView({block:'center'})");
      result.screenshots.push(await screenshot(page, `${route}-${locale}-${width}-pagination.png`));
    }
    if (route === "objects") {
      await page.eval("(document.querySelector('.contextFacts')?.closest('.panel') || [...document.querySelectorAll('main h2')].find((element) => /授权范围|Authorized scope/.test(element.textContent))?.closest('.panel'))?.scrollIntoView({block:'start'})");
      result.screenshots.push(await screenshot(page, `${route}-${locale}-${width}-scope.png`));
    }
    if (route === "files") {
      // Fill valid local values after screenshots so disabled controls prove the
      // read-only authorization gate, rather than merely an empty-input guard.
      await page.eval(`(async () => {
        const values = [['New folder name', '新目录名', 'layout-fixture'], ['Source path', '源路径', '/coast.jpg'], ['Target path', '目标路径', '/coast-copy.jpg']];
        for (const [en, zh, value] of values) {
          const label = [...document.querySelectorAll('main label.field')].find((node) => [en, zh].includes(node.textContent.trim()));
          const input = label?.querySelector('input');
          if (!input) throw new Error('Fixture input absent: ' + en);
          Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value);
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
          await new Promise((resolve) => setTimeout(resolve, 0));
        }
      })()`);
      await wait(50);
      result.readOnlyProbe = await page.eval(`([...document.querySelectorAll('main button')].filter((button) => /^(Create directory|创建目录|Rename\\s*\\/\\s*move|重命名\\/移动)$/.test(button.textContent.trim())).map((button) => ({text:button.textContent.trim(), disabled:button.disabled})))`);
      check(result.readOnlyProbe.length === 2 && result.readOnlyProbe.every((button) => button.disabled), "populated file write controls bypassed read-only gate", result);
    }
    if (!baseline && route === "buckets" && locale === "en" && width === 1280) {
      await page.eval(`(() => {
        window.layoutDisclosure = (title) => [...document.querySelectorAll('details.actionDisclosure')].find((node) => node.querySelector('summary').textContent.trim().startsWith(title));
        window.layoutField = (title, label) => [...layoutDisclosure(title).querySelectorAll('label.field')].find((node) => node.textContent.trim() === label)?.querySelector('input,select');
        window.layoutSetField = async (title, label, value) => {
          const control = layoutField(title, label);
          if (!control) throw new Error('Field absent: ' + title + '/' + label);
          Object.getOwnPropertyDescriptor(control instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype, 'value').set.call(control, value);
          control.dispatchEvent(new Event('input', {bubbles:true})); control.dispatchEvent(new Event('change', {bubbles:true}));
          await new Promise((resolve) => setTimeout(resolve, 0));
        };
        layoutDisclosure('Create a bucket').open = true;
        layoutDisclosure('Owner, quota and deletion').open = true;
        layoutDisclosure('Create a bucket').querySelector('label.ack input').click();
      })()`);
      await until(page, "!!layoutField('Create a bucket', 'Owner')", "advanced creation fields");
      await page.eval(`(async () => {
        await layoutSetField('Create a bucket', 'Bucket', 'layout-create-draft');
        await layoutSetField('Create a bucket', 'Owner', 'creation-owner');
        await layoutSetField('Create a bucket', 'Quota', '12');
        await layoutSetField('Owner, quota and deletion', 'Owner', 'selected-owner');
        await layoutSetField('Owner, quota and deletion', 'Quota', '34');
      })()`);
      const draftSnapshot = `(() => ({creation: {owner:layoutField('Create a bucket','Owner').value, quota:layoutField('Create a bucket','Quota').value}, selected: {owner:layoutField('Owner, quota and deletion','Owner').value, quota:layoutField('Owner, quota and deletion','Quota').value}, writesDisabled:[...layoutDisclosure('Create a bucket').querySelectorAll('.toolbar button'), ...layoutDisclosure('Owner, quota and deletion').querySelectorAll('.toolbar button')].every((button)=>button.disabled)}))()`;
      result.bucketDraftIsolation = { afterEditing: await page.eval(draftSnapshot) };
      check(JSON.stringify(result.bucketDraftIsolation.afterEditing.creation) === JSON.stringify({ owner: "creation-owner", quota: "12" }), "selected-bucket drafts altered advanced creation", result);
      check(JSON.stringify(result.bucketDraftIsolation.afterEditing.selected) === JSON.stringify({ owner: "selected-owner", quota: "34" }), "selected-bucket owner/quota drafts not independent", result);
      check(result.bucketDraftIsolation.afterEditing.writesDisabled, "populated bucket drafts bypassed read-only gate", result);
      await page.eval("[...document.querySelectorAll('button.linkButton')].find((node)=>node.textContent==='thumbnails').click()");
      await until(page, "layoutField('Owner, quota and deletion', 'Owner').value === '' && layoutField('Owner, quota and deletion', 'Quota').value === ''", "selected drafts reset on bucket switch");
      result.bucketDraftIsolation.afterSwitch = await page.eval(draftSnapshot);
      check(JSON.stringify(result.bucketDraftIsolation.afterSwitch.creation) === JSON.stringify({ owner: "creation-owner", quota: "12" }), "bucket selection cleared unrelated creation drafts", result);
      check(result.bucketDraftIsolation.afterSwitch.writesDisabled, "bucket switching bypassed read-only gate", result);
      await wait(150);
    }
  } finally {
    page.close();
    const browserCdp = await Cdp.connect(browser.endpoint);
    await browserCdp.send("Target.closeTarget", { targetId: page.targetId });
    browserCdp.close();
  }
  console.log(`${baseline ? "BASELINE" : result.failures.length ? "FAIL" : "PASS"} ${route} ${locale} ${width}x900`);
}

async function main() {
  assert.ok(fs.existsSync(path.join(dist, "index.html")), "Build frontend/dist first.");
  buildHashes = fs.readdirSync(path.join(dist, "assets")).filter((file) => /\.(js|css)$/.test(file)).sort().map((file) => ({ file: `assets/${file}`, sha256: createHash("sha256").update(fs.readFileSync(path.join(dist, "assets", file))).digest("hex") }));
  fs.mkdirSync(output, { recursive: true });
  let server;
  let browser;
  try {
    server = await startServer();
    browser = await launchBrowser();
    const baseUrl = `http://127.0.0.1:${server.address().port}`;
    for (const route of ["buckets", "files", "objects", "dashboard", "assets"]) {
      const widths = [1280, 595, 320];
      for (const width of widths) await runCase(browser, baseUrl, route, "zh-CN", width);
    }
    for (const route of ["buckets", "files", "objects", "dashboard", "assets"]) await runCase(browser, baseUrl, route, "en", 1280);
    assert.equal(writes.length, 0, "Visual check dispatched an unexpected write.");
    assert.equal(unknownRoutes.length, 0, `Mock routes missing: ${JSON.stringify(unknownRoutes)}`);
    fs.writeFileSync(path.join(output, "verification.json"), JSON.stringify({ mode: baseline ? "baseline" : "verification", fixture: "Fully mocked loopback service; no real backend, credentials or storage", browser: browser.exe, dist, buildHashes, passed: baseline ? null : failures.length === 0, cases, requestCount: requests.length, writes, unknownRoutes, failures }, null, 2));
    if (!baseline) assert.equal(failures.length, 0, failures.join("\n"));
    console.log(`UI_LAYOUT_${baseline ? "BASELINE_SAVED" : "PASS"} ${cases.length} cases; artifacts ${output}`);
  } catch (error) {
    fs.writeFileSync(path.join(output, "verification.json"), JSON.stringify({ mode: baseline ? "baseline" : "verification", passed: false, buildHashes, cases, writes, unknownRoutes, failures, error: error.stack }, null, 2));
    throw error;
  } finally {
    await stopBrowser(browser);
    if (server) { server.closeIdleConnections?.(); server.closeAllConnections?.(); await new Promise((resolve) => server.close(resolve)); }
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
