const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
let monotonic=1000;
const context={window:{},performance:{now:()=>monotonic},Date};
vm.runInNewContext(fs.readFileSync(path.join(assets,'dashboard-state.js'),'utf8'),context);
const display=context.window.ldconDisplayState, observe=context.window.ldconObserveDisplaySnapshot;
const server=1000000000; // Deliberately different from the viewing computer clock.
const job=()=>({status:'running',cards:[{operation:'servo.native_current'}],
  updated_utc:new Date(server*1000-300).toISOString(),controller_state:'locked',
  latest:{source_acquired_unix_s:server-1,locked:true,native_pi_active:true}});
const sample=job();observe({jobs:[sample],read_at_unix_s:server},900);
assert.equal(display([sample]).state,'LOCKED');
sample.latest.locked=false;
assert.equal(display([sample]).state,'TRACKING');
sample.latest.native_pi_active=false;
assert.equal(display([sample]).state,'HELD');
sample.latest.native_pi_active=true;
sample.latest.source_acquired_unix_s=server-26;
assert.equal(display([sample]).state,'UNKNOWN'); // Do not hide actual staleness.
sample.latest.source_acquired_unix_s=server-1;
monotonic+=26000;
assert.equal(display([sample]).state,'UNKNOWN'); // Cached snapshots keep ageing.
const delayed=job();observe({jobs:[delayed],read_at_unix_s:server},monotonic-30000);
assert.equal(display([delayed]).state,'UNKNOWN'); // Delayed replies are not fresh.
const future=job();future.latest.source_acquired_unix_s=server+5;
observe({jobs:[future],read_at_unix_s:server},monotonic);
assert.equal(display([future]).state,'UNKNOWN');
const absent=job();delete absent.latest.native_pi_active;absent.latest.locked=false;
observe({jobs:[absent],read_at_unix_s:server},monotonic);
assert.equal(display([absent]).state,'UNKNOWN'); // No inferred PI-on evidence.
const waiting=job();waiting.controller_state='waiting_for_measurement';waiting.latest.locked=false;
observe({jobs:[waiting],read_at_unix_s:server},monotonic);
assert.equal(display([waiting]).state,'WAITING FOR MEASUREMENT');
const ecdl=job();ecdl.cards=[{operation:'servo.ecdl_steady_lock'}];ecdl.controller_state='mode_degraded';
ecdl.latest.single_mode=false;delete ecdl.latest.native_pi_active;ecdl.latest.locked=false;
observe({jobs:[ecdl],read_at_unix_s:server},monotonic);
assert.equal(display([ecdl]).state,'DEGRADED');
console.log('Display state passed: server clock, monotonic age, delayed/stale/future evidence, PI and split-publication handling.');
