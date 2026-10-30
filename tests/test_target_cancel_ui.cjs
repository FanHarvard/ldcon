// Offline browser integration: a target started in another client is cancellable.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets'),read=n=>fs.readFileSync(path.join(assets,n),'utf8');
const dom=new JSDOM(read('index.html'),{url:'http://localhost/s/test/?set=fixture#laser-settings',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window,d=w.document;
const data=structuredClone(fixture.data()),requests=[],lock=fixture.job();
let target={id:'b'.repeat(32),set_id:'fixture',status:'running',cards:[{id:'target',card:'tune.lock_target'}]};let transportFailure=false;
w.setInterval=()=>0;w.HTMLElement.prototype.scrollIntoView=()=>{};w.localStorage.setItem('ldcon.language','en');
w.fetch=async(url,options={})=>{const route=String(url).split('api/')[1],method=options.method||'GET';requests.push({route,method});let v;
 if(route==='dashboard/snapshot')v={jobs:[lock,target],exports:[]};
 else if(route.endsWith('/operating-mode'))v={mode:'characterization',revision:2};
 else if(route.startsWith('history?'))v={jobs:[],total:0};
 else if(route.endsWith('/validate'))v={runnable:true,blocked_reasons:[]};
 else if(route===`jobs/${target.id}/stop`){if(transportFailure)throw Error('Synthetic unknown delivery');v={...target,status:'stopping'};}
 else if(route===`jobs/${target.id}`)v=target;
 else v=data[route];
 if(v===undefined)throw Error('Unexpected synthetic route '+route);return {ok:true,status:200,json:async()=>structuredClone(v)};
};
w.eval(['dashboard-state.js','window-actions.js','configuration-editor.js','laser-workspace.js','committing-input.js','app.js','i18n.js'].map(n=>n==='app.js'?read(n).replace('  const render=renderLaserConfiguration;', '  window.__controls={poll,update,setJobs:jobs=>{state.jobs=jobs;update();}};\n  const render=renderLaserConfiguration;'):read(n)).join('\n')+'\nwindow.__test={idle:()=>!state.busy&&!polling};');
const tick=()=>new Promise(r=>setTimeout(r,10));async function wait(f){for(let i=0;i<200;i++){if(f())return;await tick();}throw Error('Timeout');}
(async()=>{
 await wait(()=>w.__test.idle()&&d.querySelector('[data-cancel-target]'));await w.__controls.poll();
 const stop=d.querySelector('[data-cancel-target]');assert.equal(stop.disabled,false);stop.click();
 await wait(()=>requests.some(r=>r.method==='POST'));assert.equal(stop.disabled,true);
 assert.deepEqual(requests.filter(r=>r.method==='POST'),[{route:`jobs/${target.id}/stop`,method:'POST'}]);
 stop.click();await tick();assert.equal(requests.filter(r=>r.method==='POST').length,1,'At most one cancellation submission');
 target.status='cancelled';await w.__controls.poll();assert.equal(stop.disabled,true);assert.equal(d.querySelector('[data-cancel-target]'),stop,'Keep button position and identity');
 // Unknown delivery is reported, not retried or replaced by stopping the lock.
 target={...target,id:'c'.repeat(32),status:'running'};w.__controls.setJobs([lock,target]);transportFailure=true;stop.click();
 await wait(()=>requests.filter(r=>r.method==='POST').length===2);await tick();assert.equal(stop.disabled,true);stop.click();await tick();
 assert(d.querySelector('[data-control-result]').textContent.includes('not confirmed'));
 assert(!requests.some(r=>r.route.includes(lock.id)&&r.method==='POST'));
 assert.equal(requests.filter(r=>r.method==='POST').length,2);
 // Mismatched readback must send nothing.
 const advertised={...target,id:'d'.repeat(32)};target={...advertised,cards:[{card:'servo.ecdl_steady_lock'}]};w.__controls.setJobs([lock,advertised]);stop.click();await tick();await tick();
 assert.equal(requests.filter(r=>r.method==='POST').length,2);
 dom.window.close();console.log('Target cancel UI: external job, stable button, exact identity, no lock stop or transport replay.');
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
