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
function json(res, value) {
  res.writeHead(200, { "Content-Type": "application/json", "Cache-Control": "no-store" });
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
  const state = { hold: null, requests: [] };
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
      state.requests.push({ path: url.pathname, method: req.method, payload });
      const hold = state.hold;
      if (hold && hold.match(url, payload)) {
        hold.entered = true;
        await hold.promise;
      }
      const apiPath = url.pathname.replace(/^\/api\/v1/, "");
      if (apiPath === "/auth/me") return json(res, { user: { username: "synthetic" }, csrf_token: "synthetic-csrf", projects: [project] });
      if (apiPath === "/connections") return json(res, { items: [{ id: "s3-a", display_name: "Synthetic S3" }] });
      if (apiPath === `/projects/${project.id}/scopes`) return json(res, { items: scopes });
      if (apiPath === "/management/connections") return json(res, { items: managers });
      if (apiPath.endsWith("/files/delete-preview")) return json(res, { path: url.searchParams.get("path"), items: [], complete: true, truncated: false, preview_hash: "synthetic-empty-directory", entry_count: 0 });
      if (apiPath.endsWith("/files")) return json(res, { items: [{ name: "empty", full_path: "/owned/empty", is_directory: true, size: 0 }], next_cursor: "" });
      if (apiPath.endsWith("/objects/select")) return json(res, { object: { id: `object-${payload.key}`, key: payload.key, version_id: "version-1", scope_id: payload.scope_id } });
      if (apiPath.endsWith("/objects/version-info")) return json(res, { key: url.searchParams.get("key"), version_id: "version-1", etag: "synthetic-etag", can_delete: true, identity_strength: "strong" });
      if (apiPath.endsWith("/objects")) {
        const prefix = url.searchParams.get("scope_id") === "scope-b" ? "b/" : "a/";
        return json(res, { items: ["first", "second"].map((name) => ({ key: `${prefix}${name}`, size: 1, version_id: "version-1" })), folders: [], next_cursor: "" });
      }
      if (apiPath.endsWith("/buckets")) return json(res, { items: [{ name: "bucket-a" }, { name: "bucket-b" }] });
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
const originalText = Response.prototype.text;
Response.prototype.text = async function () {
  const text = await originalText.call(this);
  window.__contextBodies.push(this.url);
  return text;
};
`;

async function verifyManagementContexts(browser, pageFactory) {
  const fixture = await startMock();
  const results = [];
  async function scenario(name, route, test) {
    let page;
    fixture.state.hold = null;
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
