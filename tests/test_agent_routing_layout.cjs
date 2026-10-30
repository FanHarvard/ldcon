// Synthetic settings only: all browser traffic is intercepted, including saves.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('playwright-core');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const profiles={},matrix={},catalog={};
for(const [provider,model] of [['claude','claude-opus-5-5'],['codex','gpt-6.1-sol'],['opencode','deepseek/deepseek-flash']]){
 matrix[provider]={};catalog[provider]=[{model,efforts:['low','medium','xhigh']}];
 for(const [difficulty,effort] of [['basic','low'],['standard','medium'],['deep','xhigh']]){
  const id=provider+'-'+difficulty;matrix[provider][difficulty]=id;
  profiles[id]={provider,model,effort,timeout_s:1800,max_turns:64,max_input_bytes:262144,required_tools:[],required_modalities:['text']};
 }
}
const table={schema_version:1,default_difficulty:'standard',provider_profiles:matrix,gateway:{provider:'claude',difficulty:'basic'},lasers:{sr422:{provider:'claude',difficulty:'basic'},sr1092:{provider:'claude',difficulty:'basic'}}};
const read=name=>fs.readFileSync(path.join(assets,name),'utf8');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{for(const width of [1440,900,600]){
  const page=await browser.newPage({viewport:{width,height:1100}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const u=new URL(route.request().url());assert.equal(route.request().method(),'GET');
   let result;
   if(u.pathname.endsWith('/api/agent-routing'))result={document:table,revision:'fixture-routing',configured:true};
   else if(u.pathname.endsWith('/api/agent-profiles'))result={document:{schema_version:1,profiles,lasers:{},resident_profile:''},revision:'fixture-profile',providers:Object.keys(matrix).map(id=>({id,label:id,available:true})),model_options:{providers:catalog}};
   else if(u.pathname.endsWith('/api/notification-policy'))result={document:{schema_version:1,global_policy:{preset:'standard',progress_interval_s:300},lasers:{},tasks:{}},revision:1,resolved:{global_policy:{preset:'standard',progress_interval_s:300},lasers:{},tasks:{}}};
   if(result)return route.fulfill({contentType:'application/json',body:JSON.stringify(result)});
   const name=u.pathname.split('/').pop();assert(['agent-settings','agent-settings.css','agent-settings.js'].includes(name));
   return route.fulfill({contentType:name.endsWith('.css')?'text/css':name.endsWith('.js')?'text/javascript':'text/html',body:read(name==='agent-settings'?'agent-settings.html':name)});
  });
  await page.goto('http://ldcon.test/agent-settings');await page.waitForFunction(()=>!document.querySelector('#save-routing').disabled);
  assert.equal(await page.locator('#routing-rows select').count(),9);
  assert(await page.locator('#routing').innerText().then(x=>x.includes('Profile IDs are arbitrary names')));
  assert.deepEqual(errors,[]);
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Page must not overflow; tables can scroll internally');
  if(process.env.LDCON_LAYOUT_SCREENSHOTS){fs.mkdirSync(process.env.LDCON_LAYOUT_SCREENSHOTS,{recursive:true});await page.locator('#routing').screenshot({path:path.join(process.env.LDCON_LAYOUT_SCREENSHOTS,'routing-'+width+'.png')});}
  console.log('Difficulty routing layout: '+width+' px, nine explicit choices, no errors or page overflow.');await page.close();
 }}finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
