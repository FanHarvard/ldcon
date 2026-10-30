const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const dom=new JSDOM('<div id="notice"></div>',{runScripts:'outside-only'}),w=dom.window,d=w.document;
let documentValue={id:'fixture',target:1,limit:45},revision='r2',puts=[],reads=0,conflict=false,mutateDuringRead=null;
w.node=(tag,text='',className='')=>{const e=d.createElement(tag);e.textContent=text;e.className=className;return e;};
w.cell=(row,...v)=>{const e=w.node('td');for(const x of v)e.append(typeof x==='string'?d.createTextNode(x):x);row.append(e);return e;};
w.button=(text,fn)=>{const e=w.node('button',text);e.run=fn;return e;};
w.$=s=>d.querySelector(s);w.loadDefinitions=async()=>{};
w.api=async(route,method='GET',body)=>{
 assert.equal(route,'configuration/lasers/fixture');
 if(method==='GET'){reads++;if(mutateDuringRead)mutateDuringRead();return {document:structuredClone(documentValue),revision};}
 assert.equal(method,'PUT');puts.push(body);if(conflict)throw Error('Configuration changed. Reload before saving.');return {};
};
w.eval(fs.readFileSync(path.join(assets,'configuration-editor.js'),'utf8'));
assert.equal(w.ldconParameterGroup('devices.native.control_settings.native_current.native.integral_limit_count'.split('.')),'operating');
assert.equal(w.ldconParameterGroup('devices.native.control_settings.native_current.current_bounds_a.0'.split('.')),'limits');
const initial={id:'fixture',revision:'r1',document:{id:'fixture',target:0,limit:45}};
const editor=w.ldconConfigurationEditor(initial);d.body.append(editor);
const field=key=>[...editor.querySelectorAll('[data-configuration-path]')].find(e=>JSON.parse(e.dataset.configurationPath)[0]===key);
const btn=text=>[...editor.querySelectorAll('button')].find(e=>e.textContent===text);
const review=btn('変更差分を確認'),save=btn('承認した差分を検証して保存'),ack=editor.querySelector('input[type=checkbox]');
function edit(value){field('limit').value=String(value);field('limit').dispatchEvent(new w.Event('input'));}
function approve(){ack.checked=true;ack.onchange();}
(async()=>{
 edit(100);await review.run();assert.equal(reads,1);assert.equal(puts.length,0);assert(save.disabled);
 assert.equal(field('limit').value,'100');assert.equal(field('target').value,'1');approve();await save.run();
 assert.equal(puts.length,1);assert.equal(puts[0].expected_revision,'r2');assert.deepEqual(JSON.parse(puts[0].yaml),{id:'fixture',target:1,limit:100});
 // Concurrent edit of the exact scalar is visible and cannot reuse old approval.
 documentValue={id:'fixture',target:2,limit:54};revision='r3';await review.run();
 assert.equal(field('limit').closest('tr').children[1].textContent,'54');assert.equal(field('limit').value,'100');
 assert(editor.textContent.includes('An edited field also changed'));assert(!ack.checked);assert(save.disabled);
 await assert.rejects(save.run(),/差分/);assert.equal(puts.length,1);
 approve();conflict=true;await assert.rejects(save.run(),/Configuration changed/);
 assert.equal(puts.length,2);assert(save.disabled);assert(!ack.checked);assert.equal(field('limit').value,'100');
 assert(editor.textContent.includes('Your draft is preserved'));
 // Explicit re-review preserves a newer target, then uses the new CAS revision.
 conflict=false;documentValue.target=3;revision='r4';await review.run();approve();await save.run();
 assert.equal(puts.length,3);assert.equal(puts[2].expected_revision,'r4');assert.equal(JSON.parse(puts[2].yaml).target,3);
 // Editing during the read must not be silently reviewed or approved.
 mutateDuringRead=()=>edit(101);await assert.rejects(review.run(),/Draft changed/);
 assert.equal(field('limit').value,'101');assert(!ack.checked);assert(save.disabled);assert.equal(puts.length,3);
 dom.window.close();console.log('Configuration review passed: latest-base merge, same-field conflict, preserved draft, renewed approval, CAS and edit-race guard.');
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
