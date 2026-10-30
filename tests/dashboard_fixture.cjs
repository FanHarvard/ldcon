// Synthetic data only. No live addresses, credentials or device access.
const laser={schema_version:1,id:'fixture',name:'Sr422 · UI fixture',allow_output:true,
 target:{frequency_hz:710e12},devices:{rig:{type:'ecdl.rig.owner',connection:{endpoint:'http://owner.invalid',instrument:'fixture',resources:{piezo:{host:'pzt.invalid'},current_awg:{host:'ldc.invalid',channel:1,preserve_channels:[2]},tec_awg:{host:'tec.invalid',channel:1}},wavemeter:{endpoint:'http://wavemeter.invalid',channel:3}},control_settings:{axes:{current:{unit:'V'},temperature:{unit:'V'},piezo:{unit:'count'}}}}},
 interfaces:Object.fromEntries(['lock','current','piezo','temperature','observation'].map(port=>[port,{device:'rig',capability:port}]))};
const cards=[{id:'target',operation:'tune.lock_target',parameters:{target_frequency_hz:null,target_wavelength_nm:null,offset_hz:null}},
 {id:'point',operation:'tune.operating_point',parameters:{moves:[],schedule:null}},
 {id:'steady-lock',operation:'servo.ecdl_steady_lock',parameters:{}},
 {id:'diagnostic-agent',operation:'agent.resident',parameters:{provider:'claude',session_id:'',remote_control:false,mode:'create_or_connect'}},
 {id:'agent-request',operation:'agent.request',parameters:{agent_card_id:'diagnostic-agent',prompt:'Read the latest evidence'}}].map(c=>({...c,uses:[]}));
const assignment={schema_version:1,id:'fixture',laser_id:'fixture',decks:[{deck_id:'ecdl',parameters:{'steady-lock':{recovery:{procedure:{classes:[{name:'mode-hop',sequence:[{action:'current_excursion',offsets:[.1,-.1]}]}]}}}}}]};
const deck={id:'ecdl',document:{schema_version:1,id:'ecdl',name:'ECDL operations',interfaces:{},cards},revision:'deck-revision',source_path:'/tmp/fixture/config/interface-decks/ecdl.yaml',yaml:'{}',card_status:Object.fromEntries(cards.map(c=>[c.id,{runnable:true,parameters:c.parameters,blocked_reasons:[]}]))};
const bundle={id:'fixture',document:{name:laser.name},source_document:assignment,assignment_revision:'assignment-revision',source_path:'/tmp/fixture/config/assignments/fixture.yaml',yaml:JSON.stringify(assignment),revision:'bundle-revision',laser_definition:{id:laser.id,document:laser,revision:'laser-revision',source_path:'/tmp/fixture/config/lasers/fixture.yaml',yaml:JSON.stringify(laser)},deck_definitions:[deck]};
function data(){return {'sets':{sets:[{id:'fixture',name:laser.name}]},'sets/fixture':bundle,'catalog':{cards:[]},'interface-decks':{decks:[{id:'ecdl'}]},'configuration/interface-decks/ecdl':deck,'config':{state_directory:'/tmp/synthetic'},'retention':{enabled:false,retention_days:30,backup_directory:'',use_defaults:false,defaults_available:false,last_run:{}},'agent-providers':{providers:[{id:'claude',label:'Claude Code',available:true},{id:'codex',label:'Codex',available:true},{id:'opencode',label:'OpenCode',available:true}]}};}
function job(){return {id:'a'.repeat(32),set_id:'fixture',set_name:laser.name,deck_id:'ecdl',status:'running',controller_state:'locked',updated_utc:new Date().toISOString(),created_utc:new Date().toISOString(),cards:[{id:'steady-lock',operation:'servo.ecdl_steady_lock'}],latest:{recorded_unix_s:Date.now()/1000,frequency_hz:710e12,outputs:{current:1.2,piezo:32000,temperature:1.8},tec_center_reference_confirmed:true,tec_center_reference_dac:32768}};}
const originalData=data;
function dataWithConfiguration(){return {...originalData(),
 'configuration/lasers/fixture':bundle.laser_definition,
 'configuration/assignments/fixture':{id:'fixture',document:assignment,revision:'assignment-revision'}};}
module.exports={data:dataWithConfiguration,job};
