const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const script=fs.readFileSync(path.join(assets,'laser-workspace.js'),'utf8');
const entries=[{service_id:'sr422',set_id:'sr422',name:'Sr422'}, {service_id:'sr1092',set_id:'sr1092',name:'Sr1092'}];
function setup(url, reply={workspaces:entries}) {
 const dom=new JSDOM('<header><a class="brand">LDCon</a></header><label><select id="laser-select"></select></label><textarea></textarea>',{url,runScripts:'outside-only'});
 const w=dom.window, calls=[],navigation=[],local=[];
 w.fetch=async(url,options)=>{calls.push({url,options});return {ok:reply!==null,json:async()=>reply};};
 w.eval(script);
 const select=w.document.querySelector('#laser-select');
 const render=(selected,choices=[{id:selected,name:selected==='sr422'?'Sr422':'Sr1092'}])=>w.ldconRenderLaserSelector({select,choices,selected,selectLocal:id=>local.push(id),navigate:url=>navigation.push(url)});
 return {dom,w,calls,navigation,local,select,render};
}
(async()=>{
 for(const current of ['sr422','sr1092']) {
  const other=current==='sr422'?'sr1092':'sr422';
  const t=setup(`http://localhost/s/test/services/${current}/?set=${current}#laser-settings`);
  await t.render(current);
  assert.deepEqual([...t.select.options].map(o=>o.textContent).sort(),['Sr1092','Sr422']);
  assert.equal(t.select.value,current);
  t.select.value=other+'/'+other;
  t.select.dispatchEvent(new t.w.Event('input',{bubbles:true}));
  const unload=new t.w.Event('beforeunload',{cancelable:true});t.w.dispatchEvent(unload);
  assert.equal(unload.defaultPrevented,false,'Changing the navigation selector must not mark configuration dirty');
  t.select.dispatchEvent(new t.w.Event('change'));
  assert.deepEqual(t.navigation,[`/s/test/services/${other}/?set=${other}#laser-settings`]);
  assert.equal(t.select.value,current,'Cancelled navigation must leave the old scope selected');
  assert.deepEqual(t.local,[],'Remote selection must never render another laser against the current API');
  assert.equal(t.calls.length,1);assert.equal(t.calls[0].url,'/s/test/api/workspaces');
  assert(!t.calls[0].options.method||t.calls[0].options.method==='GET');
  t.w.document.querySelector('textarea').dispatchEvent(new t.w.Event('input',{bubbles:true}));
  const dirty=new t.w.Event('beforeunload',{cancelable:true});t.w.dispatchEvent(dirty);assert.equal(dirty.defaultPrevented,true);
  await t.render(current);assert.equal(t.select.options.length,2);
  t.dom.window.close();
 }
 const direct=setup('http://localhost/s/test/#laser-settings');await direct.render('a',[{id:'a',name:'A'},{id:'b',name:'B'}]);
 direct.select.value='b';direct.select.dispatchEvent(new direct.w.Event('change'));assert.deepEqual(direct.local,['b']);assert.equal(direct.calls.length,0);direct.dom.window.close();
 const failed=setup('http://localhost/s/test/services/sr422/#laser-settings',null);await failed.render('sr422');assert.equal(failed.select.options.length,1);assert.equal(failed.w.document.querySelector('[data-workspace-status]').hidden,false);failed.dom.window.close();
 const malicious=setup('http://localhost/s/test/services/sr422/#settings',{workspaces:[...entries,{service_id:'//elsewhere',set_id:'x',name:'Bad'}]});await malicious.render('sr422');assert.equal(malicious.select.options.length,2);malicious.select.value='sr1092/sr1092';malicious.select.dispatchEvent(new malicious.w.Event('change'));assert(malicious.navigation[0].endsWith('#settings'));malicious.dom.window.close();
 console.log('Workspace selector passed: both directions, local standalone selection, view preservation, GET-only navigation, scope isolation, dirty-edit warning and unavailable inventory.');
})().catch(e=>{console.error(e);process.exitCode=1;});
