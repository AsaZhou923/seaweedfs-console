// Dependency-free locale and SSR regression checks using the existing Vite/React toolchain.
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '..');
const localRequire = createRequire(path.join(root, 'frontend/package.json'));
const React = localRequire('react');
const { renderToStaticMarkup } = localRequire('react-dom/server');

async function main() {
  const { createServer } = await import(pathToFileURL(localRequire.resolve('vite')).href);
  const { parseSync } = await import(pathToFileURL(localRequire.resolve('rolldown/utils')).href);
  const server = await createServer({
    root: path.join(root, 'frontend'), appType: 'custom',
    server: { middlewareMode: true, hmr: false, ws: false, watch: null },
    plugins: [{ name: 'verify-i18n-private-views', enforce: 'pre', transform(source, id) {
      if (id.endsWith('/src/main.tsx')) {
        return source.replace(/createRoot\(document\.getElementById\("root"\)!\)\.render\(<App \/>\);/, '') +
          '\nexport { App, Login, Assets, Scans, Diagnostics, Operations, Presets, Settings };';
      }
      if (id.endsWith('/management/ManagementViews.tsx')) return source + '\nexport { StateRow, TablePreviewPanel, ServiceInventory, DependencyStates, ManagementConnectionTable };';
    } }],
  });
  try {
    const i18n = await server.ssrLoadModule('/src/i18n/index.ts');
    const views = await server.ssrLoadModule('/src/main.tsx');
    const management = await server.ssrLoadModule('/src/management/ManagementViews.tsx');
    const { ResultSummary } = await server.ssrLoadModule('/src/components/ResultSummary.tsx');
    const { LanguageSelector } = await server.ssrLoadModule('/src/i18n/LanguageSelector.tsx');
    assert.equal(i18n.getLocale(), 'en');
    for (const invalid of [null, undefined, '', 'zh', 'ja', 'en-US', 'ZH-CN', {}]) assert.equal(i18n.resolveLocale(invalid), 'en');
    assert.equal(i18n.resolveLocale('zh-CN'), 'zh-CN');
    const placeholders = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
    for (const [key, message] of Object.entries(i18n.messages)) {
      assert.ok(message.en?.trim(), `Missing English: ${key}`);
      assert.ok(message['zh-CN']?.trim(), `Missing Chinese: ${key}`);
      assert.doesNotMatch(message.en, /\p{Script=Han}/u, `Chinese leaks into English: ${key}`);
      assert.deepEqual(placeholders(message.en), placeholders(message['zh-CN']), `Placeholder mismatch: ${key}`);
      const words = message.en.replace(/\{\w+\}/g, '').match(/[a-zA-Z]+/g) || [];
      if (words.length >= 4) assert.notEqual(message.en, message['zh-CN'], `Untranslated Chinese prose: ${key}`);
    }
    for (const filename of ['main.tsx', 'management/ManagementViews.tsx', 'components/ResultSummary.tsx']) {
      const source = fs.readFileSync(path.join(root, 'frontend/src', filename), 'utf8');
      const parsed = parseSync(filename, source);
      assert.equal(parsed.errors.length, 0, `Syntax errors in ${filename}`);
      const dynamicKeys = new Set();
      function collectStrings(node) {
        if (!node || typeof node !== 'object') return;
        if (node.type === 'Literal' && typeof node.value === 'string') dynamicKeys.add(node.value);
        Object.values(node).forEach((child) => Array.isArray(child) ? child.forEach(collectStrings) : child && typeof child === 'object' && collectStrings(child));
      }
      function visit(node, parent) {
        if (!node || typeof node !== 'object') return;
        if (node.type === 'FunctionDeclaration' && node.id?.name === 'stateLabel') collectStrings(node.body);
        if (node.type === 'JSXAttribute' && ['Metric', 'StateRow', 'DataPanel', 'DataTable', 'EvidenceDetails', 'TopologyGroup'].includes(parent?.name?.name)) {
          if (['label', 'title', 'caption'].includes(node.name.name) && typeof node.value?.value === 'string') dynamicKeys.add(node.value.value);
          if (node.name.name === 'columns' && node.value?.expression?.type === 'ArrayExpression') {
            node.value.expression.elements.forEach((column) => { if (typeof column?.value === 'string') dynamicKeys.add(column.value); });
          }
        }
        if (node.type === 'FunctionDeclaration' && ['ServiceInventory', 'DependencyStates'].includes(node.id?.name)) {
          for (const statement of node.body.body) {
            for (const declaration of statement.declarations || []) {
              if (declaration.id.name === 'rows') declaration.init.elements.forEach((row) => { if (typeof row.elements?.[0]?.value === 'string') dynamicKeys.add(row.elements[0].value); });
              if (declaration.id.name === 'dependencies') declaration.init.elements.forEach((row) => row.properties.forEach((property) => { if (property.key.name === 'name' && typeof property.value.value === 'string') dynamicKeys.add(property.value.value); }));
            }
          }
        }
        if (node.type === 'CallExpression' && node.callee.type === 'Identifier' && node.callee.name === 't' && typeof node.arguments[0]?.value === 'string') {
          const key = node.arguments[0].value;
          assert.ok(Object.hasOwn(i18n.messages, key), `Missing translation in ${filename}: ${key}`);
        }
        if (node.type === 'JSXText') assert.doesNotMatch(node.value, /\p{Script=Han}/u, `Hardcoded JSX copy in ${filename}: ${node.value.trim()}`);
        if (node.type === 'JSXAttribute' && ['title', 'placeholder', 'aria-label', 'alt'].includes(node.name.name) && typeof node.value?.value === 'string') {
          assert.doesNotMatch(node.value.value, /\p{Script=Han}/u, `Hardcoded accessible copy in ${filename}: ${node.value.value}`);
        }
        if (node.type === 'JSXElement' && node.openingElement.name.name === 'option') {
          const hasValue = node.openingElement.attributes.some((attr) => attr.type === 'JSXAttribute' && attr.name.name === 'value');
          const translatedLabel = node.children.some((child) => child.type === 'JSXExpressionContainer' && child.expression.type === 'CallExpression' && child.expression.callee.name === 't');
          assert.ok(hasValue || !translatedLabel, `Translated option changes its wire value in ${filename}: ${source.slice(node.start, node.end)}`);
        }
        for (const child of Object.values(node)) {
          if (Array.isArray(child)) child.forEach((value) => visit(value, node));
          else if (child && typeof child === 'object') visit(child, node);
        }
      }
      visit(parsed.program);
      const missingDynamic = [...dynamicKeys].filter((key) => !Object.hasOwn(i18n.messages, key));
      assert.deepEqual(missingDynamic, [], `Missing owned labels in ${filename}`);
    }
    assert.equal(i18n.t('unknown.translation.key'), 'unknown.translation.key');
    const mockRequest = async () => ({ items: [] });
    const noop = () => {};
    const common = { request: mockRequest, scopeId: '', projectId: '', projects: [], connections: [],
      managementConnections: [], refresh: noop, refreshManagementConnections: noop, setProjectId: noop,
      onChanged: noop, onSelectionChange: noop, sendToOperationsCopy: noop, sendToPresets: noop,
      initialObjectIds: [], sourceObjectIds: [], initialTab: 'copy' };
    const cases = [
      ['login', views.Login, { request: mockRequest, refresh: noop, onAuthenticated: noop, error: '' }],
      ...['Assets', 'Scans', 'Diagnostics', 'Operations', 'Presets', 'Settings'].map((name) => [name, views[name], common]),
      ['management setup', management.ManagementRoute, { ...common, managementId: '', route: 'dashboard', s3Connections: [] }],
      ...['dashboard', 'topology', 'storage', 'buckets', 'files', 'objects', 'iam', 'maintenance', 'services'].map((route) =>
        [route, management.ManagementRoute, { ...common, managementId: 'test-management', route, s3Connections: [] }]),
      ['jobs result', ResultSummary, { data: [{ id: 'job-1', kind: 'scan', state: 'running', processed: 1234, total: null, errors: 0 }] }],
      ['capacity result', ResultSummary, { data: { current_logical_bytes: 1024, object_count: 2, coverage: 'unknown', by_format: { jpeg: 1024 } } }],
      ['duplicates result', ResultSummary, { data: { groups: [], eligible_count: 5, covered_count: 0, unscanned_count: 5 } }],
      ['generic result', ResultSummary, { data: { sample_complete: false, physical_disk_bytes: null, unknown_external_relations: true } }],
      ['loaded service inventory', management.ServiceInventory, { data: {} }],
      ['loaded dependencies', management.DependencyStates, { data: {} }],
      ['loaded management connections', management.ManagementConnectionTable, { items: [{ id: 'fixture', name: 'Fixture connection', admin_url: 'http://fixture.invalid' }], selectedId: 'fixture', onSelect: noop }],
      ['loaded table preview', management.TablePreviewPanel, { data: { status: 'supported', preview_kind: 'raw_file_sample', columns: [{ name: 'id', type: 'int64' }], rows: [{ id: '9223372036854775807' }] } }],
    ];
    let rendered = 0;
    const untranslatedViews = [];
    for (const locale of ['en', 'zh-CN']) {
      i18n.setLocale(locale);
      assert.equal(i18n.getLocale(), locale);
      assert.equal(i18n.t('Language'), locale === 'en' ? 'Language' : '语言');
      const invalidLogin = i18n.translateMessage('UNAUTHENTICATED: Invalid username or password.');
      assert.equal(i18n.translateMessage('UPSTREAM_NEW_CODE: Language'), 'UPSTREAM_NEW_CODE: Language');
      assert.ok(invalidLogin.startsWith('UNAUTHENTICATED: '));
      assert.equal(invalidLogin, locale === 'en' ? 'UNAUTHENTICATED: Invalid username or password.' : 'UNAUTHENTICATED: ' + i18n.t('Invalid username or password.'));
      for (const [name, Component, props] of cases) {
        assert.equal(typeof Component, 'function', `Missing test component ${name}`);
        const html = renderToStaticMarkup(React.createElement(Component, props));
        assert.ok(html.length, `${name} rendered empty in ${locale}`);
        if (locale === 'en') {
          const owned = html.replace(/<pre\b[^>]*>[\s\S]*?<\/pre>/g, '').replaceAll('简体中文', '');
          const leaks = owned.match(/[^<>]*\p{Script=Han}[^<>]*/gu) || [];
          if (leaks.length) untranslatedViews.push({ name, leaks });
        }
        rendered++;
      }
      const selector = renderToStaticMarkup(React.createElement(LanguageSelector));
      assert.match(selector, /value="en"/);
      assert.match(selector, /value="zh-CN"/);
      for (const value of ['Key', 'Name', 'Unknown', 'Status', 'Source']) {
        const row = renderToStaticMarkup(React.createElement(management.StateRow, { label: 'Bucket', value, state: 'reported' }));
        assert.ok(row.includes(`<span>${value}</span>`), `Opaque StateRow value translated: ${locale} ${value}`);
      }
      const schema = ['Name', 'Status', 'Key', 'Version', 'Unknown'];
      const table = renderToStaticMarkup(React.createElement(management.TablePreviewPanel, { data: {
        status: 'supported', preview_kind: 'raw_file_sample', columns: schema.map((name) => ({ name, type: 'string' })),
        rows: [Object.fromEntries(schema.map((name) => [name, 'Key']))],
      } }));
      for (const name of schema) assert.ok(table.includes(`<th>${name}</th>`), `Raw schema name translated: ${locale} ${name}`);
      assert.equal((table.match(/<td>Key<\/td>/g) || []).length, schema.length);
    }
    assert.deepEqual(untranslatedViews, [], 'Untranslated English views');
    // Raw evidence must remain byte-for-byte in the API language, including user content.
    const raw = { user_key: '用户/原始对象.jpg', message: '原始证据', state: 'running' };
    i18n.setLocale('en');
    const evidence = renderToStaticMarkup(React.createElement(ResultSummary, { data: raw }));
    assert.ok(evidence.includes('用户/原始对象.jpg'));
    assert.ok(evidence.includes('原始证据'));
    assert.equal(i18n.translateMessage('UPSTREAM_NEW_CODE: opaque upstream detail'), 'UPSTREAM_NEW_CODE: opaque upstream detail');
    // A stored source message changes language without being overwritten.
    const stored = 'UNAUTHENTICATED: Invalid username or password.';
    i18n.setLocale('zh-CN');
    assert.notEqual(i18n.translateMessage(stored), stored);
    i18n.setLocale('en');
    assert.equal(i18n.translateMessage(stored), stored);
    assert.match(fs.readFileSync(path.join(root, 'frontend/index.html'), 'utf8'), /<html lang="en">/);
    console.log(`I18N_PASS ${Object.keys(i18n.messages).length} bilingual messages; ${rendered} SSR views; default/fallback, interpolation, errors and raw evidence verified.`);
  } finally {
    await server.close();
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
