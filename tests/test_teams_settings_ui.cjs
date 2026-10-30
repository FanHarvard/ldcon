const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const assets=process.env.LDCON_TEST_ASSETS||path.join(__dirname,'../src/ldcon/web_assets');
async function test(fleet){
 const dom=new JSDOM(`<html lang="en"><body><div id="teams-settings" data-no-translate data-teams-scope="${fleet?'fleet':'laser'}"></div></body></html>`,{url:'http://localhost/s/test/'+(fleet?'':'services/sr422/'),runScripts:'outside-only'});
 const w=dom.window,d=w.document,requests=[];
 let rev='revision1',mode='inherit',conflict=false;
 const view=()=>({revision:rev,default:{mode:'custom',configured:true},lasers:[{id:'sr422',name:'Sr422',mode,configured:mode==='custom',effective_mode:mode==='inherit'?'custom':mode,effective_configured:mode!=='disabled'}]});
 w.fetch=async(url,options={})=>{
  assert.equal(url,w.location.pathname+'api/teams-destinations');requests.push(options);
  if(options.method==='PUT'){
   assert.equal(options.headers['X-LDCon-Client'],'web');
   if(conflict)return {ok:false,status:409};
   const data=JSON.parse(options.body);assert.equal(data.expected_revision,rev);assert.equal(data.laser_id,'sr422');mode=data.mode;rev='revision2';
  }
  return {ok:true,status:200,json:async()=>view()};
 };
 w.eval(fs.readFileSync(path.join(assets,'teams-settings.js'),'utf8'));
 const tick=()=>new Promise(r=>setTimeout(r,5));await tick();
 const selects=d.querySelectorAll('select'),scope=selects[0],choice=selects[1],input=d.querySelector('input');
 assert.equal(input.type,'password');assert.equal(input.value,'');assert.equal(scope.options.length,fleet?2:1);
 assert(!/[\u3040-\u30ff\u4e00-\u9fff]/u.test(d.body.textContent));
 scope.value='sr422';scope.dispatchEvent(new w.Event('change'));
 choice.value='custom';choice.dispatchEvent(new w.Event('change'));assert.equal(input.disabled,false);
 input.value='https://example.webhook.office.com/x?sig=SECRET';
 d.querySelector('form').dispatchEvent(new w.Event('submit',{cancelable:true}));await tick();
 assert.equal(input.value,'');assert.equal(JSON.parse(requests.at(-1).body).webhook_url,'https://example.webhook.office.com/x?sig=SECRET');
 assert(!d.body.innerHTML.includes('SECRET'));assert(d.body.textContent.includes('no message was sent'));
 choice.value='disabled';choice.dispatchEvent(new w.Event('change'));assert.equal(input.disabled,true);
 conflict=true;d.querySelector('form').dispatchEvent(new w.Event('submit',{cancelable:true}));await tick();assert(d.body.textContent.includes('Reload before saving'));
 d.documentElement.lang='ja';await tick();assert(d.body.textContent.includes('Teams通知先'));
 d.documentElement.lang='en';await tick();assert(!/[\u3040-\u30ff\u4e00-\u9fff]/u.test(d.body.textContent));
 assert(requests.every(r=>!r.method||r.method==='PUT'));dom.window.close();
}
(async()=>{await test(true);await test(false);console.log('Teams settings UI: fleet/local scopes, redaction, conflict and languages passed');})().catch(e=>{console.error(e);process.exit(1)});
