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
    const selection = {...props,initialTab:"object",sourceObjectIds:["object-a"]};
    await render(selection);
    const objectId = page.getByLabel("Object ID", {exact:true});
    assert.equal(await objectId.inputValue(), "object-a", "initial handoff survives mount reset");
    await render({...selection,sourceObjectIds:["object-b"]});
    assert.equal(await objectId.inputValue(), "object-b", "fresh same-Scope handoff");
    await render({...selection,scopeId:"scope-b",scope:{id:"scope-b",writable:true}});
    assert.equal(await objectId.inputValue(), "", "old selection cannot cross Scope");
    await render({...selection,scopeId:"scope-b",scope:{id:"scope-b",writable:true},sourceObjectIds:["object-c"]});
    assert.equal(await objectId.inputValue(), "object-c", "fresh selection in new Scope");
    assert.deepEqual(errors, []);
    console.log("OPERATIONS-SELECTION-HANDOFF_PASS");
  } finally { await browser?.close(); await server.close(); }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
