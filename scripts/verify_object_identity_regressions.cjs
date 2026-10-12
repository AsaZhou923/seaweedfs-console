// Real sandboxed Chromium, synthetic HTTP responses; no live storage writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.env.SWC_PLAYWRIGHT_MODULE || '/opt/codex/runtimes/cua/lib/node_modules/playwright-core');
const dist = process.env.SWC_REGRESSION_DIST || path.join(__dirname, '../frontend/dist');
const project = {id:'p',project_key:'p',display_name:'Project'};
const scope = {id:'s',project_id:'p',connection_id:'c',display_name:'Scope',bucket:'test',prefix:'',writable:true};
const assets = ['a','b'].map(id => ({id,asset_id:`asset-${id}`,scope_id:'s',key:`photo-${id}.jpg`,revision:'1',properties:{},size_bytes:10}));
const gates = new Map();
const writes = [];
const json = (res, value) => { res.writeHead(200, {'Content-Type':'application/json'}); res.end(JSON.stringify(value)); };
function hold(endpoint) { let release; const promise = new Promise(r => release=r); const gate={promise,release,entered:false}; gates.set(endpoint,gate); return gate; }
async function until(fn) { for(let i=0;i<200;i++){if(await fn())return;await new Promise(r=>setTimeout(r,25));}throw Error('condition timed out'); }
const server = http.createServer(async(req,res)=>{
  const url = new URL(req.url,'http://localhost'); const p=url.pathname;
  if(p.startsWith('/api/')) {
    let body='';for await(const chunk of req)body+=chunk;
    if(req.method!=='GET')writes.push({p,body:JSON.parse(body||'{}')});
    const gate=gates.get(p);if(gate){gate.entered=true;await gate.promise;}
    if(p.endsWith('/auth/me'))return json(res,{user:{username:'tester'},csrf_token:'synthetic',projects:[project]});
    if(p==='/api/v1/projects')return json(res,{items:[project]});
    if(p.endsWith('/scopes'))return json(res,{items:[scope]});
    if(p.endsWith('/assets'))return json(res,{items:assets});
    if(p.includes('/assets/asset-'))return json(res,{marker:`EVIDENCE_${p.slice(-1).toUpperCase()}`});
    if(p.endsWith('/versions'))return json(res,{versions:[{version_id:`VERSION_${p.split('/').at(-2).toUpperCase()}`,size:10}]});
    return json(res,{items:[],ok:true});
  }
  const file=path.join(dist,p==='/'?'index.html':p);
  if(!file.startsWith(path.resolve(dist)+path.sep)){res.writeHead(403);return res.end();}
  try{res.setHeader('Content-Type',file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'text/html');res.end(fs.readFileSync(file));}catch{res.writeHead(404);res.end();}
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({executablePath:process.env.SWC_CHROME_PATH||'/usr/bin/chromium',headless:true,chromiumSandbox:true});
 const page=await browser.newPage();page.setDefaultTimeout(5000);
 const base=`http://127.0.0.1:${server.address().port}`;
 try {
  if (process.env.SWC_IDENTITY_CASE !== 'operations') {
  await page.goto(base+'/#assets');await page.locator('article.asset').first().waitFor();
  const info=hold('/api/v1/scopes/s/assets/asset-a');const versions=hold('/api/v1/scopes/s/objects/a/versions');
  await page.getByText('photo-a.jpg',{exact:true}).first().click();await until(()=>info.entered&&versions.entered);
  await page.getByText('photo-b.jpg',{exact:true}).first().click({force:true});
  await page.locator('.drawer').getByText('VERSION_B',{exact:true}).waitFor();
  info.release();versions.release();await page.waitForTimeout(150);
  const drawer=page.locator('.drawer');assert.ok(!(await drawer.innerText()).includes('VERSION_A'));
  assert.ok(!(await drawer.innerText()).includes('EVIDENCE_A'));
  assert.ok((await drawer.locator('a.buttonLink').getAttribute('href')).includes('/objects/b/download?version_id=VERSION_B'));
  page.once('dialog',dialog=>dialog.accept('restore-b.jpg'));await drawer.getByRole('button',{name:'Restore to new key',exact:true}).click();
  await until(()=>writes.some(w=>w.p.endsWith('/b/restore')));assert.equal(writes.at(-1).body.version_id,'VERSION_B');
  }
  gates.clear();
  await page.goto(base+'/#operations');await page.getByRole('button',{name:'Versions / Tags / Retention',exact:true}).click();
  const object=page.getByLabel('Object ID',{exact:true});const selected=page.getByLabel('Version ID',{exact:true});
  await object.fill('a');const old=hold('/api/v1/scopes/s/objects/a/versions');
  await page.getByRole('button',{name:'Read versions',exact:true}).click();await until(()=>old.entered);
  await object.fill('b');await page.getByRole('button',{name:'Read versions',exact:true}).click();
  await page.getByRole('button',{name:'Use this version',exact:true}).click();assert.equal(await selected.inputValue(),'VERSION_B');
  old.release();await page.waitForTimeout(150);assert.ok(!(await page.locator('.imageWorkbench').innerText()).includes('VERSION_A'));
  await page.getByLabel('Restore target Key',{exact:true}).fill('new-b.jpg');
  for(const name of ['Restore to new key','Write Retention','Write Legal Hold'])await page.getByRole('button',{name,exact:true}).click();
  await until(()=>writes.filter(w=>w.p.includes('/objects/b/')).length===(process.env.SWC_IDENTITY_CASE === 'operations' ? 3 : 4));
  assert.ok(writes.filter(w=>w.p.includes('/objects/b/')).every(w=>w.body.version_id==='VERSION_B'));
  await object.fill('a');assert.equal(await selected.inputValue(),'');
  for(const name of ['Restore to new key','Write Retention','Write Legal Hold'])assert.ok(await page.getByRole('button',{name,exact:true}).isDisabled());
  assert.equal(await page.getByRole('button',{name:'Use this version',exact:true}).count(),0);
  console.log(JSON.stringify({passed:process.env.SWC_IDENTITY_CASE === 'operations' ? 2 : 4,cases:['Detail reversed evidence/version responses','Detail download and restore bind B version','Operations reversed versions bind restore/retention/legal-hold','Object change clears selection and disables version mutations'],browser:'sandboxed Chromium',service:'synthetic local HTTP'},null,2));
 } finally {for(const g of gates.values())g.release();await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
