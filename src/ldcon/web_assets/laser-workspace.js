'use strict';
// Presentation only. Writes continue through the existing card/configuration APIs.
window.ldconLaserWorkspace = function(bundle) {
  const fragment=document.createDocumentFragment(), laser=bundle.laser_definition;
  document.querySelector('#laser-settings > h1').textContent=laser.document.name||laser.id;
  const nav=node('div','','workspace-nav');nav.setAttribute('aria-label','レーザー内の表示');
  for(const [id,label] of [['live-controls','目標波長・動作点'],['laser-cards','操作カード'],['laser-parameters','パラメーター'],['laser-topology','機器構成'],['laser-agent','エージェント・特性'],['laser-history','実行ログ']]) {
    nav.append(button(label,()=>{const target=document.getElementById(id);target?.scrollIntoView({behavior:'smooth',block:'start'});}));
  }
  fragment.append(nav);
  function section(id,title,note) {const s=node('section','','workspace-section');s.id=id;s.append(node('h2',title));if(note)s.append(node('p',note,'muted'));fragment.append(s);return s;}
  const modeSection=section('laser-operating-mode','Operating mode',
    'Measurement prioritizes continuity. Characterization allows informative experiments within the existing approved limits. Changing mode does not change safety limits or restart the lock.');
  const modeStatus=node('p','Reading owner mode…','muted'),modeSelect=node('select'),modeReason=node('input');
  for(const [value,label] of [['measurement','Measurement — preserve continuity'],['characterization','Characterization — explore and learn']]){const option=node('option',label);option.value=value;modeSelect.append(option);}
  modeReason.type='text';modeReason.maxLength=1000;modeReason.placeholder='Reason for this operating mode';
  const modeRecord=node('dl','','operating-mode-record'),savedReason=node('dd'),changedAt=node('time');
  savedReason.dataset.operatingModeReason='';savedReason.dataset.noTranslate='';changedAt.dataset.operatingModeChanged='';
  const changedValue=node('dd');changedValue.append(changedAt);
  modeRecord.append(node('dt','Current reason'),savedReason,node('dt','Changed at'),changedValue);
  savedReason.textContent='Not recorded';changedAt.textContent='Not recorded';
  function showModeRecord(value){
    savedReason.textContent=typeof value.reason==='string'&&value.reason.trim()?value.reason:'Not recorded';
    const seconds=value.updated_unix_s,date=typeof seconds==='number'?new Date(seconds*1000):null;
    if(date&&Number.isFinite(date.getTime())){changedAt.dateTime=date.toISOString();changedAt.textContent=date.toLocaleString(undefined,{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',timeZoneName:'short'});}
    else{changedAt.removeAttribute('datetime');changedAt.textContent='Not recorded';}
  }
  let modeRevision=null;
  async function readMode(){try{const v=await api(`lasers/${encodeURIComponent(laser.id)}/operating-mode`);modeRevision=v.revision;modeSelect.value=v.mode;showModeRecord(v);modeStatus.textContent=`${v.mode} · revision ${v.revision}`+(v.background_interruption?.pending?' · Restoring the previous adjustment checkpoint':'');applyMode.disabled=false;}catch(e){modeRevision=null;applyMode.disabled=true;modeStatus.textContent='Owner mode unavailable: '+e.message;}}
  const applyMode=button('Apply operating mode',async()=>{
    if(modeRevision===null)return;applyMode.disabled=true;
    try{
      // LAN HTTP pages have getRandomValues, but not secure-context randomUUID.
      const requestId='mode-'+[...crypto.getRandomValues(new Uint8Array(16))].map(value=>value.toString(16).padStart(2,'0')).join('');
      const response=await fetch(base+`lasers/${encodeURIComponent(laser.id)}/operating-mode`,{method:'PUT',headers:{'Content-Type':'application/json','X-LDCon-Client':'web'},body:JSON.stringify({request_id:requestId,expected_revision:modeRevision,desired_mode:modeSelect.value,reason:modeReason.value})});
      const value=await response.json();if(!response.ok)throw Error(value.detail||'Operating mode update rejected');
      await readMode();
    }catch(e){modeRevision=null;modeStatus.textContent=e.message+' Read owner mode before trying again.';}
    finally{applyMode.disabled=modeRevision===null;}
  });
  applyMode.disabled=true;
  for(const [text,element] of [['Mode',modeSelect],['Reason',modeReason]]){const label=node('label',text);label.append(element);modeSection.append(label);}
  modeSection.append(modeStatus,modeRecord,applyMode,button('Read owner mode',readMode));void readMode();
  const guidance=section('laser-adjustment-guidance','Suggested initial adjustment steps',
    'Suggestions for the first local trial, not safety limits or required increments. The task model may change them using measured evidence and must explain its choice. Saved advice is read again before each adjustment decision.');
  const guidanceTable=node('table'),guidanceStatus=node('p','','muted');
  let guidanceValue=null;
  const saveGuidance=button('Save suggested steps',async()=>{
    if(!guidanceValue)return;saveGuidance.disabled=true;
    try{await api(`lasers/${encodeURIComponent(laser.id)}/adjustment-guidance`,'PUT',{expected_revision:guidanceValue.revision,document:guidanceValue.document});await readGuidance();guidanceStatus.textContent='Saved. No physical setting was changed.';}
    catch(e){guidanceStatus.textContent=e.message;}finally{saveGuidance.disabled=!guidanceValue;}
  });
  saveGuidance.disabled=true;
  async function readGuidance(){
    try{
      guidanceValue=await api(`lasers/${encodeURIComponent(laser.id)}/adjustment-guidance`);guidanceTable.replaceChildren();
      const head=node('tr');for(const label of ['Knob','Suggested initial step','Unit / interpretation','Basis'])head.append(node('th',label));guidanceTable.append(head);
      for(const [knob,advice] of Object.entries(guidanceValue.document.knobs)){
        const row=node('tr'),input=node('input');input.type='number';input.step='any';input.value=advice.kind==='relative'?advice.step*100:advice.step;
        input.setAttribute('aria-label',`${knob} suggested step`);input.addEventListener('input',()=>{advice.step=Number(input.value)/(advice.kind==='relative'?100:1);});
        cell(row,knob);cell(row,input);cell(row,advice.kind==='relative'?'% of current magnitude':advice.unit);cell(row,advice.reason);guidanceTable.append(row);
      }
      saveGuidance.disabled=false;guidanceStatus.textContent=guidanceValue.availability==='not_configured'?'No suggestions configured; use measured history.':'';
    }catch(e){guidanceValue=null;saveGuidance.disabled=true;guidanceStatus.textContent=e.message;}
  }
  guidance.append(guidanceTable,guidanceStatus,saveGuidance,button('Reload suggested steps',readGuidance));void readGuidance();
  const params=section('laser-parameters','パラメーター','保存された設定です。現在の実機への反映状態は動作点・実行ログで確認してください。');
  const sources=node('dl','','configuration-sources');
  for(const [label,definition] of [['Laser YAML',laser],['Assignment YAML',bundle],...bundle.deck_definitions.map(d=>['Deck YAML · '+d.id,d])]){
    const value=node('dd');value.append(definition.source_path?node('code',definition.source_path):node('span','Source path unavailable'));sources.append(node('dt',label),value);
  }
  params.append(sources,node('h3','Routine settings'));
  guidance.className='adjustment-guidance';const guidanceToggle=node('details');guidanceToggle.append(node('summary','Suggested initial adjustment steps'));guidance.querySelector('h2').remove();while(guidance.firstChild)guidanceToggle.append(guidance.firstChild);guidance.append(guidanceToggle);
  // Move the existing editor, including its revision-bound handlers, into Parameters.
  params.append(guidance);
  params.append(window.ldconConfigurationEditor(laser,{label:'Target, control and optical quality',group:'operating'}));
  params.append(window.ldconConfigurationEditor({id:bundle.id,document:bundle.source_document,revision:bundle.assignment_revision,source_path:bundle.source_path},{kind:'assignments',label:'Card defaults for this laser'}));
  const advanced=node('details','','parameter-advanced');advanced.append(node('summary','Advanced configuration — limits, calibration and device definitions'),
    node('p','Open these settings when changing hardware, calibration or approved limits. Grouping does not change edit permissions or validation.','muted'),
    window.ldconConfigurationEditor(laser,{label:'Limits, calibration and output permissions',group:'limits'}),
    window.ldconConfigurationEditor(laser,{label:'Device definitions, connections and measurement sources',group:'definitions'}));
  const raw=node('details');raw.append(node('summary','定義全体の編集（YAML）'),editor('lasers',laser),editor('assignments',{id:bundle.id,yaml:bundle.yaml,revision:bundle.assignment_revision},'割当・レーザー固有の既定パラメーター'));advanced.append(raw);params.append(advanced);
  const topology=section('laser-topology','機器構成','Configured control and measurement routes. Channels and methods come from the saved definition; this is not a live wiring test. Missing details are marked as unspecified.');
  topology.append(window.ldconTopology(laser.document));
  const agent=section('laser-agent','エージェント・レーザーの特性','全エージェントで共有する知識と、カードごとの個別依頼を分けて保存します。');
  const resident=bundle.deck_definitions.flatMap(d=>d.document.cards).find(c=>c.operation==='agent.resident');
  const chatLink=link('Open gateway conversation ↗','/gateway-chat/'+encodeURIComponent(laser.id));chatLink.target='_blank';chatLink.rel='noopener noreferrer';agent.append(chatLink);
  const agentOptions=node('div','','laser-agent-options');agent.append(agentOptions,residentWorkspaceEditor(bundle,resident||{id:'laser-context'}));
  const cards=section('laser-cards','操作カード','開始は1回の実行です。今回のパラメーターと保存済みの既定値を区別してください。');
  for(const deck of bundle.deck_definitions) {const group=node('details');group.open=bundle.deck_definitions.length===1;group.append(node('summary',deck.document.name||deck.id),cardTable(bundle,deck,agentOptions));cards.append(group);}
  nav.after(cards); // mount() inserts live controls before this existing section.
  const history=section('laser-history','このレーザーの実行ログ');
  const rows=node('div'), status=node('p','','muted');history.append(status,rows);
  let offset=0,version=0;
  const prev=button('前へ',()=>{offset=Math.max(0,offset-25);return load();}),next=button('次へ',()=>{offset+=25;return load();});
  async function load(){const turn=++version;status.textContent='読込中';try{const v=await api(`history?set_id=${encodeURIComponent(bundle.id)}&limit=25&offset=${offset}`);if(turn!==version)return;rows.replaceChildren();const table=node('table'),head=node('tr');for(const label of ['開始日時','実行カード','状態','PDFレポート'])head.append(node('th',label));table.append(head);for(const job of v.jobs){const r=node('tr');cell(r,timestamp(job.created_utc));cell(r,(job.cards||[]).map(c=>cardLabel(c.operation||c.card,c.params)||c.id).join(', ')||job.deck_id||'—');cell(r,badge(job.status));cell(r,link('PDFレポートを読む ↗',base+`jobs/${encodeURIComponent(job.id)}/report.pdf`));table.append(r);}rows.append(table);status.textContent=v.total+' 件';prev.disabled=offset===0;next.disabled=offset+25>=v.total;}catch(e){status.textContent=e.message;}}
  history.append(prev,next,button('更新',load));void load();
  return fragment;
};

window.ldconTopology = function(laser) {
  const container=node('div','','topology');
  const tables={};
  for(const [id,title] of [['control','Control outputs'],['measurement','Measurements and readbacks']]){
    const table=node('table'),head=node('tr');table.dataset.instrumentTable=id;
    for(const text of ['Quantity / purpose','Device / owner','Connection','Channel / input','Method / interpretation'])head.append(node('th',text));
    table.append(head);const scroll=node('div','','table-wrap');scroll.append(table);container.append(node('h3',title),scroll);tables[id]=table;
  }
  function address(r={}) {
    const raw=String(r.resource||r.host||r.endpoint||r.address||r.instrument||'Not specified');
    // Never turn an endpoint capability, query or credential into display text.
    if(/^https?:/.test(raw)){try{return new URL(raw).origin;}catch(_){return 'Not specified';}}
    return raw.split('?')[0];
  }
  function channel(r={},fallback='Not specified') {return r.channel==null?fallback:'CH '+r.channel;}
  function add(kind,quantity,device,r,ch,method,path) {
    const row=node('tr');row.dataset.configurationRoute=path;
    const q=node('div');q.append(node('strong',quantity),node('code',path));
    cell(row,q);cell(row,device);cell(row,address(r));cell(row,ch);cell(row,method);
    tables[kind].append(row);
  }
  const names={current:'Diode current',temperature:'Temperature',piezo:'PZT drive',current_modulation:'Current modulation',measurement:'Scalar measurement',observation:'Optical observation',wavelength:'Wavelength / frequency',native_current:'Current lock',actuator:'Actuator',lock:'Stationary lock',loop:'Loop status'};
  for(const [id,d] of Object.entries(laser.devices||{})) {
    const c=d.connection||{},settings=d.control_settings||{},path='devices.'+id,owner=id+' · '+d.type;
    if(d.type==='ecdl.rig.owner') {
      for(const [port,key,label,method] of [
        ['piezo','piezo','PZT drive','ESP32 servo DAC output; count readback, not measured PZT voltage.'],
        ['current','current_awg','Current modulation input','AWG voltage into the current modulation input; this is not measured diode current.'],
        ['temperature','tec_awg','TEC modulation input','AWG voltage into the temperature control input; this is not measured temperature.']]) {
        const resource=c.resources?.[key];if(!resource)continue;
        add('control',label,key+' / '+id,resource,channel(resource,key==='piezo'?'Servo DAC':'Not specified'),method+(resource.preserve_channels?.length?' Preserved channels: '+resource.preserve_channels.join(', ')+'.':''),path+'.connection.resources.'+key);
        if(key==='piezo')add('measurement','PZT DAC count',key+' / '+id,resource,'Servo DAC','Digital controller readback (count); no voltage measurement.',path+'.connection.resources.'+key);
      }
      if(c.wavemeter){
        add('measurement','Wavelength / frequency','Wavemeter / '+id,c.wavemeter,channel(c.wavemeter)+(c.wavemeter.cycle==null?'':' · cycle '+c.wavemeter.cycle),'Wavemeter acquisition.',path+'.connection.wavemeter');
        if(settings.optical){const o=settings.optical;add('measurement','Optical mode quality','Wavemeter pattern / '+id,c.wavemeter,channel(c.wavemeter)+' · interferometer '+(o.interferometer??'Not specified'),
          'Side-peak height, prominence and width from the raw fringe pattern; method: '+(o.metric_method||'Not specified')+'.',path+'.control_settings.optical');}
      }
      const dither=settings.steady_lock?.dither;
      if(dither)add('measurement','Dither response','ESP32 telemetry / '+id,{endpoint:dither.telemetry_endpoint},'Servo DAC',
        'Fit the servo DAC response to current dither'+(dither.frequency_hz==null?'':' at '+dither.frequency_hz+' Hz')+'. Source selector: '+(dither.selector||'Not specified'),path+'.control_settings.steady_lock.dither');
    } else if(d.type==='esp32servocon.native_current.owner') {
      const native=settings.native_current||{};
      add('control','Current modulation / frequency lock',owner,c,'Servo DAC → current driver modulation input','ESP32 PI and feedforward; configured current driver: '+(native.cld_interlock?.instrument||'Not specified')+'.',path+'.control_settings.native_current');
      add('measurement','Current DAC count',owner,c,'Servo DAC','Digital controller readback (count).',path+'.control_settings.native_current.dac');
      if(native.calibration)add('measurement','Estimated modulated current',owner,c,'Servo DAC','Calculated from DAC voltage, bias and calibration; not an independent current measurement.',path+'.control_settings.native_current.calibration');
    } else if(d.type==='fizeau.owner') {
      const source=c.source||c;
      add('measurement','Wavelength / frequency',owner,source,channel(source)+(source.cycle==null?'':' · cycle '+source.cycle),'Wavemeter acquisition.',path+'.connection'+(c.source?'.source':''));
    } else {
      const bindings=Object.entries(laser.interfaces||{}).filter(([,b])=>b.device===id);
      const seen=new Set();
      for(const [role,b] of bindings){
        // An actuator alias does not describe another physical output.
        const key=b.axis||role;if(seen.has(key))continue;seen.add(key);
        const control=/controller|actuator|modulation|bounded_axis|lock/.test(b.capability||'');
        const method=d.type==='thorlabs.cld1015.broker'?'Direct driver setpoint; controller readback through its owner.':/awg|sdg/.test(d.type||'')?'AWG output; downstream wiring is not specified by the channel alone.':'Configured capability: '+(b.capability||'Not specified');
        add(control?'control':'measurement',names[key]||key,owner,c,channel(c,d.type==='thorlabs.cld1015.broker'?'Single controller':undefined),method,'interfaces.'+role);
      }
      if(!bindings.length)add('control','Unassigned device',owner,c,channel(c),'No interface is assigned; no control purpose inferred.',path+'.connection');
    }
  }
  for(const table of Object.values(tables))if(table.rows.length===1){const row=node('tr'),text=node('td','No routes configured');text.colSpan=5;row.append(text);table.append(row);}
  return container;
};

window.ldconResidentOptions = function(params,laserId) {
  const section=node('section','','resident-options'),provider=node('select'),difficulty=node('select'),status=node('p','','muted'),resolved=node('p','','muted');
  provider.dataset.agentProvider='';difficulty.dataset.gatewayDifficulty='';
  const labels={claude:'Claude Code',codex:'Codex',opencode:'OpenCode',deepseek_harness:'DeepSeek Harness'};
  for(const [id,title] of Object.entries(labels)){const o=node('option',title);o.value=id;provider.append(o);}
  for(const level of ['basic','standard','deep']){const o=node('option',level[0].toUpperCase()+level.slice(1));o.value=level;difficulty.append(o);}
  let routing=null,profiles={},dirty=false;
  const label=node('label','Agent provider '),levelLabel=node('label','Gateway difficulty ');label.append(provider);levelLabel.append(difficulty);
  const save=button('Save laser agent default',async()=>{
    if(!routing?.configured)throw Error('Configure the difficulty routing table first.');
    const selection={provider:provider.value,difficulty:difficulty.value},document=structuredClone(routing.document);
    if(!document.provider_profiles[selection.provider]?.[selection.difficulty])throw Error('Configure a profile for this interface and difficulty first.');
    document.lasers[laserId]=selection;
    save.disabled=true;
    try{
      routing=await api('agent-routing','PUT',{document,expected_revision:routing.revision});dirty=false;
      applyDraft();status.textContent='Saved for this laser. Used on the next gateway connection; the running gateway and admitted tasks are unchanged.';
    }finally{save.disabled=!dirty;}
  });save.dataset.saveGatewayDefault='';save.disabled=true;
  function describe(){
    const id=routing?.document.provider_profiles[provider.value]?.[difficulty.value],p=profiles[id];
    resolved.textContent=p?`${id}: ${p.model} / ${p.effort||'provider default'}`:'No profile configured for this interface and difficulty.';
    save.disabled=!dirty||!p;
  }
  function applyDraft(){
    const id=routing?.document.provider_profiles[provider.value]?.[difficulty.value];
    const p=JSON.parse(params.value);Object.assign(p,{provider:provider.value,execution_profile:id||'',model:'',effort:'',session_id:'',remote_control:null,mode:'create_or_connect'});
    params.value=JSON.stringify(p,null,2);params.dispatchEvent(new Event('input'));describe();
  }
  function changed(){dirty=true;applyDraft();status.textContent='Unsaved selection. Save it as this laser default, or Start the gateway for this run only.';}
  provider.onchange=changed;difficulty.onchange=changed;
  try{provider.value=JSON.parse(params.value).provider||'claude';}catch(_){}difficulty.value='basic';
  section.append(node('h3','Gateway interface and task defaults'),label,levelLabel,save,resolved,status,
    node('p','Save records this laser’s default without starting or reconnecting an agent. Cards inherit the running gateway interface; each card’s difficulty selects its task profile from the routing table. An explicit run profile takes precedence.','muted'),
    node('p','Remote Control defaults to enabled for Claude and Codex. OpenCode uses a shared authenticated server. Changing a saved default does not interrupt an active connection.','muted'),
    link('Difficulty routing table and execution profiles ↗','agent-settings#routing'));
  Promise.all([api('agent-routing'),api('agent-profiles')]).then(([r,p])=>{
    routing=r;profiles=p.document.profiles;
    if(!dirty&&r.configured){const selected=r.document.lasers[laserId]||r.document.gateway;provider.value=selected.provider;difficulty.value=selected.difficulty;applyDraft();status.textContent='Saved laser default loaded. The running gateway may use an earlier selection.';}
    else if(!r.configured)status.textContent='Configure the difficulty routing table before saving a default.';
    describe();
  }).catch(e=>{status.textContent=e.message;save.disabled=true;});
  return section;
};

// Fleet detail is a full page, so there is one application and one scroll area.
(() => {
  const marker=location.pathname.indexOf('/services/');if(marker<0)return;
  const all=document.querySelector('[data-view=lasers]');if(all)all.textContent='全レーザー';
  let dirty=false;document.addEventListener('input',e=>{if(e.target.id!=='laser-select'&&e.target.matches('textarea,input:not([type=search]),select'))dirty=true;});
  window.addEventListener('beforeunload',e=>{if(dirty||window.ldconIsBusy?.()){e.preventDefault();e.returnValue='';}});
})();


// Navigation across independent services, without merging their control state.
// Direct single-service pages keep their existing local assignment selector.
(() => {
  const renders = new WeakMap();
  const id = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$/;
  window.ldconRenderLaserSelector = function({select, choices, selected, selectLocal,
      navigate = url => window.location.assign(url)}) {
    const context = {selected}; renders.set(select, context);
    const match = location.pathname.match(/^(.*\/)services\/([A-Za-z0-9][A-Za-z0-9_.-]{0,79})\/?$/);
    const currentService = match?.[2], root = match?.[1];
    const destinations = new Map();
    const status = select.parentElement.querySelector('[data-workspace-status]') || document.createElement('small');
    status.dataset.workspaceStatus = ''; status.setAttribute('role','status');
    status.hidden = true; select.parentElement.append(status);
    function populate(workspaces = []) {
      select.replaceChildren(); destinations.clear();
      for(const choice of choices) {
        const option = document.createElement('option');option.value = choice.id;
        option.textContent = choice.name || choice.id;select.append(option);
      }
      for(const workspace of workspaces) {
        if(!workspace || !id.test(workspace.service_id) || !id.test(workspace.set_id))continue;
        if(workspace.service_id===currentService)continue;
        const value = workspace.service_id+'/'+workspace.set_id;
        if(destinations.has(value))continue;
        destinations.set(value,workspace);
        const option = document.createElement('option');option.value = value;
        option.textContent = workspace.name || workspace.set_id;select.append(option);
      }
      select.value = context.selected;
    }
    populate();
    select.onchange = () => {
      const value = select.value, destination = destinations.get(value);
      if(destination) {
        // If a genuine unsaved-edit warning cancels navigation, the displayed
        // selection must still match the old page and its scoped action API.
        select.value = context.selected;
        const view = ['laser-settings','settings','decks','catalog'].includes(location.hash.slice(1)) ? location.hash : '#laser-settings';
        navigate(root+'services/'+encodeURIComponent(destination.service_id)+'/?set='+encodeURIComponent(destination.set_id)+view);
      } else if(choices.some(choice=>choice.id===value)) {
        context.selected=value;selectLocal(value);
      }
    };
    if(!match)return Promise.resolve();
    const abort = new AbortController(), timer = setTimeout(()=>abort.abort(),3000);
    return fetch(root+'api/workspaces',{headers:{'X-LDCon-Client':'web'},signal:abort.signal})
      .then(response=>{if(!response.ok)throw Error('Workspace list unavailable');return response.json();})
      .then(value=>{
        if(!Array.isArray(value.workspaces))throw Error('Invalid workspace list');
        if(renders.get(select)===context)populate(value.workspaces);
      })
      .catch(()=>{
        if(renders.get(select)!==context)return;
        status.textContent='Other laser workspaces are unavailable. Reload to retry.';status.hidden=false;
      })
      .finally(()=>clearTimeout(timer));
  };
})();
