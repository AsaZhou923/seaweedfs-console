// Focused management-context regressions. All endpoints are synthetic loopback mocks.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");

const dist = path.resolve(process.env.SWC_REGRESSION_DIST || path.join(__dirname, "../frontend/dist"));

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
function gate() {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { promise, release, entered: false };
}
async function until(predicate, label) {
  const deadline = Date.now() + 10000;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await wait(20);
  }
  throw new Error(`Timed out: ${label}`);
}
function json(res, value, status = 200) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  res.end(JSON.stringify(value));
}

const project = { id: "project-a", display_name: "Synthetic project", project_key: "synthetic" };
const scopes = ["a", "b"].map((name) => ({
  id: `scope-${name}`, project_id: project.id, connection_id: "s3-a", display_name: `Scope ${name}`,
  bucket: `bucket-${name}`, prefix: `${name}/`, writable: true, allow_preview: true, allow_original_download: true,
}));
const managers = ["a", "b"].map((name) => ({
  id: `manager-${name}`, name: `Manager ${name}`, admin_url: `http://synthetic-${name}.invalid`,
  s3_connection_id: "s3-a", management_write_enabled: true,
  permissions: { "file.manage": true, "object.manage": true, "bucket.manage": true },
}));

async function startMock() {
  const state = { hold: null, requests: [], buckets: ["bucket-a", "bucket-b"], versioning: {}, folders: {}, versionInfo: null };
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, "http://127.0.0.1");
      if (!url.pathname.startsWith("/api/v1/")) {
        let file = path.resolve(dist, "." + decodeURIComponent(url.pathname));
        const relative = path.relative(dist, file);
        if (relative.startsWith("..") || path.isAbsolute(relative)) {
          res.writeHead(404);
          return res.end();
        }
        if (!fs.existsSync(file) || !fs.statSync(file).isFile()) file = path.join(dist, "index.html");
        const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
        res.writeHead(200, { "Content-Type": types[path.extname(file)] || "application/octet-stream", "Cache-Control": "no-store" });
        return res.end(fs.readFileSync(file));
      }
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = Buffer.concat(chunks).toString();
      const payload = body ? JSON.parse(body) : {};
      state.requests.push({ path: url.pathname, method: req.method, payload, query: Object.fromEntries(url.searchParams) });
      const hold = state.hold;
      if (hold && hold.match(url, payload, req.method)) {
        hold.entered = true;
        await hold.promise;
        if (hold.apply && hold.status === 200) hold.apply();
        if (hold.response) return json(res, hold.response, hold.status);
      }
      const apiPath = url.pathname.replace(/^\/api\/v1/, "");
      if (apiPath === "/auth/me") return json(res, { user: { username: "synthetic" }, csrf_token: "synthetic-csrf", projects: [project] });
      if (apiPath === "/connections") return json(res, { items: [{ id: "s3-a", display_name: "Synthetic S3" }] });
      if (apiPath === `/projects/${project.id}/scopes`) return json(res, { items: scopes });
      if (apiPath === "/management/connections") return json(res, { items: managers });
      if (apiPath.endsWith("/files/delete-preview")) return json(res, { path: url.searchParams.get("path"), items: [], complete: true, truncated: false, preview_hash: "synthetic-empty-directory", entry_count: 0 });
      if (apiPath.endsWith("/files")) return json(res, { items: [{ name: "empty", full_path: "/owned/empty", is_directory: true, size: 0 }, ...(state.folders[url.searchParams.get("path")] || []).map((name) => ({ name, full_path: `${url.searchParams.get("path")}/${name}`, is_directory: true, size: 0 }))], next_cursor: "" });
      if (apiPath.endsWith("/objects/select")) return json(res, { object: { id: `object-${payload.key}`, key: payload.key, version_id: "version-1", scope_id: payload.scope_id } });
      if (apiPath.endsWith("/objects/version-info")) return json(res, { key: url.searchParams.get("key"), version_id: "version-1", etag: "synthetic-etag", can_delete: true, identity_strength: "strong", ...state.versionInfo });
      if (apiPath.endsWith("/objects/check-conditional-delete")) return json(res, { status: "confirmed", operation_id: "synthetic-probe", replayed: false, capability_status: "supported", capability: "conditional_delete_if_match", result: { confirmed: true } });
      if (apiPath.endsWith("/objects")) {
        const prefix = url.searchParams.get("scope_id") === "scope-b" ? "b/" : "a/";
        return json(res, { items: ["first", "second"].map((name) => ({ key: `${prefix}${name}`, size: 1, version_id: "version-1" })), folders: [], next_cursor: "" });
      }
      if (apiPath.endsWith("/buckets")) return json(res, { items: state.buckets.map((name) => ({ name, versioning_status: state.versioning[name] || "Suspended" })) });
      if (apiPath.endsWith("/versioning")) return json(res, { status: state.versioning[decodeURIComponent(apiPath.split("/").at(-2))] || "Suspended" });
      if (apiPath.endsWith("/policy")) return json(res, { Version: "2012-10-17", Statement: [], marker: apiPath.includes("bucket-a") ? "policy-a" : "policy-b" });
      return json(res, { items: [] });
    } catch (error) {
      res.writeHead(500, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ error: { code: "SYNTHETIC_ERROR", message: String(error) } }));
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { state, server, base: `http://127.0.0.1:${server.address().port}` };
}

const helpers = `
window.__contextTest = {
  button(text) { return [...document.querySelectorAll("button")].find((node) => node.textContent.trim() === text); },
  field(text) { return [...document.querySelectorAll("label")].find((node) => node.textContent.trim() === text)?.querySelector("input,select,textarea"); },
  set(node, value) {
    if (!node) throw new Error("Missing form control");
    const proto = node instanceof HTMLSelectElement ? HTMLSelectElement.prototype : node instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(node, value);
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
  },
  manager(value) { this.set(document.querySelector(".topbar .managementSelector select"), value); },
  scope(value) { this.set(document.querySelectorAll(".contextBar select")[1], value); },
  chooseObject(index) { [...document.querySelectorAll("button")].filter((node) => node.textContent.trim() === "View details")[index].click(); },
  policyButton() { return [...document.querySelectorAll(".formSection")].find((node) => node.querySelector("h3")?.textContent.trim() === "Bucket policy")?.querySelector("button"); }
};
window.__contextBodies = [];
window.__contextResponseBodies = [];
const originalText = Response.prototype.text;
Response.prototype.text = async function () {
  const text = await originalText.call(this);
  window.__contextBodies.push(this.url);
  window.__contextResponseBodies.push(text);
  return text;
};
`;

async function verifyManagementContexts(browser, pageFactory) {
  const fixture = await startMock();
  const results = [];
  async function scenario(name, route, test) {
    let page;
    fixture.state.hold = null;
    fixture.state.buckets = ["bucket-a", "bucket-b"];
    fixture.state.versioning = {};
    fixture.state.folders = {};
    fixture.state.versionInfo = null;
    try {
      page = await pageFactory(browser, `${fixture.base}/#${route}`);
      await until(() => page.eval(`Boolean(document.querySelector(".shell")) && document.querySelector(".topbar .managementSelector select")?.value === "manager-a"`), "authenticated context");
      await page.eval(helpers);
      await test(page);
      results.push({ name, passed: true });
    } catch (error) {
      results.push({ name, passed: false, error: String(error.message || error) });
    } finally {
      fixture.state.hold?.release();
      if (page) await page.close();
    }
  }
  try {
    for (const route of ["buckets", "files"]) {
      for (const change of ["target", "connection", "round_trip", "same_target", "unmount"]) {
        for (const failure of [false, true]) {
          await scenario(`${route}_write_${failure ? "failure" : "success"}_after_${change}`, route, async (page) => {
            const bucket = route === "buckets";
            const marker = `old-${route}-${change}-${failure ? "failure" : "success"}`;
            const writePath = bucket ? "/api/v1/management/manager-a/buckets/bucket-a/versioning" : "/api/v1/management/manager-a/files/mkdir";
            if (bucket) {
              await until(() => page.eval(`Boolean(window.__contextTest.button("bucket-a"))`), "bucket inventory");
              await page.eval(`window.__contextTest.button("bucket-a").click()`);
              await until(() => page.eval(`window.__contextTest.button("Save versioning")?.disabled === false`), "bucket-a writable controls");
            } else {
              await until(() => page.eval(`Boolean(window.__contextTest.field("Path"))`), "file inventory");
              await page.eval(`window.__contextTest.set(window.__contextTest.field("Path"), "/owned/a")`);
              await page.eval(`window.__contextTest.set(window.__contextTest.field("New folder name"), "child")`);
              await until(() => page.eval(`window.__contextTest.button("Create directory")?.disabled === false`), "file writable controls");
            }
            const firstRequest = fixture.state.requests.length;
            fixture.state.hold = {
              ...gate(), match: (url, _payload, method) => url.pathname === writePath && method === (bucket ? "PUT" : "POST"),
              status: failure ? 422 : 200,
              response: failure ? { error: { code: "INVALID_REQUEST", message: marker } } : { status: "confirmed", operation_id: marker, result: { confirmed: true }, replayed: false },
              apply: () => {
                if (bucket) fixture.state.versioning["bucket-a"] = "Enabled";
                else fixture.state.folders["/owned/a"] = ["child"];
              },
            };
            await page.eval(`window.__contextTest.button(${JSON.stringify(bucket ? "Save versioning" : "Create directory")}).click()`);
            await until(() => fixture.state.hold.entered, "held old-target write");
            if (change === "unmount") {
              await page.eval(`location.hash = "objects"`);
              await until(() => page.eval(`Boolean(window.__contextTest.button("View details"))`), "replacement page inventory");
            } else if (change === "connection") {
              await page.eval(`window.__contextTest.manager("manager-b")`);
              await until(() => page.eval(`document.querySelector(".topbar .managementSelector select").value === "manager-b"`), "new management connection");
            } else if (change !== "same_target" && bucket) {
              await page.eval(`window.__contextTest.button("bucket-b").click()`);
              await until(() => page.eval(`document.querySelector(".sectionHeading h2")?.textContent === "bucket-b"`), "new bucket selected");
              if (change === "round_trip") await page.eval(`window.__contextTest.button("bucket-a").click()`);
            } else if (change !== "same_target") {
              await page.eval(`window.__contextTest.set(window.__contextTest.field("Path"), "/owned/b")`);
              await until(() => page.eval(`window.__contextTest.field("Path")?.value === "/owned/b"`), "new Filer path loaded");
              if (change === "round_trip") {
                await page.eval(`window.__contextTest.set(window.__contextTest.field("Path"), "/owned/a")`);
                await until(() => page.eval(`window.__contextTest.field("Path")?.value === "/owned/a"`), "original Filer path reloaded");
              }
            }
            if (change !== "unmount") await until(() => page.eval(`!document.querySelector(".managementPage")?.textContent.includes("Loading") && ${bucket ? 'Boolean(window.__contextTest.button("bucket-a")) && (document.querySelector(".sectionHeading h2")?.textContent === "Select a bucket" || window.__contextTest.policyButton()?.disabled === false)' : 'Boolean(window.__contextTest.field("Path"))'}`), "new target reads settled");
            const requestCount = fixture.state.requests.length;
            fixture.state.hold.release();
            await until(() => page.eval(`window.__contextResponseBodies.some((body) => body.includes(${JSON.stringify(marker)}))`), "old write response consumed");
            await page.eval(`new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))`);
            assert.equal(await page.eval(`document.body.textContent.includes(${JSON.stringify(marker)})`), change === "same_target", "only the current target may display its write success or error");
            const refreshTarget = !failure && (change === "same_target" || change === "round_trip");
            const refreshInventory = bucket && !failure && change !== "connection" && change !== "unmount";
            if (refreshTarget || refreshInventory) {
              await until(() => fixture.state.requests.length > requestCount, "current-target success refresh");
            } else {
              assert.equal(fixture.state.requests.length, requestCount, "stale completion or a failed write must not refresh the target");
            }
            if (refreshTarget) {
              await until(() => page.eval(bucket ? `document.querySelector(".bucketFacts dd")?.textContent === "Enabled"` : `Boolean(window.__contextTest.button("Open")) && [...document.querySelectorAll("td")].some((cell) => cell.textContent === "child")`), "updated original-target evidence displayed");
            } else {
              assert.equal(fixture.state.requests.slice(requestCount).some((item) => item.method === "GET" && (bucket ? /\/buckets\/[^/]+/.test(item.path) : item.path.endsWith("/files"))), false, "unrelated target details must not refresh");
            }
            if (refreshInventory) {
              await until(() => page.eval(`[...document.querySelectorAll("tr")].some((row) => row.querySelector("button")?.textContent === "bucket-a" && row.textContent.includes("Enabled"))`), "updated bucket inventory evidence displayed");
            }
            const writes = fixture.state.requests.slice(firstRequest).filter((item) => item.method === (bucket ? "PUT" : "POST"));
            assert.equal(writes.length, 1, "the original write must dispatch exactly once");
            assert.equal(writes[0].path, writePath, "the original write must retain its management connection and bucket");
            if (!bucket) {
              assert.equal(writes[0].payload.path, "/owned/a", "the original Filer write must retain its path");
              assert.equal(writes[0].payload.folder_name, "child");
            }
            assert.equal(await page.eval(`document.querySelector(".topbar .managementSelector select").value`), change === "connection" ? "manager-b" : "manager-a");
            if (change !== "unmount" && !bucket) assert.equal(await page.eval(`window.__contextTest.field("Path").value`), change === "connection" ? "/" : change === "target" ? "/owned/b" : "/owned/a");
            if (change !== "unmount" && bucket) assert.equal(await page.eval(`document.querySelector(".sectionHeading h2").textContent`), change === "connection" ? "Select a bucket" : change === "target" ? "bucket-b" : "bucket-a");
          });
        }
      }
    }

    for (const action of ["create", "delete"]) {
      await scenario(`bucket_${action}_refreshes_inventory_after_selection_change`, "buckets", async (page) => {
        await until(() => page.eval(`Boolean(window.__contextTest.button("bucket-a"))`), "bucket inventory");
        await page.eval(`window.__contextTest.button("bucket-a").click()`);
        await until(() => page.eval(`window.__contextTest.policyButton()?.disabled === false`), "bucket-a detail");
        const create = action === "create";
        const writePath = create ? "/api/v1/management/manager-a/buckets" : "/api/v1/management/manager-a/buckets/bucket-a";
        const marker = `receipt-${action}-bucket-a`;
        fixture.state.hold = {
          ...gate(), match: (url, _payload, method) => url.pathname === writePath && method === (create ? "POST" : "DELETE"),
          status: 200, response: { status: "confirmed", operation_id: marker, result: { confirmed: true }, replayed: false },
          apply: () => { fixture.state.buckets = create ? ["bucket-a", "bucket-b", "bucket-created"] : ["bucket-b"]; },
        };
        if (create) await page.eval(`window.__contextTest.set(window.__contextTest.field("Bucket"), "bucket-created")`);
        await page.eval(`window.__contextTest.button(${JSON.stringify(create ? "Create bucket" : "Delete empty bucket")}).click()`);
        await until(() => fixture.state.hold.entered, "held bucket inventory mutation");
        await page.eval(`window.__contextTest.button("bucket-b").click()`);
        await until(() => page.eval(`document.querySelector(".sectionHeading h2")?.textContent === "bucket-b" && window.__contextTest.policyButton()?.disabled === false && !document.querySelector(".managementPage")?.textContent.includes("Loading")`), "bucket-b detail loaded");
        const requestCount = fixture.state.requests.length;
        fixture.state.hold.release();
        await until(() => page.eval(`window.__contextResponseBodies.some((body) => body.includes(${JSON.stringify(marker)}))`), "bucket mutation response consumed");
        await until(() => page.eval(create ? `Boolean(window.__contextTest.button("bucket-created"))` : `!window.__contextTest.button("bucket-a") && Boolean(window.__contextTest.button("bucket-b"))`), "changed bucket inventory rendered");
        assert.equal(await page.eval(`document.querySelector(".sectionHeading h2").textContent`), "bucket-b");
        assert.equal(await page.eval(`document.body.textContent.includes(${JSON.stringify(marker)})`), false, "obsolete receipt must remain hidden after inventory refresh");
        assert.equal(fixture.state.requests.slice(requestCount).some((item) => item.method === "GET" && /\/buckets\/[^/]+/.test(item.path)), false, "inventory refresh must not refresh unrelated bucket-b details");
        assert.equal(fixture.state.requests.filter((item) => item.path === writePath && item.method === (create ? "POST" : "DELETE")).length, 1, "inventory mutation dispatches once");
      });
    }

    await scenario("files_clear_confirmation_on_connection_switch", "files", async (page) => {
      await until(() => page.eval(`Boolean(window.__contextTest.button("Select for deletion"))`), "file inventory");
      await page.eval(`window.__contextTest.button("Select for deletion").click();`);
      await page.eval(`window.__contextTest.button("Read delete preview").click();`);
      await until(() => page.eval(`window.__contextTest.button("Confirm delete")?.disabled === false`), "enabled delete confirmation");
      fixture.state.hold = { ...gate(), match: (url) => url.pathname.endsWith("manager-b/files") };
      await page.eval(`window.__contextTest.manager("manager-b")`);
      await until(() => fixture.state.hold.entered, "pending second manager inventory");
      assert.equal(await page.eval(`window.__contextTest.field("Delete path").value`), "", "connection switch must clear the selected delete target");
      assert.equal(await page.eval(`window.__contextTest.button("Confirm delete").disabled`), true, "old preview cannot authorize the new connection");
    });

    await scenario("files_ignore_preview_after_target_is_edited", "files", async (page) => {
      await until(() => page.eval(`Boolean(window.__contextTest.button("Select for deletion"))`), "file inventory");
      await page.eval(`window.__contextTest.button("Select for deletion").click()`);
      fixture.state.hold = { ...gate(), match: (url) => url.pathname.endsWith("/files/delete-preview") };
      await page.eval(`window.__contextTest.button("Read delete preview").click()`);
      await until(() => fixture.state.hold.entered, "held delete preview");
      await page.eval(`window.__contextTest.set(window.__contextTest.field("Delete path"), "/owned/new")`);
      await page.eval(`window.__contextTest.set(window.__contextTest.field("Delete path"), "/owned/empty")`);
      fixture.state.hold.release();
      await until(() => page.eval(`window.__contextBodies.some((path) => path.includes("/files/delete-preview"))`), "old preview consumed");
      await wait(30);
      assert.equal(await page.eval(`window.__contextTest.button("Confirm delete").disabled`), true, "editing a target invalidates its in-flight confirmation even when the text is restored");
    });

    await scenario("objects_ignore_old_scope_selection", "objects", async (page) => {
      await until(() => page.eval(`Boolean(window.__contextTest.button("View details"))`), "object inventory");
      fixture.state.hold = { ...gate(), match: (url, payload) => url.pathname.endsWith("/objects/select") && payload.key === "a/first" };
      await page.eval(`window.__contextTest.chooseObject(0)`);
      await until(() => fixture.state.hold.entered, "held old-scope selection");
      await page.eval(`window.__contextTest.scope("scope-b")`);
      await until(() => page.eval(`document.body.textContent.includes("b/first")`), "new scope list");
      fixture.state.hold.release();
      await until(() => page.eval(`window.__contextBodies.some((path) => path.endsWith("/objects/select"))`), "old response consumed");
      await wait(30);
      assert.equal(await page.eval(`document.body.textContent.includes("object-a/first")`), false, "old selection must not populate new Scope details");
      await page.eval(`location.hash = "operations"`);
      await until(() => page.eval(`Boolean(window.__contextTest.button("Versions / Tags / Retention"))`), "object operations");
      await page.eval(`window.__contextTest.button("Versions / Tags / Retention").click()`);
      assert.equal(await page.eval(`window.__contextTest.field("Object ID")?.value || ""`), "", "old selection must not reach parent operation inputs");
    });

    for (const locale of ["en", "zh-CN"]) {
      await scenario(`objects_block_unknown_null_protection_${locale}`, "objects", async (page) => {
        fixture.state.versionInfo = { version_id: "null", identity_strength: "mutable", can_delete: false, delete_reason: "OBJECT_VERSION_PROTECTION_UNSUPPORTED", delete_block_reason: "OBJECT_VERSION_PROTECTION_UNSUPPORTED", legal_hold: null, retention_until: null };
        await until(() => page.eval(`Boolean(window.__contextTest.button("View details"))`), "object inventory");
        await page.eval(`window.__contextTest.chooseObject(0)`);
        await until(() => page.eval(`Boolean(window.__contextTest.button("Read version identity"))`), "selected object");
        await page.eval(`window.__contextTest.set(window.__contextTest.field("Version ID (optional)"), "null")`);
        await page.eval(`window.__contextTest.button("Read version identity").click()`);
        await until(() => page.eval(`document.body.textContent.includes("The storage service cannot verify Legal Hold or retention for this null version.")`), "unknown protection explanation");
        const probeRequestsBefore = fixture.state.requests.filter((item) => item.path.endsWith("/objects/check-conditional-delete")).length;
        await page.eval(`window.__contextTest.button("Probe conditional delete capability").click()`);
        await until(() => fixture.state.requests.filter((item) => item.path.endsWith("/objects/check-conditional-delete")).length === probeRequestsBefore + 1, "conditional delete probe");
        await until(() => page.eval(`document.body.textContent.includes("supported")`), "supported probe rendered");
        await until(() => page.eval(`window.__contextBodies.filter((url) => new URL(url).pathname.endsWith("/objects/version-info")).length === 2`), "identity consumed after supported probe");
        if (locale === "zh-CN") {
          await page.eval(`window.__contextTest.set(document.querySelector(".languageSelector select"), "zh-CN")`);
          await until(() => page.eval(`document.querySelector(".shell").dataset.locale === "zh-CN"`), "Chinese language");
        }
        const explanation = locale === "en"
          ? "The storage service cannot verify Legal Hold or retention for this null version. Deletion is blocked even when conditional deletion is supported."
          : "存储服务无法核实此 null 版本的 Legal Hold 或保留状态。即使支持条件删除，此版本仍禁止删除。";
        await until(() => page.eval(`document.body.textContent.includes(${JSON.stringify(explanation)})`), "localized protection explanation");
        await page.eval(`[...document.querySelectorAll("label.ack input")].forEach((node) => { if (!node.checked) node.click(); })`);
        const deleteLabel = locale === "en" ? "Permanently delete this version" : "永久删除此版本";
        assert.equal(await page.eval(`window.__contextTest.button(${JSON.stringify(deleteLabel)}).disabled`), true, "unknown version protection must block deletion despite confirmations and a supported probe");
        await page.eval(`window.__contextTest.button(${JSON.stringify(deleteLabel)}).click()`);
        assert.equal(fixture.state.requests.some((item) => item.path.endsWith("/objects/delete-version")), false, "disabled deletion must never dispatch");
        assert.equal(await page.eval(`document.body.textContent.includes("OBJECT_VERSION_PROTECTION_UNSUPPORTED")`), true, "raw DTO must preserve protection code");
        if (locale === "en") assert.equal(await page.eval(`document.querySelector(".stateList").textContent.includes("存储服务")`), false, "English protection status must be translated");
      });
    }

    await scenario("objects_clear_confirmed_target_on_connection_switch", "objects", async (page) => {
      await until(() => page.eval(`Boolean(window.__contextTest.button("View details"))`), "object inventory");
      await page.eval(`window.__contextTest.chooseObject(0)`);
      await until(() => page.eval(`Boolean(window.__contextTest.button("Read version identity"))`), "selected object");
      await page.eval(`window.__contextTest.button("Read version identity").click()`);
      await until(() => page.eval(`document.body.textContent.includes("synthetic-etag")`), "version identity");
      await page.eval(`[...document.querySelectorAll("label.ack input")].forEach((node) => node.click())`);
      await until(() => page.eval(`window.__contextTest.button("Permanently delete this version")?.disabled === false`), "enabled version delete");
      await page.eval(`window.__contextTest.manager("manager-b")`);
      await wait(30);
      assert.equal(await page.eval(`Boolean(window.__contextTest.button("Permanently delete this version"))`), false, "new connection must require a fresh selection and confirmation");
    });

    await scenario("objects_keep_newer_selection", "objects", async (page) => {
      await until(() => page.eval(`Boolean(window.__contextTest.button("View details"))`), "object inventory");
      fixture.state.hold = { ...gate(), match: (url, payload) => url.pathname.endsWith("/objects/select") && payload.key === "a/first" };
      await page.eval(`window.__contextTest.chooseObject(0)`);
      await until(() => fixture.state.hold.entered, "held first object");
      await page.eval(`window.__contextTest.chooseObject(1)`);
      await until(() => page.eval(`document.body.textContent.includes("object-a/second")`), "newer second object");
      fixture.state.hold.release();
      await until(() => page.eval(`window.__contextBodies.filter((path) => path.endsWith("/objects/select")).length === 2`), "both selection responses consumed");
      await wait(30);
      assert.equal(await page.eval(`document.body.textContent.includes("object-a/first")`), false, "late older selection must not replace the newer detail");
      if (process.env.SWC_VERIFY_OPERATIONS_HANDOFF === "1") {
        await page.eval(`window.__contextTest.button("Object operations").click()`);
        await until(() => page.eval(`window.__contextTest.field("Object ID")?.value === "object-a/second"`), "explicit latest-object handoff");
      }
    });

    await scenario("buckets_hide_old_policy_during_target_load", "buckets", async (page) => {
      await until(() => page.eval(`Boolean(window.__contextTest.button("bucket-a"))`), "bucket inventory");
      await page.eval(`window.__contextTest.button("bucket-a").click()`);
      await until(() => page.eval(`window.__contextTest.policyButton()?.disabled === false`), "bucket-a policy");
      fixture.state.hold = { ...gate(), match: (url) => url.pathname.endsWith("/buckets/bucket-b/policy") };
      await page.eval(`window.__contextTest.button("bucket-b").click()`);
      await until(() => fixture.state.hold.entered, "pending bucket-b policy");
      assert.equal(await page.eval(`window.__contextTest.policyButton().disabled`), true, "old bucket policy must not be offered as the new bucket's read value");
    });
  } finally {
    fixture.state.hold?.release();
    fixture.server.closeAllConnections?.();
    await new Promise((resolve) => fixture.server.close(resolve));
  }
  console.log(JSON.stringify({ managementContextRegressions: results }, null, 2));
  assert.ok(results.every((result) => result.passed), "management context regressions failed");
  return results;
}

module.exports = { verifyManagementContexts };

if (require.main === module) {
  (async () => {
    const { chromium } = require(process.env.SWC_PLAYWRIGHT_MODULE || "/opt/codex/runtimes/cua/lib/node_modules/playwright-core");
    const browser = await chromium.launch({executablePath: process.env.SWC_CHROME_PATH || "/usr/bin/chromium", headless: true, chromiumSandbox: true});
    const pageFactory = async (_, url) => {
      const page = await browser.newPage();
      await page.goto(url);
      return {eval: (script) => page.evaluate(script), close: () => page.close()};
    };
    try { await verifyManagementContexts(browser, pageFactory); }
    finally { await browser.close(); }
  })().catch((error) => { console.error(error); process.exitCode = 1; });
}
