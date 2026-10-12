// Real sandboxed Chromium against an isolated Operations component and synthetic requests.
// Set PLAYWRIGHT_MODULE and CHROMIUM_PATH if they are not available at the defaults.
const assert = require("node:assert/strict");
const path = require("node:path");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");
const root = path.resolve(__dirname, "..");
const frontendRequire = createRequire(path.join(root, "frontend/package.json"));
async function main() {
  const { createServer } = await import(pathToFileURL(frontendRequire.resolve("vite")).href);
  const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright-core");
  const server = await createServer({ root: path.join(root, "frontend"), configFile: false,
    server: { host: "127.0.0.1", port: 0, strictPort: false },
    plugins: [{ name: "operations-fixture", enforce: "pre", transform(source, id) {
      if (id.endsWith("/src/main.tsx")) return source.replace(/createRoot\(document\.getElementById\("root"\)!\)\.render\(<App \/>\);/, "") + "\nexport { Operations };";
    }, configureServer(dev) { dev.middlewares.use((req, res, next) => {
      if (req.url !== "/__operations_test") return next();
      res.setHeader("Content-Type", "text/html");
      res.end(`<link rel="icon" href="data:,"><div id="fixture"></div><script type="module">
        import React from '/node_modules/.vite/deps/react.js';
        import ReactDOM from '/node_modules/.vite/deps/react-dom_client.js';
        const { createRoot } = ReactDOM;
        import { Operations } from '/src/main.tsx';
        const root = createRoot(document.getElementById('fixture'));
        window.calls = [];
        const request = async (url, init = {}) => { window.calls.push({url, method: init.method}); return {id:'mock-upload'}; };
        window.renderOperations = props => root.render(React.createElement(Operations, {...props, request}));
      </script>`);
    }); } }], optimizeDeps: { include: ["react", "react-dom/client"] },
  });
  let browser;
  try {
    await server.listen();
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || "/usr/bin/chromium", headless: true, chromiumSandbox: true });
    const page = await browser.newPage();
    const errors = []; page.on("pageerror", error => { errors.push(error.message); console.error(error.message); });
    page.on("console", message => { if (message.type() === "error") console.error(message.text()); });
    await page.goto(`http://127.0.0.1:${server.httpServer.address().port}/__operations_test`);
    await page.waitForFunction(() => typeof window.renderOperations === "function");
    const render = async props => { await page.evaluate(props => window.renderOperations(props), props); await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))); };
    const props = {scopeId:"scope-a", scope:{id:"scope-a",writable:false},sourceObjectIds:[], initialTab:"upload"};
    await render(props);
    await page.getByLabel("Target Key", {exact:true}).fill("owned/test");
    await page.getByLabel("Upload ID", {exact:true}).fill("mock-upload");
    await page.locator('input[type="file"]').setInputFiles({name:"part.txt",mimeType:"text/plain",buffer:Buffer.from("synthetic")});
    const names = ["Initialize", "Upload part", "Complete", "Abort"];
    for (const name of names) {
      const button = page.getByRole("button", {name,exact:true});
      assert.equal(await button.isDisabled(), true, `read-only ${name}`);
      assert.equal(await button.getAttribute("title"), "The current scope does not have writable enabled");
      await button.evaluate(node => node.click());
    }
    assert.equal(await page.evaluate(() => window.calls.length), 0);
    await render({...props,scope:{...props.scope,writable:true}});
    for (const name of names) {
      const button = page.getByRole("button", {name,exact:true});
      assert.equal(await button.isEnabled(), true, `writable ${name}`);
      await button.click();
    }
    assert.equal(await page.evaluate(() => window.calls.length), 4);
    await render({...props,scopeId:"",scope:undefined});
    for (const name of names) assert.equal(await page.getByRole("button", {name,exact:true}).isDisabled(), true);
    assert.equal(await page.evaluate(() => window.calls.length), 4);
    assert.deepEqual(errors, []);
    console.log("MULTIPART-READONLY-UI_PASS");
  } finally { await browser?.close(); await server.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
