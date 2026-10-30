// Browser geometry regression using synthetic data only. Every request is intercepted.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const read=name=>fs.readFileSync(path.join(assets,name),'utf8');
let phase=0;const requests=[],errors=[];
const snapshot=()=>{
 const job=fixture.job();job.controller_state=phase===1?'mode_degraded':phase===2?'waiting_for_fresh_measurement_after_recovery':'locked';
 if(phase===3)job.latest={recorded_unix_s:Date.now()/1000,frequency_hz:710e12};
 const jobs=phase===5?[]:[job];if(phase===1||phase===2)jobs.push({...fixture.job(),id:'b'.repeat(32),read_only:true,cards:[{id:'diagnostic-agent',operation:'agent.resident'}],controller_state:phase===2?'task_requires_inspection_with_an_extra_long_reason':'agent_ready',agent:{status:'ready'}});
 return {jobs,exports:[],wavelengths:phase===0?{fixture:{valid:true,frequency_hz:710e12,wavelength_nm:299792458e9/710e12,channel:3,acquired_unix_s:Date.now()/1000}}:{}};
};
function payload(route){
 if(route==='dashboard/snapshot')return snapshot();
 if(route.startsWith('history?'))return {jobs:[fixture.job()],total:1,unavailable_services:[]};
 if(route.endsWith('/validate'))return {runnable:true,blocked_reasons:[]};
 if(route.endsWith('/operating-mode'))return {mode:'measurement',revision:1,reason:'Routine spectroscopy',updated_unix_s:1793030000};
 if(route.endsWith('/adjustment-guidance'))return {revision:1,document:{knobs:{}}};
 if(route==='fleet')return {lasers:[{service_id:'fixture',set_id:'fixture',name:'Laser fixture',reachable:phase!==4,state:phase===0?'LOCKED':phase===1?'DEGRADED':'UNKNOWN',code:phase===0?3:12,reason:phase===0?'Fresh':('Long explanation with varying values. '.repeat(30)),wavelength:snapshot().wavelengths.fixture}]};
 return fixture.data()[route]??{};
}
async function intercepted(route){
 const url=new URL(route.request().url()),name=url.pathname.split('/assets/')[1];
 if(name){assert(!name.includes('..'));return route.fulfill({body:read(name),contentType:name.endsWith('.css')?'text/css':'text/javascript'});}
 if(url.pathname.includes('/api/')){
  const tail=url.pathname.split('/api/')[1]+url.search;requests.push({tail,method:route.request().method()});
  assert(route.request().method()==='GET'||tail.endsWith('/validate'),'No operational request allowed');
  return route.fulfill({status:phase===4&&tail==='dashboard/snapshot'?503:200,contentType:'application/json',body:JSON.stringify(payload(tail))});
 }
 return route.fulfill({body:read(url.pathname.includes('/fleet/')?'fleet.html':'index.html'),contentType:'text/html'});
}
async function geometry(page,selector){return page.locator(selector).evaluateAll(items=>items.map(e=>{const r=e.getBoundingClientRect();return {text:e.textContent,x:r.x+scrollX,y:r.y+scrollY,width:r.width,height:r.height};}));}
function sameGeometry(before,after,label){assert.equal(after.length,before.length,label);for(let i=0;i<before.length;i++)for(const field of ['x','y','width','height'])assert(Math.abs(before[i][field]-after[i][field])<.6,`${label} ${i}/${before[i].text} ${field}: ${before[i][field]} -> ${after[i][field]}`);}
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  for(const width of [1440,900,600]){
   phase=0;const page=await browser.newPage({viewport:{width,height:1000}});page.on('pageerror',e=>errors.push(e.message));
   await page.addInitScript(()=>{localStorage.setItem('ldcon.language','en');window.setInterval=()=>0;});
   await page.route('**/*',intercepted);await page.goto('http://ldcon.test/?set=fixture#laser-settings');
   await page.waitForFunction(()=>!state.busy&&!polling&&document.querySelector('[data-start-card]'));
   await page.evaluate(()=>document.fonts.ready);await page.waitForTimeout(100);
   const targetInput=page.locator('[data-target-input]'),decimals=page.locator('[data-target-decimals]');
   assert.equal(await page.locator('[data-send-on-arrows]').isChecked(),false);
   const baseline=await targetInput.inputValue();assert(/\.\d{9}$/.test(baseline));
   await targetInput.fill('422.12345');await targetInput.press('Escape');
   assert.equal(await targetInput.inputValue(),baseline);assert(await targetInput.evaluate(e=>e===document.activeElement));
   await targetInput.fill('422.12345');await page.locator('#live-controls h2').click();await page.keyboard.press('Escape');
   assert.equal(await targetInput.inputValue(),baseline,'Unfocused Escape restores drafts');
   await decimals.fill('3');assert(/\.\d{3}$/.test(await targetInput.inputValue()));
   await decimals.fill('9');assert.equal(await targetInput.inputValue(),baseline);
   assert.equal(await page.locator('[data-operating-mode-reason]').textContent(),'Routine spectroscopy');
   const controls='[data-start-card], [data-stop-card], .card-primary button[aria-expanded], .target-control button';
   const before=await geometry(page,controls);
   if(width===1440&&process.env.LDCON_LAYOUT_SCREENSHOTS){fs.mkdirSync(process.env.LDCON_LAYOUT_SCREENSHOTS,{recursive:true});await page.locator('#laser-cards').screenshot({path:path.join(process.env.LDCON_LAYOUT_SCREENSHOTS,'cards.png')});await page.locator('#live-controls').screenshot({path:path.join(process.env.LDCON_LAYOUT_SCREENSHOTS,'controls.png')});}
   await page.evaluate(()=>{window.savedButtons=[...document.querySelectorAll('[data-start-card], [data-stop-card]')];});
   const input=page.locator('[data-target-input]');await input.focus();await input.fill('422.12345');const scrollBefore=await page.evaluate(()=>scrollY);
   for(phase=1;phase<=5;phase++){
    await page.evaluate(()=>refresh());await page.waitForTimeout(40);
    sameGeometry(before,await geometry(page,controls),`workspace ${width} phase ${phase}`);
    assert(await page.evaluate(()=>savedButtons.every(e=>e.isConnected)));
    assert.equal(await page.evaluate(()=>scrollY),scrollBefore);assert.equal(await input.inputValue(),'422.12345');assert(await input.evaluate(e=>e===document.activeElement));
   }
   phase=0;await page.evaluate(()=>{switchView('lasers');return refresh();});await page.waitForTimeout(40);
   const listControls='#laser-list .actions button';await page.locator(listControls).first().focus();
   const listBefore=await geometry(page,listControls);
   for(phase=1;phase<=5;phase++){
    await page.evaluate(()=>refresh());await page.waitForTimeout(40);
    sameGeometry(listBefore,await geometry(page,listControls),`laser list ${width} phase ${phase}`);
    assert(await page.locator(listControls).first().evaluate(e=>e===document.activeElement));
   }
   phase=0;await page.goto('http://ldcon.test/fleet/#lasers');await page.waitForSelector('#fleet-list button');await page.waitForTimeout(40);
   await page.locator('#fleet-list button').first().focus();const fleetBefore=await geometry(page,'#fleet-list button');
   for(phase=1;phase<=5;phase++){
    await page.locator('#fleet-refresh').evaluate(e=>e.click());await page.waitForTimeout(80);
    sameGeometry(fleetBefore,await geometry(page,'#fleet-list button'),`fleet ${width} phase ${phase}`);
    assert(await page.locator('#fleet-list button').first().evaluate(e=>e===document.activeElement));
   }
   await page.close();console.log(`Stable control positions, identity, focus and draft input at ${width}px across all refresh states.`);
  }
  assert.deepEqual(errors,[]);assert(!requests.some(r=>r.method!=='GET'&&!r.tail.endsWith('/validate')));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
