'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const root = location.pathname.replace(/\/?$/, '/');
  const views = new Set(['lasers', 'laser-settings', 'decks', 'catalog', 'settings']);
  let lasers = [], selected = '', view = 'lasers', polling = false;
  let historyOffset = 0, historyVersion = 0, historyRefreshAt = 0;
  const expanded = new Set(), rows = new Map();
  const key = row => JSON.stringify([row.service_id, row.set_id]);
  function node(tag, text = '') { const value = document.createElement(tag); value.textContent = text; return value; }
  function status(row) { return row.state + (row.reachable ? '' : ' · 接続できません'); }
  function isBusy() { return false; }
  function updateSwitching() {
    for(const button of document.querySelectorAll('[data-service-id]'))button.disabled=button.dataset.reachable!=='true';
  }
  function serviceURL(row) {return root+'services/'+encodeURIComponent(row.service_id)+'/?set='+encodeURIComponent(row.set_id)+'#'+(view==='lasers'?'laser-settings':view);}
  function renderDetail() {
    const row=lasers.find(value=>key(value)===selected),area=$('#fleet-detail');
    $('#fleet-selected-status').textContent=row?row.name+' · '+status(row):'レーザーを選択してください。';
    let link=area.querySelector('a');
    if(!link){link=node('a','レーザー画面を開く →');link.className='workspace-open';area.append(link);}
    const enabled=Boolean(row?.reachable&&row.set_id);
    link.style.visibility=enabled?'visible':'hidden';link.tabIndex=enabled?0:-1;
    if(enabled)link.href=serviceURL(row);else link.removeAttribute('href');
  }
  function switchView(value) {
    view = views.has(value) ? value : 'lasers';
    $('#fleet-overview').hidden = view !== 'lasers';
    $('#fleet-history').hidden = view !== 'lasers';
    $('#fleet-details').hidden = view === 'lasers';
    if($('#fleet-defaults')) $('#fleet-defaults').hidden = view !== 'settings';
    for (const button of document.querySelectorAll('[data-fleet-view]')) button.setAttribute('aria-selected', String(button.dataset.fleetView === view));
    if (location.hash !== '#' + view) location.hash = view;
    renderDetail();
  }
  function render() {
    const list = $('#fleet-list'), select = $('#fleet-select');list.closest('table').classList.add('fleet-overview-table');
    const seen=new Set();for(const empty of list.querySelectorAll('[data-empty]'))empty.remove();
    for(const row of lasers){
      const id=key(row);seen.add(id);let v=rows.get(id);
      if(!v){
        const tr=node('tr'),name=node('td'),state=node('td'),wavelength=node('td'),reason=node('td'),actions=node('td');
        state.className='fleet-state';reason.className='fleet-reason';actions.className='actions';
        const detailRow=node('tr'),detailCell=node('td'),details=node('dl');detailRow.dataset.fleetDetail=id;detailCell.colSpan=5;
        const fields={};for(const [field,label] of [['reason','状態の根拠'],['service','サービス'],['laser','レーザーID'],['job','制御ジョブ'],['connection','接続'],['dither','Dither quality'],['response','Dither response / threshold'],['evidence','Dither evidence']]){
          const value=node('dd');fields[field]=value;details.append(node('dt',label),value);
        }
        detailCell.append(details);detailRow.append(detailCell);
        const toggle=node('button','状態の詳細');toggle.type='button';toggle.dataset.detailToggle=id;
        toggle.onclick=()=>{detailRow.hidden=!detailRow.hidden;if(detailRow.hidden)expanded.delete(id);else expanded.add(id);toggle.setAttribute('aria-expanded',String(!detailRow.hidden));};
        const choose=node('button','設定・詳細');choose.type='button';
        choose.onclick=()=>{const latest=rows.get(id)?.current;if(!latest?.reachable||!latest.set_id)return;selected=id;view='laser-settings';location.assign(serviceURL(latest));};
        actions.append(toggle);tr.append(name,state,wavelength,reason,actions);list.append(tr,detailRow);
        const option=node('option');option.value=id;select.append(option);
        v={tr,name,state,wavelength,reason,detailRow,fields,toggle,choose,option};rows.set(id,v);
      }
      v.current=row;let workspace=v.name.querySelector('a');if(!workspace){workspace=node('a');v.name.replaceChildren(workspace);}workspace.textContent=row.name;workspace.href=root+'services/'+encodeURIComponent(row.service_id)+'/?set='+encodeURIComponent(row.set_id)+'#laser-settings';v.state.textContent=status(row);v.state.dataset.code=row.code;
      v.reason.textContent=row.reason;v.reason.title=row.reason;
      v.choose.dataset.serviceId=row.service_id;v.choose.dataset.setId=row.set_id||'';
      v.choose.dataset.reachable=String(Boolean(row.set_id&&row.reachable));v.choose.disabled=!row.set_id||!row.reachable;
      v.detailRow.hidden=!expanded.has(id);v.toggle.setAttribute('aria-expanded',String(!v.detailRow.hidden));
      const d=row.dither_quality;
      for(const [field,value] of Object.entries({reason:row.reason,service:row.service_label||row.service_id,laser:row.set_id||'未取得',job:row.job_id||'確認できません',connection:row.reachable?'接続済み':'接続不明',dither:d?.state||'No current controller evidence',response:d?.value!=null?`${d.value.toPrecision(4)} / ${d.threshold?.toPrecision(4)??'—'} ${d.unit}`:'—',evidence:d?.reason||'No current controller measurement'}))v.fields[field].textContent=value;
      const wave=row.wavelength,fresh=wave?.valid===true&&Date.now()/1000-wave.acquired_unix_s>=0&&Date.now()/1000-wave.acquired_unix_s<=15;
      v.wavelength.textContent=fresh?wave.wavelength_nm.toFixed(9):'—';v.wavelength.title=fresh?`CH ${wave.channel} · ${new Date(wave.acquired_unix_s*1000).toLocaleString()}`:'No fresh wavelength observation';
      v.option.textContent=row.name+(row.reachable?'':' (接続不明)');
    }
    for(const [id,v] of rows)if(!seen.has(id)){v.tr.remove();v.detailRow.remove();v.option.remove();rows.delete(id);expanded.delete(id);}
    if(!lasers.length){const tr=node('tr'),td=node('td','登録されたレーザーはありません。');tr.dataset.empty='';td.colSpan=5;tr.append(td);list.append(tr);}
    if(!lasers.some(row=>key(row)===selected))selected=lasers.length?key(lasers[0]):'';
    select.value=selected;renderDetail();updateSwitching();
  }
  async function refresh() {
    if (polling) return; polling = true;
    try {
      const response = await fetch(root + 'api/fleet', {headers: {'X-LDCon-Client': 'web'}});
      if (!response.ok) throw Error('一覧の取得に失敗しました。');
      const value = await response.json(); lasers = value.lasers; render();
      $('#fleet-notice').hidden = true;
      $('#fleet-connection').textContent = '更新 · ' + new Date().toLocaleTimeString();
      if (view === 'lasers' && Date.now()-historyRefreshAt>30000) { historyRefreshAt=Date.now(); void history(); }
    } catch (error) {
      // A failed refresh invalidates previous status, not just the header.
      lasers = lasers.map(row => ({...row, reachable: false, wavelength: {valid:false}, code: 12, state: 'UNKNOWN', reason: '最新の状態を確認できません。'}));
      render(); $('#fleet-notice').textContent = error.message; $('#fleet-notice').hidden = false;
      $('#fleet-connection').textContent = '接続不明';
    } finally { polling = false; }
  }
  async function history() {
    const version = ++historyVersion, limit = Number($('#fleet-history-limit').value);
    try {
      const query = new URLSearchParams({q: $('#fleet-history-search').value, limit, offset: historyOffset});
      const response = await fetch(root + 'api/history?' + query, {headers: {'X-LDCon-Client': 'web'}});
      if (!response.ok) throw Error('履歴の取得に失敗しました。');
      const value = await response.json(); if (version !== historyVersion) return;
      const list = $('#fleet-history-list'); list.replaceChildren();
      for (const job of value.jobs) {
        const row = node('tr');
        row.append(node('td', job.created_utc ? new Date(job.created_utc).toLocaleString() : '—'),
          node('td', (job.set_name || job.set_id || job.service_label) + ' / ' + (job.deck_id || '—')),
          node('td', job.status), node('td', job.archived ? '圧縮済み' : 'ローカル'));
        const cell = node('td'), link = node('a', job.id.slice(0, 12) + ' ↗');
        link.href = root + 'services/' + encodeURIComponent(job.service_id) + '/api/jobs/' + encodeURIComponent(job.id) + '/report.pdf';
        link.target = '_blank'; link.rel = 'noopener'; cell.append(link); row.append(cell); list.append(row);
      }
      $('#fleet-history-count').textContent = value.total + ' 件 / ' + (value.total ? historyOffset + 1 : 0) + '–' + Math.min(value.total, historyOffset + limit);
      $('#fleet-history-prev').disabled = historyOffset === 0;
      $('#fleet-history-next').disabled = historyOffset + limit >= value.total || historyOffset + limit > 1000;
      const notices = [];
      if(value.unavailable_services.length) notices.push('一部サービスの履歴を取得できません。表示件数は取得できた分だけです。');
      if(historyOffset + limit > 1000 && historyOffset + limit < value.total) notices.push('表示できるページ範囲に達しました。検索で絞ってください。');
      $('#fleet-history-notice').textContent = notices.join(' ');
    } catch (error) {
      if (version === historyVersion) $('#fleet-history-notice').textContent = error.message;
    }
  }
  $('#fleet-select').onchange = () => { if(isBusy()){ $('#fleet-select').value=selected; return; } selected = $('#fleet-select').value; renderDetail(); };
  function expandAll(open) { for(const row of document.querySelectorAll('[data-fleet-detail]')) { row.hidden=!open; if(open)expanded.add(row.dataset.fleetDetail);else expanded.delete(row.dataset.fleetDetail); } for(const button of document.querySelectorAll('[data-detail-toggle]'))button.setAttribute('aria-expanded',String(open)); }
  $('#fleet-expand').onclick = () => expandAll(true);
  $('#fleet-collapse').onclick = () => expandAll(false);
  $('#fleet-refresh').onclick = refresh;
  let debounce;
  $('#fleet-history-search').oninput = () => { historyOffset = 0; ++historyVersion; clearTimeout(debounce); debounce = setTimeout(history, 200); };
  $('#fleet-history-limit').onchange = () => { historyOffset = 0; history(); };
  $('#fleet-history-prev').onclick = () => { historyOffset = Math.max(0, historyOffset - Number($('#fleet-history-limit').value)); history(); };
  $('#fleet-history-next').onclick = () => { historyOffset += Number($('#fleet-history-limit').value); history(); };
  for (const button of document.querySelectorAll('[data-fleet-view]')) button.onclick = () => switchView(button.dataset.fleetView);
  window.addEventListener('hashchange', () => switchView(location.hash.slice(1)));
  switchView(location.hash.slice(1)); refresh(); setInterval(()=>{if(!document.hidden)refresh();},5000);
})();
