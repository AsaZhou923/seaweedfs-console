// Bucket/Filer handoffs use sandboxed Chromium and synthetic loopback services.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const dist = path.resolve(process.env.SWC_REGRESSION_DIST || path.join(__dirname, "../frontend/dist"));
const projects = [
  { id: "project-a", project_key: "a", display_name: "Project A" },
  { id: "project-b", project_key: "b", display_name: "Project B" },
];
const managers = [
  { id: "manager-a", name: "Read-only manager A", admin_url: "http://manager-a.invalid", s3_connection_id: "s3-a", management_write_enabled: false, permissions: {} },
  { id: "manager-b", name: "Read-only manager B", admin_url: "http://manager-b.invalid", s3_connection_id: "s3-b", management_write_enabled: false, permissions: {} },
  { id: "manager-unlinked", name: "Unlinked manager", admin_url: "http://unlinked.invalid", management_write_enabled: false, permissions: {} },
];
const scopeA = { id: "scope-a", project_id: "project-a", connection_id: "s3-a", display_name: "Scope A", bucket: "bucket-a", prefix: "a/", writable: false, allow_preview: true, allow_original_download: true };
const scopeB = { id: "scope-b", project_id: "project-b", connection_id: "s3-a", display_name: "Scope B", bucket: "bucket-b", prefix: "literal", writable: false, allow_preview: true, allow_original_download: true };
const scopeOther = { ...scopeB, id: "scope-other", connection_id: "s3-b", display_name: "Other connection scope" };
const trickyKey = "literal2026//子目录/space +%25?#.jpg ";
const canonicalFilerPath = "/buckets/bucket-b/literal2026/子目录/space +%25?#.jpg";
const filerName = trickyKey.slice(trickyKey.lastIndexOf("/") + 1);

function gate(match) {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { match, promise, release, entered: false, finished: false };
}

function json(res, body, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  res.end(JSON.stringify(body));
}

async function until(predicate, message) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  throw new Error(`Timed out waiting for ${message}`);
}

function objectLocation(bucket = "bucket-b", key, manager = "manager-a") {
  const query = new URLSearchParams({ management: manager, bucket });
  if (key !== undefined) query.set("key", key);
  return `objects?${query}`;
}

async function startMock(options = {}) {
  const state = {
    scopes: { "project-a": options.alternateScope ? [scopeA, { ...scopeB, id: "scope-alternate", project_id: "project-a", display_name: "Alternate scope" }] : [scopeA], "project-b": options.withScope ? [{ ...scopeB, allow_original_download: options.download !== false }, scopeOther] : [] },
    requests: [], gates: options.hold ? [gate(options.hold)] : [], failScopes: Boolean(options.failScopes), failObjects: Boolean(options.failObjects), empty: Boolean(options.empty),
    authenticated: options.authenticated !== false,
    objectIdentity: options.objectIdentity !== false,
    failCreate: Boolean(options.failCreate),
    failRefreshConnections: false,
  };
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, "http://127.0.0.1");
      if (!url.pathname.startsWith("/api/v1/")) {
        let file = path.resolve(dist, "." + decodeURIComponent(url.pathname));
        const relative = path.relative(dist, file);
        if (relative.startsWith("..") || path.isAbsolute(relative)) return json(res, {}, 404);
        if (!fs.existsSync(file) || !fs.statSync(file).isFile()) file = path.join(dist, "index.html");
        const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
        res.writeHead(200, { "Content-Type": types[path.extname(file)] || "application/octet-stream" });
        return res.end(fs.readFileSync(file));
      }
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = Buffer.concat(chunks).toString("utf8");
      const payload = body ? JSON.parse(body) : {};
      const request = { path: url.pathname.replace(/^\/api\/v1/, ""), method: req.method, query: Object.fromEntries(url.searchParams), payload };
      state.requests.push(request);
      let response = { items: [] };
      let status = 200;
      if (request.path === "/auth/login") {
        state.authenticated = true;
        response = { user: { username: "synthetic" }, csrf_token: "synthetic-csrf" };
      } else if (request.path === "/auth/me") {
        response = state.authenticated ? { user: { username: "synthetic" }, csrf_token: "synthetic-csrf", projects } : { error: { code: "UNAUTHENTICATED", message: "Login required" } };
        status = state.authenticated ? 200 : 401;
      } else if (request.path === "/connections") {
        response = state.failRefreshConnections ? { error: { code: "SYNTHETIC_REFRESH_FAILURE", message: "Settings refresh failed" } } : { items: ["a", "b"].map((id) => ({ id: `s3-${id}`, display_name: `S3 ${id}`, endpoint_url: `http://s3-${id}.invalid` })) };
        status = state.failRefreshConnections ? 503 : 200;
      } else if (request.path === "/management/connections") {
        response = { items: managers };
      } else if (/^\/projects\/[^/]+\/scopes$/.test(request.path)) {
        const projectId = request.path.split("/")[2];
        if (req.method === "POST") {
          if (state.failCreate) {
            response = { error: { code: "SYNTHETIC_CREATE_FAILURE", message: "Scope creation failed" } };
            status = 503;
          } else {
            const created = { ...scopeB, ...payload, ...payload.scope_policy, id: "scope-created", project_id: projectId };
            state.scopes[projectId].push(created);
            response = created;
          }
        } else {
          response = state.failScopes ? { error: { code: "SYNTHETIC_SCOPE_FAILURE", message: "Scope lookup failed" } } : { items: [...state.scopes[projectId]] };
          status = state.failScopes ? 503 : 200;
        }
      } else if (request.path.endsWith("/buckets")) {
        response = { items: [{ name: "bucket-a" }, { name: "bucket-b" }] };
      } else if (request.path.endsWith("/files")) {
        const entry = { name: filerName, full_path: canonicalFilerPath, is_directory: false, size: 12 };
        if (state.objectIdentity) entry.s3_object = { bucket: "bucket-b", key: trickyKey };
        response = { items: [entry], next_cursor: "" };
      } else if (request.path.endsWith("/objects/select")) {
        response = { object: { id: `object-${payload.scope_id}`, key: payload.key, scope_id: payload.scope_id, version_id: "version-1", size: 12 } };
      } else if (request.path.endsWith("/objects")) {
        response = state.failObjects ? { error: { code: "SYNTHETIC_OBJECT_FAILURE", message: "Object listing failed" } } : {
          items: state.empty ? [] : [{ key: request.query.scope_id === "scope-a" ? "a/photo.jpg" : trickyKey, version_id: "version-1", size: 12 }], folders: [], next_cursor: "",
        };
        status = state.failObjects ? 503 : 200;
      }
      const pending = state.gates.find((item) => item.match(request));
      if (pending) {
        pending.entered = true;
        await pending.promise;
      }
      json(res, response, status);
      if (pending) pending.finished = true;
    } catch (error) {
      json(res, { error: { code: "FIXTURE_ERROR", message: String(error) } }, 500);
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { state, server, base: `http://127.0.0.1:${server.address().port}` };
}

async function run() {
  const { chromium } = require(process.env.SWC_PLAYWRIGHT_MODULE || "/opt/codex/runtimes/cua/lib/node_modules/playwright-core");
  const browser = await chromium.launch({ executablePath: process.env.SWC_CHROME_PATH || "/usr/bin/chromium", headless: true, chromiumSandbox: true });
  const results = [];
  async function scenario(name, route, options, test) {
    const fixture = await startMock(options);
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    page.setDefaultTimeout(10000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(String(error)));
    try {
      await page.goto(`${fixture.base}/#${route}`);
      await test(page, fixture);
      assert.deepEqual(errors, [], "browser must render without JavaScript errors");
      const scopeSetup = options.allowScopeCreation || name === "scope_setup_prefills_target_and_returns_to_bucket" || name === "setup_deep_link_waits_for_requested_management_binding";
      const mutations = fixture.state.requests.filter((request) => request.method !== "GET" && request.path !== "/auth/login" && !request.path.endsWith("/objects/select") && !(scopeSetup && /^\/projects\/[^/]+\/scopes$/.test(request.path)));
      assert.deepEqual(mutations, [], "browsing must dispatch no management/storage mutations");
      results.push({ name, passed: true });
    } catch (error) {
      results.push({ name, passed: false, error: String(error.message || error) });
      console.error(`${name}: ${error.message || error}`);
    } finally {
      fixture.state.gates.forEach((pending) => pending.release());
      await page.close();
      fixture.server.closeAllConnections?.();
      await new Promise((resolve) => fixture.server.close(resolve));
    }
  }
  const objectRequests = (state) => state.requests.filter((request) => request.path.endsWith("/objects"));
  const inventory = (page) => page.getByRole("table", { name: "Live S3 objects", exact: true });
  const missingScope = (page) => page.locator(".notice").filter({ hasText: "No authorized scope for bucket bucket-b." });

  try {
    await scenario("inventory_browse_preserves_bucket_and_missing_scope", "buckets", {}, async (page, { state }) => {
      const row = page.getByRole("table", { name: "SeaweedFS buckets" }).getByRole("row").filter({ hasText: "bucket-b" });
      await row.getByRole("link", { name: "Browse files", exact: true }).click();
      await missingScope(page).waitFor();
      const hash = await page.evaluate(() => location.hash);
      const query = new URLSearchParams(hash.split("?")[1]);
      assert.equal(query.get("management"), "manager-a");
      assert.equal(query.get("bucket"), "bucket-b");
      assert.equal(await page.locator(".contextBar").count(), 0, "requested bucket must not display an unrelated global project scope");
      assert.equal(objectRequests(state).length, 0, "missing B access must not browse unrelated A");
      assert.equal(await page.getByRole("link", { name: "Configure scoped access", exact: true }).count(), 1);
    });

    await scenario("cross_project_scope_browses_readonly_without_index", objectLocation(), { withScope: true }, async (page, { state }) => {
      await inventory(page).getByText(trickyKey, { exact: true }).waitFor();
      assert.ok(state.requests.some((request) => request.path === "/projects/project-b/scopes"), "discover permitted contexts outside the initially selected project");
      assert.ok(objectRequests(state).every((request) => request.query.scope_id === "scope-b"));
      assert.equal(state.requests.some((request) => /scans|assets|snapshots/.test(request.path)), false, "live browsing needs no image index");
      await page.getByRole("button", { name: "View details", exact: true }).click();
      await page.getByText("object-scope-b", { exact: true }).first().waitFor();
      assert.ok(state.requests.some((request) => request.path.endsWith("/objects/select") && request.payload.key === trickyKey));
      await page.getByRole("button", { name: "Object operations", exact: true }).click();
      await page.getByLabel("Object ID", { exact: true }).waitFor();
      assert.equal(await page.locator(".contextBar select").nth(0).inputValue(), "project-b", "resolved scope must synchronize the parent project");
      assert.equal(await page.locator(".contextBar select").nth(1).inputValue(), "scope-b");
      assert.equal(await page.getByLabel("Object ID", { exact: true }).inputValue(), "object-scope-b", "cross-project handoff must reach operations with its selected identity");
    });

    await scenario("manual_cross_project_scope_selection_reaches_operations", objectLocation(), { withScope: true, alternateScope: true }, async (page, { state }) => {
      await inventory(page).getByText(trickyKey, { exact: true }).waitFor();
      await page.getByRole("combobox", { name: /^Storage scope/ }).selectOption("scope-b");
      await until(async () => await page.getByRole("combobox", { name: /^Storage scope/ }).inputValue() === "scope-b", "manual cross-project scope selection");
      await until(() => objectRequests(state).some((request) => request.query.scope_id === "scope-b"), "chosen cross-project scope listing");
      await page.getByRole("button", { name: "View details", exact: true }).click();
      await page.getByText("object-scope-b", { exact: true }).first().waitFor();
      await page.getByRole("button", { name: "Object operations", exact: true }).click();
      await page.getByLabel("Object ID", { exact: true }).waitFor();
      assert.equal(await page.locator(".contextBar select").nth(0).inputValue(), "project-b", "manual scope selection must synchronize the parent project");
      assert.equal(await page.locator(".contextBar select").nth(1).inputValue(), "scope-b");
      assert.equal(await page.getByLabel("Object ID", { exact: true }).inputValue(), "object-scope-b");
    });

    await scenario("filer_handoff_preserves_exact_key_and_scoped_download", "files", { withScope: true }, async (page, { state, base }) => {
      const row = page.getByRole("table", { name: "Filer directory entries" }).getByRole("row").filter({ hasText: filerName });
      const open = row.getByRole("link", { name: "Open object", exact: true });
      const href = new URL(await open.getAttribute("href"), base);
      const query = new URLSearchParams(href.hash.split("?")[1]);
      assert.equal(query.get("bucket"), "bucket-b");
      assert.equal(query.get("key"), trickyKey, "Filer handoff must preserve the literal identity including trailing space, percent, plus, repeated slash, Unicode, query and hash characters");
      assert.equal(await row.locator('a[href*="/files/download"]').count(), 0, "S3 bytes must use the scoped object endpoint");
      await row.getByRole("button", { name: "Prepare rename", exact: true }).waitFor();
      await open.click();
      await page.getByText("object-scope-b", { exact: true }).first().waitFor();
      assert.ok(state.requests.filter((request) => request.path.endsWith("/objects/select")).every((request) => request.payload.key === trickyKey && request.payload.scope_id === "scope-b"));
      const downloads = await page.locator('a[href*="/objects/download"]').evaluateAll((links) => links.map((link) => link.href));
      assert.ok(downloads.length > 0, "an authorized file has an actionable download");
      for (const value of downloads) {
        const url = new URL(value);
        assert.equal(url.searchParams.get("key"), trickyKey);
        assert.equal(url.searchParams.get("scope_id"), "scope-b");
      }
    });

    await scenario("filer_without_literal_identity_requires_fresh_listing", "files", { withScope: true, objectIdentity: false }, async (page, { state, base }) => {
      const row = page.getByRole("table", { name: "Filer directory entries" }).getByRole("row").filter({ hasText: filerName });
      await row.getByText("Exact object identity is unavailable. Refresh the file listing before opening this object.", { exact: true }).waitFor();
      assert.equal(await row.getByRole("link", { name: "Open object", exact: true }).count(), 0, "normalized paths cannot substitute for missing literal identity");
      assert.equal(await row.locator('a[href*="/files/download"]').count(), 0, "missing identity cannot enable raw S3 downloads");
      assert.equal(await row.locator('a[href*="#objects"]').count(), 0, "missing identity cannot infer a bucket or key from the normalized path");
      const before = state.requests.filter((request) => request.path.endsWith("/files")).length;
      state.objectIdentity = true;
      await row.getByRole("button", { name: "Refresh listing", exact: true }).click();
      const open = row.getByRole("link", { name: "Open object", exact: true });
      await open.waitFor();
      assert.ok(state.requests.filter((request) => request.path.endsWith("/files")).length > before, "recovery re-reads the authoritative file listing");
      const href = new URL(await open.getAttribute("href"), base);
      const query = new URLSearchParams(href.hash.split("?")[1]);
      assert.equal(query.get("management"), "manager-a");
      assert.equal(query.get("bucket"), "bucket-b");
      assert.equal(query.get("key"), trickyKey, "refreshed identity must retain the literal key");
      assert.equal(state.requests.some((request) => request.path.endsWith("/objects/select")), false, "refresh alone cannot select an inferred key");
    });

    await scenario("scope_setup_prefills_target_and_returns_to_bucket", objectLocation(), {}, async (page, { state }) => {
      await missingScope(page).waitFor();
      await page.getByRole("link", { name: "Configure scoped access", exact: true }).click();
      await page.getByLabel("Bucket", { exact: true }).waitFor();
      assert.equal(await page.getByLabel("Bucket", { exact: true }).inputValue(), "bucket-b");
      assert.equal(await page.getByRole("combobox", { name: /^Connection/ }).inputValue(), "s3-a");
      assert.equal(await page.getByRole("link", { name: "Return to requested bucket", exact: true }).count(), 1);
      await page.getByRole("button", { name: "Add scope", exact: true }).click();
      await inventory(page).getByText(trickyKey, { exact: true }).waitFor();
      const creation = state.requests.find((request) => request.method === "POST" && request.path.endsWith("/scopes"));
      assert.equal(creation.payload.connection_id, "s3-a");
      assert.equal(creation.payload.bucket, "bucket-b");
      assert.equal(creation.payload.writable, false);
      assert.ok(objectRequests(state).every((request) => request.query.scope_id === "scope-created"));
    });

    await scenario("setup_deep_link_waits_for_requested_management_binding", `settings?setup=scope&${objectLocation("bucket-b", trickyKey, "manager-b").split("?")[1]}`, {
      hold: (request) => request.path === "/management/connections",
    }, async (page, { state }) => {
      const pending = state.gates[0];
      await until(() => pending.entered, "delayed management connection hydration");
      const addScope = page.getByRole("button", { name: "Add scope", exact: true });
      assert.ok(await addScope.count() === 0 || !await addScope.isVisible() || await addScope.isDisabled(), "scope creation waits for the requested manager binding");
      pending.release();
      await page.getByRole("combobox", { name: /^Connection/ }).waitFor();
      await until(async () => await page.getByRole("combobox", { name: /^Connection/ }).inputValue() === "s3-b", "second S3 connection from the requested manager");
      assert.equal(await page.getByLabel("Bucket", { exact: true }).inputValue(), "bucket-b");
      await addScope.click();
      await page.getByText("object-scope-created", { exact: true }).first().waitFor();
      const creation = state.requests.find((request) => request.method === "POST" && request.path.endsWith("/scopes"));
      assert.equal(creation.payload.connection_id, "s3-b", "deep-link setup cannot fall back to first unrelated S3 connection");
      assert.equal(creation.payload.bucket, "bucket-b");
      assert.ok(state.requests.filter((request) => request.path.endsWith("/objects/select")).every((request) => request.payload.key === trickyKey));
    });

    const setupLocation = `settings?setup=scope&${objectLocation().split("?")[1]}`;
    const creations = (state) => state.requests.filter((request) => request.method === "POST" && /^\/projects\/[^/]+\/scopes$/.test(request.path));
    function assertOriginalCreation(state) {
      assert.equal(creations(state).length, 1, "navigation must not retry scope creation");
      assert.equal(creations(state)[0].path, "/projects/project-a/scopes");
      assert.equal(creations(state)[0].payload.bucket, "bucket-b");
      assert.equal(creations(state)[0].payload.connection_id, "s3-a");
    }

    for (const failCreate of [false, true]) {
      await scenario(`late_scope_create_${failCreate ? "failure" : "success"}_cannot_redirect_new_bucket`, setupLocation, {
        allowScopeCreation: true, failCreate, hold: (request) => request.method === "POST" && request.path.endsWith("/scopes"),
      }, async (page, { state }) => {
        await page.getByRole("button", { name: "Add scope", exact: true }).click();
        const pending = state.gates[0];
        await until(() => pending.entered, "held scope creation");
        const destination = objectLocation("bucket-a");
        await page.evaluate((hash) => { location.hash = hash; }, destination);
        await inventory(page).getByText("a/photo.jpg", { exact: true }).waitFor();
        const response = page.waitForResponse((value) => value.request().method() === "POST" && value.url().endsWith("/scopes"));
        pending.release();
        await (await response).finished();
        await page.waitForLoadState("networkidle");
        assert.equal(await page.evaluate(() => location.hash), `#${destination}`, "an old Settings completion cannot navigate away from the newer bucket");
        await inventory(page).getByText("a/photo.jpg", { exact: true }).waitFor();
        assert.equal(await page.getByText(/SYNTHETIC_CREATE_FAILURE/).count(), 0, "old Settings errors cannot appear under a new bucket");
        assertOriginalCreation(state);
      });

      await scenario(`late_scope_create_${failCreate ? "failure" : "success"}_cannot_own_remounted_settings`, setupLocation, {
        allowScopeCreation: true, failCreate, hold: (request) => request.method === "POST" && request.path.endsWith("/scopes"),
      }, async (page, { state }) => {
        await page.getByRole("button", { name: "Add scope", exact: true }).click();
        const pending = state.gates[0];
        await until(() => pending.entered, "held original Settings save");
        await page.evaluate((hash) => { location.hash = hash; }, objectLocation("bucket-a"));
        await inventory(page).getByText("a/photo.jpg", { exact: true }).waitFor();
        await page.evaluate((hash) => { location.hash = hash; }, setupLocation);
        await page.getByLabel("Bucket", { exact: true }).fill("new-unsaved-bucket");
        const response = page.waitForResponse((value) => value.request().method() === "POST" && value.url().endsWith("/scopes"));
        pending.release();
        await (await response).finished();
        await page.waitForLoadState("networkidle");
        assert.equal(await page.evaluate(() => location.hash), `#${setupLocation}`, "returning to the same Settings URL must create fresh completion ownership");
        assert.equal(await page.getByLabel("Bucket", { exact: true }).inputValue(), "new-unsaved-bucket", "old save/refresh cannot reset the new Settings form");
        assert.equal(await page.getByText(/SYNTHETIC_CREATE_FAILURE/).count(), 0, "old errors cannot populate a remounted form");
        assertOriginalCreation(state);
      });
    }

    for (const failRefresh of [false, true]) {
      await scenario(`late_settings_refresh_${failRefresh ? "failure" : "success"}_cannot_redirect_new_bucket`, setupLocation, {
        allowScopeCreation: true,
      }, async (page, { state }) => {
        await page.getByRole("button", { name: "Add scope", exact: true }).waitFor();
        const pending = gate((request) => request.path === "/connections");
        state.gates.push(pending);
        state.failRefreshConnections = failRefresh;
        await page.getByRole("button", { name: "Add scope", exact: true }).click();
        await until(() => pending.entered, "held post-save refresh");
        const destination = objectLocation("bucket-a");
        await page.evaluate((hash) => { location.hash = hash; }, destination);
        await inventory(page).getByText("a/photo.jpg", { exact: true }).waitFor();
        const response = page.waitForResponse((value) => new URL(value.url()).pathname === "/api/v1/connections");
        pending.release();
        await (await response).finished();
        await page.waitForLoadState("networkidle");
        assert.equal(await page.evaluate(() => location.hash), `#${destination}`, "an old post-save refresh cannot navigate away from the newer bucket");
        await inventory(page).getByText("a/photo.jpg", { exact: true }).waitFor();
        assert.equal(await page.getByText(/SYNTHETIC_REFRESH_FAILURE/).count(), 0, "old refresh errors cannot appear under a new bucket");
        assertOriginalCreation(state);
      });
    }

    await scenario("unlinked_manager_has_connection_specific_setup", objectLocation("bucket-b", undefined, "manager-unlinked"), { withScope: true }, async (page, { state }) => {
      const setup = page.getByRole("link", { name: /Configure/ }).first();
      await setup.waitFor();
      const hash = await setup.getAttribute("href");
      assert.equal(new URLSearchParams(hash.split("?")[1]).get("management"), "manager-unlinked");
      assert.equal(objectRequests(state).length, 0, "an unlinked management endpoint cannot inherit another S3 connection");
    });

    await scenario("unlinked_manager_recovers_through_explicit_linked_connection", objectLocation("bucket-b", trickyKey, "manager-unlinked"), { withScope: true }, async (page, { state }) => {
      await page.getByRole("link", { name: "Configure scoped access", exact: true }).click();
      const replacement = page.getByRole("link", { name: "Browse using Read-only manager A", exact: true });
      await replacement.click();
      await page.getByText("object-scope-b", { exact: true }).first().waitFor();
      const hash = await page.evaluate(() => location.hash);
      const query = new URLSearchParams(hash.split("?")[1]);
      assert.equal(query.get("management"), "manager-a");
      assert.equal(query.get("bucket"), "bucket-b");
      assert.equal(query.get("key"), trickyKey);
      assert.ok(state.requests.filter((request) => request.path.endsWith("/objects/select")).every((request) => request.path.startsWith("/management/manager-a/") && request.payload.key === trickyKey));
    });

    await scenario("key_outside_literal_prefix_has_targeted_prerequisite", objectLocation("bucket-b", "other/photo.jpg"), { withScope: true }, async (page, { state }) => {
      await missingScope(page).waitFor();
      assert.equal(objectRequests(state).length, 0);
      assert.equal(state.requests.some((request) => request.path.endsWith("/objects/select")), false);
    });

    await scenario("deep_link_survives_login", objectLocation("bucket-b", trickyKey), { withScope: true, authenticated: false }, async (page, { state }) => {
      await page.getByLabel("Password", { exact: true }).fill("synthetic-password");
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
      await page.getByText("object-scope-b", { exact: true }).first().waitFor();
      assert.ok(state.requests.some((request) => request.path.endsWith("/objects/select") && request.payload.key === trickyKey));
    });

    for (const failObjects of [false, true]) {
      await scenario(`late_${failObjects ? "failed" : "successful"}_listing_cannot_replace_new_bucket`, objectLocation(), {
        withScope: true, failObjects, hold: (request) => request.path.endsWith("/objects") && request.query.scope_id === "scope-b",
      }, async (page, { state }) => {
        const pending = state.gates[0];
        await until(() => pending.entered, "held old bucket listing");
        state.failObjects = false;
        await page.evaluate((hash) => { location.hash = hash; }, objectLocation("bucket-a"));
        await inventory(page).getByText("a/photo.jpg", { exact: true }).waitFor();
        const response = page.waitForResponse((value) => value.url().includes("/objects?") && new URL(value.url()).searchParams.get("scope_id") === "scope-b");
        pending.release();
        await (await response).finished();
        await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        assert.equal(await inventory(page).getByText(trickyKey, { exact: true }).count(), 0);
        assert.equal(await page.getByText(/SYNTHETIC_OBJECT_FAILURE/).count(), 0, "old failure must not appear under the new bucket");
        await inventory(page).getByText("a/photo.jpg", { exact: true }).waitFor();
      });
    }

    await scenario("late_key_selection_cannot_populate_new_bucket", objectLocation("bucket-b", trickyKey), {
      withScope: true, hold: (request) => request.path.endsWith("/objects/select") && request.payload.scope_id === "scope-b",
    }, async (page, { state }) => {
      const pending = state.gates[0];
      await until(() => pending.entered, "held old exact-key selection");
      await page.evaluate((hash) => { location.hash = hash; }, objectLocation("bucket-a"));
      await inventory(page).getByText("a/photo.jpg", { exact: true }).waitFor();
      const response = page.waitForResponse((value) => value.url().endsWith("/objects/select"));
      pending.release();
      await (await response).finished();
      await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await page.getByText("object-scope-b", { exact: true }).count(), 0, "late B selection cannot reach A details");
    });

    await scenario("late_scope_discovery_uses_new_management_binding", objectLocation(), {
      withScope: true, hold: (request) => request.path === "/projects/project-b/scopes",
    }, async (page, { state }) => {
      const pending = state.gates[0];
      await until(() => pending.entered, "held cross-project scopes");
      assert.equal(objectRequests(state).length, 0, "lookup in progress cannot be treated as empty or use unrelated A");
      await page.locator(".topbar .managementSelector select").selectOption("manager-b");
      pending.release();
      await inventory(page).getByText(trickyKey, { exact: true }).waitFor();
      assert.ok(objectRequests(state).every((request) => request.query.scope_id === "scope-other"), "late discovery must resolve against the new management S3 binding");
    });

    await scenario("unknown_management_target_never_substitutes_first_connection", objectLocation("bucket-b", undefined, "ghost-manager"), { withScope: true }, async (page, { state }) => {
      await page.locator(".shell").waitFor();
      await page.waitForLoadState("networkidle");
      assert.equal(objectRequests(state).length, 0);
      assert.notEqual(await page.locator(".topbar .managementSelector select").inputValue(), "manager-a");
      await page.getByText("The requested management connection is unavailable.", { exact: true }).waitFor();
      await page.getByRole("link", { name: "Configure scoped access", exact: true }).click();
      const replacement = page.getByRole("link", { name: "Browse using Read-only manager A", exact: true });
      await replacement.waitFor();
      assert.equal(await page.getByRole("heading", { name: "Access policy", exact: true }).count(), 0, "unknown target must not edit the first configured management connection");
      assert.equal(state.requests.some((request) => request.path.endsWith("/operations")), false, "unknown target must not display an unrelated manager history");
      await replacement.click();
      await inventory(page).getByText(trickyKey, { exact: true }).waitFor();
      assert.ok(objectRequests(state).every((request) => request.path.startsWith("/management/manager-a/")));
    });

    await scenario("scope_lookup_failure_remains_an_error", objectLocation(), { failScopes: true }, async (page, { state }) => {
      await page.getByText(/SYNTHETIC_SCOPE_FAILURE/).first().waitFor();
      assert.equal(await missingScope(page).count(), 0, "a rejected lookup does not establish missing access");
      assert.equal(objectRequests(state).length, 0);
    });

    await scenario("list_failure_remains_an_error", objectLocation(), { withScope: true, failObjects: true }, async (page) => {
      await page.getByText(/SYNTHETIC_OBJECT_FAILURE/).first().waitFor();
      assert.equal(await missingScope(page).count(), 0);
      assert.equal(await inventory(page).count(), 0, "failed list must not render as successful empty inventory");
    });

    await scenario("successful_empty_list_is_explicit", objectLocation(), { withScope: true, empty: true }, async (page, { state }) => {
      await page.getByText("Empty response", { exact: true }).first().waitFor();
      assert.equal(await missingScope(page).count(), 0);
      assert.ok(objectRequests(state).some((request) => request.query.scope_id === "scope-b"));
    });

    await scenario("download_requires_selected_scope_permission", objectLocation("bucket-b", trickyKey), { withScope: true, download: false }, async (page) => {
      await page.getByText("object-scope-b", { exact: true }).first().waitFor();
      assert.equal(await page.locator('a[href*="/objects/download"]').count(), 0, "a scope without download permission must offer no download href");
      assert.match(await page.locator(".managementPage").innerText(), /download.*(permission|allow|enabled|disabled)/i);
    });

    await scenario("access_prerequisite_is_bilingual_and_usable_on_mobile", objectLocation(), {}, async (page) => {
      await missingScope(page).waitFor();
      const setup = page.locator('a[href*="setup=scope"]').first();
      if (process.env.SWC_BROWSE_SCREENSHOTS) {
        fs.mkdirSync(process.env.SWC_BROWSE_SCREENSHOTS, { recursive: true });
        await page.screenshot({ path: path.join(process.env.SWC_BROWSE_SCREENSHOTS, "browse-missing-scope-desktop.png"), fullPage: true });
      }
      await page.locator(".languageSelector select").selectOption("zh-CN");
      assert.match(await setup.innerText(), /[\u3400-\u9fff]/, "setup action needs a Chinese label");
      assert.equal(await missingScope(page).count(), 0, "the access prerequisite must switch locale");
      assert.match(await page.locator(".managementPage").innerText(), /bucket-b/);
      await page.setViewportSize({ width: 595, height: 900 });
      await setup.scrollIntoViewIfNeeded();
      const box = await setup.boundingBox();
      assert.ok(box && box.x >= 0 && box.x + box.width <= 595, "setup action must remain reachable in the mobile viewport");
      if (process.env.SWC_BROWSE_SCREENSHOTS) await page.screenshot({ path: path.join(process.env.SWC_BROWSE_SCREENSHOTS, "browse-missing-scope-mobile-zh.png"), fullPage: true });
    });
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify({ browseHandoffRegressions: results }, null, 2));
  assert.ok(results.every((result) => result.passed), "bucket/Filer handoff regressions failed");
}

run().catch((error) => { console.error(error); process.exitCode = 1; });
