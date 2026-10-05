// Render the real component against the backend column DTO without new test deps.
const path = require('node:path');
const assert = require('node:assert/strict');
const { createRequire } = require('node:module');
const { pathToFileURL } = require('node:url');
const root = path.resolve(__dirname, '..');
const localRequire = createRequire(path.join(root, 'frontend', 'package.json'));
const React = localRequire('react');
const { renderToStaticMarkup } = localRequire('react-dom/server');
async function main() {
  const { createServer } = await import(pathToFileURL(localRequire.resolve('vite')).href);
  const server = await createServer({
    root: path.join(root, 'frontend'), server: { middlewareMode: true, hmr: false, watch: null }, appType: 'custom',
    plugins: [{ name: 'verify-private-component', enforce: 'pre', transform(source, id) {
      if (id.endsWith('/management/ManagementViews.tsx')) return source + '\nexport { TablePreviewPanel, ServiceHealthTable };\n';
    } }],
  });
  try {
  const { TablePreviewPanel, ServiceHealthTable } = await server.ssrLoadModule('/src/management/ManagementViews.tsx');
  const markup = renderToStaticMarkup(React.createElement(TablePreviewPanel, { data: {
    status: 'supported', preview_kind: 'raw_file_sample', snapshot_id: '9223372036854775807',
    columns: [{ name: 'id', type: 'int64', nullable: false }, { name: 'name', type: 'string', nullable: true }],
    rows: [{ id: '9223372036854775807', name: 'sample-value' }], files: [], total_rows: null,
    deletes_applied: false, sample_truncated: true, file_list_complete: true, has_delete_files: false,
  } }));
  assert.match(markup, /<th>id<\/th>/);
  assert.match(markup, /<th>name<\/th>/);
  assert.match(markup, /<td>sample-value<\/td>/);
  assert.match(markup, /<td>9223372036854775807<\/td>/);
  assert.doesNotMatch(markup, /\[object Object\]/);
  const health = renderToStaticMarkup(React.createElement(ServiceHealthTable, { data: {
    services: { master: { status: 'healthy', endpoint: 'http://master.local', is_leader: true,
      observations: [{ version: { value: '30GB 4.48 source-pin', source_field: 'Version' } }] },
      s3: { status: 'healthy', endpoint: 'http://s3.local', version: '4.48' } },
  } }));
  assert.match(health, /<td>30GB 4\.48 source-pin<\/td>/);
  assert.match(health, /<td>4\.48<\/td>/);
  assert.doesNotMatch(health, /\[object Object\]/);
  console.log('TABLE_PREVIEW_UI_PASS real column DTO and int64 rows render correctly.');
  } finally {
    await server.close();
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
