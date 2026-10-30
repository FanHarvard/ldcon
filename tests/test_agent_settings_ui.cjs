// Mock transport only. No model, webhook, service or instrument requests.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {JSDOM} = require('jsdom');
const assets = process.env.LDCON_TEST_ASSETS||path.join(__dirname, '../src/ldcon/web_assets');
const dom = new JSDOM(fs.readFileSync(path.join(assets, 'agent-settings.html'), 'utf8'), {
  url: 'http://localhost/s/test/agent-settings', runScripts: 'outside-only'
});
const w = dom.window, d = w.document, requests = [];
w.structuredClone = structuredClone;
let profileRevision = 'revision-1', notificationRevision = 2, conflict = false;
let profileDocument = {schema_version: 1, resident_profile: 'analysis', lasers: {}, profiles: {
  analysis: {provider: 'codex', model: 'exact-model', effort: 'high', timeout_s: 90, max_turns: 2,
    max_input_bytes: 16000, required_tools: ['laser_log_read'], required_modalities: ['text']}
}};
let routingDocument={schema_version:1,default_difficulty:'standard',provider_profiles:{codex:{basic:'analysis',standard:'analysis',deep:'analysis'}},gateway:{provider:'codex',difficulty:'basic'},lasers:{}};let routingRevision='routing-1';
let notificationDocument = {schema_version: 1, revision: 2, global_policy: {preset: 'standard', progress_interval_s: 300}, lasers: {}, tasks: {}};
function notificationView() {
  const global = notificationDocument.global_policy, lasers = {}, tasks = {};
  for (const [laser, override] of Object.entries(notificationDocument.lasers)) {
    lasers[laser] = {source: 'laser', effective: {...global, ...override}, inherited: global};
  }
  for (const [task, override] of Object.entries(notificationDocument.tasks)) {
    const inherited = lasers[override.laser_id]?.effective || global;
    const expired = !!override.completed || (override.expires_unix_s || Infinity) < Date.now() / 1000;
    tasks[task] = {...override, source: expired ? 'laser' : 'task', inherited,
      effective: expired ? inherited : {...inherited, ...override.policy}, task_override_expired: expired};
  }
  return {document: notificationDocument, revision: notificationRevision,
    resolved: {global_policy: global, lasers, tasks}, activation: 'Future events only; no restart or replay.'};
}
w.fetch = async (url, options = {}) => {
  const route = String(url).replace('/s/test/api/', '');
  requests.push({route, ...options});
  assert(['agent-profiles', 'agent-routing', 'notification-policy'].includes(route), 'No operation endpoint is available in this test');
  assert.equal(options.headers['X-LDCon-Client'], 'web');
  let result;
  if (options.method === 'PUT') {
    const payload = JSON.parse(options.body);
    if (conflict) return {ok: false, status: 409, json: async () => ({detail: 'Settings changed; reload before saving'})};
    if(route==='agent-routing'){assert.equal(payload.expected_revision,routingRevision);routingDocument=payload.document;routingRevision='routing-2';result={document:routingDocument,revision:routingRevision,configured:true};}
    else if (route === 'agent-profiles') {
      assert.equal(payload.expected_revision, profileRevision);
      profileDocument = payload.document;
      profileRevision = 'revision-2';
      result = {document: profileDocument, revision: profileRevision};
    } else {
      assert.equal(payload.expected_revision, notificationRevision);
      notificationDocument = payload.document;
      notificationDocument.revision = ++notificationRevision;
      result = notificationView();
    }
  } else if (route === 'agent-profiles') {
    result = {document: profileDocument, revision: profileRevision,
      model_options:{providers:{codex:[{model:'exact-model',efforts:['high']},{model:'new-exact-model',efforts:['high','xhigh']}]}},providers: [{id: 'codex', label: 'Codex <img src=x onerror=bad()>', available: true,
        authentication: 'not_checked', inference_access: 'not_checked'}], activation: 'New sessions/tasks only.'};
  } else if(route==='agent-routing')result={document:routingDocument,revision:routingRevision,configured:true};
  else result = notificationView();
  return {ok: true, json: async () => structuredClone(result)};
};
function submit(id) { d.querySelector(id).dispatchEvent(new w.Event('submit', {bubbles: true, cancelable: true})); }
async function wait(fn) {
  for (let count = 0; count < 100; count++) {
    if (fn()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw Error('Timed out: ' + d.querySelector('#status').textContent);
}
async function run() {
  w.eval(fs.readFileSync(path.join(assets, 'agent-settings.js'), 'utf8'));
  await wait(() => !d.querySelector('#save-profiles').disabled && !d.querySelector('#save-notifications').disabled);
  assert.equal(d.documentElement.lang, 'en');
  await wait(()=>!d.querySelector('#save-routing').disabled);
  assert.equal(d.querySelectorAll('#routing-rows select').length,3);
  assert([...d.querySelectorAll('#routing-rows select')].every(s=>s.value==='analysis'));
  d.querySelector('#default-difficulty').value='deep';submit('#routing-form');
  await wait(()=>routingRevision==='routing-2');assert.equal(routingDocument.default_difficulty,'deep');

  assert.equal(d.querySelectorAll('img').length, 0, 'Provider labels must remain text');
  assert(d.querySelector('#providers').textContent.includes('Authentication: not checked'));
  assert.equal(d.querySelector('#resident-profile').value, 'analysis');
  const row = d.querySelector('#profile-rows tr');
  assert.equal(row.querySelector('[data-field=model]').tagName,'SELECT');
  row.querySelector('[data-field=model]').value = 'new-exact-model';
  row.querySelector('[data-field=model]').dispatchEvent(new w.Event('change'));
  row.querySelector('[data-field=effort]').value = 'xhigh';
  row.querySelector('[data-field=timeout_s]').value = '120';
  d.querySelector('#profile-lasers').value = JSON.stringify({laser1: {resident_profile: 'analysis'}});
  submit('#profile-form');
  await wait(() => d.querySelector('#status').textContent.startsWith('Execution profiles saved'));
  assert.equal(profileDocument.profiles.analysis.model, 'new-exact-model');
  assert.equal(profileDocument.profiles.analysis.effort, 'xhigh');
  assert.equal(profileDocument.profiles.analysis.timeout_s, 120);
  assert.deepEqual(profileDocument.profiles.analysis.required_tools, ['laser_log_read']);
  assert.equal(profileDocument.lasers.laser1.resident_profile, 'analysis');
  assert(d.querySelector('#profile-revision').textContent.includes('revision-2'));
  d.querySelector('#notification-preset').value = 'investigation';
  d.querySelector('#progress-interval').value = '60';
  d.querySelector('#notification-lasers').value = JSON.stringify({laser1: {preset: 'quiet'}});
  d.querySelector('#notification-tasks').value = JSON.stringify({task1: {
    laser_id: 'laser1', policy: {preset: 'investigation'}, expires_unix_s: 1
  }});
  submit('#notification-form');
  await wait(() => d.querySelector('#status').textContent.startsWith('Notification policy saved'));
  assert.equal(notificationDocument.global_policy.preset, 'investigation');
  assert.equal(notificationDocument.global_policy.progress_interval_s, 60);
  assert(d.querySelector('#effective-rows').textContent.includes('deadline passed; override expired'));
  const prior = requests.length;
  d.querySelector('#notification-lasers').value = '{bad';
  submit('#notification-form');
  await wait(() => d.querySelector('#status').classList.contains('error'));
  assert.equal(requests.length, prior, 'Invalid local JSON never reaches the API');
  d.querySelector('#notification-lasers').value = '{}';
  conflict = true;
  d.querySelector('#notification-preset').value = 'quiet';
  submit('#notification-form');
  await wait(() => d.querySelector('#status').textContent.includes('reload before saving'));
  assert.equal(d.querySelector('#notification-preset').value, 'quiet', 'A conflict preserves unsaved edits');
  assert.equal(notificationDocument.global_policy.preset, 'investigation', 'A conflict cannot overwrite saved state');
  assert(requests.every(item => ['GET', 'PUT'].includes(item.method)));
  assert(requests.every(item => !/restart|stop|start|probe|login|teams|hardware/.test(item.route)));
  dom.window.close();
  process.stdout.write('Agent settings DOM: profile editing, notification overrides/expiry, CAS conflict, escaping, no operations passed\n');
}
run().catch(error => { process.stderr.write(error.stack + '\n'); dom.window.close(); process.exitCode = 1; });
