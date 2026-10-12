// Optional DOM-only fallback. Install jsdom outside the application and set
// SWC_JSDOM_ROOT to that install directory. This is not Chromium/layout evidence.
const path = require("node:path");
const { createRequire } = require("node:module");
const { pathToFileURL } = require("node:url");

const root = path.resolve(__dirname, "../..");
const localRequire = createRequire(path.join(root, "frontend/package.json"));
const nativeFetch = globalThis.fetch;
const NativeResponse = globalThis.Response;
const responseText = NativeResponse.prototype.text;

async function createDomHarness(options = {}) {
  if (!process.env.SWC_JSDOM_ROOT) throw new Error("Set SWC_JSDOM_ROOT to a separate jsdom installation; do not add it to application dependencies.");
  const testRequire = createRequire(path.join(path.resolve(process.env.SWC_JSDOM_ROOT), "package.json"));
  const { JSDOM } = testRequire("jsdom");
  let activePage;
  let React;
  let createRoot;
  let views;
  const installWindow = (dom) => {
    const window = dom.window;
    for (const key of ["window", "document", "location", "localStorage", "sessionStorage", "HTMLElement", "Element", "HTMLSelectElement", "HTMLInputElement", "HTMLTextAreaElement", "Event", "MouseEvent", "CustomEvent", "FormData", "File", "Blob"]) {
      globalThis[key] = key === "window" ? window : window[key];
    }
    Object.defineProperty(globalThis, "navigator", { configurable: true, value: window.navigator });
    globalThis.addEventListener = window.addEventListener.bind(window);
    globalThis.removeEventListener = window.removeEventListener.bind(window);
    globalThis.requestAnimationFrame = window.requestAnimationFrame.bind(window);
    globalThis.cancelAnimationFrame = window.cancelAnimationFrame.bind(window);
    Object.defineProperty(window.HTMLElement.prototype, "innerText", {
      configurable: true, get() { return this.textContent; }, set(value) { this.textContent = value; },
    });
    window.Response = NativeResponse;
    window.Headers = globalThis.Headers;
    const fetch = (input, init) => nativeFetch(new URL(input, window.location.href), init);
    window.fetch = fetch;
    globalThis.fetch = fetch;
  };
  const initial = new JSDOM("<div id='root'></div>", { url: "http://127.0.0.1/", pretendToBeVisual: true, runScripts: "outside-only" });
  installWindow(initial);
  React = localRequire("react");
  ({ createRoot } = localRequire("react-dom/client"));
  const { createServer } = await import(pathToFileURL(localRequire.resolve("vite")).href);
  const server = await createServer({
    root: path.join(root, "frontend"), appType: "custom",
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
    plugins: [{ name: "verify-dom-private-components", enforce: "pre", transform(source, id) {
      if (id.endsWith("/src/main.tsx")) return source.replace(/createRoot\(document\.getElementById\("root"\)!\)\.render\(<App \/>\);/, "") + "\nexport { App, Assets, Operations, Scans, Diagnostics, Presets };";
      if (id.endsWith("/management/ManagementViews.tsx") && options.managementExports?.length) {
        return source + `\nexport { ${options.managementExports.join(", ")} };`;
      }
    } }],
  });
  views = await server.ssrLoadModule("/src/main.tsx");
  const management = await server.ssrLoadModule("/src/management/ManagementViews.tsx");

  async function createPage(_browser, url, component = views.App, props = {}) {
    activePage?.close();
    const dom = new JSDOM("<!doctype html><html lang='en'><body><div id='root'></div></body></html>", { url, pretendToBeVisual: true, runScripts: "outside-only" });
    installWindow(dom);
    const reactRoot = createRoot(dom.window.document.getElementById("root"));
    let closed = false;
    reactRoot.render(React.createElement(component, props));
    const page = {
      window: dom.window,
      eval: async (expression) => dom.window.eval(expression),
      render: (Component, nextProps) => reactRoot.render(React.createElement(Component, nextProps)),
      send: async (method, params) => {
        if (method !== "Page.navigate") throw new Error(`DOM harness does not implement ${method}`);
        const next = new URL(params.url);
        if (next.origin !== dom.window.location.origin || next.pathname !== dom.window.location.pathname) throw new Error("DOM harness supports same-document hash navigation only.");
        dom.window.location.hash = next.hash;
      },
      close: () => {
        if (closed) return;
        closed = true;
        reactRoot.unmount();
        NativeResponse.prototype.text = responseText;
        dom.window.close();
        if (activePage === page) activePage = null;
      },
    };
    activePage = page;
    return page;
  }
  return {
    createPage, views, management, React,
    close: async () => {
      activePage?.close();
      initial.window.close();
      NativeResponse.prototype.text = responseText;
      globalThis.fetch = nativeFetch;
      await server.close();
    },
  };
}

module.exports = { createDomHarness };
