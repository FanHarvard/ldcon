// Synthetic DOM/network fixture only: no service or instrument access.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const read=name=>fs.readFileSync(path.join(assets,name),'utf8');
const dom=new JSDOM(read('index.html'),{url:'http://localhost/s/test/?set=fixture#laser-settings',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window,d=w.document,data=fixture.data(),requests=[];
const deck=data['sets/fixture'].deck_definitions[0];
for(const card of [
 {id:'pi-coordinate',operation:'tune.pid_coordinate_search',parameters:{controller_card_id:'steady-lock'}},
 {id:'target-response',operation:'measure.target_response',parameters:{offsets_hz:[1e7,0]}},
 {id:'new-capability',operation:'custom.future_card',parameters:{}}
]){deck.document.cards.push({...card,uses:[]});deck.card_status[card.id]={runnable:true,parameters:card.parameters,blocked_reasons:[]};}
let jobs=[fixture.job()];
w.setInterval=()=>0;w.HTMLElement.prototype.scrollIntoView=()=>{};
w.fetch=async(url,options={})=>{
 const route=String(url).split('api/')[1],method=options.method||'GET';requests.push({route,method});let value;
 if(route==='dashboard/snapshot')value={jobs,exports:[]};
 else if(route==='jobs?active_only=true')value={jobs};
 else if(route==='jobs?operation=report.grafana_dashboard')value={jobs:[]};
 else if(route.endsWith('/validate'))value={runnable:true,blocked_reasons:[]};
 else if(route.startsWith('history?'))value={jobs:[],total:0};
 else value=data[route];
 if(value===undefined)throw Error('Unexpected route '+route);
 return {ok:true,status:200,json:async()=>structuredClone(value)};
};
w.eval(['dashboard-state.js','window-actions.js','configuration-editor.js','laser-workspace.js','committing-input.js','card-browser.js','app.js','i18n.js'].map(read).join('\n')+'\nwindow.__test={idle:()=>!state.busy&&!polling,refresh,renderLaserConfiguration,setConnected:v=>{state.connected=v;updateCardStatus();}};');
const pause=()=>new Promise(resolve=>setTimeout(resolve,15));
async function wait(fn){for(let i=0;i<150;i++){if(fn())return;await pause();}throw Error(d.querySelector('#notice').textContent||'Timed out');}
const row=id=>d.querySelector(`[data-start-card="${id}"]`).closest('tr');
function search(text){const input=d.querySelector('[data-card-search]');input.value=text;input.dispatchEvent(new w.Event('input'));return input;}
(async()=>{
 await wait(()=>w.__test.idle()&&d.querySelector('[data-card-search]'));
 const model=w.ldconCardBrowser;
 assert(model.matches('respnse','Target response measurement'),'one missing letter');
 assert(model.matches('trgt rsp','Target response measurement'),'abbreviated multiword query');
 assert(model.matches('ＰＺＴ　center','PZT center'),'normalise width and whitespace');
 assert(!model.matches('pi','Temperature alignment'),'short queries do not fuzzy-match arbitrary words');
 assert.equal(model.category('custom.future_card'),'other');
 assert(model.matches('gateway',model.searchable({id:'diagnostic-agent',operation:'agent.resident'})));
 assert.equal(model.label('agent.resident',{role:'operator'}),'Operator agent');
 assert.equal(model.label('agent.task',{adjustment_family:'pi'}),'PI optimization (agent)');
 assert.equal(model.label('agent.task',{adjustment_family:'current'}),'Current operating-point optimization');
 assert.notEqual(model.label('agent.task',{adjustment_family:'slow_loop'}),model.label('agent.task',{adjustment_family:'feedforward'}));
 assert(model.matches('電流動作点',model.searchable({id:'current-optimization',operation:'agent.task',parameters:{adjustment_family:'current'}})));
 assert(d.querySelector('[data-card-category="other"]'),'unrecognised cards remain accessible');
 const piRow=row('pi-coordinate'),responseRow=row('target-response'),detail=piRow.nextElementSibling;
 const piStart=piRow.querySelector('[data-start-card]'),area=detail.querySelector('textarea');
 assert.equal(piStart.dataset.operation,'tune.pid_coordinate_search');
 assert.equal(detail.dataset.group,'cards-ecdl','details remain adjacent to their action row');
 detail._toggle.click();await wait(()=>!detail.hidden);
 area.value='{"controller_card_id":"steady-lock","note":"unsent draft"}';
 const draft=area.value;
 d.querySelector('[data-card-categories-collapse]').click();
 assert(piRow.hidden&&responseRow.hidden);
 const piToggle=d.querySelector('[data-card-category-toggle="optimization"]');
 assert.equal(piToggle.getAttribute('aria-expanded'),'false');
 search('pi coordnate');
 assert(!piRow.hidden,'search reveals a match in a collapsed category');
 assert(responseRow.hidden,'unmatched cards stay filtered');
 assert(!detail.hidden&&!detail.classList.contains('card-list-hidden'),'open details survive search');
 assert.equal(piToggle.getAttribute('aria-expanded'),'true');assert(piToggle.disabled,'search temporarily controls visibility');
 const searchInput=d.querySelector('[data-card-search]');searchInput.focus();
 for(const controller_state of ['locked','mode_degraded','UNKNOWN','very_long_status '.repeat(20)]){
  jobs=[{...fixture.job(),controller_state}];await w.__test.refresh();
  assert.strictEqual(row('pi-coordinate'),piRow);assert.strictEqual(piRow.querySelector('[data-start-card]'),piStart);
  assert.equal(area.value,draft);assert.strictEqual(d.activeElement,searchInput);
  assert.equal(searchInput.value,'pi coordnate');assert(!piRow.hidden);
 }
 w.__test.setConnected(false);assert(piStart.disabled);assert.strictEqual(row('pi-coordinate'),piRow);
 w.__test.setConnected(true);
 search('');assert(piRow.hidden&&responseRow.hidden,'clearing search restores collapsed categories');
 assert(!detail.hidden,'category hiding does not reset card-detail state');
 d.querySelector('[data-card-category-toggle="optimization"]').click();assert(!piRow.hidden);assert.equal(area.value,draft);
 search('波長'); // no implicit data parameter matching
 search('目標応答');assert(!responseRow.hidden,'Japanese aliases find English-labelled cards');
 search('zzzzzzzzzzzzzzzzzz');assert(!d.querySelector('[data-card-search-empty]').hidden);
 d.querySelector('[data-card-search-clear]').click();assert(d.querySelector('[data-card-search-empty]').hidden);
 d.querySelector('[data-language="en"]').click();await pause();
 assert.equal(piRow.querySelector('strong').textContent,'PI optimization (P/I independently)');
 assert.equal(d.querySelector('[data-card-category="optimization"] .card-category-toggle span').textContent,'Optimization');
 assert.equal(searchInput.placeholder,'Name, function or ID; partial names and typos work');
 d.querySelector('[data-language="ja"]').click();await pause();assert.equal(piRow.querySelector('strong').textContent,'PI最適化（P・I独立）');
 // A new mount reads category preferences; rebuilding is reserved for actual configuration reloads.
 w.__test.renderLaserConfiguration();await pause();await pause();assert(!row('pi-coordinate').hidden);assert(row('target-response').hidden);
 assert(!requests.some(r=>r.method!=='GET'&&!r.route.endsWith('/validate')),'filtering/grouping never starts, stops or saves a card');
 assert(!requests.some(r=>/owner|restart/.test(r.route)));
 dom.window.close();console.log('Card browser UI: category persistence, fuzzy bilingual search through collapsed groups, stable draft/action nodes, refresh/failure preservation and no operational writes passed.');
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});
