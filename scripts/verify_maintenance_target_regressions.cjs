// Real sandboxed Chromium, synthetic HTTP responses; no live storage writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.env.SWC_PLAYWRIGHT_MODULE || '/opt/codex/runtimes/cua/lib/node_modules/playwright-core');
const dist = process.env.SWC_REGRESSION_DIST || path.join(__dirname, '../frontend/dist');
const project = {id:'p',project_key:'p',display_name:'Project'};
const scope = {id:'s',project_id:'p',connection_id:'c',display_name:'Scope',bucket:'test',prefix:'',writable:true};
const gates = new Map();
const json = (res, value) => { res.writeHead(200, {'Content-Type':'application/json'}); res.end(JSON.stringify(value)); };
function hold(endpoint) { let release; const promise = new Promise(r => release=r); const gate={promise,release,entered:false}; gates.set(endpoint,gate); return gate; }
async function until(fn) { for(let i=0;i<200;i++){if(await fn())return;await new Promise(r=>setTimeout(r,25));}throw Error('condition timed out'); }
const server = http.createServer(async(req,res)=>{
  const url = new URL(req.url,'http://localhost'); const p=url.pathname;
  if(p.startsWith('/api/')) {
    let body='';for await(const chunk of req)body+=chunk;
    const gate=gates.get(p);if(gate){gate.entered=true;await gate.promise;}
    if(p.endsWith('/management/connections'))return json(res,{items:[{id:'m',name:'Synthetic',management_write_enabled:true,permissions:{'maintenance.execute':true}}]});
    if(p.includes('/maintenance/job-types/'))return json(res,{config:{status:'supported',data:{marker:p.split('/').at(-1)}}});
    if(p.includes('/maintenance/jobs/'))return json(res,{source:p.split('/').at(-1)});
    if(p.endsWith('/auth/me'))return json(res,{user:{username:'tester'},csrf_token:'synthetic',projects:[project]});
    if(p==='/api/v1/projects')return json(res,{items:[project]});
    if(p.endsWith('/scopes'))return json(res,{items:[scope]});
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
 try{
 await page.goto(base+'/#maintenance');
 const jobType=page.getByLabel('Job type',{exact:true});const load=page.getByRole('button',{name:'Load config editor',exact:true});
 await jobType.fill('TYPE_A');const typeGate=hold('/api/v1/management/m/maintenance/job-types/TYPE_A');
 await page.getByRole('button',{name:'Read JobType details',exact:true}).click();await until(()=>typeGate.entered);
 await jobType.fill('TYPE_B');assert.ok(await load.isDisabled());
 await page.getByRole('button',{name:'Read JobType details',exact:true}).click();await until(async()=>await load.isEnabled());
 typeGate.release();await page.waitForTimeout(100);await load.click();
 assert.equal(await jobType.inputValue(),'TYPE_B');assert.ok((await page.locator('textarea').inputValue()).includes('TYPE_B'));
 await jobType.fill('TYPE_C');assert.ok(await load.isDisabled());assert.equal(await page.locator('textarea').inputValue(),'{}');
 const job=page.getByLabel('Job ID',{exact:true});await job.fill('JOB_A');const jobGate=hold('/api/v1/management/m/maintenance/jobs/JOB_A');
 await page.getByRole('button',{name:'Read Job ID details',exact:true}).click();await until(()=>jobGate.entered);
 await job.fill('JOB_B');await page.getByRole('button',{name:'Read Job ID details',exact:true}).click();
 await page.getByText('JOB_B',{exact:true}).first().waitFor();jobGate.release();await page.waitForTimeout(100);
 assert.ok(!(await page.locator('.managementPage').innerText()).includes('JOB_A'));
 console.log(JSON.stringify({passed:3,cases:['JobType reversed responses preserve B config','JobType change clears prior copied config','Job ID reversed responses preserve B detail'],browser:'sandboxed Chromium',service:'synthetic local HTTP'},null,2));
 }finally{for(const g of gates.values())g.release();await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
