// Full live-control UI with synthetic jobs; no devices or actual commands.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom'),fixture=require('./dashboard_fixture.cjs');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets'),read=n=>fs.readFileSync(path.join(assets,n),'utf8');
const dom=new JSDOM(read('index.html'),{url:'http://localhost/s/test/?set=fixture#laser-settings',runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window,d=w.document;
const data=structuredClone(fixture.data()),requests=[],submitted=[];let complete=false,sequence=null,tec=null;
w.setInterval=()=>0;w.HTMLElement.prototype.scrollIntoView=()=>{};
w.fetch=async(url,options={})=>{const route=String(url).split('api/')[1],method=options.method||'GET';requests.push({route,method,body:options.body});let v;
 if(route==='dashboard/snapshot')v={jobs:[fixture.job()],exports:[]};
 else if(route.endsWith('/operating-mode'))v={mode:'characterization',revision:2,reason:'Characterize drift <img src=x>',updated_unix_s:1793030000};
 else if(route.startsWith('history?'))v={jobs:[],total:0};
 else if(route.endsWith('/validate'))v={runnable:true,blocked_reasons:[]};
 else if(route.endsWith('/start')){const body=JSON.parse(options.body),id=String(submitted.length+1).repeat(32);submitted.push({id,body});v={id,status:'queued'};}
 else if(route.startsWith('jobs/'))v={id:route.split('/')[1],status:complete?'succeeded':'running',lock_target:sequence?{sequence,tec_centering:tec}:null};
 else v=data[route];
 if(v===undefined)throw Error('Unexpected synthetic route '+route);return {ok:true,status:200,json:async()=>structuredClone(v)};
};
w.eval(['dashboard-state.js','window-actions.js','configuration-editor.js','laser-workspace.js','committing-input.js','app.js','i18n.js'].map(n=>n==='app.js'?read(n).replace('  const render=renderLaserConfiguration;', '  window.__controls={poll};\n  const render=renderLaserConfiguration;'):read(n)).join('\n')+'\nwindow.__test={idle:()=>!state.busy&&!polling};');
const tick=()=>new Promise(r=>setTimeout(r,10));async function wait(f){for(let i=0;i<200;i++){if(f())return;await tick();}throw Error(d.querySelector('#notice').textContent||d.querySelector('[data-control-availability]')?.textContent||'Timeout');}
(async()=>{
 await wait(()=>w.__test.idle()&&d.querySelector('.target-control'));await w.__controls.poll();
 await wait(()=>w.__test.idle()&&d.querySelector('.target-control button')&&!d.querySelector('.target-control button').disabled);
 const targetInput=d.querySelector('[data-target-input]'),decimalInput=d.querySelector('[data-target-decimals]'),send=d.querySelector('[data-send-on-arrows]');
 assert.equal(send.checked,false);assert.equal(decimalInput.value,'9');assert(/\.\d{9}$/.test(targetInput.value));
 const baseline=targetInput.value,plus=d.querySelector('.target-control .control-nudge button:last-child');
 plus.click();await tick();assert.equal(submitted.length,0);assert.equal(targetInput.dataset.syncState,'pending');
 targetInput.focus();targetInput.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 assert.equal(targetInput.value,baseline);assert.equal(d.activeElement,targetInput,'Escape restores the draft, not just blur');
 const knob=d.querySelector('[data-axis=current] input');knob.value='1.25';knob.dispatchEvent(new w.Event('input',{bubbles:true}));
 targetInput.blur();d.body.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 assert.equal(knob.dataset.syncState,'synced');assert.notEqual(knob.value,'1.25');assert.equal(submitted.length,0);
 // Escape in an unrelated editor must not discard control drafts.
 plus.click();const modeReason=d.querySelector('#laser-operating-mode input');modeReason.focus();
 modeReason.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));assert.equal(targetInput.dataset.syncState,'pending');
 modeReason.blur();d.body.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 decimalInput.value='3';decimalInput.dispatchEvent(new w.Event('input',{bubbles:true}));
 assert(/\.\d{3}$/.test(targetInput.value));assert.equal(targetInput.dataset.syncState,'synced');
 decimalInput.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));assert.equal(submitted.length,0);
 send.checked=true;send.dispatchEvent(new w.Event('change',{bubbles:true}));plus.click();await wait(()=>submitted.length===1);
 const originalHz=data['sets/fixture'].laser_definition.document.target.frequency_hz;
 assert.equal(submitted[0].body.parameters.queue_steps,true,'Every Web target entry opts into bounded server-side sequencing');
 assert(Math.abs(submitted[0].body.parameters.target_wavelength_nm-299792458e9/(originalHz+10e6))<1e-10,'Display decimals never quantize the nudge command');
 assert.equal(d.querySelector('[data-operating-mode-reason]').textContent,'Characterize drift <img src=x>');
 assert.equal(d.querySelector('#laser-operating-mode img'),null);
 assert.equal(d.querySelector('[data-operating-mode-changed]').dateTime,new Date(1793030000*1000).toISOString());
 assert(!d.querySelector('[data-operating-mode-changed]').textContent.includes('Not recorded'));
 complete=true;await w.__controls.poll();await wait(()=>w.__test.idle());
 const target=d.querySelector('[data-target-input]'),current=d.querySelector('[data-axis=current] input');
 function edit(e,value){e.value=value;e.dispatchEvent(new w.Event('input',{bubbles:true}));}
 edit(target,String(299792458e9/(originalHz+250e6)));edit(current,'1.2001');complete=false;
 target.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));await wait(()=>submitted.length===2);
 assert.equal(submitted[1].body.card_id,'target');assert.equal(submitted.length,2,'Knobs wait for target completion');
 assert.equal(submitted[1].body.parameters.queue_steps,true);
 sequence={step:1,step_target_hz:originalHz+100e6,final_target_hz:originalHz+250e6};
 await w.__controls.poll();assert(d.querySelector('[data-control-result]').textContent.includes('Step 1'));
 tec={status:'confirming',reason:'Confirming PZT remains centered'};
 await w.__controls.poll();assert(d.querySelector('[data-control-result]').textContent.includes('Waiting for TEC/PZT center'));
 assert.equal(submitted.length,2,'Frequency arrival alone must not release the other bulk edits');tec=null;
 sequence={...sequence,step:2,step_target_hz:originalHz+200e6};
 await w.__controls.poll();assert(d.querySelector('[data-control-result]').textContent.includes('Step 2'));
 assert.equal(submitted.length,2,'Intermediate completion must not submit the queued knob edit or another target job');
 assert.equal(JSON.parse(w.localStorage.getItem('ldcon.pending-controls.'+w.location.pathname+'.fixture')).id,submitted[1].id);
 // A later draft must not be overwritten by acknowledgement of the frozen bulk request.
 edit(current,'1.2002');complete=true;await w.__controls.poll();await wait(()=>submitted.length===3);
 assert.equal(submitted[2].body.card_id,'point');assert.equal(submitted[2].body.parameters.moves[0].target.value,1.2001);
 await w.__controls.poll();await wait(()=>w.__test.idle());
 assert.equal(current.value,'1.2002');assert.equal(current.dataset.syncState,'pending');
 assert.equal(submitted.length,3);assert(!requests.some(r=>/stop|restart/.test(r.route)));
 dom.window.close();console.log('Bulk UI: arrows opt in, focused/unfocused Escape, display precision, saved mode provenance, target completes before knobs, later drafts preserved, no implicit replay.');
})().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
