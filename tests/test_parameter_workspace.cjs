const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const assets=process.env.LDCON_TEST_ASSETS||path.resolve(__dirname,'../src/ldcon/web_assets');
const dom=new JSDOM('<div id="notice"></div>',{url:'http://fixture.invalid/',runScripts:'outside-only'}),w=dom.window,d=w.document;
let saved=[],errors=[],stale=false;
w.node=(tag,text='',cls='')=>{const el=d.createElement(tag);el.textContent=text;el.className=cls;return el;};
w.cell=(row,...children)=>{const el=d.createElement('td');for(const child of children)el.append(child?.nodeType?child:String(child));row.append(el);return el;};
w.button=(text,fn)=>{const b=w.node('button',text);b.onclick=()=>Promise.resolve().then(fn).catch(e=>errors.push(e.message));return b;};
w.api=async(route,method,body)=>{if(stale)throw Error('revision conflict');saved.push({route,method,body});return {};};
w.loadDefinitions=async()=>{};w.$=q=>d.querySelector(q);
w.eval(fs.readFileSync(path.join(assets,'configuration-editor.js'),'utf8')+'\n'+fs.readFileSync(path.join(assets,'laser-workspace.js'),'utf8'));
const document={id:'synthetic',schema_version:1,name:'Synthetic',allow_output:true,target:{frequency_hz:1e12,bounds_hz:[.9e12,1.1e12]},devices:{rig:{type:'ecdl.rig.owner',connection:{host:'never-contact.invalid',resources:{current_awg:{host:'awg.invalid',channel:2,preserve_channels:[1]}}},absolute_conditions:{current_bounds:[0,2]},control_settings:{steady_lock:{tec_gain:.1,integral_limit_count:40,native_input:{channel:3},calibration:{slope:1}},optical:{single_mode_max:{side_peak_height_max:.3},warning_max:{side_peak_height_max:.4},interferometer:'2',metric_method:'phase_free_v1'}}}},interfaces:{current:{device:'rig',capability:'bounded_axis'}},telemetry:{endpoint:'never-contact.invalid'}};
const definition={id:'synthetic',document,revision:'r1',source_path:'/tmp/synthetic/config/lasers/synthetic.yaml'};
const editors={};for(const group of ['operating','limits','definitions']){const editor=w.ldconConfigurationEditor(definition,{group});editors[group]=editor;d.body.append(editor);}
const paths=el=>[...el.querySelectorAll('[data-configuration-path]')].map(e=>e.dataset.configurationPath);
const all=paths(w.ldconConfigurationEditor(definition)),partition=Object.values(editors).flatMap(paths);
assert.equal(new Set(partition).size,all.length);assert.deepEqual(partition.sort(),all.sort());
const has=(group,parts)=>paths(editors[group]).includes(JSON.stringify(parts.split('.')));
assert(has('operating','devices.rig.control_settings.steady_lock.tec_gain'));
assert(has('operating','devices.rig.control_settings.optical.single_mode_max.side_peak_height_max'));
assert(has('limits','devices.rig.control_settings.steady_lock.integral_limit_count'));
assert(has('definitions','devices.rig.connection.host'));assert(!has('operating','devices.rig.connection.host'));
assert(has('definitions','devices.rig.control_settings.steady_lock.native_input.channel'));
async function tick(){await new Promise(r=>setTimeout(r,0));}
async function submit(editor){const b=t=>[...editor.querySelectorAll('button')].find(b=>b.textContent===t);b('変更差分を確認').click();await tick();const ack=editor.querySelector('[type=checkbox]');ack.checked=true;ack.dispatchEvent(new w.Event('change'));b('承認した差分を検証して保存').click();await tick();}
async function run(){
 const editor=editors.operating,field=[...editor.querySelectorAll('input')].find(e=>e.dataset.configurationPath==='["devices","rig","control_settings","steady_lock","tec_gain"]');field.value='.2';field.dispatchEvent(new w.Event('input'));await submit(editor);
 assert.equal(saved.length,1);assert.equal(saved[0].body.expected_revision,'r1');const actual=JSON.parse(saved[0].body.yaml),expected=structuredClone(document);expected.devices.rig.control_settings.steady_lock.tec_gain=.2;assert.deepEqual(actual,expected);
 stale=true;field.value='.3';field.dispatchEvent(new w.Event('input'));await submit(editor);assert.equal(saved.length,1);assert.deepEqual(errors,['revision conflict']);
 const fixture=structuredClone(document);fixture.devices.rig.connection.wavemeter={endpoint:'http://user:secret@wave.invalid/private?token=hidden',channel:3};
 fixture.devices.native={type:'esp32servocon.native_current.owner',connection:{device_id:'controller',endpoint:'http://native.invalid'},control_settings:{native_current:{calibration:{bias_current_a:.05},cld_interlock:{instrument:'current-driver'}}}};
 fixture.devices.wave={type:'fizeau.owner',connection:{endpoint:'http://broker.invalid',source:{endpoint:'http://source.invalid',channel:4}}};
 const table=w.ldconTopology(fixture);assert.equal(table.querySelectorAll('svg').length,0);
 const rows=[...table.querySelectorAll('tr')];const current=rows.find(r=>r.textContent.includes('awg.invalid'));assert(current.textContent.includes('CH 2'));assert(current.textContent.includes('not measured diode current'));
 const optical=rows.find(r=>r.textContent.includes('Optical mode quality'));assert(optical.textContent.includes('CH 3 · interferometer 2'));assert(optical.textContent.includes('phase_free_v1'));
 const wave=rows.find(r=>r.textContent.includes('source.invalid'));assert(wave.textContent.includes('CH 4'));assert(!wave.textContent.includes('broker.invalid'));
 assert(table.textContent.includes('Estimated modulated current'));assert(!/secret|token=|private\?|hidden/.test(table.textContent));
 assert.equal(table.querySelector('a'),null);dom.window.close();console.log('Parameter groups preserve unedited fields and CAS; instrumentation routes and estimates match configured sources.');
}
run().catch(e=>{console.error(e);dom.window.close();process.exitCode=1;});
