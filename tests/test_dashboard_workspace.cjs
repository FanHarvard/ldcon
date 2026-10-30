const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const read=name=>fs.readFileSync(path.join(assets,name),'utf8');
const dom=new JSDOM(read('index.html'),{url:'http://localhost/s/test/?set=fixture#laser-settings',runScripts:'outside-only',pretendToBeVisual:true});
const w=dom.window,d=w.document,requests=[];let unknown=false,disconnected=false,independentWave=false,partial=false;
w.setInterval=()=>0;w.HTMLElement.prototype.scrollIntoView=()=>{};
w.fetch=async(url,options={})=>{
 const route=String(url).split('api/')[1],method=options.method||'GET';requests.push({route,method,body:options.body});let value;
 if(route==='dashboard/snapshot'){if(disconnected)throw Error('no snapshot');value=independentWave?{jobs:[],exports:[],wavelengths:{fixture:{valid:true,frequency_hz:711e12,wavelength_nm:299792458e9/711e12,channel:3,acquired_unix_s:Date.now()/1000}}}:{jobs:[partial?{...fixture.job(),latest:{recorded_unix_s:Date.now()/1000,frequency_hz:710e12}}:fixture.job()],exports:[]};}
 else if(route==='jobs?active_only=true')value={jobs:[fixture.job()]};
 else if(route==='jobs?operation=report.grafana_dashboard')value={jobs:[]};
 else if(route.startsWith('history?'))value={jobs:[fixture.job()],total:1};
 else if(route.endsWith('/agent-workspace'))value={markdown:method==='PUT'?JSON.parse(options.body).markdown:'# DFB fixture\n<img src=x onerror=bad()>',revision:'context-revision',path:'/tmp/fixture/agent/INSTRUCTIONS.md'};
 else if(route.endsWith('/agent-sessions'))value={sessions:[{provider:'codex',session:'<img src=x>',deck_id:'ecdl',card_id:'diagnostic-agent',status:'disconnected',updated:1}]};
 else if(route.endsWith('/validate'))value={runnable:true,blocked_reasons:[]};
 else if(route.endsWith('/start')){if(unknown)throw Error('lost response');value={id:'b'.repeat(32),status:'queued'};}
 else if(route==='jobs/'+'b'.repeat(32))value={id:'b'.repeat(32),status:'succeeded'};
 else if(route==='configuration/assignments/fixture'&&method==='PUT')value={revision:'saved'};
 else value=fixture.data()[route];
 if(value===undefined)throw Error('Unexpected API route '+route);
 return {ok:true,status:200,json:async()=>structuredClone(value)};
};
const code=['dashboard-state.js','window-actions.js','configuration-editor.js','laser-workspace.js','committing-input.js','app.js','i18n.js'].map(name=>name==='app.js'?read(name).replace('  const render=renderLaserConfiguration;', '  window.__controls={poll};\n  const render=renderLaserConfiguration;'):read(name)).join('\n');
w.eval(code+'\nwindow.__test={idle:()=>!state.busy&&!polling,refresh,renderLaserConfiguration};');
async function wait(fn){for(let i=0;i<150;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw Error(d.querySelector('#notice').textContent||'Timed out');}
const clickText=(root,text)=>{const b=[...root.querySelectorAll('button')].find(b=>b.textContent===text);assert(b,text);b.click();};
async function run(){
 await wait(()=>w.__test.idle()&&d.querySelector('#laser-topology table'));await new Promise(r=>setTimeout(r,20));
 assert.equal(d.querySelectorAll('.live-controls').length,1);
 const gateway=d.querySelector('[data-start-card="diagnostic-agent"]').closest('tr');assert(!gateway.querySelector('strong').textContent.includes('diagnostic'));
 assert.equal(gateway.querySelector('small').textContent,'agent.resident');
 assert.equal(d.querySelector('#laser-settings').hidden,false);
 assert(d.querySelector('[data-control-current]').textContent.includes('422.242898592 nm'));
 independentWave=true;await w.__test.refresh();w.__test.renderLaserConfiguration();await new Promise(r=>setTimeout(r,20));
 assert(d.querySelector('[data-control-current]').textContent.includes((299792458e9/711e12).toFixed(9)+' nm'));
 assert.equal(w.ldconControlModel.model(fixture.data()['sets/fixture'].laser_definition.document,[]).fresh,false);
 independentWave=false;await w.__test.refresh();w.__test.renderLaserConfiguration();await new Promise(r=>setTimeout(r,20));
 const current=[...d.querySelectorAll('#laser-topology tr')].find(r=>r.textContent.includes('ldc.invalid'));assert(current.textContent.includes('CH 1'));assert(current.textContent.includes('Current modulation input'));
 assert.equal(d.querySelector('#laser-topology svg'),null);
 assert(d.querySelector('#laser-parameters #laser-adjustment-guidance'));
 assert.equal(d.querySelector('.parameter-advanced').open,false);
 assert(d.querySelector('.configuration-sources').textContent.includes('/tmp/fixture/config/lasers/fixture.yaml'));
 assert(d.querySelector('.configuration-sources').textContent.includes('/tmp/fixture/config/assignments/fixture.yaml'));
 assert.equal(d.querySelector('iframe'),null);
 assert([...d.querySelectorAll('#laser-history a')].every(a=>a.href.endsWith('/report.pdf')));
 const search=d.querySelector('[data-parameter-search="assignments"]');search.value='current_excursion offsets';search.dispatchEvent(new w.Event('input'));
 const editor=search.closest('details'),rows=[...editor.querySelectorAll('tr')].filter(r=>!r.hidden&&r.querySelector('input[data-configuration-path]'));
 assert.equal(rows.length,2);assert(rows[0].textContent.includes('classes[0].sequence[0].offsets[0]'));
 const field=rows[0].querySelector('input');field.value='.2';field.dispatchEvent(new w.Event('input'));
 clickText(editor,'変更差分を確認');await wait(()=>w.__test.idle());
 const ack=editor.querySelector('input[type=checkbox]');ack.checked=true;ack.dispatchEvent(new w.Event('change'));
 clickText(editor,'承認した差分を検証して保存');await wait(()=>requests.some(r=>r.route==='configuration/assignments/fixture'&&r.method==='PUT'));await wait(()=>w.__test.idle());
 const saved=JSON.parse(requests.find(r=>r.route==='configuration/assignments/fixture'&&r.method==='PUT').body);assert.equal(saved.expected_revision,'assignment-revision');
 assert.equal(JSON.parse(saved.yaml).laser_id,'fixture');assert.equal(JSON.parse(saved.yaml).decks[0].parameters['steady-lock'].recovery.procedure.classes[0].sequence[0].offsets[0],.2);
 const provider=d.querySelector('[data-agent-provider]');provider.value='opencode';provider.dispatchEvent(new w.Event('change'));
 assert(provider.closest('#laser-agent'));assert.equal(d.querySelectorAll('#laser-cards [data-agent-provider]').length,0);const options=d.querySelector('textarea[aria-label="diagnostic-agent 今回のパラメーター"]');assert.equal(JSON.parse(options.value).provider,'opencode');assert.equal(JSON.parse(options.value).session_id,'');assert.equal(JSON.parse(options.value).execution_profile,'');assert.equal(JSON.parse(options.value).effort,'');
 clickText(d.querySelector('#laser-agent'),'Markdownテンプレートを読み込む');await wait(()=>!d.querySelector('[data-agent-template]').disabled);await wait(()=>w.__test.idle());
 const area=d.querySelector('[data-agent-template]');assert(area.value.includes('DFB'));assert.equal(d.querySelector('#laser-configuration img'),null);
 area.value='# ECDL notes';clickText(d.querySelector('#laser-agent'),'Markdownを保存');await wait(()=>requests.some(r=>r.method==='PUT'&&r.route.endsWith('/agent-workspace')));await wait(()=>w.__test.idle());
 const context=JSON.parse(requests.find(r=>r.method==='PUT'&&r.route.endsWith('/agent-workspace')).body);assert.equal(context.expected_revision,'context-revision');assert.equal(context.markdown,'# ECDL notes');
 d.querySelector('[data-language=en]').click();await new Promise(r=>setTimeout(r,20));assert.equal(d.querySelector('#laser-topology h2').textContent,'Device configuration');assert.equal(d.querySelector('[data-start-card="diagnostic-agent"]').closest('tr').querySelector('strong').textContent,'Gateway agent');
 d.querySelector('[data-language=ja]').click();await new Promise(r=>setTimeout(r,20));
 // Display transport loss must disable writes even when definitions still load.
 disconnected=true;await w.__test.refresh();assert([...d.querySelectorAll('.live-controls [data-control-write]')].every(b=>b.disabled));
 disconnected=false;await w.__test.refresh();
 // A partial trace must not erase the last recorded output or pretend it is new.
 const output=d.querySelector('[data-axis=current] [data-actual]');assert(output.textContent.includes('1.2'));
 partial=true;await w.__test.refresh();assert(output.textContent.includes('1.2'));partial=false;
 // One accepted target request, then an unknown reply that must not replay.
 await wait(()=>!d.querySelector('.target-control button[data-control-write]').disabled);
 let target=d.querySelector('[data-target-input]');target.value='422.3';target.dispatchEvent(new w.Event('input'));
 target.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
 await wait(()=>requests.some(r=>r.route.endsWith('/start')));await w.__controls.poll();await wait(()=>w.__test.idle());
 const starts=requests.filter(r=>r.route.endsWith('/start'));assert.equal(starts.length,1);assert.equal(JSON.parse(starts[0].body).parameters.target_wavelength_nm,422.3);
 w.__test.renderLaserConfiguration();await wait(()=>!d.querySelector('.target-control button[data-control-write]').disabled);
 unknown=true;target=d.querySelector('[data-target-input]');target.value='422.4';target.dispatchEvent(new w.Event('input'));
 target.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
 await wait(()=>requests.filter(r=>r.route.endsWith('/start')).length===2);await wait(()=>w.__test.idle());
 w.__test.renderLaserConfiguration();await new Promise(r=>setTimeout(r,25));assert([...d.querySelectorAll('.live-controls [data-control-write]')].every(b=>b.disabled));
 assert.equal(requests.filter(r=>r.route.endsWith('/start')).length,2);
 assert(!requests.some(r=>/restart|owner|\/stop$/.test(r.route)));
 dom.window.close();console.log('Dashboard DOM passed: topology, exact assignment search/save, provider selection, context CAS, PDF links, languages, single submission and unknown-result replay guard.');
 const fleet=new JSDOM(read('fleet.html'),{url:'http://localhost/s/test/',runScripts:'outside-only',pretendToBeVisual:true});fleet.window.setInterval=()=>0;
 fleet.window.fetch=async()=>({ok:true,json:async()=>({lasers:[{service_id:'laser-service',set_id:'fixture',name:'Fixture',reachable:true,state:'UNKNOWN',reason:'No live evidence'}],jobs:[],total:0,unavailable_services:[]})});
 fleet.window.eval(read('fleet.js'));await new Promise(r=>setTimeout(r,20));
 assert.equal(fleet.window.document.querySelector('iframe'),null);
 assert(fleet.window.document.querySelector('#fleet-list tr td a').href.endsWith('/services/laser-service/?set=fixture#laser-settings'));
 assert.equal(fleet.window.document.querySelectorAll('#fleet-list .actions button').length,1);
 assert(fleet.window.document.querySelector('#fleet-detail a').href.includes('/services/laser-service/?set=fixture#laser-settings'));
 assert(!fleet.window.document.querySelector('#fleet-detail a').href.includes('embed='));fleet.window.close();console.log('Fleet DOM passed: full-page laser navigation without nested iframe.');
}
run().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
