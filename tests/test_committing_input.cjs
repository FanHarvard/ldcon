// Shared input behavior only: synthetic clock and no fetch/network.
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),{JSDOM}=require('jsdom');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const dom=new JSDOM('<input id=a><input id=b>',{runScripts:'outside-only',pretendToBeVisual:true}),w=dom.window,d=w.document;
w.eval(fs.readFileSync(path.join(assets,'committing-input.js'),'utf8'));
let now=0,send=false,calls=0;const a=d.querySelector('#a'),b=d.querySelector('#b');
const e=new w.LDConCommittingInput(a,{step:(v,s)=>v+s*.0001,sendOnArrows:()=>send,onCommit:()=>calls++,clock:()=>now,format:v=>v.toFixed(4),quantum:1e-8});
e.observe(.75,1);assert.equal(a.value,'0.7500');a.focus();a.dispatchEvent(new w.KeyboardEvent('keydown',{key:'ArrowUp',bubbles:true}));
assert.equal(a.value,'0.7501');assert.equal(e.state,'pending');assert.equal(calls,0);
e.observe(.75,2);assert.equal(a.value,'0.7501');send=true;
a.dispatchEvent(new w.WheelEvent('wheel',{deltaY:-1,cancelable:true}));assert.equal(calls,1);assert.equal(a.value,'0.7502');
b.focus();a.dispatchEvent(new w.WheelEvent('wheel',{deltaY:-1,cancelable:true}));assert.equal(calls,1);
a.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));assert.equal(calls,2);
const sent=e.begin();e.observe(.7502,3);assert.equal(e.state,'confirming');e.accepted(sent);assert.equal(e.state,'synced');
e.nudge(1);const old=e.begin();e.nudge(1);e.accepted(old);assert.equal(e.state,'pending','Old acknowledgement cannot erase a newer edit');
now=61000;e.expire();assert.equal(e.state,'synced');assert.equal(a.value,'0.7502');
e.nudge(1);const uncertain=e.begin(),beforeReject=calls;e.rejected(uncertain);assert.equal(e.state,'rejected');assert.equal(calls,beforeReject,'No implicit retries');
a.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));assert.equal(e.state,'synced');
// Lower display precision does not alter an unsent exact value or acknowledgement.
let digits=3;const exact=new w.LDConCommittingInput(b,{step:(v,s)=>v+s*.00001,sendOnArrows:()=>false,onCommit:()=>{},format:v=>v.toFixed(digits),quantum:1e-10});
exact.observe(422.123456789,1);assert.equal(b.value,'422.123');assert.equal(exact.value(),422.123456789);
exact.nudge(1);const numeric=exact.value();digits=1;exact.reformat();assert.equal(exact.value(),numeric);assert.equal(exact.state,'pending');
const token=exact.begin();exact.revert();assert.equal(exact.state,'confirming','Escape cannot cancel a submitted operation');
exact.accepted(token);exact.observe(numeric,2);assert.equal(exact.state,'synced');assert.equal(exact.value(),numeric);
exact.nudge(1);exact.revert();assert.equal(exact.value(),numeric);assert.equal(b.value,numeric.toFixed(1));
dom.window.close();console.log('Committing input: draft/readback separation, selected steps, focused wheel, bulk callback, expiry and unknown result pass.');
