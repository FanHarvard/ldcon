'use strict';
const $=s=>document.querySelector(s), base=location.pathname.replace(/\/?$/,'/')+'api/';
const terminal=new Set(['succeeded','completed','completed_with_failures','failed','cancelled','interrupted','inconclusive']);
const state={sets:[],bundles:[],decks:[],cards:[],jobs:[],exports:[],wavelengths:{},selected:'',view:'lasers',offset:0,total:0,busy:false,historyVersion:0,connected:false};
function node(tag,text='',className=''){const e=document.createElement(tag);e.textContent=text;e.className=className;return e;}
function button(text,fn,cls=''){const e=node('button',text,cls);e.type='button';e.addEventListener('click',()=>act(fn));return e;}
function link(text,url){const e=node('a',text);e.href=url;e.target='_blank';e.rel='noopener';return e;}
function pre(value){return node('pre',typeof value==='string'?value:JSON.stringify(value,null,2));}
function displayReadings(value){return Object.fromEntries(Object.entries(value||{}).filter(([key])=>key!=='wavelength_nm'));}
function badge(value){return node('span',value||'idle','badge '+(value||'idle'));}
function cell(row,...values){const td=node('td');for(const v of values)td.append(typeof v==='string'?document.createTextNode(v):v);row.append(td);return td;}
function empty(tbody,cols,text){const row=node('tr'),td=cell(row,text);td.colSpan=cols;td.className='empty';tbody.append(row);}
function error(e){$('#notice').textContent=e.message||String(e);$('#notice').hidden=false;}
async function api(route,method='GET',data,retried){const response=await fetch(base+route,{method,headers:{'Content-Type':'application/json','X-LDCon-Client':'web'},body:data===undefined?undefined:JSON.stringify(data)});const value=await response.json();if(!response.ok){const detail=value.detail||response.statusText;if(!retried&&response.status===422&&data&&typeof detail==='string'&&/maintainer reference|approval_id/.test(detail)){const reference=window.prompt('安全範囲または保守者専用の項目を変更します。承認・根拠の参照（誰が・いつ・何を承認したか）を入力してください。空欄で中止します。\n'+detail,'');if(reference&&reference.trim())return api(route,method,{...data,maintainer_reference:reference.trim()},true);}throw Error(detail);}return value;}
function signalBusy(){if(new URLSearchParams(location.search).get('embed')==='1'&&window.parent!==window)window.parent.postMessage({type:'ldcon-busy',busy:state.busy,context:location.pathname},location.origin);}
window.ldconIsBusy=()=>state.busy;
async function act(fn){if(state.busy)return;state.busy=true;signalBusy();try{$('#notice').hidden=true;await fn();}catch(e){error(e);}finally{state.busy=false;signalBusy();updateCardStatus();}}
function cardJob(id,card){return state.jobs.find(j=>j.set_id===id&&!terminal.has(j.status)&&j.cards?.some(c=>c.id===card));}
function active(id){const jobs=state.jobs.filter(j=>j.set_id===id&&!j.read_only&&!terminal.has(j.status));return jobs.find(j=>j.cards?.some(c=>(c.operation||c.card||'').startsWith('servo.')))||jobs.find(j=>!j.cards?.every(c=>['agent.resident','agent.log_watch','agent.monitor','agent.task','agent.history_review','agent.request','monitor.wavelength','acquire.metrics','notify.teams','report.grafana_dashboard'].includes(c.operation||c.card)));}
function timestamp(s){return s?new Date(s).toLocaleString():'—';}
function cardLabel(operation,parameters={},id=''){const roleName=window.ldconGatewayPresentation?.cardName({operation,parameters,id});if(roleName)return roleName;const label=window.ldconCardBrowser?.label(operation,parameters,id);if(label)return label;return operation==='agent.resident'?(parameters.role==='operator'?'Operator agent':'Gateway agent'):operation==='servo.native_current'?'電流ロック（コントローラー内PI）':null;}
function cardDisplay(job){return job?.cards?.some(c=>(c.operation||c.card||'').startsWith('servo.'))&&window.ldconDisplayState?window.ldconDisplayState([job]).state:(job?.controller_state||job?.status);}
function expandable(tbody,row,group,cols,content){tbody.append(row);const detail=node('tr','','details-row');detail.hidden=true;detail.dataset.group=group;const td=cell(detail,content);td.colSpan=cols;content.classList.add('details-body');tbody.append(detail);const toggle=button('詳細',()=>{detail.hidden=!detail.hidden;toggle.setAttribute('aria-expanded',String(!detail.hidden));});toggle.setAttribute('aria-expanded','false');detail._toggle=toggle;return toggle;}
function switchView(view){const fleet=location.pathname.match(/^(.*\/)services\/[^/]+\/?$/);if(view==='lasers'&&fleet){location.assign(fleet[1]+'#lasers');return;}if(!['lasers','laser-settings','decks','catalog','settings'].includes(view))view='lasers';state.view=view;for(const section of document.querySelectorAll('.view'))section.hidden=section.id!==view;for(const b of document.querySelectorAll('[data-view]'))b.setAttribute('aria-selected',String(b.dataset.view===view));location.hash=view;}
function editor(kind,value,label='YAML設定を編集'){const wrapper=node('details');wrapper.append(node('summary',label));const area=node('textarea');area.value=value.yaml;area.setAttribute('aria-label',label);area.rows=14;wrapper.append(area,node('p','この定義を使うカードの稼働中は保存できません。無関係な定義は編集できます。保存だけではownerへ反映しません。安全条件の変更は対象のカード・所有者リースが空であることを確認して反映してください。','muted'),button('検証して保存',async()=>{const saved=await api(`configuration/${kind}/${encodeURIComponent(value.id)}`,'PUT',{yaml:area.value,expected_revision:value.revision});value.revision=saved.revision;value.yaml=saved.yaml;await loadDefinitions();$('#notice').textContent='保存しました。所有者サービスの設定反映は運用ガイドを確認してください。';$('#notice').hidden=false;},'primary'));return wrapper;}
// Polling updates values in place: a focused or pressed control keeps its identity.
function renderLasers(){
 const list=$('#laser-list');list.closest('table').classList.add('laser-overview-table');
 const retained=new Map([...list.querySelectorAll('[data-laser-row]')].map(row=>[row.dataset.laserRow,row]));
 for(const emptyRow of list.querySelectorAll('[data-empty-row]'))emptyRow.remove();
 for(const set of state.sets){
  let row=retained.get(set.id);retained.delete(set.id);
  if(!row){
   row=node('tr');row.dataset.laserRow=set.id;
   const name=node('strong'),id=node('small'),status=badge('UNKNOWN');
   cell(row,name,id);cell(row,status);const cards=cell(row),updated=cell(row),body=node('div');
   const wave=node('p'),note=node('p'),reason=node('p','','muted'),record=pre({});body.append(wave,note,reason,record);
   const toggle=expandable(list,row,'laser-details',5,body),detail=row.nextSibling;detail.dataset.laserDetail=set.id;
   const settings=button('設定',()=>{state.selected=set.id;$('#laser-select').value=set.id;renderLaserConfiguration();switchView('laser-settings');});
   const stop=button('停止',async()=>{const current=active(set.id);if(!current)return;await api(`jobs/${current.id}/stop`,'POST',{});await refresh();},'danger');
   cell(row,toggle,settings,stop).className='actions';
   row._parts={name,id,status,cards,updated,wave,note,reason,record,detail,stop};
  }
  const v=row._parts,job=active(set.id),display=window.ldconDisplayState?window.ldconDisplayState(state.jobs.filter(j=>j.set_id===set.id)):{state:'UNKNOWN'};
  v.name.textContent=set.name||set.id;v.id.textContent=set.id;
  const label=set.error?'設定エラー':!state.connected?'UNKNOWN':display.state;
  v.status.textContent=label;v.status.className='badge '+label;
  v.cards.textContent=job?.cards?.map(c=>cardLabel(c.operation||c.card,c.params,c.id)||c.id).join(', ')||'—';v.updated.textContent=timestamp(job?.updated_utc);
  const wave=state.wavelengths[set.laser_id||set.id];
  v.wave.textContent=wave?.valid?`Wavelength: ${wave.wavelength_nm.toFixed(9)} nm · CH ${wave.channel} · ${new Date(wave.acquired_unix_s*1000).toLocaleTimeString()}`:'Wavelength: —';
  v.note.textContent=set.blocked_reason||set.error||'定義の検証済み';v.reason.textContent=display.reason||'状態を確認できません。';
  v.record.textContent=JSON.stringify(job?{status:job.status,message:job.message,online:job.online_tuning,latest:displayReadings(job.latest)}:{status:'idle',note:'現在の光学状態は未測定'},null,2);
  v.stop.disabled=!job||!state.connected;
 }
 for(const row of retained.values()){row._parts.detail.remove();row.remove();}
 if(!state.sets.length){empty(list,5,'レーザー定義がありません。運用ガイドの移行手順で設定を導入してください。');list.lastChild.dataset.emptyRow='';}
}
// Readiness is a runtime preflight, not permission to start a card.
function readinessLabel(reasons){
 const text=(reasons||[]).join(' ');
 if(/running.*controller|running ECDL|live controller|active card|online operation/.test(text))return 'Waiting for controller';
 if(/staircases only|system binding required/.test(text))return 'Unsupported route';
 if(/recorded (map|plan) artifact/.test(text))return 'Requires measurement';
 if(/Agent card and prompt|objective is required/.test(text))return 'Enter request';
 return 'Needs configuration';
}
function checkAttachedReadiness(bundle,deck,card,params,start,statusCell,validation){
 if(!['tune.pid_factor_reduction','tune.pid_coordinate_search','study.ecdl_locked_current_map','tune.ecdl_window_center'].includes(card.operation))return;
 const snapshot=params.value;
 queueMicrotask(async()=>{try{
  const ready=await api(`sets/${bundle.id}/decks/${deck.id}/cards/${card.id}/validate`,'POST',
    {expected_revision:bundle.revision,parameters:JSON.parse(snapshot)});
  if(params.value!==snapshot||!start.isConnected)return;
  start.dataset.runnable=String(ready.runnable);statusCell.dataset.ready=String(ready.runnable);
  statusCell.dataset.blockedLabel=readinessLabel(ready.blocked_reasons);
  start.title=ready.blocked_reasons.join(' / ');start.dataset.validationReason=start.title;validation.textContent=ready.runnable?'Ready; checked again at Start.':start.title;
  updateCardStatus();
 }catch(error){if(params.value===snapshot)validation.textContent=error.message;}});
}
function cardTable(bundle,deck,agentOptionsHost=null){const box=node('div','','card-table-wrap'),table=node('table','','card-table'),head=node('thead'),hr=node('tr');for(const text of ['カード','状態','操作'])hr.append(node('th',text));head.append(hr);table.append(head);const tbody=node('tbody');table.append(tbody);box.append(table);const browserEntries=[];for(const card of deck.document.cards){const status=deck.card_status[card.id],row=node('tr'),body=node('div');cell(row,node('strong',cardLabel(card.operation,status.parameters,card.id)||card.id),node('small',card.operation==='agent.resident'?card.operation:card.id+' · '+card.operation));if(card.operation==='servo.native_current')body.append(node('p','ESP32サーボコントローラー内のPIで電流を調整して周波数を保ちます。nativeはこの制御方式を指し、ロック状態ではありません。','muted'));const statusCell=cell(row);statusCell.dataset.ready=String(status.runnable);statusCell.dataset.blockedLabel=readinessLabel(status.blocked_reasons);statusCell.dataset.cardStatus=card.id;statusCell.dataset.setId=bundle.id;const stateDetail=node('p','','card-state-detail muted');stateDetail.dataset.cardStateDetail=card.id;body.append(stateDetail);const params=node('textarea');params.value=JSON.stringify(status.parameters,null,2);params.rows=10;params.setAttribute('aria-label',card.id+' 今回のパラメーター');const help=link('使える条件・測定内容 ↗',`cards/${encodeURIComponent(card.operation)}`);const raw=node('details');raw.append(node('summary','今回の実行パラメーター（JSON）'),params);body.append(raw);if(status.blocked_reasons.length)body.prepend(node('p',status.blocked_reasons.join(' / '),'muted'));const toggle=expandable(tbody,row,'cards-'+deck.id,3,body);const start=button('開始',async()=>{const parameters=JSON.parse(params.value);await api(`sets/${bundle.id}/decks/${deck.id}/start`,'POST',{expected_revision:bundle.revision,card_id:card.id,parameters});await refresh();},'primary');start.dataset.runnable=String(status.runnable);start.title=status.blocked_reasons.join(' / ');start.dataset.validationReason=start.title;start.dataset.startCard=card.id;start.dataset.operation=card.operation;start.dataset.setId=bundle.id;let validationVersion=0,validationTimer;const validation=node('p',status.blocked_reasons.join(' / '),'muted');body.append(validation);params.addEventListener('input',()=>{const version=++validationVersion;start.dataset.runnable='false';start.dataset.validationReason='Checking run parameters.';statusCell.dataset.ready='false';updateCardStatus();clearTimeout(validationTimer);validation.textContent='入力の検証待ち';validationTimer=setTimeout(async()=>{try{const ready=await api(`sets/${bundle.id}/decks/${deck.id}/cards/${card.id}/validate`,'POST',{expected_revision:bundle.revision,parameters:JSON.parse(params.value)});if(version!==validationVersion)return;start.dataset.runnable=String(ready.runnable);statusCell.dataset.ready=String(ready.runnable);statusCell.dataset.blockedLabel=readinessLabel(ready.blocked_reasons);start.title=ready.blocked_reasons.join(' / ');start.dataset.validationReason=start.title;validation.textContent=ready.runnable?'入力の検証済み':start.title;updateCardStatus();}catch(e){if(version===validationVersion)validation.textContent=e.message;}},250);});if(card.operation==='agent.monitor')body.append(node('p','Continuous monitoring. Runs an initial one-shot assessment, then another when a schedule or configured condition is due. A completed adjustment does not stop monitoring.','muted'));if(card.operation==='agent.resident'){body.append(node('p','Gateway agent receives requests, dispatches task cards and relays their results. Monitoring and physical adjustments run as separate tasks.','muted'));const details=node('div');details.dataset.agentCard=card.id;details.dataset.setId=bundle.id;body.append(node('h3','エージェントの接続・依頼結果'),details);const options=window.ldconResidentOptions(params,bundle.laser_definition.document.id);options.dataset.gatewayCard=card.id;(agentOptionsHost||body).append(options);}if(card.operation==='report.grafana_dashboard'){const downloads=node('div');downloads.dataset.exportSet=bundle.id;downloads.dataset.exportCard=card.id;body.append(downloads);}const stop=button('停止',async()=>{const online=['tune.pid_factor_reduction','tune.pid_coordinate_search','study.ecdl_locked_current_map','tune.ecdl_window_center'].includes(card.operation);const job=cardJob(bundle.id,card.id)||(online?active(bundle.id):null);if(!job)return;if(online&&job.online_tuning?.card_id!==card.id)throw Error('このカードの実行を確認できません。状態を更新してください。');if(online)await api(`jobs/${job.id}/online-tuning/stop`,'POST',{});else await api(`jobs/${job.id}/stop`,'POST',{});await refresh();},'danger');stop.dataset.stopCard=card.id;stop.dataset.setId=bundle.id;const secondary=node('div','','card-secondary');body.prepend(secondary);const primary=node('div','','card-primary');primary.append(start,stop,toggle);cell(row,primary).className='actions card-actions';checkAttachedReadiness(bundle,deck,card,params,start,statusCell,validation);browserEntries.push({card,row,detail:row.nextElementSibling,search:window.ldconCardBrowser?.searchable({...card,parameters:status.parameters},state.cards.find(c=>c.id===card.operation))||card.id});}window.ldconCardBrowser?.mount(box,table,browserEntries,bundle.id+':'+deck.id);return box;}
function renderLaserConfiguration(){
 const panel=$('#laser-configuration');panel.replaceChildren();const bundle=state.bundles.find(b=>b.id===state.selected);
 const laser=bundle?.laser_definition;document.title=laser?`${laser.document?.name||laser.id||state.selected} — LDCon`:'LDCon';
 if(!bundle){panel.append(node('p','設定するレーザーを選択してください。'));return;}
 panel.append(window.ldconLaserWorkspace(bundle));updateCardStatus();
}

function updateExportLinks(){for(const element of document.querySelectorAll('[data-export-set]')){const latest=state.exports.find(j=>j.set_id===element.dataset.exportSet&&j.cards?.some(c=>c.id===element.dataset.exportCard)&&j.status==='succeeded');if(latest&&element.dataset.job!==latest.id){element.replaceChildren(link('Grafana JSON ↗',base+`jobs/${latest.id}/artifacts/grafana-dashboard.json`));element.dataset.job=latest.id;}}}
// A response study attaches to the running lock; it must not be treated as
// a second exclusive controller. The backend still revalidates every start.
function targetResponseStartBlocked(setId){
 const jobs=state.jobs.filter(j=>j.set_id===setId&&!terminal.has(j.status));
 const operations=j=>(j.cards||[]).map(c=>c.operation||c.card);
 const locks=jobs.filter(j=>operations(j).some(op=>['servo.ecdl_steady_lock','servo.native_current'].includes(op)));
 if(!state.connected||locks.length!==1||locks[0].status!=='running')return true;
 const observers=new Set(['monitor.wavelength','report.grafana_dashboard','plan.ecdl_recovery','agent.resident','agent.request','agent.log_watch','agent.monitor','acquire.metrics','notify.teams','analyze.shot_sweep','tune.sweep_tracking']);
 return jobs.some(j=>j!==locks[0]&&(!operations(j).length||!operations(j).every(op=>observers.has(op))));
}
function measurementStartBlocked(setId){
 const jobs=state.jobs.filter(j=>j.set_id===setId&&!terminal.has(j.status));
 const allowed=new Set(['servo.ecdl_steady_lock','servo.native_current','agent.resident','agent.request','agent.monitor','agent.history_review','acquire.metrics','notify.teams','report.grafana_dashboard']);
 return !state.connected||jobs.some(j=>!(j.cards||[]).length||!j.cards.every(c=>allowed.has(c.operation||c.card)));
}
function cardStartAvailability(e){
 const job=active(e.dataset.setId), operation=e.dataset.operation;
 const blocked=(summary,detail=summary)=>({summary,detail});
 if(!state.connected)return blocked('Connection unavailable','Reconnect before starting a card.');
 if(e.dataset.runnable!=='true')return blocked('Parameters need validation',e.dataset.validationReason||'Check the parameters for this run.');
 if(['measure.target_response','optimize.settling_criteria','optimize.target_feedforward','optimize.target_thermal','optimize.target_step'].includes(operation))return targetResponseStartBlocked(e.dataset.setId)?blocked('Controller unavailable or busy','Requires one running lock and no conflicting operation.'):null;
 if(['measure.change','measure.batch'].includes(operation))return measurementStartBlocked(e.dataset.setId)?blocked('Control slot in use','Another control operation is running.'):null;
 if(['tune.pid_factor_reduction','tune.pid_coordinate_search','study.ecdl_locked_current_map','tune.ecdl_window_center'].includes(operation)){
  if(!job||!job.live_controller?.controller_card_id)return blocked('Running lock required','Start the compatible lock before this card.');
  return ['queued','running'].includes(job.online_tuning?.status)&&!(operation==='tune.ecdl_window_center'&&job.window_controller?.status==='running')?blocked('Adjustment already running'):null;
 }
 if(['report.grafana_dashboard','agent.request','acquire.metrics','notify.teams'].includes(operation))return null;
 if(['agent.resident','agent.log_watch','agent.monitor','agent.task','agent.history_review'].includes(operation))return cardJob(e.dataset.setId,e.dataset.startCard)?blocked('This card is already running'):null;
 return job?blocked('Control slot in use',['Control slot in use',(job.cards||[]).map(c=>c.id||c.operation||c.card).join(', '),'Stop the running control card before starting this card.'].join(' · ')):null;
}
function updateCardStatus(){
 updateExportLinks();
 const availability=new Map();
 for(const e of document.querySelectorAll('[data-start-card]')){const reason=cardStartAvailability(e);availability.set(e.dataset.setId+'/'+e.dataset.startCard,reason);e.disabled=Boolean(reason);e.title=reason?.detail||'';}
 for(const e of document.querySelectorAll('[data-agent-card]')){const job=cardJob(e.dataset.setId,e.dataset.agentCard);if(window.ldconGatewayPresentation)window.ldconGatewayPresentation.render(e,job?.agent,job?.cards?.find(c=>c.id===e.dataset.agentCard));else e.textContent=JSON.stringify(job?.agent||{status:'disconnected'},null,2);}
 for(const e of document.querySelectorAll('[data-card-status]')){
  const job=cardJob(e.dataset.setId,e.dataset.cardStatus)||active(e.dataset.setId),running=job?.cards?.some(c=>c.id===e.dataset.cardStatus),online=job?.online_tuning?.card_id===e.dataset.cardStatus&&['queued','running'].includes(job.online_tuning.status);
  if(!e._parts){
   const label=badge(''),meta=node('div','','card-status-meta'),updated=node('small'),report=link('PDFレポート ↗','#');
   meta.append(updated,report);e.append(label,meta);e._parts={label,updated,report};
  }
  const unavailable=availability.get(e.dataset.setId+'/'+e.dataset.cardStatus);
  const v=e._parts,label=running?cardDisplay(job):online?job.online_tuning.status:unavailable&&e.dataset.ready==='true'?'Blocked':e.dataset.ready==='true'?'待機':e.dataset.blockedLabel||'要設定';
  v.label.textContent=label;v.label.className='badge '+label;
  const display=running&&window.ldconDisplayState&&job.cards?.some(c=>(c.operation||c.card||'').startsWith('servo.'))?window.ldconDisplayState([job]):null;
  e.title=display?.reason||unavailable?.detail||label;v.label.title=e.title;
  const detail=e.closest('tr').nextElementSibling?.querySelector('[data-card-state-detail]');
  if(detail)detail.textContent=display?.reason||unavailable?.detail||label;
  v.updated.textContent=running?'更新: '+timestamp(job.updated_utc):unavailable?.summary||'—';
  v.updated.title=v.updated.textContent;
  v.report.style.visibility=running?'visible':'hidden';v.report.tabIndex=running?0:-1;
  v.report.setAttribute('aria-hidden',String(!running));
  if(running)v.report.href=base+`jobs/${encodeURIComponent(job.id)}/report.pdf`;else v.report.removeAttribute('href');
 }
 for(const e of document.querySelectorAll('[data-stop-card]')){const job=cardJob(e.dataset.setId,e.dataset.stopCard)||active(e.dataset.setId);e.hidden=false;e.disabled=!state.connected||!job||(!job.cards?.some(c=>c.id===e.dataset.stopCard)&&!(job.online_tuning?.card_id===e.dataset.stopCard&&['queued','running'].includes(job.online_tuning.status)));}}
function renderDecks(){const list=$('#deck-list');list.replaceChildren();for(const deck of state.decks){const row=node('tr'),body=node('div');cell(row,node('strong',deck.document.name),node('small',deck.id));cell(row,String(deck.document.cards.length));cell(row,state.bundles.filter(b=>b.deck_definitions.some(d=>d.id===deck.id)).map(b=>b.document.name).join(', ')||'未割当');const t=node('table');for(const c of deck.document.cards){const r=node('tr');cell(r,cardLabel(c.operation,c.parameters,c.id)||c.id);cell(r,c.operation);cell(r,link('HTML説明 ↗','cards/'+c.operation));t.append(r);}body.append(t,editor('interface-decks',deck,'共通デッキの設定を編集'));cell(row,expandable(list,row,'deck-details',4,body));}}
function renderCatalog(){const list=$('#catalog-list'),q=$('#catalog-search').value.toLowerCase();list.replaceChildren();for(const card of state.cards.filter(c=>window.ldconCardBrowser?window.ldconCardBrowser.matches(q,window.ldconCardBrowser.searchable(c,c)):JSON.stringify(c).toLowerCase().includes(q))){const row=node('tr'),body=node('div');cell(row,node('strong',card.id));cell(row,card.description);cell(row,card.capabilities.join(', ')||(['report.grafana_dashboard','agent.resident','agent.request','agent.log_watch','agent.monitor','agent.task','agent.history_review','notify.teams','plan.ecdl_recovery'].includes(card.id)?'不要（機器アクセスなし）':'実行中の制御器'));body.append(node('p',card.physical_basis),node('h3','公開する測定値'),pre(card.outputs),link('詳細なHTML説明 ↗','cards/'+card.id));cell(row,expandable(list,row,'catalog-details',4,body),link('HTML説明 ↗','cards/'+card.id));}if(!list.children.length)empty(list,4,'一致するカードがありません。');}
function toggleGroup(group,open){for(const row of document.querySelectorAll('[data-group]'))if(row.dataset.group===group){row.hidden=!open;row._toggle?.setAttribute('aria-expanded',String(open));}}
async function loadRunHistory(){const version=++state.historyVersion,limit=Number($('#history-limit').value),q=$('#history-search').value;const value=await api(`history?q=${encodeURIComponent(q)}&limit=${limit}&offset=${state.offset}`);if(version!==state.historyVersion)return;state.total=value.total;const list=$('#history-list');list.replaceChildren();for(const j of value.jobs){const row=node('tr');cell(row,timestamp(j.created_utc));cell(row,j.set_name||j.set_id||'—',node('small',j.deck_id));cell(row,badge(j.status));cell(row,j.archived?'圧縮済み':'ローカル');cell(row,link(j.id.slice(0,12)+' ↗',base+`jobs/${j.id}/report.pdf`));list.append(row);}if(!value.jobs.length)empty(list,5,'該当する実行ログがありません。');$('#history-count').textContent=`${value.total} 件 / ${value.total?state.offset+1:0}–${Math.min(value.total,state.offset+limit)}`;$('#history-prev').disabled=state.offset===0;$('#history-next').disabled=state.offset+limit>=value.total;}
async function loadDefinitions(){state.sets=(await api('sets')).sets;state.bundles=await Promise.all(state.sets.filter(s=>!s.error).map(s=>api('sets/'+s.id)));const decks=(await api('interface-decks')).decks;state.decks=await Promise.all(decks.filter(d=>!d.error).map(d=>api('configuration/interface-decks/'+d.id)));if(!state.bundles.some(b=>b.id===state.selected)){const requested=new URLSearchParams(location.search).get('set');state.selected=state.bundles.find(b=>b.id===requested)?.id||state.bundles[0]?.id||'';}const select=$('#laser-select');window.ldconRenderLaserSelector({select,choices:state.bundles.map(b=>({id:b.id,name:b.laser_definition?.document?.name||b.document.name||b.id})),selected:state.selected,selectLocal:id=>{state.selected=id;renderLaserConfiguration();}});renderLasers();renderLaserConfiguration();renderDecks();}
let polling=false, historyRefreshAt=0;
function dashboardVisible(){try{return !document.hidden && !(window.frameElement && (window.frameElement.hidden || window.frameElement.closest('[hidden]')));}catch(_){return false;}}
async function refresh(){
 if(polling)return;polling=true;
 try{
  const requestStarted=performance.now(),snapshot=await api('dashboard/snapshot');
  window.ldconObserveDisplaySnapshot?.(snapshot,requestStarted);
  state.jobs=snapshot.jobs;state.exports=snapshot.exports;state.wavelengths=snapshot.wavelengths||{};state.connected=true;
  renderLasers();updateCardStatus();$('#connection').textContent='接続済み · '+new Date().toLocaleTimeString();
  if(state.view==='lasers' && Date.now()-historyRefreshAt>30000){historyRefreshAt=Date.now();loadRunHistory().catch(error);}
 }catch(e){state.connected=false;renderLasers();updateCardStatus();$('#connection').textContent='接続不明';error(e);}
 finally{polling=false;}
}
async function retention(){const v=await api('retention');const inherit=$('#retention-inherit');if(inherit){inherit.checked=v.use_defaults===true;inherit.disabled=v.defaults_available!==true;$('#retention-source').textContent=v.source==='built_in'?'共通設定への接続が未設定です。サービス既定値を使用中':v.use_defaults===true?'全レーザー共通の既定値を使用中':'このレーザーの個別設定を使用中';for(const id of ['#retention-enabled','#retention-days','#backup-directory'])$(id).disabled=inherit.checked;}$('#retention-enabled').checked=v.enabled;$('#retention-days').value=v.retention_days;$('#backup-directory').value=v.backup_directory;$('#archive-status').textContent=JSON.stringify(v.last_run,null,2);}
async function boot(){for(const b of document.querySelectorAll('[data-view]'))b.onclick=()=>switchView(b.dataset.view);for(const b of document.querySelectorAll('[data-expand]'))b.onclick=()=>toggleGroup(b.dataset.expand,b.dataset.open==='true');window.addEventListener('hashchange',()=>switchView(location.hash.slice(1)));$('#laser-select').onchange=()=>{state.selected=$('#laser-select').value;renderLaserConfiguration();};$('#catalog-search').oninput=renderCatalog;let debounce;$('#history-search').oninput=()=>{state.offset=0;clearTimeout(debounce);debounce=setTimeout(()=>act(loadRunHistory),200);};$('#history-limit').onchange=()=>{state.offset=0;act(loadRunHistory);};$('#history-prev').onclick=()=>{state.offset=Math.max(0,state.offset-Number($('#history-limit').value));act(loadRunHistory);};$('#history-next').onclick=()=>{state.offset+=Number($('#history-limit').value);act(loadRunHistory);};$('#retention-form').onsubmit=e=>{e.preventDefault();act(async()=>{await api('retention','PUT',$('#retention-inherit')?.checked?{use_defaults:true}:{enabled:$('#retention-enabled').checked,retention_days:Number($('#retention-days').value),backup_directory:$('#backup-directory').value});await retention();});};$('#archive-now').onclick=()=>act(async()=>{await api('retention/archive','POST',{});await retention();await loadRunHistory();});await act(async()=>{state.cards=(await api('catalog')).cards;renderCatalog();await loadDefinitions();await retention();const config=await api('config');$('#storage-directory').textContent='運用データ: '+config.state_directory;await refresh();});switchView(location.hash.slice(1)||'lasers');setInterval(()=>{if(!state.busy&&dashboardVisible())refresh();},3000);}
if(new URLSearchParams(location.search).get('embed')==='1')document.documentElement.classList.add('fleet-embedded');
boot();

/* Notification settings are independent of the running control process. */

// Device metadata does not alter frozen execution settings.

// Definition drafts are configuration-only; no owner registration or restart RPC.
(()=>{let revision=null;const kind=$('#configuration-kind'),id=$('#configuration-id'),area=$('#configuration-yaml'),result=$('#configuration-result');
function reset(){revision=null;result.textContent='新規作成。既存のIDは上書きしません。';}
function template(){reset();const name=id.value;const value=kind.value==='lasers'?{schema_version:1,id:name,name,allow_output:false,devices:{},interfaces:{}}:kind.value==='interface-decks'?{schema_version:1,id:name,name,interfaces:{wavelength:'frequency_monitor'},cards:[{id:'wavelength-monitor',operation:'monitor.wavelength',uses:['wavelength'],parameters:{}}]}:{schema_version:1,id:name,laser_id:state.bundles[0]?.laser_definition?.id||'laser-id',decks:[{deck_id:state.decks[0]?.id||'deck-id',parameters:{}}]};area.value=JSON.stringify(value,null,2);}
kind.onchange=reset;id.oninput=reset;$('#configuration-template').onclick=template;$('#configuration-load').onclick=()=>act(async()=>{const value=await api(`configuration/${kind.value}/${encodeURIComponent(id.value)}`);area.value=value.yaml;revision=value.revision;result.textContent='既存定義を読込みました。保存時に変更競合を確認します。';});
$('#configuration-draft-form').onsubmit=e=>{e.preventDefault();act(async()=>{const saved=await api(`configuration/${kind.value}/${encodeURIComponent(id.value)}`,'PUT',{yaml:area.value,expected_revision:revision});revision=saved.revision;area.value=saved.yaml;result.textContent='保存しました。ownerの起動・再起動や実行中カードへの反映は行っていません。';await loadDefinitions();});};template();})();

function residentWorkspaceEditor(bundle,card){
 const laser=bundle.laser_definition.id,section=node('section'),area=node('textarea'),status=node('p','','muted'),sessions=node('div'),paths=node('div');let revision=null,loaded=false;
 area.rows=16;area.disabled=true;area.dataset.agentTemplate=card.id;area.setAttribute('aria-label',laser+' / '+card.id+' エージェントMarkdown指示');
 const route=`lasers/${encodeURIComponent(laser)}/agent-workspace`;
 function renderPaths(value){
  const labels={laser_root:'レーザー専用フォルダ',cards:'レーザー固有のカード',scripts:'派生スクリプト',data:'測定・解析データ',reports:'PDFレポート',agent:'エージェントの作業場所',instructions:'共有Markdown指示',laser_config:'レーザー設定',assignment:'デッキ割当',job_logs:'カード実行ログ'};
  const current=value.paths&&typeof value.paths==='object'&&!Array.isArray(value.paths)?value.paths:{instructions:value.path};
  const table=node('table'),head=node('tr');for(const title of ['保存するもの','絶対パス'])head.append(node('th',title));table.append(head);
  for(const [key,label] of Object.entries(labels))if(typeof current[key]==='string'&&current[key]){const row=node('tr');row.dataset.agentAssetPath=key;cell(row,label);cell(row,node('code',current[key]));table.append(row);}
  paths.replaceChildren(node('h3','レーザー固有の保存場所'),node('p','起動・再接続時に、この保存場所をエージェントへ渡します。カード案・スクリプト・データは用途別のフォルダに保存できます。','muted'),table);
 }
 async function refreshSessions(){const value=await api(`lasers/${encodeURIComponent(laser)}/agent-sessions`);sessions.replaceChildren();const table=node('table'),head=node('tr');for(const title of ['接続先','デッキ / カード','セッション','状態 / 更新'])head.append(node('th',title));table.append(head);for(const item of value.sessions){const row=node('tr');cell(row,item.provider||'—');cell(row,`${item.deck_id||'—'} / ${item.display_name||item.card_id||'—'}`);cell(row,item.session||'未作成');cell(row,`${item.active?'Active · ':''}${item.status||'—'} / ${item.updated?new Date(item.updated*1000).toLocaleString():'—'}`);table.append(row);}sessions.append(value.sessions.length?table:node('p','このレーザーの保存済みセッションはありません。'));}
 const save=button('Markdownを保存',async()=>{if(!loaded)throw Error('先にテンプレートを読み込んでください。');const value=await api(route,'PUT',{markdown:area.value,expected_revision:revision});revision=value.revision;area.value=value.markdown;renderPaths(value);status.textContent='保存しました。次の作成・再接続から使います。現在の会話やownerには変更していません。';},'primary');save.disabled=true;save.dataset.agentTemplateSave=card.id;
 const load=button('Markdownテンプレートを読み込む',async()=>{const value=await api(route);revision=value.revision;loaded=true;area.disabled=false;save.disabled=false;area.value=value.markdown;renderPaths(value);status.textContent='レーザーごとに保存します。本文はMarkdownのまま編集できます。';await refreshSessions();});load.dataset.agentTemplateLoad=card.id;
 const starter=button('特性の記載欄を追加',()=>{if(!loaded)throw Error('先にテンプレートを読み込んでください。');area.value+='\n\n## Laser characteristics\n- Architecture (ECDL / DBR / DFB / other): not documented\n- Tuning direction and sensitivity, units, conditions, evidence: not documented\n- Mode-hop behaviour, branch selection and recovery constraints: not documented\n- Individual characteristics and reference reports: not documented\n';});section.append(node('h3','このレーザーのエージェント指示'),node('p','テンプレートはレーザー内で共有し、各接続時のデッキ・カード情報を埋め込みます。/opt/agentbridgeの連携とデッキのTeamsカード利用を含みます。保存しても稼働中のエージェントは再起動しません。','muted'),load,starter,area,save,status,paths,node('h3','レーザー別の接続履歴'),button('接続履歴を更新',refreshSessions),sessions);return section;
}

// Consultations and recovery evidence are separate from the live control process.
(()=>{const previous=renderLaserConfiguration;renderLaserConfiguration=function(){previous();const bundle=state.bundles.find(value=>value.id===state.selected),laser=bundle?.laser_definition?.id;if(laser)$('#laser-configuration').append(link('Failure history and agent questions ↗','failures?laser='+encodeURIComponent(laser)));};})();

// Report selection uses only the current deck's semantic declarations.
function reportSelection(bundle,deck,card,params){
 const section=node('section');section.dataset.reportSelection=card.id;
 const catalog=new Map(state.cards.map(item=>[item.id,item]));
 const known=new Map(state.cards.flatMap(item=>item.report_quantities||[]).map(q=>[q.id,q]));
 const laser=bundle.laser_definition.document;
 function contextualize(quantity){
  const q={...quantity},axis={'control.current_axis':'current','control.temperature_axis':'temperature','control.piezo_dac':'piezo','response.current_dither':'current'}[q.id];
  if(axis){const binding=laser.interfaces?.[axis],unit=laser.devices?.[binding?.device]?.control_settings?.axes?.[axis]?.unit;if(!unit)return null;q.unit=q.id==='response.current_dither'?'nm/'+unit:unit;}
  return q;
 }
 const sources=deck.document.cards.map(source=>{
  let quantities=catalog.get(source.operation)?.report_quantities||[];
  if(source.operation==='acquire.metrics'){
   const effective=deck.card_status[source.id]?.parameters||source.parameters||{};
   quantities=(Array.isArray(effective.quantities)?effective.quantities:['optical.frequency']).map(id=>known.get(id)||{id,title_ja:id,unit:'',meaning:''});
  }
  return {...source,quantities:quantities.filter(q=>q.id!=='optical.wavelength').map(contextualize).filter(Boolean)};
 }).filter(source=>source.quantities.length);
 function update(patch){try{const value=JSON.parse(params.value);params.value=JSON.stringify({...value,...patch},null,2);params.dispatchEvent(new Event('input'));}catch(e){error(e);}}
 function checkbox(text,checked,changed,key,value,disabled=false){const label=node('label'),input=node('input');input.type='checkbox';input.checked=checked;input.disabled=disabled;input.dataset[key]=value;input.addEventListener('change',()=>changed(input.checked));label.append(input,document.createTextNode(text));return label;}
 function render(){
  let values;try{values=JSON.parse(params.value);if(!values||Array.isArray(values)||typeof values!=='object'||['report_card_ids','report_quantity_ids'].some(key=>values[key]!=null&&!Array.isArray(values[key])))throw Error('Invalid report selection');}catch(e){section.replaceChildren(node('p','パラメーターJSONを修正すると、表示項目を選択できます。','muted'));return;}
  section.replaceChildren(node('h3','Grafanaに含めるカード・測定量'),node('p','このデッキのカードが報告する量から選びます。自動選択では、レーザーの構成に対応する項目をまとめます。','muted'));
  const autoCards=values.report_card_ids==null,selectedCards=new Set(autoCards?sources.map(source=>source.id):values.report_card_ids);
  section.append(checkbox('カードを自動選択',autoCards,checked=>update({report_card_ids:checked?null:sources.map(source=>source.id)}),'reportAutoCards',card.id));
  const cards=node('div');
  for(const source of sources)cards.append(checkbox(source.id+' — '+source.operation,selectedCards.has(source.id),checked=>{
   const selected=new Set(JSON.parse(params.value).report_card_ids||[]);if(checked)selected.add(source.id);else selected.delete(source.id);
   const current=JSON.parse(params.value),available=new Set(sources.filter(s=>selected.has(s.id)).flatMap(s=>s.quantities.map(q=>q.id)));
   update({report_card_ids:[...selected],report_quantity_ids:current.report_quantity_ids==null?null:current.report_quantity_ids.filter(id=>available.has(id))});
  },'reportSourceCard',source.id,autoCards));
  section.append(cards);
  const available=new Map();for(const source of sources.filter(source=>selectedCards.has(source.id)))for(const q of source.quantities)if(!available.has(q.id))available.set(q.id,q);
  const autoQuantities=values.report_quantity_ids==null,selectedQuantities=new Set(autoQuantities?available.keys():values.report_quantity_ids);
  section.append(checkbox('測定量を自動選択',autoQuantities,checked=>update({report_quantity_ids:checked?null:[...available.keys()]}),'reportAutoQuantities',card.id));
  const quantities=node('div');for(const q of available.values()){
   const label=checkbox((q.title_ja||q.title||q.id)+(q.unit?' ['+q.unit+']':''),selectedQuantities.has(q.id),checked=>{
    const selected=new Set(JSON.parse(params.value).report_quantity_ids||[]);if(checked)selected.add(q.id);else selected.delete(q.id);update({report_quantity_ids:[...selected]});
   },'reportQuantity',q.id,autoQuantities);label.title=q.meaning||'';quantities.append(label);
  }
  section.append(quantities);if(!available.size)section.append(node('p','選択したカードが報告する測定量はありません。','muted'));
 }
 params.addEventListener('input',render);render();return section;
}

function runtimeParameterEditor(bundle,card){
 const section=node('section','','runtime-editor'),form=node('fieldset'),status=node('p','','muted'),revision=node('p','','muted');
 section.dataset.runtimeCard=card.id;section.dataset.runtimeSet=bundle.id;
 let loaded=null,pendingId=null;const inputs=new Map(),selections=new Map();
 function activeJob(){const job=cardJob(bundle.id,card.id);return job?.status==='running'?job:null;}
 function selectionGroup(key,checked){const group=loaded.schema[key].group;for(const [name,input] of selections)if(name===key||(group&&loaded.schema[name].group===group))input.checked=checked;available();}
 function describeResult(value){const labels={queued:'適用待ち',applied:'適用済み',rejected:'拒否',uncertain:'適用結果が不明'};return (labels[value.status]||value.status)+(value.reason?'：'+value.reason:'')+(value.boundary?' / 適用境界: '+value.boundary:'');}
 function available(){
  const job=activeJob();load.disabled=!job;form.disabled=!job||!loaded||job.id!==loaded.job_id||loaded.status!=='running';
  section.hidden=!job;
  form.hidden=!loaded||!Object.keys(loaded.schema||{}).length;
  apply.hidden=form.hidden;
  const selected=[...selections].filter(([k,v])=>v.checked).map(([k])=>k);
  apply.disabled=form.disabled||!selected.length||Boolean(pendingId)||Boolean(loaded?.busy&&!selected.every(k=>loaded.schema[k].while_busy&&loaded.schema[k].effect==='memory'));
 }
 function render(value){
  if(loaded&&loaded.job_id!==value.job_id)pendingId=null;
  loaded=value;inputs.clear();selections.clear();form.replaceChildren();revision.textContent='現在の版: '+(value.revision||'—');
  const result=value.last_result;
  if(result&&(!pendingId||result.request_id===pendingId)){status.textContent=describeResult(result);if(result.request_id===pendingId)pendingId=null;}
  else if(pendingId)status.textContent='適用待ち。適用結果を更新して確認してください。';
  else status.textContent=value.status==='running'?'変更する項目を選び、安全な処理境界で一括適用します。':'このカードは実行中の変更に対応していません。';
  if(value.capabilities?.reason)form.append(node('p',value.capabilities.reason,'muted'));
  if(value.status!=='running'||!Object.keys(value.schema||{}).length){available();return;}
  const table=node('table'),head=node('tr');for(const title of ['変更','項目','現在値','変更後','適用条件'])head.append(node('th',title));table.append(head);
  for(const [key,spec] of Object.entries(value.schema)){
   const row=node('tr'),choose=node('input'),input=node('input');choose.type='checkbox';choose.dataset.runtimeSelect=key;choose.setAttribute('aria-label',key+' を変更');
   input.type='text';input.value=JSON.stringify(value.parameters[key]);input.dataset.runtimeValue=key;input.setAttribute('aria-label',key+' 変更後の値（JSON）');
   choose.addEventListener('change',()=>selectionGroup(key,choose.checked));input.addEventListener('input',()=>selectionGroup(key,true));selections.set(key,choose);inputs.set(key,input);
   cell(row,choose);cell(row,key,spec.description?node('small',spec.description):node('span'));cell(row,JSON.stringify(value.parameters[key]));cell(row,input);
   cell(row,[spec.group?'一括変更: '+spec.group:'',...(spec.boundaries||[]),spec.maximum!=null?'上限 '+spec.maximum:''].filter(Boolean).join(' / '));table.append(row);
  }
  form.append(table,node('p','数値・true/false・文字列・配列はJSONで入力してください。関連項目はまとめて送信します。','muted'));available();
 }
 const load=button('実行中の値・適用結果を更新',async()=>{const job=activeJob();if(!job)throw Error('対象カードは実行中ではありません。');const value=await api(`jobs/${job.id}/runtime-parameters`);if(value.job_id&&value.job_id!==job.id)throw Error('実行中のジョブが変わりました。もう一度読み込んでください。');render(value);});load.dataset.runtimeLoad=card.id;
 const apply=button('選択項目を実行中カードへ一括適用',async()=>{
  const job=activeJob();if(!loaded||!job||job.id!==loaded.job_id)throw Error('対象ジョブが変わりました。現在の値を読み直してください。');
  const patch={};for(const [key,input] of inputs)if(selections.get(key).checked)patch[key]=JSON.parse(input.value);
  if(!Object.keys(patch).length)throw Error('変更する項目を選んでください。');
  for(const key of Object.keys(patch)){const group=loaded.schema[key].group;if(group)for(const [name,spec] of Object.entries(loaded.schema))if(spec.group===group)patch[name]=JSON.parse(inputs.get(name).value);}
  const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);const requestId=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');
  const result=await api(`jobs/${job.id}/runtime-parameters`,'POST',{request_id:requestId,job_id:job.id,card_id:card.id,expected_revision:loaded.revision,patch});
  pendingId=result.status==='queued'?requestId:null;status.textContent=describeResult(result)+(pendingId?'。まだ制御には反映されていません。適用結果を更新してください。':'');available();
 },'primary');apply.dataset.runtimeApply=card.id;
 section.append(node('h3','実行中のパラメーター'),node('p','ここでの変更は現在のジョブだけに適用します。次回の実行設定とは別です。変更できる項目と適用条件は実行中カードから読み込みます。','muted'),load,revision,form,apply,status);
 if(card.operation==='servo.ecdl_steady_lock'&&typeof windowActionsEditor==='function')section.append(windowActionsEditor(bundle,card));
 section._updateAvailability=available;available();return section;
}

function taskExecutionEditor(card,params,bundle=null,deck=null){
 const section=node('section','','task-profile'),provider=node('select'),profile=node('select'),adoption=node('select'),message=node('p','','muted');
 provider.setAttribute('aria-label','Task execution interface');profile.setAttribute('aria-label','Task model / effort profile');
 let registry={},providers=[];
 let inherited=card.agent_execution?.provider_profiles||{};
 const allowed=new Set([card.agent_execution?.task_profile,...Object.values(inherited),...(card.agent_execution?.escalation_profiles||[])]);
 function option(v,label){const o=node('option',label);o.value=v;return o;}
 provider.append(option('','Same interface as selected gateway (card model / effort)'));
 for(const [v,label] of [['report_only','Propose only'],['human_confirmation','Ask before adoption'],['auto_within_approved','Automatically adopt within approved scope']])adoption.append(option(v,label));
 for(const [label,input] of [['Task execution interface ',provider],['Task model / effort profile ',profile],['Adoption ',adoption]]){const l=node('label',label);l.append(input);section.append(l);}
 section.append(message,node('p','The task model writes the explanation. The gateway relays it verbatim. Model selection does not expand approved physical limits. The selected interface and exact profile are frozen when the task is accepted.','muted'));
 function choices(selected=''){
  profile.replaceChildren(option('','Card default'));profile.disabled=!provider.value;
  for(const [id,p] of Object.entries(registry))if(p.provider===provider.value&&(!card.agent_execution||allowed.has(id)))profile.append(option(id,`${id}: ${p.model} / ${p.effort||'provider default'}`));
  if(selected&&!Array.from(profile.options).some(o=>o.value===selected))profile.append(option(selected,selected+' (not allowed by this card)'));
  profile.value=selected;
  message.textContent=!provider.value?Object.entries(inherited).map(([v,id])=>`${providers.find(p=>p.id===v)?.label||v}: ${id}`).join(' · '):'';
 }
 function sync(){try{const p=JSON.parse(params.value);provider.value=registry[p.execution_profile]?.provider||'';choices(p.execution_profile||'');adoption.value=p.adoption_mode||(card.operation==='agent.task'?'auto_within_approved':'report_only');}catch(_){} }
 function change(){const p=JSON.parse(params.value);p.execution_profile=provider.value?profile.value:'';if(provider.value&&!p.execution_profile)throw Error('Choose a model / effort profile for this interface.');p.adoption_mode=adoption.value;if(card.operation==='agent.task')p.request_delegation=true;params.value=JSON.stringify(p,null,2);params.dispatchEvent(new Event('input',{bubbles:true}));}
 provider.addEventListener('change',()=>act(()=>{choices();if(provider.value&&profile.options.length>1)profile.selectedIndex=1;change();}));
 profile.addEventListener('change',()=>act(change));adoption.addEventListener('change',()=>act(change));params.addEventListener('input',sync);
 Promise.all([api('agent-profiles'),card.agent_execution?.difficulty?api('agent-routing'):Promise.resolve(null)]).then(([r,routing])=>{if(routing){const level=card.agent_execution.difficulty==='default'?routing.document.default_difficulty:card.agent_execution.difficulty;inherited=Object.fromEntries(Object.entries(routing.document.provider_profiles).map(([p,levels])=>[p,levels[level]]));for(const levels of Object.values(routing.document.provider_profiles))for(const id of Object.values(levels))allowed.add(id);section.prepend(node('p',`Card difficulty: ${card.agent_execution.difficulty} · effective: ${level}. Profile IDs do not determine difficulty.`,'muted'));}registry=r.document.profiles;providers=r.providers||[];for(const v of new Set(Object.entries(registry).filter(([id])=>!card.agent_execution||allowed.has(id)).map(([,p])=>p.provider)))provider.append(option(v,providers.find(p=>p.id===v)?.label||v));sync();}).catch(e=>{message.textContent=e.message;});sync();return section;
}

function adjustmentExecutionEditor(bundle,deck,card,params){
 const section=node('section','','adjustment-execution'),family=node('select'),agent=node('select'),objective=node('textarea'),status=node('p','','muted'),bounds=node('pre');
 section.dataset.adjustmentExecution=card.id;section.dataset.setId=bundle.id;
 family.dataset.adjustmentFamily=card.id;family.setAttribute('aria-label','What to adjust');
 agent.dataset.adjustmentAgent=card.id;agent.setAttribute('aria-label','Execution resident agent');
 objective.dataset.adjustmentObjective=card.id;objective.setAttribute('aria-label','Adjustment objective');objective.rows=3;
 objective.placeholder='Example: improve dither response and position-free side peaks; keep the target unchanged.';
 const choices=[['auto','Automatic choice within approved bounds'],['current','Current operating point'],['pi','PI gains and approved integral limits'],['slow_loop','TEC outer loop and PZT center'],['feedforward','Feedforward coefficient']];
 for(const [value,label] of choices){const o=node('option',label);o.value=value;family.append(o);}
 for(const c of deck.document.cards.filter(c=>c.operation==='agent.resident')){const o=node('option',cardLabel(c.operation,c.parameters,c.id)||c.id);o.value=c.id;agent.append(o);}
 for(const [label,input] of [['What to adjust ',family],['Result gateway ',agent],['Objective (optional) ',objective]]){const l=node('label',label);l.append(input);section.append(l);}
 section.prepend(node('h3','One-shot agent adjustment'));section.append(taskExecutionEditor(card,params,bundle,deck));
 section.append(node('p','Uses the selected resident as parent; a selected task profile launches a separate scoped decision session. Leave the objective empty to use the registered adjustment instructions. Automatic choice selects one supported family; it does not expand approved bounds. Monitoring need not be running.','muted'),status,
  node('p','Foreground target changes and sweeps take priority. Uncommitted trials return to their own starting checkpoint; a newer target is never restored to an older value. Changing safety bounds requires human approval.','muted'));
 const limits=node('details');limits.append(node('summary','Approved plan for this run'),bounds);section.append(limits);
 function available(){
  const p=(()=>{try{return JSON.parse(params.value);}catch(_){return {};}})(),plan=deck.document.cards.find(c=>c.id===(p.adjustment_plan_card_id||'agent-monitor'));
  const mapped=p.adjustment_measurement_cards||{},dlc=Object.values(bundle.laser_definition?.document?.devices||{}).some(v=>v.type==='dlcpro.laser.owner');
  const b=Object.keys(mapped).length?{enabled:true,families:Object.keys(mapped),measurement_cards:mapped,authority:'Each candidate is revalidated by the laser owner.'}:dlc&&plan?.operation==='measure.change'?{enabled:true,families:['pi'],measurement_card:plan.id}:{...(plan?.parameters||{}),...(deck.invocation_parameters?.[plan?.id]||{})}.background||{},allowed=b.families||['current'];
  for(const o of family.options)o.disabled=o.value!=='auto'&&!allowed.includes(o.value);
  bounds.textContent=JSON.stringify(b,null,2);
  const running=cardJob(bundle.id,agent.value)?.status==='running';
  const delegated=Boolean(p.execution_profile)||card.agent_execution?.mode!=='resident';
  status.textContent=b.enabled!==true?'No enabled approved adjustment plan is configured.':delegated?'A separate task session selects measurements and writes its explanation. By default it uses the selected gateway interface and the card model / effort. Monitoring need not be running.':!running?'Start the legacy operator or select a task profile.':'Legacy operator execution.';
 }
 function sync(){try{const p=JSON.parse(params.value);family.value=p.adjustment_family||'auto';agent.value=p.agent_card_id||agent.options[0]?.value||'';objective.value=p.adjustment_objective||'';}catch(_){}available();}
 function update(){const p=JSON.parse(params.value);p.adjustment_family=family.value;p.agent_card_id=agent.value;p.adjustment_objective=objective.value;params.value=JSON.stringify(p,null,2);params.dispatchEvent(new Event('input',{bubbles:true}));}
 for(const input of [family,agent,objective])input.addEventListener('change',()=>{try{update();}catch(e){error(e);}});params.addEventListener('input',sync);
 section._updateAvailability=available;sync();return section;
}

function studyExecutionEditor(bundle,deck,card,params){
 const section=node('section','','study-execution'),status=node('p','','muted');
 section.dataset.studyExecution=card.id;section.dataset.setId=bundle.id;
 const analysis=card.operation.startsWith('optimize.')&&card.operation!=='optimize.target_step';
 section.append(node('h3',cardLabel(card.operation,card.parameters)||card.operation));
 if(analysis)section.append(taskExecutionEditor(card,params,bundle,deck));
 section.append(node('p','The card identity fixes the algorithm. Raw measurements and plots are saved in every execution. Choose a separate measurement or optimization card to change the procedure.','muted'),status);
 function available(){status.textContent=analysis?'The task model analyzes the evidence and writes its explanation; the gateway receives the report.':'No agent is required for this card.';}
 const list=node('div'),refresh=button('Show previous study results',async()=>{
  const value=await api('jobs?set_id='+encodeURIComponent(bundle.id));
  list.replaceChildren();for(const job of value.jobs.filter(j=>j.set_id===bundle.id&&terminal.has(j.status)&&j.cards?.some(c=>c.id===card.id||(c.card||c.operation)===card.operation)&&!j.archived).slice(0,10)){
   const row=node('p'),artifact=`jobs/${job.id}/artifacts/target-response.json`;const hasRaw=!job.available_artifacts||job.available_artifacts.includes('target-response.json');
   row.append(node('span',timestamp(job.created_utc)+' · '+job.status+' '));
   if(job.pdf_available!==false)row.append(link('Response PDF ↗',base+`jobs/${job.id}/report.pdf`));
   else row.append(node('span','PDF not published'));
   if(hasRaw)row.append(document.createTextNode(' · '),link('Raw data JSON ↗',base+artifact));
   else row.append(node('span',' · Response data not published'));
   if(hasRaw)row.append(button('Use proposed settings for next run',async()=>{
    const result=await api(artifact),candidate=result.optimization;
    if(candidate?.status!=='candidate_only'||candidate.applied!==false)throw Error('No validated agent candidate is available in this result.');
    const allowed=['expected_settle_s','evaluation_window_s','check_interval_s','confirmation_s','mean_error_max_hz','rms_error_max_hz','peak_error_max_hz','minimum_samples','max_gap_s'];
    const patch={};for(const key of allowed)if(Object.hasOwn(candidate.parameters||{},key))patch[key]=candidate.parameters[key];
    params.value=JSON.stringify({...JSON.parse(params.value),...patch},null,2);params.dispatchEvent(new Event('input',{bubbles:true}));params.parentElement.open=true;
    status.textContent='Candidate copied into this run draft. Review the JSON before Start; saved defaults are unchanged.';
   }));list.append(row);
  }
  if(!list.children.length)list.append(node('p','No completed runs of this card were found for this laser.','muted'));
 });refresh.dataset.studyResults=card.id;section.append(refresh,list);
 section._updateAvailability=available;available();return section;
}

function workflowArtifactLinks(bundle,card){
 const files={'measure.change':['measurement.json','Measurement JSON'],'measure.batch':['measurement.json','Measurement JSON'],'tune.ecdl_window_center':['window-center.json','Window JSON'],'study.ecdl_mode_map':['mode-map.json','Map JSON'],'prepare.ecdl_recovery':['recovery.json','復旧手順JSON（策定＋試験）'],'plan.ecdl_recovery':['recovery-plan.json','復旧計画JSON'],'test.ecdl_recovery':['recovery-test.json','復旧試験JSON']};
 if(!files[card.operation])return null;
 const section=node('section'),list=node('div');section.dataset.workflowArtifacts=card.id;
 const refresh=button('完了した実行の成果物を表示',async()=>{
  const value=await api('jobs?set_id='+encodeURIComponent(bundle.id));
  const jobs=value.jobs.filter(job=>job.set_id===bundle.id&&job.cards?.some(c=>c.id===card.id||(c.card||c.operation)===card.operation)&&terminal.has(job.status)).slice(0,10);
  list.replaceChildren();for(const job of jobs){const row=node('p');row.append(node('span',job.id.slice(0,12)+' · '+job.status+' '),(!job.available_artifacts||job.available_artifacts.includes(files[card.operation][0]))?link(files[card.operation][1]+' ↗',base+`jobs/${job.id}/artifacts/${files[card.operation][0]}`):node('span','Data not published'),document.createTextNode(' · '),job.pdf_available!==false?link('PDFレポート ↗',base+`jobs/${job.id}/report.pdf`):node('span','PDF not published'));list.append(row);}
  if(!jobs.length)list.append(node('p','完了した成果物はありません。','muted'));
 });refresh.dataset.workflowArtifactsLoad=card.id;section.append(node('h3','測定・計画の成果物'),refresh,list);return section;
}

(()=>{
 const previous=cardTable;cardTable=function(bundle,deck,agentOptionsHost=null){
  const box=previous(bundle,deck,agentOptionsHost);
  for(const card of deck.document.cards){
   const params=[...box.querySelectorAll('textarea')].find(area=>area.getAttribute('aria-label')===card.id+' 今回のパラメーター');if(!params)continue;
   const body=params.parentElement.parentElement;
   if(['measure.target_response','optimize.settling_criteria','optimize.target_feedforward','optimize.target_thermal','optimize.target_step'].includes(card.operation))params.parentElement.before(studyExecutionEditor(bundle,deck,card,params));
   if(card.operation==='agent.task'&&card.parameters?.instruction_profile==='adjustment')params.parentElement.before(adjustmentExecutionEditor(bundle,deck,card,params));
   for(const title of body.querySelectorAll('h3'))if(title.textContent==='今回の実行パラメーター')title.textContent='次回の実行パラメーター';
   if(card.operation==='report.grafana_dashboard')body.append(reportSelection(bundle,deck,card,params));
   body.append(runtimeParameterEditor(bundle,card));const artifacts=workflowArtifactLinks(bundle,card);if(artifacts)body.append(artifacts);
  }
  return box;
 };
 const oldStatus=updateCardStatus;updateCardStatus=function(){oldStatus();

  for(const element of document.querySelectorAll('.runtime-editor'))element._updateAvailability();
  for(const element of document.querySelectorAll('.study-execution'))element._updateAvailability();
  for(const element of document.querySelectorAll('.adjustment-execution'))element._updateAvailability();
  for(const start of document.querySelectorAll('[data-start-card]'))if(start.dataset.operation==='plan.ecdl_recovery')start.disabled=start.dataset.runnable!=='true';
 };
})();

if($('#retention-inherit'))$('#retention-inherit').onchange=()=>{for(const id of ['#retention-enabled','#retention-days','#backup-directory'])$(id).disabled=$('#retention-inherit').checked;};

/* BEGIN isolated live controls */
/* Live controls use registered cards only. No owner, service, or safety-setting writes. */
(() => {
  const C = 299792458e9;
  const defaults = {
    'ecdl.rig.owner': {
      current: {port:'current', locked:'reference_move', open_loop:'output', unit:'V'},
      piezo: {port:'piezo', locked:'dac_center', open_loop:'output', unit:'count'},
      temperature: {port:'temperature', locked:'refused', open_loop:'output', unit:'V'}
    },
    'esp32servocon.native_current.owner': {
      current: {port:'native_current', locked:'dac_center', open_loop:'output', unit:'count'},
      temperature: {port:'temperature', locked:'refused', open_loop:'refused', unit:'C'}
    }
  };
  const passive = new Set(['agent.resident','agent.log_watch','agent.monitor','agent.task','agent.history_review','agent.request','monitor.wavelength','acquire.metrics','notify.teams','report.grafana_dashboard']);
  const finished = status => terminal.has(status) || status === 'completed_unsettled';
  const cancellationRequests = new Set();
  function targetChange() {
    const jobs=state.jobs.filter(j=>j.set_id===bundle?.id&&!finished(j.status)&&
      j.cards?.some(c=>(c.operation||c.card)==='tune.lock_target'));
    return jobs.find(j=>j.status!=='queued')||jobs[0]||
      (pending?.id&&pending.operation==='tune.lock_target'?{id:pending.id,status:'pending'}:null);
  }
  function operatingPointChange() {
    return state.jobs.find(j=>j.set_id===bundle?.id&&!finished(j.status)&&j.cards?.some(c=>(c.operation||c.card)==='tune.operating_point'))||
      (pending?.id&&pending.operation==='tune.operating_point'?{id:pending.id,status:'pending'}:null);
  }
  async function cancelOperatingPoint() {
    const job=operatingPointChange(),laserId=bundle?.id;
    if(!job||cancellationRequests.has(job.id))return;
    cancellationRequests.add(job.id);update();
    try{
      const current=await safeFetch('jobs/'+encodeURIComponent(job.id));
      if(bundle?.id!==laserId||current.set_id!==laserId||current.cards?.length!==1||
         (current.cards[0].operation||current.cards[0].card)!=='tune.operating_point')throw Error('Job identity differs; no stop sent.');
      if(!finished(current.status)){
        const result=await safeFetch('jobs/'+encodeURIComponent(job.id)+'/stop','POST',{});
        const proof=result.stop_reconciliation;
        message(proof?.reason||'Operating-point stop requested. Waiting for its confirmed outcome; the lock is not stopped.');
      }
      await poll();
    }catch(e){message('Operating-point stop not confirmed: '+e.message);}
    finally{cancellationRequests.delete(job.id);update();}
  }
  async function cancelTargetChange() {
    const job=targetChange(),laserId=bundle?.id;
    if(!job||cancellationRequests.has(job.id))return;
    cancellationRequests.add(job.id);update();
    try {
      const current=await safeFetch('jobs/'+encodeURIComponent(job.id));
      if(bundle?.id!==laserId)throw Error('Laser selection changed; no cancellation sent.');
      if(current.set_id!==laserId||current.cards?.length!==1||
         (current.cards[0].operation||current.cards[0].card)!=='tune.lock_target')
        throw Error('Job identity differs; no cancellation sent.');
      if(finished(current.status)){state.jobs=state.jobs.map(j=>j.id===current.id?current:j);return;}
      await safeFetch('jobs/'+encodeURIComponent(job.id)+'/stop','POST',{});
      message('Target cancellation requested. Waiting for the job to stop; the lock and applied target are retained.');
    } catch(e) {
      message('Target cancellation not confirmed. Read job status before retrying: '+e.message);
      // Do not blindly repeat a POST after a transport failure.
    } finally {update();}
  }

  const finite = x => typeof x === 'number' && Number.isFinite(x);
  function axes(laser) {
    const binding = laser.interfaces?.lock || laser.interfaces?.native_current;
    const device = laser.devices?.[binding?.device];
    const map = JSON.parse(JSON.stringify(defaults[device?.type] || {}));
    for (const [key, config] of Object.entries(device?.control_settings?.operating_point?.axes || {})) {
      map[key] = {...map[key], ...config};
      if(config.enabled===false)Object.assign(map[key],{locked:'refused',open_loop:'refused',disabled_reason:'Disabled in laser configuration'});
    }
    return {device, map};
  }
  function targetParameters(value, unit, relative, frequency) {
    if (!finite(value) || (relative ? value === 0 : value <= 0)) throw Error('Enter a finite, nonzero value.');
    const p = {target_frequency_hz:null, target_wavelength_nm:null, offset_hz:null};
    if (relative && unit === 'MHz') p.offset_hz = value * 1e6;
    else if (relative) {
      if (!finite(frequency) || frequency <= 0) throw Error('The configured target is unavailable.');
      p.target_wavelength_nm = C / frequency + value * (unit === 'pm' ? .001 : 1);
      if (p.target_wavelength_nm <= 0) throw Error('The resulting wavelength must be positive.');
    } else if (unit === 'THz') p.target_frequency_hz = value * 1e12;
    else p.target_wavelength_nm = value;
    return p;
  }
  function model(laser, jobs, now=Date.now()) {
    const live = jobs.filter(j => !finished(j.status));
    const locks = live.filter(j => j.cards?.some(c => (c.operation || c.card || '').startsWith('servo.')));
    const blockers = live.filter(j => !j.read_only && j.cards?.some(c => {
      const op=c.operation || c.card || ''; return !op.startsWith('servo.') && !passive.has(op);
    }));
    const lock = locks.length === 1 ? locks[0] : null;
    const age = lock ? (now-Date.parse(lock.updated_utc))/1000 : null;
    const row = lock?.latest || {};
    const stamp = row.recorded_unix_s ?? row.source_acquired_unix_s ?? (row.acquired_s > 1e9 ? row.acquired_s : null);
    const fresh = Boolean(lock && finite(age) && age >= 0 && age <= 25 && finite(stamp) && now/1000-stamp >= 0 && now/1000-stamp <= 25);
    const {device, map} = axes(laser);
    return {lock, blockers, fresh, row, device, map, ambiguous:locks.length > 1, hasLock:locks.length > 0};
  }
  window.ldconControlModel = {axes, targetParameters, model};
  if (typeof renderLaserConfiguration !== 'function') return;
  let box=null, bundle=null, pending=null, unknown=false, polling=false, connected=false;
  let ownerReadback=null, ownerPolling=false, ownerReadAt=-Infinity;
  const preferences = new Map(), outputRecords = new Map();let definitionReadAt=0;
  let editors=[],lastEdit=Date.now(),batchBusy=false;const waiters=new Map();
  const fmt = (x,d=6) => finite(x) ? x.toLocaleString('en-US',{maximumFractionDigits:d,useGrouping:false}) : '未取得';
  function message(value) { if(box) box.querySelector('[data-control-result]').textContent=value; }
  function findCard(operation, b=bundle) {
    for (const deck of b?.deck_definitions || []) {
      const card=deck.document.cards.find(c=>c.operation===operation);
      if(card) return {deck,card,status:deck.card_status[card.id]};
    }
    return null;
  }
  function context() {
    const m=model(bundle?.laser_definition?.document || {},state.jobs.filter(j=>j.set_id===bundle?.id));
    const o=ownerReadback,age=o?.receivedAt===undefined?NaN:o.ageAtReceipt+Math.max(0,performance.now()-o.receivedAt)/1000;
    m.ownerFresh=Boolean(o?.laser_id===bundle?.laser_definition?.document.id&&o.fresh===true&&finite(age)&&age>=0&&age<=5);
    m.owner=m.ownerFresh?o:null;
    // A missing worker never makes a physically active servo an open-loop knob.
    if(m.owner?.pi_stopped===false)m.hasLock=true;
    return m;
  }
  async function pollOwner() {
    if(ownerPolling||!bundle||state.view!=='laser-settings'||(typeof dashboardVisible==='function'&&!dashboardVisible())||performance.now()-ownerReadAt<1000)return;
    ownerPolling=true;const id=bundle.id,laserId=bundle.laser_definition.document.id,started=performance.now();
    ownerReadAt=started;
    try{const value=await safeFetch('lasers/'+encodeURIComponent(laserId)+'/control-readback');
      if(bundle?.id===id&&value.laser_id===laserId){const received=performance.now();ownerReadback={...value,receivedAt:received,ageAtReceipt:(value.served_unix_s??value.read_finished_unix_s)-value.sample_unix_s+Math.max(0,received-started)/1000};}
    }catch(_){if(bundle?.id===id&&ownerReadback)ownerReadback={...ownerReadback,fresh:false};}
    finally{ownerPolling=false;update();}
  }
  function storageKey() { return 'ldcon.pending-controls.'+location.pathname+'.'+bundle.id; }
  function safeFetch(route, method='GET', data) {
    // Unlike the legacy generic editor, this path never offers to bypass a
    // refused operation with a maintainer reference and never retries a POST.
    return fetch(base+route,{method,headers:{'Content-Type':'application/json','X-LDCon-Client':'web'},body:data===undefined?undefined:JSON.stringify(data)})
      .then(async response=>{const value=await response.json();if(!response.ok){const error=Error(typeof value.detail==='string'?value.detail:JSON.stringify(value.detail||value));error.httpStatus=response.status;throw error;}return value;});
  }
  async function submit(operation, params) {
    if(pending || unknown || !connected || !state.connected) throw Error('Wait for the previous action and a fresh connection.');
    const id=bundle.id, old=context();
    if(old.blockers.length || old.ambiguous || (old.hasLock && !old.fresh)) throw Error('Wait for fresh, unambiguous control state and completion of the active action.');
    // Re-read the complete projected definition; start uses its revision.
    const b=await safeFetch('sets/'+encodeURIComponent(id));
    if(bundle.id!==id) throw Error('Selected laser changed.');
    bundle=b;
    const selected=findCard(operation,b);
    if(!selected) throw Error('This laser has no registered '+operation+' card.');
    const parameters={...selected.status.parameters,...(typeof params==='function'?params(b):params)};
    const body={expected_revision:b.revision,parameters};
    const route=`sets/${encodeURIComponent(id)}/decks/${encodeURIComponent(selected.deck.id)}`;
    const check=await safeFetch(route+`/cards/${encodeURIComponent(selected.card.id)}/validate`,'POST',body);
    if(!check.runnable) throw Error((check.blocked_reasons||[]).join('; ')||'Operation rejected before execution.');
    pending={id:null,operation};
    localStorage.setItem(storageKey(),JSON.stringify(pending));
    update();message('Submitting one action…');
    try {
      const job=await safeFetch(route+'/start','POST',{...body,card_id:selected.card.id});
      if(!job.id) throw Error('No job ID was returned; inspect the job list before doing anything else.');
      pending.id=job.id;localStorage.setItem(storageKey(),JSON.stringify(pending));
      message('Accepted: '+job.id.slice(0,8)+'. Waiting for the card result.');
      return job.id;
    } catch(error) {
      if([400,403,404,409,422].includes(error.httpStatus)) {
        localStorage.removeItem(storageKey());pending=null;
        message('Request rejected: '+error.message);
      } else {
        unknown=true;message('Outcome unconfirmed. Do not repeat this action. Check the job list: '+error.message);
      }
      throw error;
    } finally { update(); }
  }
  function input(label, value, unit, cls='') {
    const wrap=node('label',label), field=node('input');field.type='number';field.step='any';field.value=value;field.className=cls;
    field.setAttribute('aria-label',label+(unit?' ('+unit+')':''));wrap.append(field);return {wrap,field};
  }
  function select(label, choices) {
    const wrap=node('label',label), field=node('select');field.setAttribute('aria-label',label);
    for(const [value,text] of choices){const o=node('option',text);o.value=value;field.append(o);}wrap.append(field);return {wrap,field};
  }
  function nudgeButtons(parent, callback, label) {
    const pad=node('div','','control-nudge');pad.tabIndex=0;pad.setAttribute('role','group');pad.setAttribute('aria-label',label+'; arrow keys change one step');
    const minus=node('button','−'),plus=node('button','+');for(const [b,sign] of [[minus,-1],[plus,1]]){b.type='button';b.dataset.controlWrite='';b.onclick=()=>{try{callback(sign);}catch(e){error(e);}};}
    minus.setAttribute('aria-label','Decrease '+label);plus.setAttribute('aria-label','Increase '+label);
    pad.append(minus,plus);pad.addEventListener('keydown',e=>{
      if(!['ArrowUp','ArrowRight','ArrowDown','ArrowLeft'].includes(e.key))return;
      e.preventDefault();if(e.repeat || plus.disabled)return;
      try{callback(['ArrowUp','ArrowRight'].includes(e.key)?1:-1);}catch(err){error(err);}
    });parent.append(pad);return pad;
  }
  // One handler for the active workspace, not one listener per refresh.
  // Capture also handles Escape after a browser/client has moved focus away.
  document.addEventListener('keydown',event=>{
    if(event.key!=='Escape'||event.isComposing||event.defaultPrevented||!box?.isConnected||state.view!=='laser-settings')return;
    const focused=document.activeElement;
    if(!box.contains(focused)&&focused?.matches('input,textarea,select,[contenteditable=true]'))return;
    if(document.querySelector('dialog[open],[role=dialog]:not([hidden])'))return;
    event.preventDefault();event.stopPropagation();lastEdit=Date.now();
    for(const editor of editors)editor.revert();
  },true);
  function mount() {
    bundle=state.bundles.find(b=>b.id===state.selected);box=null;connected=false;pending=null;unknown=false;
    ownerReadback=null;ownerReadAt=-Infinity;
    if(!bundle)return;definitionReadAt=0;
    try {pending=JSON.parse(localStorage.getItem(storageKey())||'null');unknown=Boolean(pending&&!pending.id);}catch(_){}
    box=node('section','','live-controls workspace-section');box.id='live-controls';box.append(node('h2','目標波長・動作点'));
    box.append(node('p','Edits are drafts until Enter or an enabled arrow/wheel send. With a running lock, large target changes are queued as bounded steps in one server job. Without a lock, the final target is saved once without a step cap or settling wait; absolute target bounds still apply. Each step completes before the next; knob edits wait for the final target. Failure or cancellation stops remaining steps. Existing owner limits remain in force.','muted'));
    const current=node('p','','control-current');current.dataset.controlCurrent='';box.append(current);
    const target=node('div','','target-control');target.append(node('h3','目標波長'));
    const absolute=input('Absolute target','',''),decimals=input('Decimals',preferences.get(bundle.id)?.decimals??9,''),unit=select('Input unit',[['nm','Wavelength (nm)'],['THz','Frequency (THz)']]);
    const initial=bundle.laser_definition.document.target?.frequency_hz;
    let decimalPlaces=Number(decimals.field.value),previousUnit='nm';
    decimals.field.min='0';decimals.field.max='12';decimals.field.step='1';decimals.field.dataset.targetDecimals='';decimals.wrap.classList.add('target-decimals');
    decimals.field.setAttribute('aria-label','Absolute target decimal places');
    decimals.field.title='Display only; retained precision and command values are unchanged.';
    const targetFormat=value=>value.toFixed(decimalPlaces);
    if(finite(initial)&&initial>0)absolute.field.value=targetFormat(C/initial);
    absolute.field.dataset.targetInput='';
    const step=input('Nudge step',preferences.get(bundle.id)?.step||10,'');step.field.min='0';step.field.classList.add('step-field');
    const stepUnit=select('Step unit',[['MHz','Frequency (MHz)'],['pm','Wavelength (pm)'],['nm','Wavelength (nm)']]);
    stepUnit.field.value=preferences.get(bundle.id)?.unit||'MHz';
    const send=node('input');send.type='checkbox';send.checked=preferences.get(bundle.id)?.send===true;send.dataset.sendOnArrows='';
    const remember=()=>preferences.set(bundle.id,{step:step.field.value,unit:stepUnit.field.value,send:send.checked,decimals:decimalPlaces});step.field.oninput=remember;stepUnit.field.onchange=remember;send.onchange=remember;
    const options=node('div','','input-options'),sendLabel=node('label');sendLabel.append(send,node('span','Send on arrows'));options.append(sendLabel);
    options.append(button('Send edited values',()=>commit()),button('Revert unsent edits',()=>editors.forEach(e=>e.revert())));
    options.append(node('span','Enter sends edited fields; Esc reverts. Unfocused edits revert after 60 seconds of inactivity. Focus a field to use the wheel.'));
    box.append(options);editors=[];
    function editor(field,key,stepper,quantum){const e=new window.LDConCommittingInput(field,{step:stepper,sendOnArrows:()=>send.checked,onCommit:chosen=>act(()=>commit(chosen)),format:v=>key==='target'?targetFormat(v):String(Number(v.toFixed(8))),quantum});e.key=key;editors.push(e);return e;}
    function targetStep(value,sign){const amount=Number(step.field.value);if(!(amount>0))throw Error('The nudge step must be positive.');
      let hz=unit.field.value==='THz'?value*1e12:C/value;
      if(stepUnit.field.value==='MHz')hz+=sign*amount*1e6;else hz=C/(C/hz+sign*amount*(stepUnit.field.value==='pm'?.001:1));
      return unit.field.value==='THz'?hz/1e12:C/hz;}
    const targetEditor=editor(absolute.field,'target',targetStep,1e-10);targetEditor.unit=unit.field;
    unit.field.onchange=()=>{const value=targetEditor.value();const converted=finite(value)&&value>0&&unit.field.value!==previousUnit?299792.458/value:value;previousUnit=unit.field.value;const f=bundle.laser_definition.document.target?.frequency_hz;if(finite(f))targetEditor.last=unit.field.value==='THz'?f/1e12:C/f;targetEditor.edit(converted);targetEditor.reformat();};
    const setDecimals=()=>{const n=Number(decimals.field.value);if(!decimals.field.value.trim()||!Number.isInteger(n)||n<0||n>12)return;decimalPlaces=n;targetEditor.reformat();remember();};
    decimals.field.addEventListener('input',setDecimals);decimals.field.addEventListener('change',setDecimals);
    decimals.field.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();e.stopPropagation();setDecimals();}});
    target.append(absolute.wrap,decimals.wrap,unit.wrap,step.wrap,stepUnit.wrap);
    nudgeButtons(target,sign=>targetEditor.nudge(sign),'optical target');
    const stopTarget=node('button','目標変更を中止');stopTarget.type='button';stopTarget.dataset.cancelTarget='';
    stopTarget.onclick=()=>{void cancelTargetChange();};target.append(stopTarget);
    const cancelNote=node('small','残りの目標ステップを中止します。ロックと反映済みの目標は維持します。');target.append(cancelNote);
    box.append(target);
    const table=node('table','','knob-table'),header=node('tr');for(const t of ['Knob','Current output','Editable value / mode','New value','Step','操作'])header.append(node('th',t));table.append(header);
    for(const [axis,info] of Object.entries(axes(bundle.laser_definition.document).map)) {
      const row=node('tr');row.dataset.axis=axis;
      cell(row,node('strong',axis==='piezo'?'PZT':axis==='current'?'Current modulation':'TEC'),node('small',info.unit||''));
      cell(row).dataset.actual='';cell(row).dataset.reference='';
      const v=input('新しい値','',info.unit),delta=input('変化幅',info.unit==='count'?1:.0001,info.unit);delta.field.min='0';
      cell(row,v.wrap);cell(row,delta.wrap);
      const e=editor(v.field,axis,(value,sign)=>{const d=Number(delta.field.value);if(!(d>0))throw Error('The step must be positive.');return value+sign*d;},info.unit==='count'?.5:1e-8);
      nudgeButtons(cell(row),sign=>e.nudge(sign),axis);table.append(row);
    }
    const wrap=node('div','','table-wrap');wrap.append(table);box.append(node('h3','動作点の調整'),wrap);
    const stopPoint=node('button','Stop operating-point change');stopPoint.type='button';stopPoint.dataset.cancelOperatingPoint='';
    stopPoint.onclick=()=>{void cancelOperatingPoint();};
    box.append(stopPoint,node('small','Stops only the operating-point request. If its controller ended, checks the request and owner before clearing the wait. It does not stop or restart the lock.'));
    box.addEventListener('input',()=>{lastEdit=Date.now();});box.addEventListener('keydown',e=>{lastEdit=Date.now();if(e.key==='Enter'&&e.target.tagName!=='TEXTAREA'){e.preventDefault();act(()=>commit());}});
    async function commit(chosen=editors){
      if(batchBusy||pending||unknown)throw Error('An action is pending. Inspect its result before sending more.');
      const selected=chosen.filter(e=>e.state==='pending'||e.state==='rejected');if(!selected.length)return;
      if(selected.some(e=>e.input.disabled||!Number.isFinite(e.value())))throw Error('Enter valid values for currently editable fields.');
      const laserId=bundle.id,commands=[],target=selected.find(e=>e.key==='target'),knobs=selected.filter(e=>e.key!=='target');
      if(target)commands.push({operation:'tune.lock_target',parameters:{...targetParameters(target.value(),unit.field.value,false,null),queue_steps:true},fields:[target]});
      if(knobs.length)commands.push({operation:'tune.operating_point',parameters:{schedule:null,moves:knobs.map(e=>({axis:e.key,mode:context().hasLock?'locked_reference':'open_loop_output',target:{value:e.value()}}))},fields:knobs});
      for(const command of commands)command.sent=command.fields.map(e=>[e,e.begin()]);
      batchBusy=true;
      try{for(const command of commands){if(bundle.id!==laserId)throw Error('Laser selection changed; remaining edits were not sent.');
        const sent=command.sent;
        try{const id=await submit(command.operation,command.parameters);await new Promise((resolve,reject)=>waiters.set(id,{resolve,reject}));for(const [e,token] of sent)e.accepted(token);}
        catch(error){for(const [e,token] of sent)e.rejected(token);throw error;}
      }}catch(error){for(const command of commands)for(const [e,token] of command.sent)if(e.state==='confirming'&&!e.acknowledged)e.rejected(token);throw error;}finally{batchBusy=false;update();}
    }
    const availability=node('p','','muted');availability.dataset.controlAvailability='';box.append(availability);const status=node('p');status.dataset.controlResult='';status.setAttribute('role','status');box.append(status);
    const reconcile=button('Clear local pending marker after reviewing job history',()=>{
      if(!unknown)return;
      if(!window.confirm('Have you checked the job history and current outputs? This clears only the browser marker. It does not cancel, repeat or start a command.'))return;
      localStorage.removeItem(storageKey());pending=null;unknown=false;message('Local marker cleared. No command was sent.');update();
    });reconcile.dataset.reconcile='';reconcile.disabled=!unknown;reconcile.style.visibility=unknown?'visible':'hidden';box.append(reconcile);
    box.append(node('p','During lock, PZT changes the centre sought by the TEC outer loop, not the servo DAC output. Direct TEC changes are disabled while that loop controls it. The last recorded output stays visible with its age when a sample is missing. It is not fresh confirmation; the card reads the owner again before moving.','muted'));
    const nav=$('#laser-configuration .workspace-nav');if(nav)nav.after(box);else $('#laser-configuration').prepend(box);
    if(unknown)message('A previous request has an unconfirmed outcome. Check job history before further changes.');
    update();poll();
  }
  function update() {
    if(!box||!bundle)return;
    for(const e of editors)e.expire(Date.now(),lastEdit);
    const m=context(),laser=bundle.laser_definition.document,f=laser.target?.frequency_hz;
    const targetJob=targetChange(),stopTarget=box.querySelector('[data-cancel-target]');
    stopTarget.disabled=!state.connected||!targetJob||cancellationRequests.has(targetJob.id);
    stopTarget.title=targetJob?'Target job '+targetJob.id+' · '+targetJob.status:'No active target change';
    const pointJob=operatingPointChange(),stopPoint=box.querySelector('[data-cancel-operating-point]');
    stopPoint.disabled=!state.connected||!pointJob||cancellationRequests.has(pointJob.id);
    stopPoint.textContent=pointJob?.status==='stopping'?'Recheck operating-point stop':'Stop operating-point change';
    stopPoint.title=pointJob?'Operating-point job '+pointJob.id+' · '+pointJob.status:'No active operating-point change';
    const wave=state.wavelengths[laser.id], waveFresh=wave?.valid===true&&Date.now()/1000-wave.acquired_unix_s>=0&&Date.now()/1000-wave.acquired_unix_s<=15;
    const measured=waveFresh?wave.frequency_hz:m.fresh?m.row.frequency_hz:null;
    box.querySelector('[data-reconcile]').disabled=!unknown;box.querySelector('[data-reconcile]').style.visibility=unknown?'visible':'hidden';
    const current=box.querySelector('[data-control-current]');
    if(!current._parts){
      const metrics=[];
      for(const label of ['保存された目標','最新の測定']){
        const metric=node('span','','control-metric'),nm=node('strong'),hz=node('small');
        metric.append(node('small',label),nm,hz);current.append(metric);metrics.push({nm,hz});
      }
      const source=node('small'),lock=node('small'),context=node('span','','control-context');context.append(source,lock);current.append(context);current._parts={metrics,source,lock};
    }
    const v=current._parts;
    for(const [i,hz] of [f,measured].entries()){
      v.metrics[i].nm.textContent=finite(hz)?fmt(C/hz,9)+' nm':'未取得';
      v.metrics[i].hz.textContent=finite(hz)?fmt(hz/1e12,9)+' THz':'読戻し未確認';
    }
    v.source.textContent=waveFresh?`CH ${wave.channel} · ${new Date(wave.acquired_unix_s*1000).toLocaleTimeString()}`:'CH —';
    v.lock.textContent=!state.connected?'接続不明':m.hasLock?'ロックの実行あり':'ロックの実行なし';

    const targetEditor=editors.find(e=>e.key==='target');if(targetEditor&&finite(f))targetEditor.observe(targetEditor.unit.value==='THz'?f/1e12:C/f,Date.now()/1000);
    const blocked=!connected||!state.connected||state.busy||Boolean(pending)||unknown||m.blockers.length>0||m.ambiguous||(m.hasLock&&!m.fresh);
    box.querySelector('[data-control-availability]').textContent=!connected||!state.connected?'最新の状態を確認中です。':unknown?'前の要求の結果を確認してください。':pending?'操作の完了を確認中です。':m.ambiguous||m.blockers.length?'実行中の操作を確認してください。':m.hasLock&&!m.fresh?'新しい制御記録を待っています。':!findCard('tune.lock_target')?'目標変更カードがこのレーザーに登録されていません。':'';
    for(const e of box.querySelectorAll('.target-control [data-control-write]'))e.disabled=blocked||!findCard('tune.lock_target')||!finite(f);
    for(const row of box.querySelectorAll('[data-axis]')) {
      const axis=row.dataset.axis,info=m.map[axis],kind=info?.[m.hasLock?'locked':'open_loop'];
      let actual=m.row.outputs?.[info?.port],reference=actual;
      if(m.device?.type==='esp32servocon.native_current.owner'&&axis==='current'){actual=m.row.dac_code;reference=m.row.native_config?.control_offset;}
      if(m.ownerFresh&&finite(m.owner.outputs?.[info?.port])){
        actual=m.owner.outputs[info.port];reference=m.owner.references?.[info.port]??actual;
      }
      if(m.hasLock&&kind==='dac_center'&&axis==='piezo')reference=m.row.tec_center_reference_confirmed===true?m.row.tec_center_reference_dac:null;
      const stamp=m.ownerFresh?m.owner.sample_unix_s:m.row.recorded_unix_s??m.row.source_acquired_unix_s;
      const cacheKey=bundle.id+'/'+axis;
      let cached=outputRecords.get(cacheKey)||{};
      // Keep outputs visible after a lock ends, explicitly as historical data.
      // A stopped lock's former center is not the present open-loop output.
      if(!m.hasLock)cached.reference=cached.actual;
      if(finite(stamp)){
        if(finite(actual))cached.actual={value:actual,stamp,...(m.ownerFresh?{receivedAt:m.owner.receivedAt,ageAtReceipt:m.owner.ageAtReceipt}:{})};
        if(finite(reference))cached.reference={value:reference,stamp,...(m.ownerFresh?{receivedAt:m.owner.receivedAt,ageAtReceipt:m.owner.ageAtReceipt}:{})};
        outputRecords.set(cacheKey,cached);
      }
      const recorded=value=>{
        if(!value)return '未取得';
        const age=value.receivedAt===undefined?Math.max(0,Date.now()/1000-value.stamp):value.ageAtReceipt+Math.max(0,performance.now()-value.receivedAt)/1000;
        return fmt(value.value,info?.unit==='count'?0:6)+' · '+age.toFixed(0)+' s ago'+(!state.connected||!(m.ownerFresh||m.fresh)?' · last record':'');
      };
      row.querySelector('[data-actual]').textContent=recorded(cached.actual);
      const editor=editors.find(e=>e.key===axis);if(editor){editor.input.disabled=!kind||kind==='refused';if((m.ownerFresh||m.fresh)&&state.connected&&finite(reference))editor.observe(reference,stamp);}
      const referenceCell=row.querySelector('[data-reference]');
      if(!referenceCell._parts){const value=node('span'),mode=node('span');referenceCell.append(value,document.createTextNode('\n'),mode);referenceCell._parts={value,mode};}
      referenceCell._parts.value.textContent=recorded(cached.reference);
      referenceCell._parts.mode.textContent=info?.disabled_reason|| (kind==='dac_center'?'ループの中心目標':kind==='reference_move'?'ロックの基準値':kind==='output'?'保持出力':'Read only / unavailable for this control mode');
      for(const e of row.querySelectorAll('[data-control-write]'))e.disabled=blocked||!findCard('tune.operating_point')||!kind||kind==='refused';
    }
  }
  async function poll() {
    if(polling||!box||state.view!=='laser-settings'||(typeof dashboardVisible==='function'&&!dashboardVisible()))return;
    polling=true;const id=bundle.id;void pollOwner();
    try {
      if(!connected||Date.now()-definitionReadAt>=5000){const b=await safeFetch('sets/'+encodeURIComponent(id));if(bundle?.id!==id)return;bundle=b;connected=true;definitionReadAt=Date.now();}
      const external=targetChange();
      if(external&&external.id!==pending?.id){
        const j=await safeFetch('jobs/'+encodeURIComponent(external.id));if(bundle?.id!==id)return;
        state.jobs=state.jobs.map(old=>old.id===j.id?{...old,...j}:old);
        if(cancellationRequests.has(j.id))message('Target change '+j.id.slice(0,8)+': '+j.status+'. Lock and applied target retained.');
      }
      if(pending?.id) {
        const j=await safeFetch('jobs/'+encodeURIComponent(pending.id));
        const sequence=j.lock_target?.sequence;
        const step=sequence?` · Step ${sequence.step} → ${fmt(sequence.step_target_hz/1e12,9)} THz · Final ${fmt(sequence.final_target_hz/1e12,9)} THz`:'';
        const tec=j.lock_target?.tec_centering;
        const centering=tec&&tec.status!=='settled'?` · Waiting for TEC/PZT center: ${tec.reason}`:'';
        const point=j.operating_point,waiting=point?.status==='submitted'?` · Waiting for controller${point.reason?' — '+point.reason:''}`:'';
        message('Action '+j.id.slice(0,8)+': '+j.status+step+centering+waiting+(j.message?' — '+j.message:''));
        if(finished(j.status)){state.jobs=state.jobs.map(old=>old.id===j.id?{...old,...j}:old);localStorage.removeItem(storageKey());pending=null;definitionReadAt=0;ownerReadAt=-Infinity;void pollOwner();const waiter=waiters.get(j.id);waiters.delete(j.id);if(['succeeded','completed'].includes(j.status))waiter?.resolve(j);else waiter?.reject(Error('Action ended: '+j.status+'. Remaining bulk edits were not sent.'));}
      }
    } catch(e){connected=false;message('Readback unavailable: '+e.message);}
    finally{polling=false;update();}
  }
  const render=renderLaserConfiguration;renderLaserConfiguration=function(){render();mount();};
  window.addEventListener('hashchange',()=>{void poll();});
  const refreshStatus=updateCardStatus;updateCardStatus=function(){refreshStatus();update();};
  setInterval(()=>{update();poll();},1000);
  // Lightweight progress is independent of full dashboard/definition refresh.
  // Never overlap requests or resend a mutation; hidden pages do not poll.
  setInterval(()=>{void pollOwner();if(pending?.id)void poll();},250);
})();
