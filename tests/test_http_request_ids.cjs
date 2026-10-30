// HTTP browser compatibility. Every API response is synthetic; no devices.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const read=name=>fs.readFileSync(path.join(assets,name),'utf8');
const dom=new JSDOM(read('index.html'),{url:'http://192.0.2.1/s/test/?set=fixture#laser-settings',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window,d=w.document,requests=[],data=fixture.data();
Object.defineProperty(w.crypto,'randomUUID',{value:undefined,configurable:true});
assert.equal(typeof w.crypto.getRandomValues,'function');
let currentMode='measurement',revision=0,unknown=false;
w.setInterval=()=>0;w.HTMLElement.prototype.scrollIntoView=()=>{};
w.fetch=async(url,options={})=>{
 const route=String(url).split('api/')[1],method=options.method||'GET';requests.push({route,method,body:options.body,headers:options.headers});let value;
 if(route.endsWith('/operating-mode')){
  if(method==='PUT'){
   const body=JSON.parse(options.body);assert.match(body.request_id,/^mode-[a-f0-9]{32}$/);
   assert.equal(body.expected_revision,revision);assert.equal(options.headers['X-LDCon-Maintainer'],undefined);
   currentMode=body.desired_mode;revision++;
   if(unknown)throw Error('Unknown response');
  }
  value={mode:currentMode,revision};
 }else if(route.endsWith('/window-actions')){
  value=method==='GET'?{status:'running',config:{enabled:false,agent_enabled:true,card_id:'window-center',conditions:[]},cards:[{id:'window-center',parameters:{}}],selected_parameters:{},requests:[],capability_revision:'cap-r1',rate:{}}:{status:'queued'};
 }else if(route.endsWith('/runtime-parameters'))value=method==='GET'?{revision:'runtime-r1'}:{status:'queued'};
 else if(route==='dashboard/snapshot')value={jobs:[fixture.job()],exports:[]};
 else if(route.startsWith('history?'))value={jobs:[],total:0};
 else value=data[route];
 if(value===undefined)throw Error('Unexpected synthetic route: '+route);
 return {ok:true,status:200,json:async()=>structuredClone(value)};
};
w.eval(['dashboard-state.js','window-actions.js','configuration-editor.js','laser-workspace.js','committing-input.js','app.js','i18n.js'].map(read).join('\n')+'\nwindow.__test={idle:()=>!state.busy&&!polling,windowActionsEditor};');
async function wait(fn){for(let i=0;i<150;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw Error(d.querySelector('#notice').textContent||'Timed out');}
const find=(area,text)=>[...area.querySelectorAll('button')].find(b=>b.textContent===text);
(async()=>{
 await wait(()=>w.__test.idle()&&d.querySelector('#laser-operating-mode select'));
 const mode=d.querySelector('#laser-operating-mode'),apply=find(mode,'Apply operating mode');
 await wait(()=>!apply.disabled);
 assert.equal(requests.filter(r=>r.method!=='GET').length,0);
 async function change(value){mode.querySelector('select').value=value;mode.querySelector('input[type=text]').value='Synthetic mode change';apply.click();apply.click();await wait(()=>w.__test.idle());}
 await change('characterization');assert.equal(currentMode,'characterization');assert.equal(mode.querySelector('input[type=password]'),null);assert(!apply.disabled);assert(mode.textContent.includes('revision 1'));
 await change('measurement');assert.equal(currentMode,'measurement');assert(mode.textContent.includes('revision 2'));
 const puts=requests.filter(r=>r.method==='PUT');assert.equal(puts.length,2);assert.notEqual(JSON.parse(puts[0].body).request_id,JSON.parse(puts[1].body).request_id);
 unknown=true;await change('characterization');assert(apply.disabled);assert.equal(requests.filter(r=>r.method==='PUT').length,3);
 find(mode,'Read owner mode').click();await wait(()=>w.__test.idle()&&!apply.disabled);assert.equal(requests.filter(r=>r.method==='PUT').length,3,'Readback must not repeat a mode change');
 // Exercise both midpoint-policy saves and manual requests without randomUUID.
 const bundle=data['sets/fixture'];const section=w.__test.windowActionsEditor(bundle,{id:'steady-lock'});d.body.append(section);
 find(section,'監視・予約状態を更新').click();await wait(()=>w.__test.idle()&&!section.querySelector('fieldset').disabled);
 find(section,'監視条件・次回の探索設定を適用').click();await wait(()=>w.__test.idle());
 find(section,'中点探索を予約').click();await wait(()=>w.__test.idle());
 const actions=requests.filter(r=>r.method==='POST');assert.equal(actions.length,2);
 const ids=actions.map(r=>JSON.parse(r.body).request_id);for(const id of ids)assert.match(id,/^[a-f0-9]{32}$/);assert.notEqual(ids[0],ids[1]);
 assert.equal(JSON.parse(actions[0].body).expected_revision,'runtime-r1');assert.equal(JSON.parse(actions[1].body).capability_revision,'cap-r1');
 assert(!requests.some(r=>/\/start$|\/stop$|restart/.test(r.route)));
 console.log('HTTP request IDs passed: mode change/readback, unique IDs, duplicate-click protection, unknown-result no replay, midpoint save/request; all APIs mocked.');
 dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
