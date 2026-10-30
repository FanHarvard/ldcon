'use strict';
// Presentation groups only. Admission and safety classification remain server-side.
window.ldconParameterGroup = function(path) {
  const keys=path.map(String), leaf=keys.at(-1);
  if(keys[0]==='target')return leaf==='frequency_hz'||leaf==='wavelength_nm'?'operating':'limits';
  if(keys[0]==='name')return 'operating';
  if(keys[0]==='allow_output'||keys.includes('absolute_conditions'))return 'limits';
  if(keys[0]!=='devices'||keys[2]!=='control_settings')return 'definitions';
  if(keys.length===6&&keys[3]==='native_current'&&keys[4]==='native'&&leaf==='integral_limit_count')return 'operating';
  if(keys.some(k=>/^(writes_enabled|arm_enabled|calibration|calibration_priors|current_safety|current_bounds_a|activation_range_a)$/.test(k)))return 'limits';
  if(keys.some(k=>/^(connection|dac|adc|native_input|input|optical|preserved|cld_interlock|udp_gate|programming|commissioning)$/.test(k))) {
    if(keys.includes('optical')&&keys.some(k=>['single_mode_max','warning_max','classification_enabled'].includes(k)))return 'operating';
    return 'definitions';
  }
  if(/(?:^|_)(?:revision|evidence|source|identity|verified|firmware_version)$/.test(leaf)||leaf==='unit')return 'definitions';
  if(keys.some(k=>/(?:^|_)(?:bounds|limit|limits|safety|timeout|watchdog)(?:_|$)/.test(k)||/^(?:min|max)_/.test(k)))return 'limits';
  return 'operating';
};
// Typed editing of an existing definition only. No path creation or runtime writes.
window.ldconConfigurationEditor = function(value, options={}) {
  const wrapper = node('details', '', 'configuration-editor');
  const kind=options.kind||'lasers';wrapper.open=options.open===true;
  wrapper.append(node('summary', options.label||'レーザー設定・安全パラメーターを編集'));
  const draft = JSON.parse(JSON.stringify(value.document)), fields = [], table = node('table');
  let baseDocument=JSON.parse(JSON.stringify(value.document)), baseRevision=value.revision;
  const head = node('tr');
  for (const title of ['項目', '保存値', '変更値', '単位']) head.append(node('th', title));
  table.append(head);
  function visit(original, path = []) {
    if (original !== null && typeof original === 'object') {
      for (const [key, child] of Object.entries(original)) visit(child, [...path, key]);
      return;
    }
    if(options.group && window.ldconParameterGroup(path)!==options.group)return;
    const row = node('tr'), field = node(original === true || original === false ? 'select' : 'input');
    field.dataset.configurationPath = JSON.stringify(path);
    const current = original === null ? 'null' : String(original);
    if (typeof original === 'boolean') {
      for (const text of ['true', 'false']) { const option = node('option', text); option.value = text; field.append(option); }
    } else { field.type = typeof original === 'number' ? 'number' : 'text'; if(field.type === 'number') field.step = 'any'; }
    field.value = current; field.setAttribute('aria-label', path.join(' / '));
    // Identity and schema changes belong to the explicit advanced definition editor.
    if (path.length === 1 && ['id', 'schema_version'].includes(path[0])) field.disabled = true;
    const key = [...path].reverse().find(part=>!/^\d+$/.test(part))||'';
    const units = [['hz_per_v','Hz/V'],['a_per_v','A/V'],['v_per_s','V/s'],['a_per_s','A/s'],['count_per_nm_s','count/(nm·s)'],['count_per_nm','count/nm'],['count','count'],['hz','Hz'],['nm','nm'],['ms','ms'],['s','s'],['v','V'],['a','A']];
    let unit = units.find(([suffix])=>key.endsWith('_'+suffix))?.[1]||'—';
    if(unit==='—'&&path[0]==='devices'&&path.includes('axes')) {
      const axis=path[path.indexOf('axes')+1];
      const base=value.document.devices?.[path[1]]?.control_settings?.axes?.[axis]?.unit;
      if(base&&/^(bounds|max_step|max_slew)/.test(key))unit=base+(key.startsWith('max_slew')?'/s':'');
    }
    let parent=draft,context=[];for(const key of path.slice(0,-1)){parent=parent[key];if(parent&&typeof parent==='object')for(const name of ['id','action','name','deck_id'])if(typeof parent[name]==='string')context.push(parent[name]);}
    const pathText=path.reduce((text,key)=>/^\d+$/.test(key)?text+'['+key+']':text+(text?'.':'')+key,'');
    const title=node('div');title.append(node('code',pathText));if(context.length)title.append(node('small',[...new Set(context)].join(' · ')));
    cell(row, title); const savedCell=cell(row, current); cell(row, field); cell(row, unit);
    table.append(row); fields.push({path, original, field, row, savedCell, search:(pathText+' '+context.join(' ')+' '+current).toLowerCase()});
  }
  visit(draft);
  const searchLabel=node('label','パラメーター検索'),search=node('input'),count=node('span');search.type='search';search.placeholder='名前・パス・値（例: current_excursion、offsets）';search.dataset.parameterSearch=kind;searchLabel.append(search);
  let scope=[];
  const breadcrumbs=node('nav','','parameter-breadcrumbs'),suggestions=node('datalist');
  const serial=(window.ldconParameterSearchSequence=(window.ldconParameterSearchSequence||0)+1);
  suggestions.id='parameter-completion-'+serial;search.setAttribute('list',suggestions.id);
  search.setAttribute('autocomplete','off');search.placeholder='Search within this level; use a name, path or value';
  const folders=new Map(),normalize=s=>String(s).normalize('NFKC').toLowerCase().replace(/[\s_.\[\]\/\-]+/g,'');
  const prefix=(p,base)=>base.every((k,i)=>p[i]===k);
  function score(text,query){
    const target=normalize(text),terms=query.trim().split(/\s+/).map(normalize).filter(Boolean);let result=0;
    for(const term of terms){let at=target.indexOf(term);if(at>=0){result+=at;continue;}
      let cursor=0,first=-1,last=-1;for(const c of term){cursor=target.indexOf(c,cursor);if(cursor<0)return Infinity;if(first<0)first=cursor;last=cursor++;}
      result+=100+last-first-term.length+first;
    }return result;
  }
  function navigate(path){scope=[...path];search.value='';filter();search.focus();}
  for(const f of fields)for(let depth=1;depth<f.path.length;depth++){
    const path=f.path.slice(0,depth),key=JSON.stringify(path);if(folders.has(key))continue;
    const row=node('tr'),name=node('td'),open=button(path.at(-1)+' /',()=>navigate(path));
    open.dataset.parameterFolder=path.join('.');name.colSpan=4;name.append(open);row.append(name);row.hidden=true;table.append(row);
    folders.set(key,{path,row,field:null,search:path.join('.')});
  }
  let firstMatch=null;
  function filter(){
    const query=search.value.trim(),candidates=[...folders.values(),...fields];
    const matches=candidates.filter(f=>prefix(f.path,scope)&&f.path.length>scope.length)
      .map(f=>({f,rank:query?score(f.search,query):0}))
      .filter(({f,rank})=>Number.isFinite(rank)&&(query||f.path.length===scope.length+1))
      .sort((a,b)=>a.rank-b.rank||a.f.path.join('.').localeCompare(b.f.path.join('.')));
    for(const f of candidates)f.row.hidden=true;
    for(const {f} of matches.slice(0,100)){f.row.hidden=false;table.append(f.row);}
    firstMatch=matches[0]?.f;
    count.textContent=matches.length>100?'100 / '+matches.length+' matches':matches.length+' items';
    breadcrumbs.replaceChildren(button('All parameters',()=>navigate([])));
    for(let i=0;i<scope.length;i++){breadcrumbs.append(document.createTextNode(' / '),button(scope[i],()=>navigate(scope.slice(0,i+1))));}
    suggestions.replaceChildren();
    for(const {f} of matches.slice(0,50)){const o=node('option');o.value=f.path.slice(scope.length).join('.')+(f.field?'':'.');suggestions.append(o);}
  }
  search.addEventListener('input',filter);
  search.addEventListener('keydown',event=>{
    if(event.key!=='Enter'&&event.key!=='Tab')return;
    if(event.key==='Tab'&&(!search.value.trim()||event.shiftKey))return;
    const query=search.value.replace(/\.$/,'');
    const exact=[...folders.values(),...fields].find(f=>prefix(f.path,scope)&&f.path.slice(scope.length).join('.')===query);
    const next=exact||firstMatch;if(!next)return;
    event.preventDefault();
    if(!next.field)navigate(next.path);
    else{search.value=next.path.slice(scope.length).join('.');filter();next.field.focus();}
  });
  filter();
  const scroll = node('div', '', 'table-wrap configuration-fields'); scroll.append(table);
  const reference=node('input'), referenceLabel=node('label','承認・根拠の参照（承認者・日時・理由）');reference.type='text';reference.maxLength=1000;referenceLabel.append(reference);
  const changes = node('div'), acknowledge = node('input'); acknowledge.type = 'checkbox';
  const label = node('label', '', 'check'); label.append(acknowledge, document.createTextNode('上記の差分を確認し、この設定変更を承認します。'));
  let reviewed = null, reviewedRevision = null;
  const revisionLabel=node('p',kind+'/'+value.id+' · revision '+baseRevision,'config-revision');
  function candidate() {
    const result = JSON.parse(JSON.stringify(baseDocument)), diffs = [];
    for (const {path, original, field} of fields) {
      let next;
      if (original === null) { next = JSON.parse(field.value); if(next !== null && typeof next === 'object') throw Error('スカラー値を入力してください。'); }
      else if (typeof original === 'number') { if(!field.value.trim()) throw Error('数値は空にできません。'); next = Number(field.value); if(!Number.isFinite(next)) throw Error('有限の数値を入力してください。'); }
      else if(typeof original === 'boolean') next = field.value === 'true';
      else next = field.value;
      if(next !== original) {
        let parent=result; for(const key of path.slice(0,-1)) parent=parent[key]; parent[path.at(-1)]=next;
        diffs.push({path:path.join(' / '), keys:path, before:original, after:next});
      }
    }
    return {result, diffs};
  }
  const save = button('承認した差分を検証して保存', async () => {
    const next=candidate();
    if(!reviewed || !acknowledge.checked || JSON.stringify(next.result,null,2)!==reviewed) throw Error('差分を確認して承認してください。');
    let saved;
    try {
      saved=await api(`configuration/${kind}/${encodeURIComponent(value.id)}`, 'PUT', {yaml:reviewed, expected_revision:reviewedRevision, ...(reference.value.trim()?{maintainer_reference:reference.value.trim()}:{})});
    } catch(error) {
      if (/^Configuration changed[. ]/.test(error.message)) {
        reset();
        changes.append(node('p','Configuration changed after review. Your draft is preserved. Review the latest differences and approve again.'));
      }
      throw error; // A rejected or unknown save is never replayed automatically.
    }
    const native=Object.values(saved?.live?.native||{});
    let notice='保存しました。実行中の制御やownerへの反映とは別です。';
    if(native.some(v=>v.gains_pending))notice='Saved. Requested PI settings differ from the running controller; inspect pending application.';
    else if(native.some(v=>v.integral_limit_applied===true))notice='Saved. The owner verified the new integral output limit on the running PI controller.';
    await loadDefinitions(); $('#notice').textContent=notice; $('#notice').hidden=false;
  }, 'primary'); save.disabled=true;
  function reset() { reviewed=null; reviewedRevision=null; acknowledge.checked=false; save.disabled=true; changes.replaceChildren(); }
  for(const {field} of fields) field.addEventListener('input',reset);
  acknowledge.onchange=()=>{save.disabled=!reviewed||!acknowledge.checked;};
  const preview = button('変更差分を確認', async () => {
    const draftBefore=candidate();reset();
    if(!draftBefore.diffs.length){changes.append(node('p','変更はありません。'));return;}
    const latest=await api(`configuration/${kind}/${encodeURIComponent(value.id)}`);
    if(JSON.stringify(candidate().result)!==JSON.stringify(draftBefore.result))
      throw Error('Draft changed while reading configuration. Review the differences again.');
    // Rebase only explicitly edited scalar fields, never an old whole document.
    // Same-field conflicts are shown with the new saved value and require a new
    // human review/acknowledgement. No PUT is made by this read-only operation.
    const readPath=(document,path)=>path.reduce((v,key)=>{
      if(v===null||typeof v!=='object'||!Object.hasOwn(v,key))throw Error('Configuration structure changed. Your draft is preserved; reload before editing.');
      return v[key];
    },document);
    const current=fields.map(f=>readPath(latest.document,f.path));
    if(current.some((v,i)=>v!==null&&typeof v==='object'||typeof v!==typeof fields[i].original))
      throw Error('Configuration field types changed. Your draft is preserved; reload before editing.');
    const edited=new Set(draftBefore.diffs.map(d=>JSON.stringify(d.keys)));
    let sameFieldConflict=false;
    fields.forEach((f,i)=>{
      const changed=edited.has(JSON.stringify(f.path));
      if(changed&&current[i]!==f.original)sameFieldConflict=true;
      f.original=current[i];f.savedCell.textContent=current[i]===null?'null':String(current[i]);
      if(!changed)f.field.value=f.savedCell.textContent;
    });
    const refreshed=latest.revision!==baseRevision;
    baseDocument=JSON.parse(JSON.stringify(latest.document));baseRevision=latest.revision;
    revisionLabel.textContent=kind+'/'+value.id+' · revision '+baseRevision;
    const next=candidate();
    if(refreshed)changes.append(node('p',sameFieldConflict?
      'An edited field also changed on the server. Review its current saved value before approving your draft.':
      'Latest configuration loaded. Other changes are preserved; only your edited fields will be saved.'));
    if(!next.diffs.length){reviewed=null;changes.append(node('p','変更はありません。'));return;}
    reviewed=JSON.stringify(next.result,null,2);reviewedRevision=baseRevision;const diff=node('table'),header=node('tr');
    for(const title of ['項目','変更前','変更後'])header.append(node('th',title));diff.append(header);
    for(const item of next.diffs){const row=node('tr');cell(row,item.path);cell(row,JSON.stringify(item.before));cell(row,JSON.stringify(item.after));diff.append(row);}
    changes.append(diff);
  });
  wrapper.dataset.parameterGroup=options.group||kind;
  wrapper.append(node('p','安全範囲・較正・制御上限の変更は、人間が具体的な差分を確認してください。保存時に既存の安全検証・稼働中編集制限・版の競合検査を行います。保存だけでは機器を操作しません。','muted'),revisionLabel);
  if(value.source_path){const source=node('p','','config-source');source.append(node('span','YAML file'),node('code',value.source_path));wrapper.append(source);}
  wrapper.append(searchLabel,suggestions,breadcrumbs,count, scroll, preview, changes, referenceLabel, label, save);
  return wrapper;
};
