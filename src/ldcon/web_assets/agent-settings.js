'use strict';
(() => {
  const $ = selector => document.querySelector(selector);
  const base = location.pathname.replace(/\/agent-settings\/?$/, '/api/');
  let profiles = null;
  let notifications = null;
  let routing = null;
  let providers = [];
  let modelOptions = {};

  function node(tag, text = '') {
    const result = document.createElement(tag);
    result.textContent = text;
    return result;
  }
  function status(text, failed = false) {
    $('#status').textContent = text;
    $('#status').classList.toggle('error', failed);
  }
  async function api(path, data) {
    const response = await fetch(base + path, {
      method: data ? 'PUT' : 'GET',
      headers: {'Content-Type': 'application/json', 'X-LDCon-Client': 'web'},
      body: data ? JSON.stringify(data) : undefined
    });
    const result = await response.json();
    if (!response.ok) throw Error(typeof result.detail === 'string' ? result.detail : 'Settings request failed (' + response.status + ').');
    return result;
  }
  async function action(button, task) {
    button.disabled = true;
    try { await task(); } catch (error) { status(error.message, true); }
    finally { button.disabled = false; }
  }
  function option(value, label) {
    const result = node('option', label);
    result.value = value;
    return result;
  }
  function input(value, label, type = 'text', bounds = {}) {
    const result = document.createElement('input');
    result.type = type;
    result.value = value;
    result.setAttribute('aria-label', label);
    Object.assign(result, bounds);
    return result;
  }
  function objectJSON(id, label) {
    let result;
    try { result = JSON.parse($(id).value); }
    catch (_) { throw Error(label + ': enter valid JSON.'); }
    if (!result || Array.isArray(result) || typeof result !== 'object') throw Error(label + ': expected a JSON object.');
    return result;
  }
  function refreshDefaultOptions() {
    const select = $('#resident-profile');
    const previous = select.value;
    select.replaceChildren(option('', 'Use existing card selection'));
    const names = [...$('#profile-rows').querySelectorAll('tr[data-profile]')].map(row => row.querySelector('[data-field="id"]').value.trim()).filter(Boolean);
    for (const name of names) select.append(option(name, name));
    if (previous && !names.includes(previous)) select.append(option(previous, previous + ' (missing profile; choose another)'));
    select.value = previous;
  }
  function profileRow(name, profile) {
    const row = node('tr');
    row.dataset.profile = name;
    const fields = [
      ['id', input(name, 'Profile ID', 'text', {required: true})],
      ['provider', node('select')],
      ['model', node('select')],
      ['effort', node('select')],
      ['timeout_s', input(profile.timeout_s ?? 600, 'Task timeout in seconds', 'number', {min: '0.001', max: '86400', step: 'any', required: true})],
      ['max_turns', input(profile.max_turns ?? 8, 'Maximum turns', 'number', {min: '1', max: '100', step: '1', required: true})],
      ['max_input_bytes', input(profile.max_input_bytes ?? 32000, 'Maximum input bytes', 'number', {min: '1', max: '262144', step: '1', required: true})]
    ];
    const provider = fields[1][1];
    provider.setAttribute('aria-label', 'Provider');
    for (const item of providers) provider.append(option(item.id, item.label || item.id));
    provider.value = profile.provider || providers[0]?.id || '';
    const model=fields[2][1], effort=fields[3][1];
    model.required=true;model.setAttribute('aria-label','Exact model');effort.setAttribute('aria-label','Effort');
    function loadEfforts(saved='') {
      const supported=(modelOptions[provider.value]||[]).find(m=>m.model===model.value);
      effort.replaceChildren(option('','Provider default'));
      for(const value of supported?.efforts||[])effort.append(option(value,value));
      if(saved&&![...effort.options].some(o=>o.value===saved))effort.append(option(saved,saved+' (saved; not verified)'));
      effort.value=saved;
    }
    function loadModels(saved='',savedEffort='') {
      model.replaceChildren(option('','Select model'));
      for(const value of modelOptions[provider.value]||[])model.append(option(value.model,value.model));
      if(saved&&![...model.options].some(o=>o.value===saved))model.append(option(saved,saved+' (saved; not verified)'));
      model.value=saved;loadEfforts(savedEffort);
    }
    provider.addEventListener('change',()=>loadModels());model.addEventListener('change',()=>loadEfforts());
    loadModels(profile.model||'',profile.effort||'');
    for (const [key, element] of fields) {
      element.dataset.field = key;
      const cell = node('td');
      cell.append(element);
      row.append(cell);
    }
    fields[0][1].addEventListener('change', refreshDefaultOptions);
    const requirements = node('td');
    for (const [key, label, value] of [
      ['required_tools', 'Required tools (comma separated)', profile.required_tools || []],
      ['required_modalities', 'Required modalities (text, image)', profile.required_modalities || ['text']]
    ]) {
      const element = input(value.join(', '), label);
      element.dataset.field = key;
      const field = node('label', key === 'required_tools' ? 'Tools' : 'Modalities');
      field.append(element);
      requirements.append(field);
    }
    row.append(requirements);
    const cell = node('td');
    const remove = node('button', 'Remove');
    remove.type = 'button';
    remove.className = 'danger';
    remove.addEventListener('click', () => { row.remove(); refreshDefaultOptions(); });
    cell.append(remove);
    row.append(cell);
    return row;
  }
  function renderProfiles(value) {
    profiles = value;
    providers = value.providers || providers;
    modelOptions = value.model_options?.providers || modelOptions;
    $('#profile-revision').textContent = 'Revision: ' + (value.revision || 'not saved');
    $('#profile-activation').textContent = value.activation || 'New sessions/tasks only; current runs pin their profile.';
    const catalog = $('#providers');
    catalog.replaceChildren();
    for (const provider of providers) {
      const item = node('div');
      item.className = 'provider-item';
      item.append(node('strong', provider.label || provider.id),
        node('span', provider.available ? 'Executable found by this service' : 'Executable not found by this service'),
        node('span', 'Authentication: not checked'), node('span', 'Model / inference access: not checked'));
      catalog.append(item);
    }
    $('#profile-rows').replaceChildren(...Object.entries(value.document.profiles).map(([name, profile]) => profileRow(name, profile)));
    $('#resident-profile').replaceChildren(option('', 'Use existing card selection'));
    refreshDefaultOptions();
    $('#resident-profile').value = value.document.resident_profile;
    $('#profile-lasers').value = JSON.stringify(value.document.lasers, null, 2);
    $('#save-profiles').disabled = false;
  }
  function profileDocument() {
    const result = structuredClone(profiles.document);
    result.profiles = Object.create(null);
    for (const row of $('#profile-rows').querySelectorAll('tr[data-profile]')) {
      const fields = Object.fromEntries([...row.querySelectorAll('[data-field]')].map(element => [element.dataset.field, element.value.trim()]));
      if (!fields.id || Object.hasOwn(result.profiles, fields.id)) throw Error('Every profile needs a unique ID.');
      result.profiles[fields.id] = {
        provider: fields.provider, model: fields.model, effort: fields.effort,
        timeout_s: Number(fields.timeout_s), max_turns: Number(fields.max_turns), max_input_bytes: Number(fields.max_input_bytes),
        required_tools: fields.required_tools.split(',').map(value => value.trim()).filter(Boolean),
        required_modalities: fields.required_modalities.split(',').map(value => value.trim()).filter(Boolean)
      };
    }
    result.resident_profile = $('#resident-profile').value;
    result.lasers = objectJSON('#profile-lasers', 'Per-laser resident defaults');
    return result;
  }
  function effectiveRow(scope, effective, inherited, source) {
    const row = node('tr');
    for (const text of [scope, effective.preset, effective.progress_interval_s + ' s', inherited || '—', source]) row.append(node('td', String(text)));
    return row;
  }
  function renderNotifications(value) {
    notifications = value;
    $('#notification-revision').textContent = 'Revision: ' + value.revision;
    $('#notification-preset').value = value.document.global_policy.preset;
    $('#progress-interval').value = value.document.global_policy.progress_interval_s;
    $('#notification-lasers').value = JSON.stringify(value.document.lasers, null, 2);
    $('#notification-tasks').value = JSON.stringify(value.document.tasks, null, 2);
    $('#notification-activation').textContent = value.activation;
    const rows = [effectiveRow('Global default', value.resolved.global_policy, '', 'global')];
    for (const [laser, resolved] of Object.entries(value.resolved.lasers)) rows.push(effectiveRow('Laser: ' + laser, resolved.effective, value.resolved.global_policy.preset, resolved.source));
    for (const [task, resolved] of Object.entries(value.resolved.tasks)) {
      let lifetime = resolved.task_override_expired ? (resolved.completed ? 'completed; override expired' : 'deadline passed; override expired') : 'active override';
      if (resolved.expires_unix_s) lifetime += '; deadline ' + new Date(resolved.expires_unix_s * 1000).toISOString();
      rows.push(effectiveRow('Task: ' + task + ' / ' + resolved.laser_id, resolved.effective, resolved.inherited.preset, resolved.source + ' · ' + lifetime));
    }
    $('#effective-rows').replaceChildren(...rows);
    $('#save-notifications').disabled = false;
  }
  function renderRouting(value) {
    routing=value;
    const levels=['basic','standard','deep'];
    for(const id of ['default-difficulty','gateway-difficulty']) {
      const select=$('#'+id);select.replaceChildren(...levels.map(level=>option(level,level[0].toUpperCase()+level.slice(1))));
    }
    $('#default-difficulty').value=value.document.default_difficulty;
    $('#gateway-difficulty').value=value.document.gateway.difficulty;
    $('#gateway-provider').replaceChildren();$('#routing-rows').replaceChildren();
    const available=profiles?.document.profiles||{};
    for(const provider of new Set(Object.values(available).map(p=>p.provider))) {
      const label=providers.find(p=>p.id===provider)?.label||provider;
      $('#gateway-provider').append(option(provider,label));
      const row=node('tr');row.dataset.routingProvider=provider;row.append(node('th',label));
      for(const level of levels){
        const cell=node('td'),select=node('select');select.dataset.routingLevel=level;select.setAttribute('aria-label',label+' '+level+' profile');
        select.append(option('','Not configured'));
        for(const [name,p] of Object.entries(available))if(p.provider===provider)select.append(option(name,`${name}: ${p.model} / ${p.effort||'provider default'}`));
        select.value=value.document.provider_profiles[provider]?.[level]||'';cell.append(select);row.append(cell);
      }
      $('#routing-rows').append(row);
    }
    $('#gateway-provider').value=value.document.gateway.provider;
    $('#routing-lasers').replaceChildren();
    for(const [name,selection] of Object.entries(value.document.lasers)){
      const row=node('tr');for(const text of [name,selection.provider,selection.difficulty])row.append(node('td',text));$('#routing-lasers').append(row);
    }
    $('#routing-revision').textContent='Revision: '+(value.revision||'not saved');$('#save-routing').disabled=!profiles;
  }
  $('#routing-form').addEventListener('submit',event=>{
    event.preventDefault();action($('#save-routing'),async()=>{
      if(!routing||!profiles)throw Error('Load profiles and routing before saving.');
      const document=structuredClone(routing.document);document.default_difficulty=$('#default-difficulty').value;
      document.gateway={provider:$('#gateway-provider').value,difficulty:$('#gateway-difficulty').value};document.provider_profiles={};
      for(const row of $('#routing-rows').children){
        const levels=Object.fromEntries([...row.querySelectorAll('select')].map(s=>[s.dataset.routingLevel,s.value]));
        if(Object.values(levels).every(v=>!v))continue;
        if(Object.values(levels).some(v=>!v))throw Error('Choose all three difficulties for '+row.dataset.routingProvider+'.');
        document.provider_profiles[row.dataset.routingProvider]=levels;
      }
      const saved=await api('agent-routing',{document,expected_revision:routing.revision});renderRouting(saved);
      status('Difficulty routing saved. Profile IDs have no meaning beyond their explicit table entries. Active sessions and tasks are unchanged.');
    });
  });
  async function loadAll() {
    const values = await Promise.allSettled([api('agent-profiles'), api('notification-policy'), api('agent-routing')]);
    const errors = [];
    if (values[0].status === 'fulfilled') renderProfiles(values[0].value); else errors.push('Profiles: ' + values[0].reason.message);
    if (values[1].status === 'fulfilled') renderNotifications(values[1].value); else errors.push('Notifications: ' + values[1].reason.message);
    if (values[2].status === 'fulfilled') renderRouting(values[2].value); else errors.push('Routing: '+values[2].reason.message);
    if (errors.length) throw Error(errors.join(' '));
    status('Saved settings loaded. Provider authentication has not been checked.');
  }
  $('#reload').addEventListener('click', () => action($('#reload'), loadAll));
  $('#add-profile').addEventListener('click', () => {
    if (!profiles) return status('Load the profile registry before adding a profile.', true);
    $('#profile-rows').append(profileRow('', {provider: providers[0]?.id || '', model: ''}));
    refreshDefaultOptions();
  });
  $('#profile-form').addEventListener('submit', event => {
    event.preventDefault();
    action($('#save-profiles'), async () => {
      if (!profiles) throw Error('Profile registry has not loaded.');
      const saved = await api('agent-profiles', {document: profileDocument(), expected_revision: profiles.revision});
      renderProfiles(saved);
      if(routing)renderRouting(routing);
      status('Execution profiles saved. New sessions and tasks use the new revision; active runs retain their selected profile.');
    });
  });
  $('#notification-form').addEventListener('submit', event => {
    event.preventDefault();
    action($('#save-notifications'), async () => {
      if (!notifications) throw Error('Notification policy has not loaded.');
      const document = structuredClone(notifications.document);
      document.global_policy = {preset: $('#notification-preset').value, progress_interval_s: Number($('#progress-interval').value)};
      document.lasers = objectJSON('#notification-lasers', 'Per-laser notification overrides');
      document.tasks = objectJSON('#notification-tasks', 'Temporary task notification overrides');
      renderNotifications(await api('notification-policy', {document, expected_revision: notifications.revision}));
      status('Notification policy saved for future events. No lock restart or historical outbox replay was requested.');
    });
  });
  action($('#reload'), loadAll);
})();
