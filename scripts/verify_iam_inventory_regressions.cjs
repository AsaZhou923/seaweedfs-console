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
let accessKeyReceipts = 0;
let latestDeleteMarker = false;
const createdCredentials = {access_key:'SYNTHETIC-ONCE-PUBLIC',secret_key:'synthetic-once-secret'};
const serviceAccountId = `sa:alice:${'long-identity-'.repeat(24)}`;
const journal = [
  {id:'create-user',method:'POST',path:'/api/users',target:'username=alice',action:'iam.manage',state:'confirmed'},
  {id:'delete-user',method:'DELETE',path:'/api/users/alice',target:'/api/users/alice',action:'iam.manage',state:'needs_review'},
  {id:'long-operation',method:'PUT',path:'/api/users/alice/policies',target:`username=${'long-target-'.repeat(36)}`,action:'iam.manage',state:'needs_review'},
  {id:'legacy-operation',method:null,path:null,target:null,action:'iam.manage',state:'needs_review'},
];
const asset = {id:'version-object',asset_id:'version-asset',scope_id:'s',key:'versioned.jpg',revision:'synthetic-revision',size_bytes:12,preview_state:'not_ready',properties:{format:'JPEG',width:10,height:10}};
const json = (res, value) => { res.writeHead(200, {'Content-Type':'application/json'}); res.end(JSON.stringify(value)); };
async function until(fn) { for(let i=0;i<200;i++){if(await fn())return;await new Promise(r=>setTimeout(r,25));}throw Error('condition timed out'); }
const server = http.createServer(async(req,res)=>{
  const url = new URL(req.url,'http://localhost'); const p=url.pathname;
  if(p.startsWith('/api/')) {
    let body='';for await(const chunk of req)body+=chunk;
    if(req.method!=='GET')writes.push({p,body:JSON.parse(body||'{}')});
    if(p.endsWith('/management/connections'))return json(res,{items:[{id:'m',name:'Synthetic',management_write_enabled:true,permissions:{'maintenance.execute':true,'iam.manage':true}}]});
    if(p.includes('/iam/')) {
      if(req.method!=='GET'){
        revision++;
        if(p.endsWith('/users/user-new/access-keys')){
          accessKeyReceipts++;
          return json(res,{status:'confirmed',operation_id:'synthetic-access-key-operation',replayed:accessKeyReceipts>1,result:{confirmed:true},secret_receipt_available:accessKeyReceipts===1,...(accessKeyReceipts===1?{created_credentials:createdCredentials}:{})});
        }
        return json(res,{status:'confirmed',operation_id:`operation-${revision}`,replayed:false,result:{confirmed:true}});
      }
      reads[p]=(reads[p]||0)+1;
      return json(res,{items:p.endsWith('/users')?[{username:`inventory-${revision}`}]:p.endsWith('/service-accounts')?[{id:serviceAccountId,parent_user:'alice',access_key_id:'public-synthetic-key',status:'Inactive'}]:[]});
    }
    if(p.endsWith('/auth/me'))return json(res,{user:{username:'tester'},csrf_token:'synthetic',projects:[project]});
    if(p==='/api/v1/projects')return json(res,{items:[project]});
    if(p.endsWith('/scopes'))return json(res,{items:[scope]});
    if(p==='/api/v1/management/m/buckets')return json(res,{items:[{name:'versioning-off',versioning_status:''},{name:'versioning-missing'},{name:'versioning-null',versioning_status:null},{name:'versioning-enabled',versioning_status:'Enabled'},{name:'versioning-suspended',versioning_status:'Suspended'}]});
    if(p==='/api/v1/management/m/operations')return json(res,{items:journal});
    if(p==='/api/v1/management/m/services/health')return json(res,{services:{filer:{status:'healthy',endpoint:'synthetic-filer',source:'mock'},master:{status:'healthy',endpoint:'synthetic-master',source:'mock'}}});
    if(p==='/api/v1/scopes/s/assets')return json(res,{items:[asset],next_cursor:null});
    if(p==='/api/v1/scopes/s/assets/version-asset')return json(res,{asset,objects:[asset],references:[]});
    if(p==='/api/v1/scopes/s/objects/version-object/versions')return json(res,{versions:[{version_id:'current-data',is_latest:!latestDeleteMarker,size:12},{version_id:'older-data',is_latest:false,size:11},{version_id:'delete-marker',is_delete_marker:true,is_latest:latestDeleteMarker}]});
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
 await button('Create access key').click();
 const receipt=page.locator('.summary-card').filter({has:page.getByRole('heading',{name:'Execution result',exact:true})});
 await until(()=>accessKeyReceipts===1);
 await receipt.locator('.summary-raw summary').click();
 const evidence=receipt.locator('.summary-raw pre');
 await until(async()=>await evidence.isVisible()&&(await evidence.innerText()).includes(createdCredentials.secret_key));
 const created=JSON.parse(await evidence.innerText());
 assert.equal(created.status,'confirmed');
 assert.equal(created.replayed,false);
 assert.deepEqual(created.created_credentials,createdCredentials,'first confirmed receipt must expose the exact synthetic key pair');
 assert.equal(created.secret_receipt_available,true);
 await button('Create access key').click();
 await until(()=>accessKeyReceipts===2);
 await until(async()=>{
   if(await evidence.count()!==1)return false;
   const text=await evidence.textContent();
   return text.length>0&&JSON.parse(text).replayed===true;
 });
 await receipt.locator('.summary-raw summary').click();
 const replay=JSON.parse(await evidence.innerText());
 assert.equal(replay.status,'confirmed');
 assert.equal(replay.secret_receipt_available,false);
 assert.ok(!Object.hasOwn(replay,'created_credentials'));
 assert.ok(!(await page.locator('body').innerText()).includes(createdCredentials.secret_key),'replayed receipt must replace the one-time secret in visible content');
 assert.ok(!(await page.locator('body').textContent()).includes(createdCredentials.secret_key),'replayed receipt must remove the one-time secret from raw evidence');
 const statusCases=[];
 async function verifyScrollableTable(table) {
   const evidence=await table.evaluate(element=>{
     const wrap=element.closest('.tableWrap');
     wrap.scrollLeft=wrap.scrollWidth;
     const last=element.querySelector('tbody tr td:last-child').getBoundingClientRect();
     const bounds=wrap.getBoundingClientRect();
     return {overflow:getComputedStyle(wrap).overflowX,scrollable:wrap.scrollWidth>wrap.clientWidth,reachable:last.right<=bounds.right+1,documentOverflow:document.documentElement.scrollWidth-document.documentElement.clientWidth};
   });
   assert.ok(['auto','scroll'].includes(evidence.overflow));
   assert.ok(evidence.scrollable,'populated long table must have horizontal scrolling');
   assert.ok(evidence.reachable,'the final column must be reachable by scrolling');
   assert.ok(evidence.documentOverflow<=1,'table contents must stay inside the document width');
 }
 for(const locale of ['en','zh-CN']) {
   await page.locator('.languageSelector select').selectOption(locale);
   await page.goto(base+'/#buckets');
   const buckets=page.getByRole('table',{name:locale==='en'?'SeaweedFS buckets':'SeaweedFS 桶',exact:true});
   await buckets.waitFor();
   const versioningCell=name=>buckets.locator('tbody tr').filter({hasText:name}).locator('td').nth(4);
   assert.equal(await versioningCell('versioning-off').innerText(),locale==='en'?'Off':'关闭');
   for(const name of ['versioning-missing','versioning-null'])assert.equal(await versioningCell(name).innerText(),locale==='en'?'Unknown':'未知');
   assert.equal(await versioningCell('versioning-enabled').innerText(),locale==='en'?'Enabled':'已启用');
   assert.equal(await versioningCell('versioning-suspended').innerText(),locale==='en'?'Suspended':'已暂停');
   statusCases.push(`${locale}: observed empty versioning is Off, missing/null is Unknown`);

   await page.goto(base+'/#dashboard');
   for(const name of locale==='en'?['Filer endpoint','Master endpoint']:['Filer 端点','Master 端点']) {
     const row=page.locator('.stateRow').filter({has:page.locator('strong').filter({hasText:name})});
     await row.waitFor();
     assert.ok((await row.innerText()).includes(locale==='en'?'Not checked':'未检查'));
     assert.ok((await row.innerText()).includes(locale==='en'?'Open Topology':'拓扑页面'));
   }
   // The caption has its own bilingual catalog entry, so locate by the stable data.
   const operations=page.locator('table').filter({hasText:'create-user'});
   await operations.waitFor();
   assert.ok((await operations.locator('tbody tr').filter({hasText:'create-user'}).innerText()).includes('POST'));
   assert.ok((await operations.locator('tbody tr').filter({hasText:'create-user'}).innerText()).includes('username=alice'));
   assert.ok((await operations.locator('tbody tr').filter({hasText:'delete-user'}).innerText()).includes('DELETE'));
   assert.ok((await operations.locator('tbody tr').filter({hasText:'delete-user'}).innerText()).includes('/api/users/alice'));
   const historical=operations.locator('tbody tr').filter({hasText:'legacy-operation'}).locator('td');
   for(const column of [1,2,3])assert.equal(await historical.nth(column).innerText(),locale==='en'?'Unknown':'未知');
   for(const width of [1280,595,320]) {
     await page.setViewportSize({width,height:950});
     await verifyScrollableTable(operations);
   }
   statusCases.push(`${locale}: operation identity and historical Unknown with reachable responsive columns`);

   await page.goto(base+'/#topology');
   const health=page.locator('table').filter({hasText:'synthetic-filer'});
   await health.waitFor();
   assert.equal(await health.locator('tbody tr').count(),2,'Topology must show the actual probe results');
   for(const endpoint of ['synthetic-filer','synthetic-master'])assert.ok((await health.locator('tbody tr').filter({hasText:endpoint}).locator('td').nth(2).innerText()).toLowerCase().includes(locale==='en'?'healthy':'健康'));
   statusCases.push(`${locale}: Dashboard labels unchecked endpoints while Topology displays healthy probe evidence`);

   await page.goto(base+'/#iam');
   const serviceTable=page.locator('table').filter({hasText:serviceAccountId});
   await serviceTable.waitFor();
   for(const width of [1280,595,320]) {
     await page.setViewportSize({width,height:950});
     await verifyScrollableTable(serviceTable);
   }
   statusCases.push(`${locale}: long service-account identity leaves status column reachable`);

   await page.setViewportSize({width:1280,height:950});
   await page.goto(base+'/#assets');
   await page.locator('.asset').filter({hasText:'versioned.jpg'}).click();
   const drawer=page.locator('.drawer');
   const versionRow=id=>drawer.locator('tbody tr').filter({hasText:id});
   await versionRow('current-data').waitFor();
   assert.equal(await versionRow('current-data').locator('td').nth(1).innerText(),locale==='en'?'latest':'最新');
   assert.equal(await versionRow('older-data').locator('td').nth(1).innerText(),locale==='en'?'version':'版本');
   assert.equal(await versionRow('delete-marker').locator('td').nth(1).innerText(),locale==='en'?'DeleteMarker':'删除标记');
   await drawer.locator('.drawerClose').click();
   latestDeleteMarker=true;
   await page.locator('.asset').filter({hasText:'versioned.jpg'}).click();
   await versionRow('delete-marker').waitFor();
   assert.equal(await versionRow('delete-marker').locator('td').nth(1).innerText(),locale==='en'?'DeleteMarker · latest':'删除标记 · 最新');
   assert.equal(await versionRow('current-data').locator('td').nth(1).innerText(),locale==='en'?'version':'版本');
   assert.equal(await versionRow('delete-marker').locator('a').count(),0,'latest delete marker must have no download link');
   await drawer.locator('.drawerClose').click();
   latestDeleteMarker=false;
   statusCases.push(`${locale}: current data and current delete-marker versions are identified accurately`);
 }
 console.log(JSON.stringify({passed:6+statusCases.length,cases:['IAM user mutations refresh inventories','IAM group/policy mutations refresh inventories','IAM service-account mutations refresh inventories','Populated IAM unknown fields switch English/Chinese','Confirmed access-key credentials are reachable in Raw API evidence','Replayed receipt removes the earlier one-time secret',...statusCases],browser:'sandboxed Chromium',service:'synthetic local HTTP'},null,2));
 }finally{await browser.close();await new Promise(r=>server.close(r));}
})().catch(e=>{console.error(e);server.close();process.exitCode=1;});
