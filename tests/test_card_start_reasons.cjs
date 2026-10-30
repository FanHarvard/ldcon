// Offline browser regression: all requests intercepted, no control service access.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets'),read=n=>fs.readFileSync(path.join(assets,n),'utf8');
(async()=>{const browser=await chromium.launch({headless:true});try{for(const width of [1440,600]){
 const page=await browser.newPage({viewport:{width,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{localStorage.setItem('ldcon.language','en');window.setInterval=()=>0;});
 const data=structuredClone(fixture.data()),map={id:'mode-map',operation:'study.ecdl_mode_map',uses:[],parameters:{write_report:true}};
 for(const deck of new Set([data['sets/fixture'].deck_definitions[0],data['configuration/interface-decks/ecdl']])){deck.document.cards.push(map);deck.card_status['mode-map']={runnable:true,blocked_reasons:[],parameters:map.parameters};}
 await page.route('**/*',async route=>{
  const u=new URL(route.request().url()),asset=u.pathname.split('/assets/')[1];
  if(asset)return route.fulfill({body:read(asset),contentType:asset.endsWith('.css')?'text/css':'text/javascript'});
  if(!u.pathname.includes('/api/'))return route.fulfill({body:read('index.html'),contentType:'text/html'});
  const q=u.pathname.split('/api/')[1]+u.search;assert(route.request().method()==='GET'||q.endsWith('/validate'),'Operational request forbidden');let v=data[q]||{};
  if(q==='dashboard/snapshot')v={jobs:[fixture.job()],exports:[],wavelengths:{}};
  if(q.startsWith('jobs?'))v={jobs:[{id:'b'.repeat(32),set_id:'fixture',status:'succeeded',cards:[map],available_artifacts:['mode-map.json'],pdf_available:true}]};
  if(q.startsWith('history?'))v={jobs:[],total:0};
  if(q.endsWith('/operating-mode'))v={mode:'measurement',revision:1};
  if(q.endsWith('/adjustment-guidance'))v={revision:1,document:{knobs:{}}};
  if(q.endsWith('/validate'))v={runnable:true,blocked_reasons:[]};
  await route.fulfill({contentType:'application/json',body:JSON.stringify(v)});
 });
 await page.goto('http://ldcon.test/?set=fixture#laser-settings');await page.waitForFunction(()=>!state.busy&&!polling&&document.querySelector('[data-start-card="mode-map"]'));
 const start=page.locator('[data-start-card="mode-map"]'),status=page.locator('[data-card-status="mode-map"]');
 assert(await start.isDisabled());assert.match(await status.textContent(),/Blocked/);assert.match(await status.textContent(),/Control slot in use/);assert.match(await start.getAttribute('title'),/steady-lock/);
 await start.evaluate(e=>e.closest('tr').querySelector('button[aria-expanded]').click());assert.match(await page.locator('[data-card-state-detail="mode-map"]').textContent(),/Stop the running control card/);
 await page.locator('[data-workflow-artifacts-load="mode-map"]').click();const results=page.locator('[data-workflow-artifacts="mode-map"]');await results.locator('a').first().waitFor();assert.match(await results.textContent(),/Map JSON/);
 const pdf=results.locator('a').filter({hasText:'PDF report'});assert.equal(await pdf.count(),1);assert.match(await pdf.getAttribute('href'),/report\.pdf$/);
 const before=await start.boundingBox();await page.evaluate(()=>{state.jobs=[];updateCardStatus();});assert(!(await start.isDisabled()));assert.match(await status.textContent(),/Ready/);assert.equal(await start.getAttribute('title'),'');
 const after=await start.boundingBox();for(const k of ['x','y','width','height'])assert(Math.abs(before[k]-after[k])<.6,'Position changed: '+k);
 await page.evaluate(()=>{state.connected=false;updateCardStatus();});assert(await start.isDisabled());assert.match(await status.textContent(),/Connection unavailable/);
 await page.evaluate(()=>{state.connected=true;const e=document.querySelector('[data-start-card="mode-map"]');e.dataset.runnable='false';e.dataset.validationReason='A required axis is missing';updateCardStatus();});assert(await start.isDisabled());assert.match(await start.getAttribute('title'),/required axis/);assert.deepEqual(errors,[]);
 if(process.env.LDCON_LAYOUT_SCREENSHOTS){fs.mkdirSync(process.env.LDCON_LAYOUT_SCREENSHOTS,{recursive:true});await page.evaluate(()=>{const e=document.querySelector('[data-start-card="mode-map"]');e.dataset.runnable='true';state.jobs=[{id:'a',set_id:'fixture',status:'running',cards:[{id:'steady-lock',operation:'servo.ecdl_steady_lock'}]}];updateCardStatus();});await results.locator('..').screenshot({path:path.join(process.env.LDCON_LAYOUT_SCREENSHOTS,`card-report-${width}.png`)});}
 await page.close();console.log(`PDF links and explicit Start blockers verified at ${width}px.`);
}}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
