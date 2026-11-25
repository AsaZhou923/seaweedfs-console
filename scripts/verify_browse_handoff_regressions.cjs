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

function fileLocation(filePath, manager = "manager-a") {
  return `files?${new URLSearchParams({ management: manager, path: filePath })}`;
}

function scopedLocation(prefix, scope = "scope-b", manager = "manager-a") {
  return `objects?${new URLSearchParams({ management: manager, bucket: "bucket-b", scope, prefix })}`;
}

async function startMock(options = {}) {
  const state = {
    scopes: { "project-a": options.alternateScope ? [scopeA, { ...scopeB, id: "scope-alternate", project_id: "project-a", display_name: "Alternate scope" }] : [scopeA], "project-b": options.withScope ? [...(options.prependScopes || []), { ...scopeB, allow_original_download: options.download !== false }, scopeOther, ...(options.overlappingScope ? [{ ...scopeB, id: "scope-narrow", display_name: "Narrow literal scope", prefix: "literal2026/" }] : [])] : [] },
    requests: [], gates: options.hold ? [gate(options.hold)] : [], failScopes: Boolean(options.failScopes), failObjects: Boolean(options.failObjects), failFiles: Boolean(options.failFiles), empty: Boolean(options.empty),
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
        response = { items: options.navigation ? managers.map((manager) => ({ ...manager, permissions: { ...manager.permissions, "file.read_roots": options.fileRoots || ["/allowed"] } })) : managers };
      } else if (/^\/projects\/[^/]+\/scopes$/.test(request.path)) {
        const projectId = request.path.split("/")[2];
        if (req.method === "POST") {
          if (state.failCreate) {
            response = { error: { code: "SYNTHETIC_CREATE_FAILURE", message: "Scope creation failed" } };
            status = 503;
          } else {
            const created = { ...scopeB, ...payload, ...payload.scope_policy, id: "scope-created", project_id: projectId };
            state.scopes[projectId].push(created);
            response = { ...created, ...options.createdScopeResponse };
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
        response = state.failFiles ? { error: { code: "SYNTHETIC_FILE_FAILURE", message: "Filer listing failed" } } : { items: options.navigation ? [{ name: "child", full_path: `${request.query.path}/child`, is_directory: true }, { name: "navigation.txt", full_path: `${request.query.path}/navigation.txt`, is_directory: false, size: 12 }, { name: `entry-at-${request.query.path}`, full_path: `${request.query.path}/marker.txt`, is_directory: false, size: 1 }] : [entry], next_cursor: options.navigationPagination && !request.query.cursor ? "navigation-next-page" : "" };
        status = state.failFiles ? 503 : 200;
      } else if (request.path.endsWith("/objects/select")) {
        response = { object: { id: `object-${payload.scope_id}`, key: payload.key, scope_id: payload.scope_id, version_id: "version-1", size: 12 } };
      } else if (request.path.endsWith("/objects")) {
        response = state.failObjects ? { error: { code: "SYNTHETIC_OBJECT_FAILURE", message: "Object listing failed" } } : {
          items: state.empty ? [] : [{ key: options.navigation ? `${request.query.prefix}navigation.jpg` : request.query.scope_id === "scope-a" ? "a/photo.jpg" : trickyKey, version_id: "version-1", size: 12 }], folders: options.navigation ? [`${request.query.prefix}child/`] : [], next_cursor: options.navigationPagination && !request.query.cursor ? "navigation-next-page" : "",
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
  const locationParams = (page) => page.evaluate(() => Object.fromEntries(new URLSearchParams(location.hash.split("?")[1] || "")));
  const fileRequests = (state) => state.requests.filter((request) => request.path.endsWith("/files"));
  async function assertLocation(page, expected) {
    const actual = await locationParams(page);
    for (const [key, value] of Object.entries(expected)) assert.equal(actual[key], value, `location ${key}`);
    assert.ok(Object.keys(actual).every((key) => ["management", "bucket", "scope", "prefix", "path", "key"].includes(key)), "locations contain navigational identity only");
  }
  async function fileAt(page, expected, manager = "manager-a") {
    await until(async () => await page.getByLabel("Path", { exact: true }).inputValue() === expected, `Filer path ${expected}`);
    await page.getByRole("table", { name: "Filer directory entries" }).getByText("navigation.txt", { exact: true }).waitFor();
    await page.getByRole("table", { name: "Filer directory entries" }).getByText(`entry-at-${expected}`, { exact: true }).waitFor();
    await assertLocation(page, { management: manager, path: expected });
  }
  async function objectsAt(page, prefix, scope = "scope-b", manager = "manager-a") {
    await inventory(page).getByText(`${prefix}navigation.jpg`, { exact: true }).waitFor();
    await assertLocation(page, { management: manager, bucket: "bucket-b", scope, prefix });
  }

  try {
    for (const route of ["files", "objects"]) {
      const initial = route === "files" ? fileLocation("/allowed/team/nested") : scopedLocation("literal2026/team/");
      await scenario(`review_sidebar_${route}_remembers_locations_per_connection`, initial, { withScope: true, navigation: true }, async (page, { state }) => {
        const show = async (manager, value) => route === "files" ? fileAt(page, value, manager) : objectsAt(page, value, manager === "manager-a" ? "scope-b" : "scope-other", manager);
        await show("manager-a", route === "files" ? "/allowed/team/nested" : "literal2026/team/");
        const changeElsewhere = async (manager) => {
          await page.locator('#main-navigation a[href="#buckets"]').click();
          await page.getByRole("table", { name: "SeaweedFS buckets" }).waitFor();
          const before = state.requests.length;
          await page.locator(".topbar .managementSelector select").selectOption(manager);
          await until(() => state.requests.slice(before).some((request) => request.path === `/management/${manager}/buckets`), "newly chosen connection inventory");
          await page.locator(`#main-navigation a[href^="#${route}"]`).click();
        };
        await changeElsewhere("manager-b");
        await show("manager-b", route === "files" ? "/allowed" : "literal");
        assert.equal(await page.locator(".topbar .managementSelector select").inputValue(), "manager-b", "sidebar return cannot undo an explicit connection choice");
        const next = route === "files" ? "/allowed/b-team" : "literalb/";
        const field = page.getByLabel(route === "files" ? "Path" : "Object prefix", { exact: true });
        await field.fill(next);
        await field.press("Enter");
        await show("manager-b", next);
        await changeElsewhere("manager-a");
        await show("manager-a", route === "files" ? "/allowed/team/nested" : "literal2026/team/");
        await changeElsewhere("manager-b");
        await show("manager-b", next);
      });
    }

    for (const withScope of [false, true]) {
      await scenario(`review_setup_stale_scope_selects_new_scope_${withScope ? "existing_alternative" : "new_only"}`, scopedLocation("literal2026/team/", "ghost-scope"), {
        withScope, navigation: true, allowScopeCreation: true,
      }, async (page, { state }) => {
        await missingScope(page).waitFor();
        await page.getByRole("link", { name: "Configure scoped access", exact: true }).click();
        await page.getByLabel("Prefix", { exact: true }).waitFor();
        assert.equal(await page.getByLabel("Prefix", { exact: true }).inputValue(), "literal2026/team/", "prefix-only setup must prefill the requested directory");
        const returnParams = new URLSearchParams((await page.getByRole("link", { name: "Return to requested bucket", exact: true }).getAttribute("href")).split("?")[1]);
        assert.equal(returnParams.has("scope"), false, "return without creating cannot retain a stale Scope binding");
        await page.getByRole("button", { name: "Add scope", exact: true }).click();
        await objectsAt(page, "literal2026/team/", "scope-created");
        assert.ok(objectRequests(state).every((request) => request.query.scope_id === "scope-created"), "newly created Scope must win over an existing covering Scope");
        const created = state.requests.filter((request) => request.method === "POST" && request.path.endsWith("/scopes"));
        assert.equal(created.length, 1);
        assert.equal(created[0].payload.prefix, "literal2026/team/");
        assert.equal(created[0].payload.connection_id, "s3-a");
      });
    }

    await scenario("review_setup_return_without_create_resolves_existing_scope", scopedLocation("literal2026/team/", "ghost-scope"), {
      withScope: true, navigation: true,
    }, async (page, { state }) => {
      await missingScope(page).waitFor();
      await page.getByRole("link", { name: "Configure scoped access", exact: true }).click();
      await page.getByRole("link", { name: "Return to requested bucket", exact: true }).click();
      await objectsAt(page, "literal2026/team/");
      assert.equal(state.requests.some((request) => request.method === "POST" && request.path.endsWith("/scopes")), false, "return without creation must use existing authorized access");
      assert.ok(objectRequests(state).every((request) => request.query.scope_id === "scope-b"));
    });

    await scenario("review_setup_edited_scope_root_returns_to_created_identity", `${scopedLocation("literal2026/team/", "ghost-scope")}&key=${encodeURIComponent(trickyKey)}`, {
      withScope: true, navigation: true, allowScopeCreation: true,
    }, async (page, { state }) => {
      await missingScope(page).waitFor();
      await page.getByRole("link", { name: "Configure scoped access", exact: true }).click();
      await page.getByLabel("Prefix", { exact: true }).fill("new-root/");
      await page.getByRole("button", { name: "Add scope", exact: true }).click();
      await objectsAt(page, "new-root/", "scope-created");
      const params = await locationParams(page);
      assert.equal(Object.hasOwn(params, "key"), false, "an exact-key handoff outside the created Scope must be dropped");
      assert.equal(state.requests.some((request) => request.path.endsWith("/objects/select")), false, "old object selection cannot cross the newly created prefix");
      assert.ok(objectRequests(state).every((request) => request.query.scope_id === "scope-created" && request.query.prefix === "new-root/"));
    });

    await scenario("review_setup_different_connection_cannot_fall_back_to_old_scope", scopedLocation("literal2026/team/", "ghost-scope"), {
      withScope: true, navigation: true, allowScopeCreation: true,
    }, async (page, { state }) => {
      await missingScope(page).waitFor();
      await page.getByRole("link", { name: "Configure scoped access", exact: true }).click();
      await page.getByRole("combobox", { name: /^Connection/ }).selectOption("s3-b");
      assert.equal(await page.getByRole("button", { name: "Add scope", exact: true }).isDisabled(), true, "targeted setup must prevent creation against a different S3 connection");
      await page.waitForLoadState("networkidle");
      assert.equal(state.requests.some((request) => request.method === "POST" && request.path.endsWith("/scopes")), false);
      assert.equal(objectRequests(state).length, 0, "a Scope created for a different S3 connection cannot fall back to old manager data");
      assert.equal(state.requests.some((request) => request.path.endsWith("/objects/select")), false);
    });

    await scenario("review_setup_mismatching_created_response_stays_in_settings", scopedLocation("literal2026/team/", "ghost-scope"), {
      withScope: true, navigation: true, allowScopeCreation: true, createdScopeResponse: { connection_id: "s3-b" },
    }, async (page, { state }) => {
      await missingScope(page).waitFor();
      await page.getByRole("link", { name: "Configure scoped access", exact: true }).click();
      await page.getByRole("button", { name: "Add scope", exact: true }).click();
      await until(() => state.requests.some((request) => request.method === "POST" && request.path.endsWith("/scopes")), "mismatching creation response");
      await page.waitForLoadState("networkidle");
      assert.ok((await page.evaluate(() => location.hash)).startsWith("#settings?"), "a mismatching creation response must keep setup visible");
      assert.equal(objectRequests(state).length, 0, "mismatching creation response cannot fall back to a covering existing Scope");
    });

    await scenario("review_setup_browse_using_rebinds_scope_and_preserves_directory", scopedLocation("literal2026/team/", "scope-b", "manager-unlinked"), {
      withScope: true, navigation: true,
    }, async (page, { state }) => {
      await page.getByRole("link", { name: "Configure scoped access", exact: true }).click();
      const replacement = page.getByRole("link", { name: "Browse using Read-only manager B", exact: true });
      const params = new URLSearchParams((await replacement.getAttribute("href")).split("?")[1]);
      assert.equal(params.has("scope"), false, "connection handoff cannot carry a foreign Scope ID");
      await replacement.click();
      await objectsAt(page, "literal2026/team/", "scope-other", "manager-b");
      assert.ok(objectRequests(state).every((request) => request.path.startsWith("/management/manager-b/") && request.query.scope_id === "scope-other"));
    });

    for (const prefix of ["literal2026/team/", "uncovered/"]) {
      const first = { ...scopeB, id: "b-first", connection_id: "s3-b", display_name: "First unrelated prefix", prefix: "zzz/" };
      const startingPrefix = prefix === "uncovered/" ? "literal2026/team/" : prefix;
      await scenario(`review_manager_switch_${prefix === "uncovered/" ? "uncovered_prefix_uses_root" : "finds_covering_later_scope"}`, scopedLocation(startingPrefix), {
        withScope: true, navigation: true, prependScopes: [first],
      }, async (page, { state }) => {
        await objectsAt(page, startingPrefix);
        if (prefix === "uncovered/") state.scopes["project-b"] = state.scopes["project-b"].filter((scope) => scope.id !== "scope-other");
        const before = state.requests.length;
        await page.locator(".topbar .managementSelector select").selectOption("manager-b");
        await objectsAt(page, prefix === "uncovered/" ? "zzz/" : startingPrefix, prefix === "uncovered/" ? "b-first" : "scope-other", "manager-b");
        assert.ok(objectRequests({ requests: state.requests.slice(before) }).every((request) => request.path.startsWith("/management/manager-b/") && request.query.scope_id === (prefix === "uncovered/" ? "b-first" : "scope-other")), "new connection must choose a covering Scope or its approved root");
        if (prefix === "uncovered/") {
          const fallbackReads = objectRequests(state).length;
          await page.evaluate((hash) => { location.hash = hash; }, `objects?${new URLSearchParams({ management: "manager-b", bucket: "bucket-b", prefix: startingPrefix })}`);
          await missingScope(page).waitFor();
          await page.waitForLoadState("networkidle");
          assert.equal(objectRequests(state).length, fallbackReads, "pasting an earlier uncovered URL must not reuse the completed connection-switch fallback");
        }
      });
    }

    await scenario("filer_location_survives_sidebar_reload_and_copied_link", fileLocation("/allowed/team/nested"), { navigation: true }, async (page, { state, base }) => {
      await fileAt(page, "/allowed/team/nested");
      const copied = await page.evaluate(() => location.hash);
      await page.locator('#main-navigation a[href="#assets"]').click();
      await page.locator('#main-navigation a[href^="#files"]').click();
      await fileAt(page, "/allowed/team/nested");
      await page.reload();
      await fileAt(page, "/allowed/team/nested");
      const fresh = await browser.newPage();
      try {
        await fresh.goto(`${base}/${copied}`);
        await fileAt(fresh, "/allowed/team/nested");
      } finally { await fresh.close(); }
      assert.ok(fileRequests(state).every((request) => request.path.startsWith("/management/manager-a/") && request.query.path === "/allowed/team/nested"), "restored location cannot briefly list an unrelated directory");
    });

    await scenario("filer_up_breadcrumb_and_back_stay_under_read_root", fileLocation("/allowed/team/nested"), { navigation: true }, async (page, { state }) => {
      await fileAt(page, "/allowed/team/nested");
      await page.getByRole("button", { name: "Up", exact: true }).click();
      await fileAt(page, "/allowed/team");
      await page.goBack();
      await fileAt(page, "/allowed/team/nested");
      const crumbs = page.getByRole("navigation", { name: "Location breadcrumbs", exact: true });
      await crumbs.getByRole("link", { name: "/allowed", exact: true }).click();
      await fileAt(page, "/allowed");
      assert.equal(await page.getByRole("button", { name: "Up", exact: true }).isDisabled(), true, "Up stops at the approved Filer read root");
      assert.ok(fileRequests(state).every((request) => request.query.path === "/allowed" || request.query.path.startsWith("/allowed/")), "parent traversal cannot read outside root");
    });

    await scenario("filer_committed_path_is_navigable_without_per_keystroke_requests", fileLocation("/allowed"), { navigation: true }, async (page, { state }) => {
      await fileAt(page, "/allowed");
      const before = fileRequests(state).length;
      await page.getByLabel("Path", { exact: true }).fill("/allowed/typed path");
      assert.equal(fileRequests(state).length, before, "draft input does not list partial paths");
      await assertLocation(page, { path: "/allowed" });
      await page.getByLabel("Path", { exact: true }).press("Enter");
      await fileAt(page, "/allowed/typed path");
      await page.getByRole("table", { name: "Filer directory entries" }).getByRole("row").filter({ hasText: "child" }).getByRole("button", { name: "Open", exact: true }).click();
      await fileAt(page, "/allowed/typed path/child");
      await page.goBack();
      await fileAt(page, "/allowed/typed path");
    });

    await scenario("filer_copied_nested_location_survives_login", fileLocation("/allowed/team/nested"), { navigation: true, authenticated: false }, async (page) => {
      await page.getByLabel("Password", { exact: true }).fill("synthetic-password");
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
      await fileAt(page, "/allowed/team/nested");
    });

    for (const invalidPath of ["/outside", "/allowed-other", "/allowed/../outside", "allowed/relative", "/allowed/\u0000invalid"]) {
      await scenario(`filer_rejects_unauthorized_location_${invalidPath}`, fileLocation(invalidPath), { navigation: true }, async (page, { state }) => {
        await page.locator(".shell").waitFor();
        await page.waitForLoadState("networkidle");
        assert.equal(fileRequests(state).length, 0, "invalid copied directory cannot dispatch a Filer listing");
        assert.equal(await page.getByRole("table", { name: "Filer directory entries" }).count(), 0);
      });
    }

    await scenario("filer_protected_deep_link_is_blocked_even_with_root_read_grant", fileLocation("/.system/private"), { navigation: true, fileRoots: ["/"] }, async (page, { state }) => {
      await page.locator(".shell").waitFor();
      await page.waitForLoadState("networkidle");
      assert.equal(fileRequests(state).length, 0, "protected Filer metadata cannot dispatch through a broad read root");
      assert.equal(await page.getByRole("table", { name: "Filer directory entries" }).count(), 0);
    });

    await scenario("object_location_survives_sidebar_reload_and_copied_link", scopedLocation("literal2026//team/"), { withScope: true, navigation: true }, async (page, { state, base }) => {
      await objectsAt(page, "literal2026//team/");
      const copied = await page.evaluate(() => location.hash);
      await page.locator('#main-navigation a[href="#assets"]').click();
      await page.locator('#main-navigation a[href^="#objects"]').click();
      await objectsAt(page, "literal2026//team/");
      await page.reload();
      await objectsAt(page, "literal2026//team/");
      const fresh = await browser.newPage();
      try {
        await fresh.goto(`${base}/${copied}`);
        await objectsAt(fresh, "literal2026//team/");
      } finally { await fresh.close(); }
      assert.ok(objectRequests(state).every((request) => request.query.scope_id === "scope-b" && request.query.prefix === "literal2026//team/"), "exact literal directory and scope survive every entry point");
    });

    await scenario("object_up_breadcrumb_and_back_keep_literal_scope_root", scopedLocation("literal2026//team/"), { withScope: true, navigation: true, overlappingScope: true }, async (page, { state }) => {
      await objectsAt(page, "literal2026//team/");
      await page.getByRole("button", { name: "Up", exact: true }).click();
      await objectsAt(page, "literal2026//");
      await page.goBack();
      await objectsAt(page, "literal2026//team/");
      await page.getByRole("navigation", { name: "Location breadcrumbs", exact: true }).getByRole("link", { name: "literal", exact: true }).click();
      await objectsAt(page, "literal");
      assert.equal(await page.getByRole("button", { name: "Up", exact: true }).isDisabled(), true, "a literal prefix without trailing slash remains the authorized root");
      assert.ok(objectRequests(state).every((request) => request.query.scope_id === "scope-b" && request.query.prefix.startsWith("literal")), "overlapping scopes cannot replace the explicit root");
    });

    await scenario("object_explicit_overlapping_scope_keeps_its_own_root", scopedLocation("literal2026/team/", "scope-narrow"), { withScope: true, navigation: true, overlappingScope: true }, async (page, { state }) => {
      await objectsAt(page, "literal2026/team/", "scope-narrow");
      await page.getByRole("button", { name: "Up", exact: true }).click();
      await objectsAt(page, "literal2026/", "scope-narrow");
      assert.equal(await page.getByRole("button", { name: "Up", exact: true }).isDisabled(), true);
      assert.ok(objectRequests(state).every((request) => request.query.scope_id === "scope-narrow" && request.query.prefix.startsWith("literal2026/")), "narrow root cannot be replaced by a broader matching scope");
    });

    await scenario("object_committed_prefix_and_folder_navigation_preserve_literal_bytes", scopedLocation("literal"), { withScope: true, navigation: true }, async (page, { state }) => {
      await objectsAt(page, "literal");
      const prefix = "literal2026//子目录/space +%25?#/";
      const before = objectRequests(state).length;
      await page.getByLabel("Object prefix", { exact: true }).fill(prefix);
      assert.equal(objectRequests(state).length, before, "draft prefix does not list partial object keys");
      await assertLocation(page, { prefix: "literal" });
      await page.getByLabel("Object prefix", { exact: true }).press("Enter");
      await objectsAt(page, prefix);
      await page.getByRole("table", { name: "Live S3 prefixes" }).getByRole("row").filter({ hasText: `${prefix}child/` }).getByRole("button", { name: "Open", exact: true }).click();
      await objectsAt(page, `${prefix}child/`);
      await page.goBack();
      await objectsAt(page, prefix);
      assert.ok(objectRequests(state).some((request) => request.query.prefix === prefix), "URL and request must keep repeated slashes, Unicode, plus, percent, query and hash bytes");
    });

    await scenario("object_copied_prefix_survives_login", scopedLocation("literal2026/team/"), { withScope: true, navigation: true, authenticated: false }, async (page) => {
      await page.getByLabel("Password", { exact: true }).fill("synthetic-password");
      await page.getByRole("button", { name: "Sign in", exact: true }).click();
      await objectsAt(page, "literal2026/team/");
    });

    for (const [name, route] of [
      ["unknown_scope", scopedLocation("literal2026/team/", "ghost-scope")],
      ["empty_scope", scopedLocation("literal2026/team/", "")],
      ["wrong_connection_scope", scopedLocation("literal2026/team/", "scope-other")],
      ["prefix_outside_scope", scopedLocation("liter/team/")],
      ["unknown_manager", scopedLocation("literal2026/team/", "scope-b", "ghost-manager")],
      ["empty_manager", scopedLocation("literal2026/team/", "scope-b", "")],
    ]) {
      await scenario(`object_location_rejects_${name}`, route, { withScope: true, navigation: true }, async (page, { state }) => {
        await page.locator(".shell").waitFor();
        await page.waitForLoadState("networkidle");
        assert.equal(objectRequests(state).length, 0, "invalid explicit identity cannot fall back to a different scope");
        assert.equal(state.requests.some((request) => request.path.endsWith("/objects/select")), false);
      });
    }

    for (const manager of ["ghost-manager", ""]) {
      await scenario(`filer_invalid_manager_cannot_use_default_connection_${manager || "empty"}`, fileLocation("/allowed/team", manager), { navigation: true }, async (page, { state }) => {
        await page.locator(".shell").waitFor();
        await page.waitForLoadState("networkidle");
        assert.equal(fileRequests(state).length, 0, "invalid manager cannot dispatch through the first configured manager");
      });
    }

    for (const route of ["files", "objects"]) {
      for (const failure of [false, true]) {
        const oldValue = route === "files" ? "/allowed/old" : "literalold/";
        const newValue = route === "files" ? "/allowed/new" : "literalnew/";
        const oldRoute = route === "files" ? fileLocation(oldValue) : scopedLocation(oldValue);
        const newRoute = route === "files" ? fileLocation(newValue) : scopedLocation(newValue);
        await scenario(`${route}_late_${failure ? "failure" : "success"}_cannot_replace_new_directory`, oldRoute, {
          withScope: true, navigation: true, failFiles: route === "files" && failure, failObjects: route === "objects" && failure,
          hold: (request) => request.path.endsWith(`/${route}`) && (route === "files" ? request.query.path : request.query.prefix) === oldValue,
        }, async (page, { state }) => {
          const pending = state.gates[0];
          await until(() => pending.entered, "held original directory listing");
          state.failFiles = false;
          state.failObjects = false;
          await page.evaluate((hash) => { location.hash = hash; }, newRoute);
          if (route === "files") await fileAt(page, newValue);
          else await objectsAt(page, newValue);
          const response = page.waitForResponse((value) => {
            const url = new URL(value.url());
            return url.pathname.endsWith(`/${route}`) && url.searchParams.get(route === "files" ? "path" : "prefix") === oldValue;
          });
          pending.release();
          await (await response).finished();
          await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          assert.equal(await page.getByText(route === "files" ? `entry-at-${oldValue}` : `${oldValue}navigation.jpg`, { exact: true }).count(), 0, "old directory contents cannot populate new location");
          assert.equal(await page.getByText(/SYNTHETIC_(FILE|OBJECT)_FAILURE/).count(), 0, "old directory error cannot populate new location");
          if (route === "files") await fileAt(page, newValue);
          else await objectsAt(page, newValue);
        });
      }
    }

    for (const route of ["files", "objects"]) {
      const first = route === "files" ? fileLocation("/allowed/first") : scopedLocation("literalfirst/");
      const second = route === "files" ? fileLocation("/allowed/second") : scopedLocation("literalsecond/");
      await scenario(`${route}_directory_back_resets_cursor`, first, { withScope: true, navigation: true, navigationPagination: true }, async (page, { state }) => {
        if (route === "files") await fileAt(page, "/allowed/first");
        else await objectsAt(page, "literalfirst/");
        await page.getByRole("button", { name: "Next page", exact: true }).click();
        await until(() => state.requests.some((request) => request.path.endsWith(`/${route}`) && request.query.cursor === "navigation-next-page"), "second-page listing");
        const before = state.requests.length;
        await page.evaluate((hash) => { location.hash = hash; }, second);
        if (route === "files") await fileAt(page, "/allowed/second");
        else await objectsAt(page, "literalsecond/");
        await page.goBack();
        if (route === "files") await fileAt(page, "/allowed/first");
        else await objectsAt(page, "literalfirst/");
        assert.ok(state.requests.slice(before).filter((request) => request.path.endsWith(`/${route}`)).every((request) => !request.query.cursor), "direct navigation and Back cannot reuse a different directory's cursor");
      });
    }

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
        assert.ok((await page.evaluate(() => location.hash)).startsWith("#objects?"), "an old Settings completion cannot leave object browsing");
        await assertLocation(page, { management: "manager-a", bucket: "bucket-a" });
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
        assert.ok((await page.evaluate(() => location.hash)).startsWith("#objects?"), "an old post-save refresh cannot leave object browsing");
        await assertLocation(page, { management: "manager-a", bucket: "bucket-a" });
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
