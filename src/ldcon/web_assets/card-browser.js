'use strict';
// Presentation only: grouping and search never change admission or card parameters.
(() => {
  const categories = [
    ['control', 'Lock and operating point', 'ロック・動作点'],
    ['measurement', 'Measurement and calibration', '測定・校正'],
    ['optimization', 'Optimization', '最適化'],
    ['recovery', 'Recovery', '復旧'],
    ['sweep', 'Sweeps', '掃引'],
    ['agent', 'Agents and monitoring', 'エージェント・監視'],
    ['report', 'Reports and notifications', 'レポート・通知'],
    ['other', 'Other cards', 'その他のカード']
  ];
  const labels = {
    'agent.resident': ['Gateway agent', 'ゲートウェイエージェント'],
    'servo.native_current': ['Current lock (controller PI)', '電流ロック（コントローラー内PI）'],
    'sweep.frequency_triangle': ['Triangle frequency sweep', '周波数三角掃引'],
    'sweep.frequency_staircase': ['Staircase frequency sweep', '周波数階段掃引'],
    'tune.pid_factor_reduction': ['PI gain reduction comparison', 'PIゲイン縮小比較'],
    'tune.pid_coordinate_search': ['PI optimization (P/I independently)', 'PI最適化（P・I独立）'],
    'measure.target_response': ['Target response measurement', '目標応答測定'],
    'optimize.settling_criteria': ['Settling criteria optimization', '整定判定の最適化'],
    'optimize.target_feedforward': ['Target feedforward optimization', '目標FFの最適化'],
    'optimize.target_thermal': ['TEC recentering optimization', 'TEC中心化の最適化'],
    'optimize.target_step': ['Target step-size optimization', '目標ステップ幅の最適化'],
    'tune.ecdl_window_center': ['Current window center search', '電流窓の中点探索'],
    'tune.ecdl_initial_point': ['Current branch mapping', '電流の枝マップ取得'],
    'tune.ecdl_operating_point': ['TEC branch alignment', 'TECによる枝合わせ'],
    'tune.lock_target': ['Change target', '目標変更'],
    'tune.operating_point': ['Change operating point', '動作点変更'],
    'servo.ecdl_steady_lock': ['ECDL stationary lock', 'ECDL定常ロック'],
    'servo.dlcpro_frequency': ['DLC Pro frequency lock', 'DLC Pro周波数ロック'],
    'recover.ecdl_mode_hop': ['Mode-hop recovery', 'モードホップ復旧'],
    'prepare.ecdl_recovery': ['Prepare recovery procedure', '復旧手順の作成'],
    'test.ecdl_recovery': ['Recovery qualification test', '復旧手順の実証試験'],
    'agent.monitor': ['Continuous monitoring', '定常監視'],
    'agent.task': ['One-shot agent adjustment', 'エージェント単発調整'],
    'agent.request': ['Request gateway agent', 'ゲートウェイへの依頼'],
    'agent.history_review': ['Historical data review', '過去データの分析'],
    'report.grafana_dashboard': ['Grafana dashboard export', 'Grafanaダッシュボード出力'],
    'notify.teams': ['Teams notification', 'Teams通知']
  };
  const familyLabels = {
    pi: ['PI optimization (agent)', 'PI最適化（エージェント）'],
    current: ['Current operating-point optimization', '電流動作点の最適化'],
    slow_loop: ['TEC / PZT-centering optimization', 'TEC・PZT中心化の最適化'],
    feedforward: ['TEC-current feedforward optimization', 'TEC・電流FFの最適化']
  };
  function category(operation = '') {
    if (operation.startsWith('recover.') || /^(prepare|test)\.ecdl_recovery$/.test(operation)) return 'recovery';
    if (operation.startsWith('sweep.') || operation === 'analyze.shot_sweep') return 'sweep';
    if (operation.startsWith('measure.') || operation.startsWith('identify.') || operation.startsWith('acquire.') || /(?:initial_point|mode_map|current_response|locked_current_map)$/.test(operation)) return 'measurement';
    if (operation.startsWith('optimize.') || operation.startsWith('tune.pid_') || operation === 'tune.ecdl_window_center' || operation === 'agent.task') return 'optimization';
    if (operation.startsWith('agent.') || operation.startsWith('monitor.')) return 'agent';
    if (operation.startsWith('report.') || operation.startsWith('notify.')) return 'report';
    if (operation.startsWith('servo.') || operation.startsWith('tune.') || operation.startsWith('laser.')) return 'control';
    return 'other';
  }
  function label(operation, parameters = {}, id = '') {
    if (operation === 'agent.resident' && parameters.role === 'operator') return 'Operator agent';
    if (operation === 'agent.task' && familyLabels[parameters.adjustment_family]) return familyLabels[parameters.adjustment_family][0];
    if (labels[operation]) return labels[operation][0];
    if (['measure.change', 'measure.batch'].includes(operation) && id) return id.replace(/[-_]+/g, ' ').replace(/\b(?:pi|pid|pzt|tec|ff|rms|dac)\b/gi, word => word.toUpperCase()).replace(/^./, first => first.toUpperCase());
    return null;
  }
  function normalize(text) {
    return String(text || '').normalize('NFKC').toLowerCase().replace(/[^\p{L}\p{N}]+/gu, ' ').trim();
  }
  function distance(a, b, budget) {
    if (Math.abs(a.length - b.length) > budget) return budget + 1;
    let previous = Array.from({length: b.length + 1}, (_, i) => i);
    for (let i = 1; i <= a.length; i++) {
      const next = [i];
      for (let j = 1; j <= b.length; j++) next[j] = Math.min(next[j - 1] + 1, previous[j] + 1, previous[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      previous = next;
    }
    return previous[b.length];
  }
  function fuzzyToken(token, word) {
    if (word.includes(token)) return true;
    if (token.length < 3 || !/^[a-z0-9]+$/.test(token)) return false;
    const budget = token.length >= 6 ? 2 : 1;
    if (distance(token, word, budget) <= budget) return true;
    if (word.length > token.length * 3 || word[0] !== token[0]) return false;
    let at = 0;
    for (const letter of word) if (letter === token[at]) at++;
    return at === token.length;
  }
  function matches(query, text) {
    const terms = normalize(query).split(' ').filter(Boolean), source = normalize(text);
    if (!terms.length) return true;
    const words = source.split(' ');
    return terms.every(term => source.includes(term) || words.some(word => fuzzyToken(term, word)));
  }
  function searchable(card, catalog = {}) {
    const operation = card.operation || card.id, group = categories.find(row => row[0] === category(operation));
    return [card.id, card.operation, card.name, card.title, card.description, catalog.description,
      catalog.physical_basis, ...(catalog.capabilities || []), ...(labels[operation] || []), ...(operation === 'agent.task' ? familyLabels[card.parameters?.adjustment_family] || [] : []),
      label(operation, card.parameters, card.id), ...group.slice(1)].filter(Boolean).join(' ');
  }
  function element(tag, text = '', className = '') {
    const value = document.createElement(tag); value.textContent = text; value.className = className; return value;
  }
  function mount(box, table, entries, scope) {
    const key = 'ldcon.card-browser.' + scope, groups = new Map();
    let saved = {};
    try { const value = JSON.parse(localStorage.getItem(key)); if (value && typeof value === 'object' && !Array.isArray(value)) saved = value; } catch (_) {}
    const persist = () => { try { localStorage.setItem(key, JSON.stringify(saved)); } catch (_) {} };
    const toolbar = element('div', '', 'card-browser-toolbar'), searchLabel = element('label', 'Search cards');
    const search = element('input'); search.type = 'search'; search.maxLength = 120; search.placeholder = 'Name, function or ID; partial names and typos work'; search.setAttribute('aria-label', 'Search cards'); search.dataset.cardSearch = '';
    searchLabel.append(search);
    const clear = element('button', 'Clear search'), expand = element('button', 'Expand categories'), collapse = element('button', 'Collapse categories');
    for (const button of [clear, expand, collapse]) button.type = 'button';
    clear.dataset.cardSearchClear = ''; expand.dataset.cardCategoriesExpand = ''; collapse.dataset.cardCategoriesCollapse = '';
    const count = element('output', '', 'card-browser-count'); count.dataset.cardSearchCount = ''; count.setAttribute('aria-live', 'polite');
    const hint = element('p', 'Search includes closed categories. Clearing it restores your category selection.', 'muted card-browser-hint');
    toolbar.append(searchLabel, clear, expand, collapse, count); box.prepend(toolbar, hint);
    const oldBody = table.querySelector('tbody');
    for (const [id, name] of categories) {
      const rows = entries.filter(entry => category(entry.card.operation) === id);
      if (!rows.length) continue;
      const body = element('tbody'), heading = element('tr', '', 'card-category-row'), cell = element('td'); cell.colSpan = 3;
      body.dataset.cardCategory = id;
      const toggle = element('button', '', 'card-category-toggle'), text = element('span', name), total = element('span', '', 'card-category-count'); toggle.type = 'button'; toggle.dataset.cardCategoryToggle = id;
      toggle.append(text, total); cell.append(toggle); heading.append(cell); body.append(heading);
      for (const entry of rows) body.append(entry.row, entry.detail);
      table.append(body); groups.set(id, {body, rows, toggle, total});
      toggle.addEventListener('click', () => {saved[id] = saved[id] === false; persist(); apply();});
    }
    oldBody?.remove();
    const empty = element('p', 'No matching cards.', 'muted'); empty.dataset.cardSearchEmpty = ''; empty.hidden = true; box.append(empty);
    function apply() {
      const query = search.value.trim(); let visible = 0;
      for (const [id, group] of groups) {
        let found = 0;
        const open = query ? true : saved[id] !== false;
        for (const entry of group.rows) {
          const match = matches(query, entry.search);
          if (match) found++;
          entry.row.hidden = !match || !open;
          entry.detail.classList.toggle('card-list-hidden', !match || !open);
        }
        group.body.hidden = Boolean(query) && !found;
        group.toggle.disabled = Boolean(query);
        group.toggle.setAttribute('aria-expanded', String(open));
        group.total.textContent = query ? `${found} / ${group.rows.length}` : String(group.rows.length);
        visible += found;
      }
      count.textContent = `${visible} / ${entries.length}`;
      empty.hidden = visible !== 0;
      clear.disabled = !query;
    }
    search.addEventListener('input', apply);
    clear.addEventListener('click', () => {search.value = ''; apply(); search.focus();});
    expand.addEventListener('click', () => {for (const id of groups.keys()) saved[id] = true; persist(); apply();});
    collapse.addEventListener('click', () => {for (const id of groups.keys()) saved[id] = false; persist(); apply();});
    apply(); return {search, apply};
  }
  window.ldconCardBrowser = {category, label, normalize, matches, searchable, mount, categories, labels, familyLabels};
})();
