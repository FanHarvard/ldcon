// Synthetic browser fixture only; never talks to lasers or a model.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const read=n=>fs.readFileSync(path.join(assets,n),'utf8');
const dom=new JSDOM(read('index.html'),{url:'http://localhost/s/test/?set=fixture#laser-settings',runScripts:'outside-only'});
const w=dom.window,d=w.document,data=fixture.data(),requests=[];
const deck=data['sets/fixture'].deck_definitions[0];
const adjust={id:'agent-adjust',operation:'agent.task',uses:[],agent_execution:{mode:'resident'},parameters:{agent_card_id:'diagnostic-agent',instruction_profile:'adjustment',prompt:'Registered instructions',execution_profile:'',adjustment_family:'auto'}};
const monitor={id:'agent-monitor',operation:'agent.monitor',uses:[],parameters:{task_card_id:'agent-adjust',background:{enabled:true,families:['current'],half_span_v:.02}}};
for(const c of [adjust,monitor]){deck.document.cards.push(c);deck.card_status[c.id]={runnable:true,parameters:c.parameters,blocked_reasons:[]};}
let jobs=[fixture.job(),{id:'resident-job',set_id:'fixture',status:'running',cards:[{id:'diagnostic-agent',operation:'agent.resident'}]}];
w.setInterval=()=>0;w.HTMLElement.prototype.scrollIntoView=()=>{};
w.fetch=async(url,options={})=>{
 const route=String(url).split('api/')[1],method=options.method||'GET';requests.push({route,method,body:options.body});let v;
 if(route==='dashboard/snapshot')v={jobs,exports:[]};
 else if(route==='jobs?active_only=true')v={jobs};
 else if(route.startsWith('jobs?operation='))v={jobs:[]};
 else if(route.startsWith('history?'))v={jobs:[],total:0};
 else if(route.endsWith('/validate'))v={runnable:true,blocked_reasons:[]};
 else if(route.endsWith('/start')){v={id:'adjustment-job',status:'queued'};jobs.push({...v,set_id:'fixture',cards:[{id:'agent-adjust',operation:'agent.task'}]});}
 else v=data[route];
 if(v===undefined)throw Error(route);
 return {ok:true,status:200,json:async()=>structuredClone(v)};
};
w.eval(['dashboard-state.js','window-actions.js','configuration-editor.js','laser-workspace.js','committing-input.js','app.js','i18n.js'].map(read).join('\n')+'\nwindow.__test={idle:()=>!state.busy&&!polling};');
async function wait(fn){for(let i=0;i<100;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw Error(d.querySelector('#notice').textContent||'Timeout');}
(async()=>{
 await wait(()=>w.__test.idle()&&d.querySelector('[data-adjustment-family]'));
 d.querySelector('[data-language="en"]').click();await new Promise(r=>setTimeout(r,0));
 const family=d.querySelector('[data-adjustment-family]'),objective=d.querySelector('[data-adjustment-objective]');
 const area=[...d.querySelectorAll('textarea')].find(a=>a.getAttribute('aria-label')==='agent-adjust 今回のパラメーター');
 assert.equal(family.value,'auto');assert(family.querySelector('[value="pi"]').disabled);
 assert.equal(d.querySelector('[data-adjustment-agent]').value,'diagnostic-agent');
 family.value='current';family.dispatchEvent(new w.Event('change',{bubbles:true}));
 objective.value='Improve dither and side peaks';objective.dispatchEvent(new w.Event('change',{bubbles:true}));
 await wait(()=>w.__test.idle());
 const p=JSON.parse(area.value);assert.equal(p.adjustment_family,'current');assert.equal(p.adjustment_objective,objective.value);
 assert.equal(p.prompt,'Registered instructions');assert.equal(p.execution_profile,'');
 const section=d.querySelector('.adjustment-execution');
 assert(!/[\u3040-\u30ff\u3400-\u9fff]/.test(section.textContent));
 assert(section.textContent.includes('Monitoring need not be running'));
 await wait(()=>!d.querySelector('[data-start-card="agent-adjust"]').disabled);
 d.querySelector('[data-start-card="agent-adjust"]').click();await wait(()=>requests.some(r=>r.route.endsWith('/start')));
 const posts=requests.filter(r=>r.method==='POST'&&r.route.endsWith('/start'));assert.equal(posts.length,1);
 const body=JSON.parse(posts[0].body);assert.equal(body.card_id,'agent-adjust');assert.equal(body.parameters.adjustment_family,'current');
 assert(!requests.some(r=>/restart|owner|\/stop$/.test(r.route)));
 await wait(()=>w.__test.idle());
 console.log('Resident adjustment Web: selected family/objective, same resident, no monitor start or hardware API.');
 dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
