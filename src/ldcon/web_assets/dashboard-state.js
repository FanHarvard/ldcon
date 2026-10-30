'use strict';
// Display only. No control admission, thresholds, device requests or retries.
// Date.now() belongs to the viewing computer, not the execution machine. Anchor
// each immutable snapshot to its server observation time and age it monotonically.
// Count the full request duration conservatively; delayed replies never look new.
const ldconDisplayClocks = new WeakMap();
window.ldconObserveDisplaySnapshot = function(snapshot, requestStarted) {
  if (!Number.isFinite(snapshot.read_at_unix_s)) return;
  const received = performance.now();
  const transit = Number.isFinite(requestStarted) ? Math.max(0, received-requestStarted) : 0;
  for (const job of snapshot.jobs || []) ldconDisplayClocks.set(job, {
    server: snapshot.read_at_unix_s*1000+transit, received,
  });
};
window.ldconDisplayState = function(jobs, now) {
  const operations = new Set(['servo.ecdl_steady_lock', 'servo.native_current', 'servo.current_pid', 'servo.slow_pid']);
  const controls = jobs.filter(job => ['running', 'stopping', 'finishing'].includes(job.status) && job.cards?.some(card => operations.has(card.operation || card.card)));
  if (controls.length !== 1) return {state: 'UNKNOWN', reason: '有効な制御記録が一意に確認できません。'};
  const job = controls[0], row = job.latest || {}, clock = ldconDisplayClocks.get(job);
  if (now === undefined) now = clock ? clock.server+Math.max(0, performance.now()-clock.received) : Date.now();
  const age = (now - Date.parse(job.updated_utc)) / 1000;
  if (!Number.isFinite(age) || age < 0 || age > 25) return {state: 'UNKNOWN', reason: '制御記録が古いか、時刻が不明です。', job};
  const stamp = row.recorded_unix_s ?? row.source_acquired_unix_s;
  const gap = job.simulated_s - row.time_s;
  const measured = stamp != null ? Number.isFinite(stamp) && now/1000-stamp >= 0 && now/1000-stamp <= 25 : Number.isFinite(job.simulated_s) && Number.isFinite(row.time_s) && gap >= 0 && gap <= 2;
  if (job.controller_state === 'waiting_for_measurement') return {state: 'WAITING FOR MEASUREMENT', reason: '測定の復帰待ちです。', job};
  if (!measured) return {state: 'UNKNOWN', reason: '新しい測定記録がありません。', job};
  if (row.output_enabled === false) return {state: 'OFF', reason: '新しい出力無効の読戻しを確認しました。', job};
  if (row.native_pi_active === false) return {state: 'HELD', reason: 'PIは停止。光出力OFFとは限りません。', job};
  if (job.controller_state === 'mode_degraded' || row.single_mode === false) return {state: 'DEGRADED', reason: '制御器が光学品質の低下を報告しています。', job};
  if (row.locked === true) return {state: 'LOCKED', reason: '新しい制御標本が設定されたロック条件を満たしています。', job};
  // Progress and readings are published separately. A fresh out-of-tolerance
  // trace with PI active is TRACKING even if progress has just advanced to locked.
  // Match dashboard_state.py without asserting a lock or changing any control.
  if (job.controller_state === 'locked' && row.locked === false && row.native_pi_active === true)
    return {state: 'TRACKING', reason: 'PI is active; latest sample is outside the configured lock criteria', job};
  const states = {tracking: 'TRACKING', acquiring: 'ACQUIRING', recovering: 'RECOVERING', confirming_mode_loss: 'MODE LOST'};
  return {state: states[job.controller_state] || 'UNKNOWN', reason: '制御器の最新記録。', job};
};
