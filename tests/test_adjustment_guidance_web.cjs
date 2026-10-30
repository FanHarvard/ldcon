// Synthetic browser and metadata API only; no laser/controller connection.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const read=n=>fs.readFileSync(path.join(assets,n),'utf8');
const dom=new JSDOM(read('index.html'),{url:'http://localhost/s/test/?set=fixture#laser-settings',runScripts:'outside-only'});
const w=dom.window,d=w.document,data=fixture.data(),requests=[];
const document={schema_version:1,knobs:{current:{kind:'absolute',step:.0002,unit:'V',reason:'Provisional local probe'},tec_gain:{kind:'relative',step:.1,unit:'fraction',reason:'Provisional local probe'}}};
let advice={revision:'r1',availability:'configured',advisory_only:true,document};
w.setInterval=()=>0;w.HTMLElement.prototype.scrollIntoView=()=>{};
w.fetch=async(url,options={})=>{
 const route=String(url).split('api/')[1],method=options.method||'GET';requests.push({route,method,body:options.body});let v;
 if(route.endsWith('/adjustment-guidance')){
   if(method==='PUT'){const body=JSON.parse(options.body);assert.equal(body.expected_revision,'r1');advice={...advice,revision:'r2',document:body.document};}
   v=advice;
 }else if(route.endsWith('/operating-mode'))return {ok:false,status:503,json:async()=>({detail:'Owner upgrade pending'})};
 else if(route==='dashboard/snapshot')v={jobs:[fixture.job()],exports:[]};
 else if(route==='jobs?active_only=true')v={jobs:[fixture.job()]};
 else if(route.startsWith('jobs?operation='))v={jobs:[]};
 else if(route.startsWith('history?'))v={jobs:[],total:0};
 else v=data[route];
 if(v===undefined)throw Error(route);
 return {ok:true,status:200,json:async()=>structuredClone(v)};
};
w.eval(['dashboard-state.js','window-actions.js','configuration-editor.js','laser-workspace.js','committing-input.js','app.js','i18n.js'].map(read).join('\n')+'\nwindow.__test={idle:()=>!state.busy&&!polling};');
async function wait(fn){for(let i=0;i<100;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw Error('Timeout');}
(async()=>{
 await wait(()=>w.__test.idle()&&d.querySelector('[aria-label="current suggested step"]'));
 d.querySelector('[data-language="en"]').click();await new Promise(r=>setTimeout(r,0));
 const input=d.querySelector('[aria-label="current suggested step"]'),relative=d.querySelector('[aria-label="tec_gain suggested step"]');
 assert.equal(Number(input.value),.0002);assert.equal(Number(relative.value),10);
 input.value=.0001;input.dispatchEvent(new w.Event('input',{bubbles:true}));relative.value=5;relative.dispatchEvent(new w.Event('input',{bubbles:true}));
 const area=d.querySelector('#laser-adjustment-guidance');
 [...area.querySelectorAll('button')].find(b=>b.textContent==='Save suggested steps').click();
 await wait(()=>w.__test.idle()&&requests.some(r=>r.method==='PUT'));
 assert.equal(advice.document.knobs.current.step,.0001);assert.equal(advice.document.knobs.tec_gain.step,.05);
 assert.deepEqual(requests.filter(r=>r.method!=='GET').map(r=>r.route),[requests.find(r=>r.method==='PUT').route]);
 assert(requests.find(r=>r.method==='PUT').route.endsWith('/adjustment-guidance'));
 assert([...d.querySelector('#laser-operating-mode').querySelectorAll('button')].find(b=>b.textContent==='Apply operating mode').disabled);
 assert(!/[\u3040-\u30ff\u3400-\u9fff]/.test(area.textContent));
 console.log('Advisory UI: absolute/relative edit, CAS body, unavailable owner disables mode only; no physical API.');
 dom.window.close();
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
