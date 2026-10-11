// Real sandboxed Chromium, synthetic HTTP responses; no live storage writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require(process.env.SWC_PLAYWRIGHT_MODULE || '/opt/codex/runtimes/cua/lib/node_modules/playwright-core');
const dist = process.env.SWC_REGRESSION_DIST || path.join(__dirname, '../frontend/dist');
const project = {id:'p',project_key:'p',display_name:'Project'};
const scope = {id:'s',project_id:'p',connection_id:'c',display_name:'Scope',bucket:'test',prefix:'',writable:true};
const writes = [];
const reads = {};
let revision = 0;
const json = (res, value) => { res.writeHead(200, {'Content-Type':'application/json'}); res.end(JSON.stringify(value)); };
async function until(fn) { for(let i=0;i<200;i++){if(await fn())return;await new Promise(r=>setTimeout(r,25));}throw Error('condition timed out'); }
const server = http.createServer(async(req,res)=>{
  const url = new URL(req.url,'http://localhost'); const p=url.pathname;
  if(p.startsWith('/api/')) {
    let body='';for await(const chunk of req)body+=chunk;
    if(req.method!=='GET')writes.push({p,body:JSON.parse(body||'{}')});
    if(p.endsWith('/management/connections'))return json(res,{items:[{id:'m',name:'Synthetic',management_write_enabled:true,permissions:{'maintenance.execute':true,'iam.manage':true}}]});
    if(p.includes('/iam/')) {
      if(req.method!=='GET'){revision++;return json(res,{status:'confirmed',operation_id:`operation-${revision}`,replayed:false,result:{confirmed:true}});}
      reads[p]=(reads[p]||0)+1;
      return json(res,{items:p.endsWith('/users')?[{username:`inventory-${revision}`}]:[]});
    }
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
 await page.goto(base+'/#iam');await page.getByText('inventory-0',{exact:true}).first().waitFor();
 assert.ok(!(await page.getByRole('table',{name:'S3 IAM users'}).innerText()).includes('未知'));
 const inventories=['users','groups','service-accounts','policies','principals'];
 const button= name=>page.getByRole('button',{name,exact:true});
 const userForm=page.locator('.inlinePanel').filter({has:page.getByRole('heading',{name:'User CRUD / key rotation',exact:true})});
 await userForm.getByLabel('Username',{exact:true}).fill('user-new');
 const groupForm=page.locator('.inlinePanel').filter({has:page.getByRole('heading',{name:'Group CRUD',exact:true})});
 await groupForm.getByLabel('Group',{exact:true}).fill('group-new');
 const policyForm=page.locator('.inlinePanel').filter({has:page.getByRole('heading',{name:'Policy CRUD',exact:true})});
 await policyForm.getByLabel('Policy name',{exact:true}).fill('policy-new');
 const serviceForm=page.locator('.inlinePanel').filter({has:page.getByRole('heading',{name:'Service account CRUD',exact:true})});
 await serviceForm.getByLabel('Parent user',{exact:true}).fill('user-new');
 for(const name of ['Create user','Create group','Create policy','Create service account']) {
   const before={...reads};await button(name).click();
   await until(()=>inventories.every(i=>(reads[`/api/v1/management/m/iam/${i}`]||0)>(before[`/api/v1/management/m/iam/${i}`]||0)));
   await page.getByText(`inventory-${revision}`,{exact:true}).first().waitFor();
 }
 await page.locator('.languageSelector select').selectOption('zh-CN');
 assert.ok((await page.locator('table').filter({hasText:`inventory-${revision}`}).innerText()).includes('未知'));
 await page.locator('.languageSelector select').selectOption('en');
 assert.ok(!(await page.locator('table').filter({hasText:`inventory-${revision}`}).innerText()).includes('未知'));
 console.log(JSON.stringify({passed:4,cases:['IAM user mutations refresh inventories','IAM group/policy mutations refresh inventories','IAM service-account mutations refresh inventories','Populated IAM unknown fields switch English/Chinese'],browser:'sandboxed Chromium',service:'synthetic local HTTP'},null,2));
 }finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
