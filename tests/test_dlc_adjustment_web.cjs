// Synthetic form only; all metadata/network functions are intercepted.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const source=fs.readFileSync(path.resolve(__dirname,'../src/ldcon/web_assets/app.js'),'utf8');
const start=source.indexOf('function adjustmentExecutionEditor('),end=source.indexOf('\nfunction ',start+10);
const dom=new JSDOM('<html><body></body></html>',{runScripts:'outside-only',url:'http://localhost'}),w=dom.window;
w.eval(`function node(tag,text='',cls=''){const e=document.createElement(tag);e.textContent=text;e.className=cls;return e;}
function cardLabel(_op,_p,id){return id;}
function taskExecutionEditor(){return node('section');}
function cardJob(){return null;}
function error(e){throw e;}
`+source.slice(start,end));
const d=w.document,params=d.createElement('textarea'),mapping={pi:'pi-probe',current:'optical-probe',slow_loop:'thermal-probe',feedforward:'optical-probe'};
params.value=JSON.stringify({adjustment_family:'auto',agent_card_id:'gateway-agent',adjustment_measurement_cards:mapping});
const card={id:'agent-adjust',operation:'agent.task',agent_execution:{mode:'required'}},deck={document:{cards:[{id:'gateway-agent',operation:'agent.resident',parameters:{}}]},invocation_parameters:{}};
const bundle={id:'thf685',laser_definition:{document:{devices:{dlc:{type:'dlcpro.laser.owner'}}}}};
const form=w.adjustmentExecutionEditor(bundle,deck,card,params);d.body.append(form);
const select=form.querySelector('[data-adjustment-family]');
for(const option of select.options)assert.equal(option.disabled,false,option.value);
assert(form.textContent.includes('Monitoring need not be running'));
select.value='feedforward';select.dispatchEvent(new w.Event('change'));
assert.equal(JSON.parse(params.value).adjustment_family,'feedforward');
assert.deepEqual(JSON.parse(params.value).adjustment_measurement_cards,mapping);
assert(form.querySelector('pre').textContent.includes('thermal-probe'));
dom.window.close();console.log('DLC adjustment family selectors preserve the frozen measurement references.');
