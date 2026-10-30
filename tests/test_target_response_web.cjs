// Browser-side regression only: all API calls are synthetic; no laser access.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const read=name=>fs.readFileSync(path.join(assets,name),'utf8');
const dom=new JSDOM(read('index.html'),{url:'http://localhost/s/test/?set=fixture#laser-settings',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window,d=w.document,requests=[],data=fixture.data();
const card={id:'target-response',operation:'measure.target_response',uses:[],parameters:{offsets_hz:[10e6,0],execution_mode:'deterministic',agent_card_id:'diagnostic-agent',expected_settle_s:.7,confirmation_s:1.5}};
const deck=data['sets/fixture'].deck_definitions[0];deck.document.cards.push(card);
deck.card_status[card.id]={runnable:true,parameters:card.parameters,blocked_reasons:[]};
let jobs=[fixture.job()];
w.setInterval=()=>0;w.HTMLElement.prototype.scrollIntoView=()=>{};
w.fetch=async(url,options={})=>{
 const route=String(url).split('api/')[1],method=options.method||'GET';requests.push({route,method,body:options.body});let value;
 if(route==='dashboard/snapshot')value={jobs,exports:[]};
 else if(route==='jobs?active_only=true')value={jobs};
 else if(route==='jobs?operation=report.grafana_dashboard')value={jobs:[]};
 else if(route==='jobs?set_id=fixture')value={jobs:[{...job('measure.target_response','completed-study','succeeded'),created_utc:new Date().toISOString()}]};
 else if(route==='jobs/completed-study/artifacts/target-response.json')value={optimization:{status:'candidate_only',applied:false,parameters:{expected_settle_s:.7,confirmation_s:1.5,kp:100}}};
 else if(route.endsWith('/validate'))value={runnable:true,blocked_reasons:[]};
 else if(route.startsWith('history?'))value={jobs:[],total:0};
 else if(route.endsWith('/start')){jobs.push(job('measure.target_response','study-job'));value={id:'study-job',status:'queued'};}
 else value=data[route];
 if(value===undefined)throw Error('Unexpected route '+route);
 return {ok:true,status:200,json:async()=>structuredClone(value)};
};
const code=['dashboard-state.js','window-actions.js','configuration-editor.js','laser-workspace.js','committing-input.js','app.js','i18n.js'].map(read).join('\n');
w.eval(code+'\nwindow.__test={idle:()=>!state.busy&&!polling,refresh,setJobs:jobs=>{state.jobs=jobs;updateCardStatus();},setConnected:value=>{state.connected=value;updateCardStatus();}};');
const start=()=>d.querySelector('[data-start-card="target-response"]');
function job(operation,id,status='running',set_id='fixture'){return {id,set_id,status,cards:[{id:operation==='measure.target_response'?'target-response':id,operation}]};}
async function wait(fn){for(let i=0;i<150;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw Error(d.querySelector('#notice').textContent||'Timed out');}
async function run(){
 await wait(()=>w.__test.idle()&&start());
 const observers=[job('agent.resident','resident'),job('agent.monitor','monitor')];
 for(const operation of ['servo.ecdl_steady_lock','servo.native_current']){
  const lock=job(operation,'lock');lock.controller_state='mode_degraded';
  w.__test.setJobs([lock,...observers]);assert.equal(start().disabled,false,operation+' must allow response study while locked');
  w.__test.setJobs([lock,...observers,job('measure.target_response','study')]);assert(start().disabled,'No duplicate study');
  for(const op of ['tune.lock_target','sweep.frequency_staircase','agent.task','agent.history_review']){
   w.__test.setJobs([lock,...observers,job(op,'foreground')]);assert(start().disabled,op+' takes priority');
  }
  w.__test.setJobs([lock,...observers,job('measure.target_response','other','running','another-laser')]);assert.equal(start().disabled,false);
  w.__test.setJobs([lock,{...lock,id:'second-lock'}]);assert(start().disabled,'Ambiguous parent locks');
  w.__test.setJobs([{...lock,status:'queued'}]);assert(start().disabled,'Parent lock is not yet running');
 }
 w.__test.setJobs(observers);assert(start().disabled,'Do not offer to start without a parent lock');
 w.__test.setJobs([fixture.job(),...observers]);
 w.__test.setConnected(false);assert(start().disabled,'Disconnected display must not enable the study');
 w.__test.setConnected(true);start().dataset.runnable='false';w.__test.setJobs([fixture.job()]);assert(start().disabled,'Parameter validation still applies');
 start().dataset.runnable='true';w.__test.setJobs([fixture.job()]);assert.equal(start().disabled,false);
 const servoStart=d.querySelector('[data-start-card="steady-lock"]');assert(servoStart.disabled,'Existing exclusive-controller gate is preserved');

 const mode=d.querySelector('[data-study-mode="target-response"]'),area=[...d.querySelectorAll('textarea')].find(a=>a.getAttribute('aria-label')==='target-response 今回のパラメーター');
 d.querySelector('[data-language="en"]').click();await new Promise(r=>setTimeout(r,0));
 assert.equal(mode,null,'Algorithm selection belongs to the card, not a hidden selector');assert.equal(area.readOnly,false);
 assert(d.querySelector('[data-runtime-card="target-response"]').hidden,'No inactive live-parameter box');
 area.value=JSON.stringify({offsets_hz:[10e6,0]});area.dispatchEvent(new w.Event('input',{bubbles:true}));
 d.querySelector('[data-study-results="target-response"]').click();await wait(()=>[...d.querySelectorAll('button')].some(b=>b.textContent==='Use proposed settings for next run'));
 [...d.querySelectorAll('button')].find(b=>b.textContent==='Use proposed settings for next run').click();await wait(()=>JSON.parse(area.value).expected_settle_s===.7&&!start().disabled);
 assert.equal(JSON.parse(area.value).kp,undefined,'Only evaluation fields are imported');
 d.querySelector('[data-language="en"]').click();await new Promise(r=>setTimeout(r,0));
 const details=area.parentElement.parentElement;assert(!/[\u3040-\u30ff\u3400-\u9fff]/.test([...details.querySelectorAll('p,button,summary,label,h3')].map(e=>e.textContent).join(' ')),'Study details must render in English');
 start().click();await wait(()=>requests.some(r=>r.route.endsWith('/start')));await wait(()=>w.__test.idle());
 assert(start().disabled,'Accepted study disables duplicate start');start().click();
 const posts=requests.filter(r=>r.method==='POST'&&r.route.endsWith('/start'));assert.equal(posts.length,1);
 assert.equal(posts[0].route,'sets/fixture/decks/ecdl/start');
 assert.deepEqual(JSON.parse(posts[0].body),{expected_revision:'bundle-revision',card_id:'target-response',parameters:{offsets_hz:[10e6,0],expected_settle_s:.7,confirmation_s:1.5}});
 assert(!requests.some(r=>/owner|restart|\/stop$/.test(r.route)));
 dom.window.close();console.log('Target-response Web regression passed: ECDL/native lock attachment, concurrency/validation/disconnection gates, one ordinary start POST, no stop/restart.');
}
run().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
