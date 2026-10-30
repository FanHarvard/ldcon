// Offline consultation UI wiring: mocked HTTP, no agents or instruments.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {JSDOM}=require('jsdom'),path=require('node:path');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const dom=new JSDOM(fs.readFileSync(path.join(assets,'failures.html'),'utf8'),{url:'http://localhost/s/test/failures?laser=sr422',runScripts:'outside-only'});
const w=dom.window,d=w.document,requests=[];
const failure={id:'case',laser_id:'sr422',card_id:'lock',job_id:'failed-job',created_unix_s:1700000000,updated_unix_s:1700000001,code:'CARD_FAILED',disposition:'human_required',blocks_downstream:true,routing_status:'queued',evidence:{message:'<img src=x onerror=bad()>'}};
const question={id:'question:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',question:'<script>Review patch?</script>',reason:'Needs confirmation',status:'pending'};
w.fetch=async(url,options={})=>{
 const route=String(url).split('/api/')[1];requests.push({route,...options});let value;
 if(route.startsWith('failures?'))value={cases:[failure]};
 else if(route==='failures/case')value={case:failure,questions:[question]};
 else if(route==='failures/case/questions/question%3Aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/answer'){assert.equal(options.headers['X-LDCon-Client'],'web');question.status='answered';question.answer=JSON.parse(options.body).answer;value=question;}
 else if(route==='failures/case/verify'){failure.blocks_downstream=false;failure.disposition='resolved';value=failure;}
 else throw Error('Unexpected request '+route);
 return {ok:true,json:async()=>value};
};
async function wait(fn){for(let i=0;i<100;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw Error(d.querySelector('#notice').textContent||'Timed out');}
function button(text){return [...d.querySelectorAll('button')].find(b=>b.textContent===text);}
async function run(){
 w.eval(fs.readFileSync(path.join(assets,'failures.js'),'utf8'));
 await wait(()=>button('Details / questions'));assert(requests[0].route.endsWith('laser_id=sr422'));
 button('Details / questions').click();await wait(()=>button('Send answer to agent'));
 assert(d.querySelector('#detail').textContent.includes('<script>Review patch?</script>'));
 assert.equal(d.querySelector('#detail img,#detail script'),null);
 d.querySelector('#detail textarea').value='Test the isolated patch first';button('Send answer to agent').click();
 await wait(()=>d.querySelector('#notice').textContent.includes('remains held'));
 assert.equal(failure.blocks_downstream,true);assert.equal(requests.filter(r=>r.method==='POST').length,1);
 d.querySelector('#detail input').value='new-job';d.querySelector('#detail textarea').value='Observed the successful replacement';
 button('Verify evidence and release hold').click();await wait(()=>d.querySelector('#notice').textContent.includes('hold has been released'));
 const sent=requests.find(r=>r.route.endsWith('/verify'));assert.deepEqual(JSON.parse(sent.body),{replacement_job_id:'new-job',note:'Observed the successful replacement'});
 assert(!requests.some(r=>/restart|\/start|\/stop/.test(r.route)));
 dom.window.close();process.stdout.write('Failure review DOM: scoped consultation, escaped evidence, answer keeps hold, verified replacement only passed\n');
}
run().catch(e=>{process.stderr.write(e.stack+'\n');dom.window.close();process.exitCode=1;});
