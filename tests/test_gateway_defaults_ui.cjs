const assert=require('node:assert/strict'),fs=require('fs'),path=require('path'),{JSDOM}=require('jsdom');
const dom=new JSDOM('<body></body>',{url:'http://localhost/',runScripts:'outside-only'}),w=dom.window,d=w.document;
w.structuredClone=structuredClone;
const source=fs.readFileSync(path.resolve(process.env.LDCON_TEST_ASSETS||path.join(__dirname,'../src/ldcon/web_assets'),'laser-workspace.js'),'utf8');
const profiles={alpha:{provider:'claude',model:'opus-exact',effort:'low'},violet:{provider:'codex',model:'sol-exact',effort:'medium'}};
let routing={document:{schema_version:1,default_difficulty:'standard',provider_profiles:{claude:{basic:'alpha',standard:'alpha',deep:'alpha'},codex:{basic:'violet',standard:'violet',deep:'violet'}},gateway:{provider:'claude',difficulty:'basic'},lasers:{other:{provider:'claude',difficulty:'deep'}}},configured:true,revision:'r1'};
let writes=0,error=null;
w.node=(tag,text='',css='')=>{const e=d.createElement(tag);e.textContent=text;e.className=css;return e;};
w.link=(text,url)=>{const e=w.node('a',text);e.href=url;return e;};
w.button=(text,fn)=>{const e=w.node('button',text);e.onclick=()=>Promise.resolve().then(fn).catch(e=>{error=e;});return e;};
w.api=async(route,method='GET',data)=>{
 assert(['agent-routing','agent-profiles'].includes(route));
 if(route==='agent-profiles')return {document:{profiles}};
 if(method==='PUT'){assert.equal(data.expected_revision,routing.revision);writes++;routing={document:structuredClone(data.document),configured:true,revision:'r2'};}
 return structuredClone(routing);
};
w.eval(source.slice(source.indexOf('window.ldconResidentOptions ='),source.indexOf('// Fleet detail')));
const pause=()=>new Promise(r=>setTimeout(r,15));
function create(){const p=d.createElement('textarea');p.value=JSON.stringify({provider:'opencode',execution_profile:'old',session_id:'old-session'});const e=w.ldconResidentOptions(p,'laser');d.body.append(e,p);return {e,p};}
(async()=>{
 const {e,p}=create();await pause();assert.equal(JSON.parse(p.value).execution_profile,'alpha');
 const provider=e.querySelector('[data-agent-provider]'),save=e.querySelector('[data-save-gateway-default]');
 provider.value='codex';provider.dispatchEvent(new w.Event('change'));assert.equal(writes,0);assert(!save.disabled);
 assert.equal(JSON.parse(p.value).execution_profile,'violet');save.click();await pause();assert.equal(error,null);assert.equal(writes,1);
 assert.equal(routing.document.lasers.laser.provider,'codex');assert.equal(routing.document.lasers.other.difficulty,'deep');
 const second=create();await pause();assert.equal(second.e.querySelector('[data-agent-provider]').value,'codex');
 assert.equal(JSON.parse(second.p.value).execution_profile,'violet');assert.equal(writes,1);
 assert(e.textContent.includes('running gateway and admitted tasks are unchanged'));
 dom.window.close();console.log('Gateway defaults UI: explicit save, per-laser isolation, reload and no card execution passed.');
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
