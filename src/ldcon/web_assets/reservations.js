'use strict';
(() => {
  const $ = s => document.querySelector(s);
  const base = location.pathname.replace(/\/reservations\/?$/, '/') + 'api/';
  const online = new Set(['tune.pid_factor_reduction','tune.pid_coordinate_search', 'study.ecdl_locked_current_map', 'tune.ecdl_window_center']);
  const names = {pending:'条件待ち',waiting_agent:'復旧・相談待ち（期限停止中）',dispatching:'開始確認中',dispatched:'実行先へ引渡済み',cancelled:'取消',expired:'期限切れ',blocked:'条件不成立',review_required:'結果確認が必要'};
  const state = {bundles:[], jobs:[], reservations:[], requestId:newId()};
  function newId() { return [...crypto.getRandomValues(new Uint8Array(16))].map(x=>x.toString(16).padStart(2,'0')).join(''); }
  function el(tag, text='') { const n=document.createElement(tag);n.textContent=text;return n; }
  function notice(text, failed=false) { $('#reservation-notice').textContent=text;$('#reservation-notice').className=failed?'error':''; }
  async function api(path, method='GET', data) {
    const response=await fetch(base+path,{method,headers:{'Content-Type':'application/json','X-LDCon-Client':'web'},body:data===undefined?undefined:JSON.stringify(data)});
    const value=await response.json();if(!response.ok)throw Error(value.detail||response.statusText);return value;
  }
  async function action(fn) { try {await fn();}catch(e){notice(e.message,true);} }
  function options(select, rows, chosen) {
    select.replaceChildren();for(const [value,label] of rows){const option=el('option',label);option.value=value;select.append(option);}
    if(chosen&&rows.some(r=>r[0]===chosen))select.value=chosen;
  }
  function bundle(){return state.bundles.find(b=>b.id===$('#reservation-set').value);}
  function deck(){return bundle()?.deck_definitions.find(d=>d.id===$('#reservation-deck').value);}
  function card(){return deck()?.document.cards.find(c=>c.id===$('#reservation-card').value);}
  function cards(chosen){options($('#reservation-card'),deck()?.document.cards.map(c=>[c.id,c.id+' · '+c.operation])||[],chosen);parameters();}
  function decks(chosen, chosenCard){options($('#reservation-deck'),bundle()?.deck_definitions.map(d=>[d.id,d.document.name])||[],chosen);cards(chosenCard);}
  function parameters(){
    const c=card();$('#reservation-parameters').value=JSON.stringify(deck()?.card_status[c?.id]?.parameters||{},null,2);
    $('#reservation-controller-label').hidden=!online.has(c?.operation);controllers();
  }
  function candidates(){
    const rows=state.jobs.map(j=>[j.id,`${j.set_name||j.set_id} / ${j.cards?.map(c=>c.id).join(',')||j.deck_id} · ${j.status} · ${j.id}`]);
    for(const r of state.reservations)if(!r.online&&!rows.some(x=>x[0]===r.output_job_id))rows.push([r.output_job_id,`${r.target.set_id} / ${r.target.card_id} · 予約: ${names[r.status]||r.status} · ${r.output_job_id}`]);
    return rows;
  }
  function controllers(){
    const ids=[...document.querySelectorAll('.reservation-condition')].filter(row=>row.querySelector('[data-kind]').value==='locked').map(row=>row.querySelector('[data-job]').value);
    options($('#reservation-controller'),candidates().filter(r=>ids.includes(r[0])),$('#reservation-controller').value);
  }
  function label(text,input){const n=el('label',text);n.append(input);return n;}
  function addCondition(){
    const row=el('fieldset');row.className='reservation-condition';const job=el('select');job.dataset.job='';job.required=true;options(job,candidates());
    const kind=el('select');kind.dataset.kind='';options(kind,[['succeeded','終了後（失敗時は復旧方針に従う）'],['locked','新しい測定でロック確認']]);
    const stable=el('input');stable.dataset.stable='';stable.type='number';stable.min='1';stable.max='3600';stable.value='3';
    const age=el('input');age.dataset.age='';age.type='number';age.min='.1';age.max='10';age.step='.1';age.value='3';
    const fields=el('div');fields.append(label('安定確認時間（秒）',stable),label('測定の最大間隔（秒）',age));fields.hidden=true;
    kind.onchange=()=>{fields.hidden=kind.value!=='locked';controllers();};job.onchange=controllers;
    const remove=el('button','この条件を削除');remove.type='button';remove.onclick=()=>{row.remove();controllers();};
    row.append(label('前提ジョブ',job),label('必要な結果',kind),fields,remove);$('#reservation-prerequisites').append(row);controllers();
  }
  function cell(row,...values){const td=el('td');for(const value of values)td.append(typeof value==='string'?el('p',value):value);row.append(td);}
  function show(){
    const body=$('#reservation-list');body.replaceChildren();
    for(const r of state.reservations){
      const row=el('tr');cell(row,new Date(r.created_utc).toLocaleString(),names[r.status]||r.status);
      cell(row,`${r.target.set_id} / ${r.target.deck_id} / ${r.target.card_id}`,el('strong',r.reason));
      const detail=el('details');detail.append(el('summary','条件・設定・結果の詳細'),el('pre',JSON.stringify(r,null,2)));
      cell(row,r.prerequisites.map(p=>`${p.job_id} → ${p.condition==='locked'?'ロック確認':Object.keys(p.predicates||{}).length?'成功＋結果条件':'終了・復旧方針'}`).join('\n'),r.message||'',`実行先ジョブ: ${r.output_job_id}`,detail);
      const actions=el('div');if(['pending','waiting_agent'].includes(r.status)){const cancel=el('button','予約取消');cancel.type='button';cancel.onclick=()=>action(async()=>{await api(`reservations/${r.id}/cancel`,'POST',{});await refresh();});actions.append(cancel);}
      cell(row,r.status==='waiting_agent'?'復旧確認まで期限停止中':new Date(r.expires_unix_s*1000).toLocaleString(),actions);body.append(row);
    }
    if(!state.reservations.length){const row=el('tr'),td=el('td','予約はありません。');td.colSpan=4;row.append(td);body.append(row);}
  }
  async function refresh(){
    const [jobs,reservations]=await Promise.all([api('jobs'),api('reservations')]);state.jobs=jobs.jobs;state.reservations=reservations.reservations;show();
    for(const select of document.querySelectorAll('[data-job]'))options(select,candidates(),select.value);controllers();
  }
  $('#reservation-set').onchange=()=>decks();$('#reservation-deck').onchange=()=>cards();$('#reservation-card').onchange=parameters;
  $('#reservation-add').onclick=addCondition;$('#reservation-refresh').onclick=()=>action(refresh);
  $('#reservation-form').onsubmit=e=>{e.preventDefault();action(async()=>{
    const prerequisites=[...document.querySelectorAll('.reservation-condition')].map(row=>({job_id:row.querySelector('[data-job]').value,condition:row.querySelector('[data-kind]').value,stable_s:Number(row.querySelector('[data-stable]').value),max_age_s:Number(row.querySelector('[data-age]').value)}));
    const target={set_id:bundle().id,deck_id:deck().id,card_id:card().id,expected_revision:bundle().revision,parameters:JSON.parse($('#reservation-parameters').value)};
    if(online.has(card().operation))target.controller_job_id=$('#reservation-controller').value;
    $('#reservation-submit').disabled=true;
    try{const r=await api('reservations','POST',{request_id:state.requestId,reason:$('#reservation-reason').value,timeout_s:Number($('#reservation-timeout').value),target,prerequisites});state.requestId=newId();notice('予約しました。実行先ジョブID: '+r.output_job_id);await refresh();}
    finally{$('#reservation-submit').disabled=false;}
  });};
  action(async()=>{const sets=await api('sets');state.bundles=await Promise.all(sets.sets.filter(s=>!s.error).map(s=>api('sets/'+s.id)));const q=new URLSearchParams(location.search);options($('#reservation-set'),state.bundles.map(b=>[b.id,b.document.name]),q.get('set'));decks(q.get('deck'),q.get('card'));await refresh();addCondition();setInterval(()=>action(refresh),3000);});
})();
