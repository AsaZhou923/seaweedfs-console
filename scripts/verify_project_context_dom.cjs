// DOM + real loopback HTTP verification; this does not replace browser coverage.
const { createDomHarness } = require("./lib/console_dom_harness.cjs");
const { startServer, closeServer, verifyProjectContextRaces } = require("./verify_frontend_review_regressions.cjs");

async function main() {
  let server;
  let harness;
  try {
    server = await startServer();
    harness = await createDomHarness();
    const result = await verifyProjectContextRaces(null, `http://127.0.0.1:${server.address().port}`, harness.createPage);
    console.log(`PROJECT_CONTEXT_DOM_PASS ${result.cases} cases; real React DOM with held/reversed HTTP responses. Chromium and layout are not covered.`);
  } finally {
    await harness?.close();
    await closeServer(server);
  }
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
