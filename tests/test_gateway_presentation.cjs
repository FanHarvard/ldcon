// Offline presentation test: no network, provider or hardware.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const read=n=>fs.readFileSync(path.join(assets,n),'utf8');
const dom=new JSDOM('<body><div id="connection"></div></body>',{runScripts:'outside-only'});
const w=dom.window,d=w.document;w.eval(read('gateway-presentation.js'));
const view=w.ldconGatewayPresentation,card={id:'diagnostic-agent',card:'agent.resident',params:{role:'gateway'}};
const agent={connected:true,session:{status:'ready',provider:'opencode',key:'["laser","deck","diagnostic-agent"]',bridge_id:'laser-test-opencode-diagnostic-agent-0123456789ab'},
  model_info:{resolved_model:'provider/model',resolved_effort:'high'},requests:[{id:'immutable-request-id',status:'queued'}]};
const original=JSON.stringify(agent),box=d.querySelector('#connection');
view.render(box,agent,card);
assert.equal(view.cardName(card),'Gateway agent');
assert.equal(view.cardName({...card,params:{role:'operator'}}),'Operator agent');
assert.equal(view.cardName({id:'gateway-agent'}),'Gateway agent');
assert.equal(view.cardName({id:'unrelated-card'}),null);
assert(!box.querySelector('p').textContent.includes('diagnostic'));
assert(box.querySelector('p').textContent.includes('1 pending requests'));
assert(box.querySelector('p').textContent.includes('provider/model'));
const details=box.querySelector('details'),raw=box.querySelector('pre');
assert(!details.open);assert.equal(raw.textContent,JSON.stringify(agent,null,2));
details.open=true;agent.requests[0].status='completed';view.render(box,agent,card);
assert.strictEqual(box.querySelector('details'),details);assert(details.open);
assert(box.querySelector('p').textContent.includes('0 pending requests'));
agent.requests[0].status='queued';assert.equal(JSON.stringify(agent),original);
view.render(box,{...agent,quarantined:true},card);
assert(box.querySelector('p').textContent.includes('Review required'));
for(const name of ['agent.request','agent.resident','agent.task','agent.monitor','agent.history_review','notify.teams']) {
  assert(!/diagnostic[-_ ]agent|診断エージェント/i.test(read(`cards/${name}.html`)),name);
}
assert(read('index.html').indexOf('gateway-presentation.js')<read('index.html').indexOf('assets/app.js'));
assert(read('app.js').includes('ldconGatewayPresentation.render'));
dom.window.close();console.log('Gateway labels: preserved routing/evidence, stable refresh, explicit technical IDs, six updated guides.');
