'use strict';
(() => {
  const dictionary = {
    '接続中':'Connecting', '更新':'Refresh', 'レーザー一覧':'Lasers', '各レーザーの設定':'Laser settings', 'デッキ一覧':'Decks', 'カタログ':'Catalog', '他設定':'Settings', '全レーザー':'All lasers',
    'すべて展開':'Expand all', 'すべて折り畳む':'Collapse all', 'レーザー':'Laser', '状態':'State', '根拠':'Evidence', '操作':'Actions', '設定・詳細':'Settings / details', '状態の詳細':'State details', '状態の根拠':'State evidence', 'サービス':'Service', 'レーザーID':'Laser ID', '制御ジョブ':'Controller job', '接続':'Connection', '接続済み':'Connected', '接続不明':'Connection unknown', '確認できません':'Unverified', '未取得':'Unavailable',
    '全レーザーの実行ログ':'Run history — all lasers', '検索':'Search', '件数':'Rows', '表示件数':'Rows', '前へ':'Previous', '次へ':'Next', '開始日時':'Started', 'レーザー / デッキ':'Laser / deck', '保管':'Storage', 'レポート':'Report', 'HTMLレポート':'HTML report', '圧縮済み':'Archived', 'ローカル':'Local', '対象レーザー':'Selected laser', 'レーザーを選択してください。':'Select a laser.', '登録されたレーザーはありません。':'No lasers are registered.',
    'レーザー・カード・状態・日時・ID':'Laser, card, state, date or ID', '機能・名前・必要インターフェース':'Function, name or required interface', 'カード検索':'Search cards', 'カード':'Card', '概要':'Description', '必要機能':'Required capabilities', '説明':'Help', 'デッキ':'Deck', 'カード数':'Cards', '割当':'Assignments', '詳細・設定':'Details / settings', '実行カード':'Running card', '設定':'Settings', '停止':'Stop', '開始':'Start', '詳細':'Details', '待機':'Ready', '要設定':'Needs configuration', '結果で予約':'Schedule from result', '失敗時の通知 ↗':'Failure notifications ↗', 'カードをすべて展開':'Expand all cards', '使える条件・測定内容 ↗':'Requirements / measurements ↗', '次回の実行パラメーター':'Parameters for the next run', '今回の実行パラメーター':'Parameters for this run',
    '実行ログ':'Run history', 'リテンション設定 →':'Retention settings →', '結果による予約実行':'Result-based scheduling', '運用ガイド ↗':'Operations guide ↗', '全レーザー共通の既定値':'Defaults for all lasers', '実行ログの保管':'Run log retention', '期限後の圧縮移動を有効にする':'Archive expired logs', '保持日数':'Retention (days)', 'バックアップフォルダ（サーバー上の絶対パス）':'Backup folder (absolute server path)', '共通の既定値を保存':'Save shared defaults', '再読み込み':'Reload', 'レーザー別の上書き':'Per-laser overrides', '全レーザー共通の既定値を使う':'Inherit defaults for all lasers', '全レーザー共通の既定値を使用中':'Using shared defaults', 'このレーザーの個別設定を使用中':'Using this laser’s override', '共通の既定値を読み込みました。':'Shared defaults loaded.', '共通の既定値を保存しました。共通設定を継承するレーザーに適用されます。個別上書きは維持します。':'Shared defaults saved. Inheriting lasers use these settings; individual overrides are preserved.',
    '個別上書きがないレーザーと、今後追加するレーザーの既定値です。安全条件とカードの実行パラメーターは共有しません。':'Defaults for lasers without an override, including future lasers. Device safety conditions and card execution parameters remain laser-specific.',
    '対象レーザーを選び、「全レーザー共通の既定値を使う」を外して保存すると個別設定になります。既存の個別設定は自動では変更しません。':'Select a laser, clear “Inherit defaults for all lasers”, and save to create an override. Existing individual settings are preserved.',
    '安全パラメーター・機器設定':'Safety parameters / devices', '機器 / 軸':'Device / axis', '絶対範囲':'Absolute range', '1回の変化上限':'Maximum step', '速度上限':'Maximum slew', '補足':'Notes', '未設定':'Not configured', '保持条件':'Hold condition', '機器構成・単位・制御設定の詳細':'Device configuration, units and control details', 'レーザー設定・安全パラメーターを編集':'Edit laser settings / safety parameters', '高度な編集（YAML全文）':'Advanced editing (full YAML)', '項目':'Parameter', '現在値':'Current value', '変更値':'New value', '単位':'Unit', '変更前':'Before', '変更後':'After', '変更差分を確認':'Review changes', '承認した差分を検証して保存':'Validate and save approved changes', '上記の差分を確認し、この設定変更を承認します。':'I reviewed the changes above and approve this configuration change.', '変更はありません。':'No changes.', '差分を確認して承認してください。':'Review and approve the changes first.',
    '安全範囲・較正・制御上限の変更は、人間が具体的な差分を確認してください。保存時に既存の安全検証・稼働中編集制限・版の競合検査を行います。保存だけでは機器を操作しません。':'A human must review exact changes to safety ranges, calibration and control limits. Saving retains safety validation, active-use restrictions and revision checks. Saving does not operate devices.',
    '電流ロック（コントローラー内PI）':'Current lock (controller PI)', 'ESP32サーボコントローラー内のPIで電流を調整して周波数を保ちます。nativeはこの制御方式を指し、ロック状態ではありません。':'The ESP32 controller runs the PI loop and adjusts current to maintain frequency. “Native” identifies this control method, not the current lock state.',
    '有効な制御記録が一意に確認できません。':'No unique active controller record is available.', '制御記録が古いか、時刻が不明です。':'The controller record is stale or its timestamp is unavailable.', '測定の復帰待ちです。':'Waiting for measurement recovery.', '新しい測定記録がありません。':'No fresh measurement is available.', '新しい出力無効の読戻しを確認しました。':'Fresh readback confirms output disabled.', 'PIは停止。光出力OFFとは限りません。':'PI is stopped; optical emission is not known to be off.', '制御器が光学品質の低下を報告しています。':'The controller reports degraded optical quality.', '新しい制御標本が設定されたロック条件を満たしています。':'A fresh sample satisfies the configured lock criteria.', '制御器の最新記録。':'Latest controller record.', '最新の状態を確認できません。':'The current state could not be verified.',
    '状態は新しい制御記録から表示します。未測定・通信不明はUNKNOWNです。PIの停止やジョブ終了は光出力OFFを意味しません。':'State is based on fresh controller evidence. UNKNOWN means unmeasured or unavailable. A stopped PI or completed job does not mean optical output is off.',
    'LOCKEDは新しい制御記録、DEGRADEDは品質低下、HELDはPI停止を示します。出力の無効化を確認したときだけOFFと表示します。未測定・通信不明はUNKNOWNです。':'LOCKED uses fresh controller evidence, DEGRADED indicates reduced quality, and HELD indicates PI off. OFF requires output-disabled readback. UNKNOWN means unmeasured or unavailable.',
    'カードの実行条件':'Card requirements', '定義の検証済み':'Definition validated', '設定を保存':'Save settings', '期限超過ログを今確認':'Check expired logs now', 'FanLabAdmin が書き込める既存フォルダを指定してください。':'Choose an existing folder writable by FanLabAdmin.',
    '通知はデッキのnotify.teams、エージェントはagent.residentカードで設定します。各カードのfallbackで参照してください。':'Configure notifications with notify.teams and agents with agent.resident cards. Reference them from each card’s fallback.',
    '終了済みログを毎時確認します。保存期限を過ぎたログは圧縮・検証後にバックアップへ移し、検索用の索引とHTMLレポートは残します。実行中のログは移動しません。':'Completed logs are checked hourly. Expired logs are compressed, verified and moved to backup; searchable indexes and HTML reports remain. Running job logs are retained.',
    'Web画面と機器ownerは独立しています。画面の更新でロックは停止しません。機器ownerの停止・再起動は制御に影響します。':'The Web UI and device owners are independent. UI updates do not stop locks. Stopping or restarting an owner affects control.',
    '割当・レーザー固有の既定パラメーター':'Assignment / laser-specific defaults', '共通デッキの設定を編集':'Edit shared deck', '共通デッキに機器アドレスやレーザー固有の制限を保存しません。':'Device addresses and laser-specific limits belong to laser configuration, not shared decks.', '公開する測定値':'Reported measurements', '詳細なHTML説明 ↗':'Detailed HTML help ↗', 'HTML説明 ↗':'HTML help ↗', '実行中のパラメーター':'Live parameters', '定期ログ確認':'Periodic log review', 'エージェントの接続・依頼結果':'Agent connection / request results',
    'レーザー・機器・デッキ・割当を追加':'Add a laser, device, deck or assignment', '定義の種類':'Definition type', 'レーザー・機器':'Laser / devices', '共通デッキ':'Shared deck', 'レーザーとデッキの割当':'Laser / deck assignment', '定義ID':'Definition ID', '新規テンプレートを作る':'Create a template', '既存の定義を開く':'Open an existing definition', '検証して保存':'Validate and save', '新規作成。既存のIDは上書きしません。':'New definition. Existing IDs are not overwritten.',
    'この定義を使うカードの稼働中は保存できません。無関係な定義は編集できます。保存だけではownerへ反映しません。安全条件の変更は対象のカード・所有者リースが空であることを確認して反映してください。':'Definitions in use by an active card cannot be saved. Unrelated definitions may be edited. Saving does not update owners. Apply safety changes only after checking the affected card and owner leases are idle.',
    'LDCon · レーザーごとの独立した制御 · 共通の入口':'LDCon · Independent laser control · One dashboard', 'LDCon · 共通カード / レーザー別の安全条件 / 実行時スナップショット':'LDCon · Shared cards / laser-specific safety / execution snapshots'
  };
  dictionary['目標変更を中止']='Cancel target change';
  dictionary['残りの目標ステップを中止します。ロックと反映済みの目標は維持します。']='Cancel remaining target steps; retain the lock and applied target.';
  dictionary['PIは稼働中。最新の測定は設定されたロック条件の範囲外です。']='PI is active; latest sample is outside the configured lock criteria';
  dictionary['共通設定への接続が未設定です。サービス既定値を使用中']='Shared defaults are not connected; using service defaults.';
  dictionary['承認・根拠の参照（承認者・日時・理由）']='Approval reference (who, when and why)';
  Object.assign(dictionary, {
  "レーザーを操作": "Laser workspace",
  "目標波長・動作点": "Target and operating point",
  "目標波長": "Target wavelength",
  "動作点の調整": "Operating point adjustment",
  "パラメーター": "Parameters",
  "機器構成": "Device configuration",
  "エージェント・特性": "Agent / laser characteristics",
  "操作カード": "Operation cards",
  "エージェント・レーザーの特性": "Agent and laser characteristics",
  "このレーザーの実行ログ": "Run history for this laser",
  "保存された目標": "Saved target",
  "最新の測定": "Latest measurement",
  "ロックの実行あり": "Lock job active",
  "ロックの実行なし": "No active lock job",
  "保存値": "Saved value",
  "機器・安全条件・較正": "Devices, safety and calibration",
  "カードのレーザー別設定（復旧・探索・エージェントなど）": "Laser-specific card settings (recovery, search, agents)",
  "パラメーター検索": "Search parameters",
  "名前・パス・値（例: current_excursion、offsets）": "Name, path or value (e.g. current_excursion, offsets)",
  "定義全体の編集（YAML）": "Edit complete definitions (YAML)",
  "特性の記載欄を追加": "Add laser characteristics template",
  "今回の実行パラメーター（JSON）": "Parameters for this run (JSON)",
  "実行レポートを読む ↗": "Read run report ↗",
  "実行レポート ↗": "Run report ↗",
  "レーザー画面を開く →": "Open laser workspace →",
  "← 全レーザー": "← All lasers",
  "接続するエージェント": "Agent provider",
  "機能": "Function",
  "制御担当": "Controller / owner",
  "機器・チャンネル": "Devices / channels",
  "機器・チャンネル（設定値）": "Devices / channels (configured)",
  "接続先・役割の一覧": "Connections and roles",
  "設定上の役割": "Configured role",
  "割当なし": "Unassigned",
  "電流": "Current",
  "電流 AWG": "Current AWG",
  "波長計": "Wavemeter",
  "光学観測": "Optical observation",
  "定常ロック": "Stationary lock",
  "電流ロック": "Current lock",
  "ループ状態": "Loop state",
  "変調": "Modulation",
  "読込中": "Loading",
  "保存された設定です。現在の実機への反映状態は動作点・実行ログで確認してください。": "Saved configuration. Check operating-point evidence and run reports for the applied state.",
  "保存された構成から自動生成しています。接続の成功や実配線の確認結果ではありません。レーザー方式・個体特性は共通コンテキストに記録します。": "Generated from saved configuration; this does not verify connections or wiring. Document laser architecture and individual characteristics in the shared context.",
  "全エージェントで共有する知識と、カードごとの個別依頼を分けて保存します。": "Shared laser knowledge and individual card requests are stored separately.",
  "開始は1回の実行です。今回のパラメーターと保存済みの既定値を区別してください。": "Start launches one run. Parameters for this run are separate from saved defaults.",
  "このレーザーのエージェント指示": "Shared instructions for this laser",
  "Markdownテンプレートを読み込む": "Load Markdown template",
  "Markdownを保存": "Save Markdown",
  "レーザー別の接続履歴": "Saved laser sessions",
  "接続履歴を更新": "Refresh sessions",
  "クリック・矢印キー1回につき1操作です。押し続けても反復しません。機器の制限・インターロックは継続します。": "One action per click or arrow-key press. Hold-to-repeat is disabled. Existing owner limits and interlocks remain in force.",
  "目標の絶対値": "Absolute target",
  "入力単位": "Input unit",
  "波長 (nm)": "Wavelength (nm)",
  "周波数 (THz)": "Frequency (THz)",
  "目標を反映": "Apply target",
  "微調整幅": "Nudge step",
  "微調整の単位": "Step unit",
  "周波数 (MHz)": "Frequency (MHz)",
  "波長 (pm)": "Wavelength (pm)",
  "波長を増やすと周波数は下がります。矢印キーは＋／−の操作欄にフォーカスがあるときだけ有効です。": "Positive wavelength steps reduce frequency. Arrow keys act only while the +/− control has focus.",
  "調整軸": "Knob",
  "出力の記録": "Current output",
  "調整対象・モード": "Editable value / mode",
  "新しい値": "New value",
  "変化幅": "Step",
  "反映": "Apply",
  "電流変調": "Current modulation",
  "未取得": "Unavailable",
  "ロック中のPZT調整はTEC外側ループの中心目標を変更します。TECの直接変更はそのループの稼働中はできません。新しい読戻しがない値は未取得と表示し、操作時はカードがownerを再確認します。": "During lock, PZT changes the centre sought by the TEC outer loop, not the servo DAC output. Direct TEC changes are disabled while that loop controls it. Without a fresh readback, values are shown as unavailable; the card reads the owner again before moving.",
  "ループの中心目標": "Loop centre target",
  "ロックの基準値": "Lock reference",
  "保持出力": "Held output",
  "ロック中は読取りのみ／未対応": "Read only while locked / not supported",
  "前の要求の結果が未確認です。実行ログを確認してから続けてください。": "A previous request has an unconfirmed outcome. Check job history before further changes.",
  "実行履歴の確認後にブラウザーの保留表示を解除": "Clear local pending marker after reviewing job history",
  "ブラウザーの保留表示だけ解除しました。命令は送信していません。": "Local marker cleared. No command was sent."
});
  Object.assign(dictionary, {
  "必要なインターフェースとパラメーターの検証済み。開始時に再検証します。": "Required interfaces and parameters validated. They are checked again at Start.",
  "ここでの変更は現在のジョブだけに適用します。次回の実行設定とは別です。変更できる項目と適用条件は実行中カードから読み込みます。": "Changes here apply only to the current job, separately from the next run. Editable fields and conditions are loaded from the running card.",
  "実行中の値・適用結果を更新": "Refresh live values and application results",
  "選択項目を実行中カードへ一括適用": "Apply selected fields to the running card",
  "変更する項目を選び、安全な処理境界で一括適用します。": "Select fields to apply together at a supported execution boundary.",
  "このカードは実行中の変更に対応していません。": "This card does not support parameter changes while running.",
  "適用待ち。適用結果を更新して確認してください。": "Pending. Refresh to check the application result.",
  "入力の検証待ち": "Waiting for input validation",
  "入力の検証済み": "Input validated",
  "変更": "Change",
  "項目": "Field",
  "現在値": "Current value",
  "変更後": "New value",
  "適用条件": "Application conditions",
  "数値・true/false・文字列・配列はJSONで入力してください。関連項目はまとめて送信します。": "Enter numbers, true/false, strings and arrays as JSON. Coupled fields are submitted together.",
  "適用待ち": "Pending",
  "適用済み": "Applied",
  "拒否": "Rejected",
  "適用結果が不明": "Application outcome unknown",
  "対象カードは実行中ではありません。": "This card is not running.",
  "実行中のジョブが変わりました。もう一度読み込んでください。": "The running job changed. Reload its values.",
  "対象ジョブが変わりました。現在の値を読み直してください。": "The selected job changed. Reload its current values.",
  "変更する項目を選んでください。": "Select fields to change.",
  "次回の実行パラメーター": "Parameters for the next run",
  "測定・計画の成果物": "Measurement and planning results",
  "完了した実行の成果物を表示": "Show results from completed runs",
  "完了した成果物はありません。": "No completed results are available.",
  "スタディの実行": "Study execution",
  "実行方式": "Execution mode",
  "常駐エージェント": "Resident agent",
  "測定とプロット": "Measure and plot",
  "測定・プロットと整定判定の最適化": "Measure, plot and optimize settling criteria",
  "両モードとも全取得データとプロットを保存します。エージェントは予想整定時間・閾値・確認条件を提案します。PI・FF・保存済みの既定値は変更しません。": "Both modes keep full-rate data and plots. Agent mode proposes expected settling time, thresholds and confirmation settings. PI, FF and saved defaults are not changed.",
  "選択した常駐エージェントが記録された応答を解析します。": "The selected resident will analyse the recorded response.",
  "先に選択した常駐エージェントを起動するか、測定とプロットを選んでください。": "Start the selected resident before this study, or choose Measure and plot.",
  "エージェントは不要です。": "No agent is required.",
  "過去のスタディ結果を表示": "Show previous study results",
  "応答プロット ↗": "Response plot ↗",
  "元データJSON ↗": "Raw data JSON ↗",
  "提案された設定を次回の実行に使用": "Use proposed settings for next run",
  "この結果には検証済みのエージェント候補がありません。": "No validated agent candidate is available in this result.",
  "候補を今回の実行用JSONにコピーしました。開始前に確認してください。保存済みの既定値は変わりません。": "Candidate copied into this run draft. Review the JSON before Start; saved defaults are unchanged.",
  "完了したスタディ結果はありません。": "No completed study results are available."
});
  Object.assign(dictionary, {
    '日常の設定':'Routine settings',
    '目標・制御・光学品質':'Target, control and optical quality',
    '詳細設定 — 制限・較正・機器定義':'Advanced configuration — limits, calibration and device definitions',
    '制限・較正・出力許可':'Limits, calibration and output permissions',
    '機器定義・接続・測定元':'Device definitions, connections and measurement sources',
    'YAMLファイル':'YAML file', 'レーザーのYAML':'Laser YAML', '割当のYAML':'Assignment YAML',
    '保存先の情報なし':'Source path unavailable',
    '調整出力':'Control outputs', '測定・読み戻し':'Measurements and readbacks',
    '量・用途':'Quantity / purpose', '機器・担当':'Device / owner', '接続先':'Connection',
    'チャンネル・入力':'Channel / input', '方法・解釈':'Method / interpretation',
    'PDFレポート':'PDF report', 'PDFレポート ↗':'PDF report ↗', 'PDFレポートを読む ↗':'Read PDF report ↗',
    '応答のPDF ↗':'Response PDF ↗',
    '設定された調整・測定経路です。チャンネルと方法は保存設定から表示しており、実配線の確認結果ではありません。不明な項目は未指定と表示します。':'Configured control and measurement routes. Channels and methods come from the saved definition; this is not a live wiring test. Missing details are marked as unspecified.',
    '機器・較正・承認範囲を変更するときに開いてください。分類によって編集権限や検証条件は変わりません。':'Open these settings when changing hardware, calibration or approved limits. Grouping does not change edit permissions or validation.'
  });
  Object.assign(dictionary, {
    'ゲートウェイエージェント':'Gateway agent', '操作エージェント':'Operator agent',
    'ゲートウェイは依頼の受付・タスクカードの起動・結果の返答を担当します。監視や実機調整は別のタスクで実行します。':'Gateway agent receives requests, dispatches task cards and relays their results. Monitoring and physical adjustments run as separate tasks.',
    '監視は継続動作です。起動時に単発評価を行い、その後は周期や設定条件に応じて再評価します。調整タスクが終了しても監視は継続します。':'Continuous monitoring. Runs an initial one-shot assessment, then another when a schedule or configured condition is due. A completed adjustment does not stop monitoring.'
  });
  Object.assign(dictionary, {
    'レーザーごとのカード既定値':'Card defaults for this laser',
    '失敗履歴・エージェントからの質問 ↗':'Failure history and agent questions ↗',
    '矢印で送信':'Send on arrows', '編集した値をまとめて送信':'Send edited values',
    '未送信の編集を戻す':'Revert unsent edits',
    '出力の記録':'Current output', '取得日時':'Recorded time',
    'ゲートウェイエージェント':'Gateway agent', 'レーザー操作エージェント':'Laser operator agent'
  });
  Object.assign(dictionary, {
    '小数点以下の桁数':'Decimals', '現在のモードの理由':'Current reason',
    '変更日時':'Changed at', '記録なし':'Not recorded'
  });
  Object.assign(dictionary, {
  "ロック・動作点": "Lock and operating point",
  "測定・校正": "Measurement and calibration",
  "最適化": "Optimization",
  "復旧": "Recovery",
  "掃引": "Sweeps",
  "エージェント・監視": "Agents and monitoring",
  "レポート・通知": "Reports and notifications",
  "その他のカード": "Other cards",
  "名前・機能・ID（部分一致・あいまい検索）": "Name, function or ID; partial names and typos work",
  "検索を解除": "Clear search",
  "カテゴリをすべて展開": "Expand categories",
  "カテゴリをすべて折り畳む": "Collapse categories",
  "検索は閉じたカテゴリも含みます。解除すると元の開閉状態に戻ります。": "Search includes closed categories. Clearing it restores your category selection.",
  "一致するカードがありません。": "No matching cards.",
  "PIゲイン縮小比較": "PI gain reduction comparison",
  "PI最適化（P・I独立）": "PI optimization (P/I independently)",
  "目標応答測定": "Target response measurement",
  "整定判定の最適化": "Settling criteria optimization",
  "目標FFの最適化": "Target feedforward optimization",
  "TEC中心化の最適化": "TEC recentering optimization",
  "目標ステップ幅の最適化": "Target step-size optimization",
  "電流窓の中点探索": "Current window center search",
  "電流の枝マップ取得": "Current branch mapping",
  "TECによる枝合わせ": "TEC branch alignment",
  "目標変更": "Change target",
  "動作点変更": "Change operating point",
  "ECDL定常ロック": "ECDL stationary lock",
  "DLC Pro周波数ロック": "DLC Pro frequency lock",
  "モードホップ復旧": "Mode-hop recovery",
  "復旧手順の作成": "Prepare recovery procedure",
  "復旧手順の実証試験": "Recovery qualification test",
  "定常監視": "Continuous monitoring",
  "エージェント単発調整": "One-shot agent adjustment",
  "ゲートウェイへの依頼": "Request gateway agent",
  "過去データの分析": "Historical data review",
  "Grafanaダッシュボード出力": "Grafana dashboard export",
  "Teams通知": "Teams notification"
});
  Object.assign(dictionary, {"PI最適化（エージェント）": "PI optimization (agent)", "電流動作点の最適化": "Current operating-point optimization", "TEC・PZT中心化の最適化": "TEC / PZT-centering optimization", "TEC・電流FFの最適化": "TEC-current feedforward optimization"});
  Object.assign(dictionary, {'周波数三角掃引':'Triangle frequency sweep','周波数階段掃引':'Staircase frequency sweep'});
  Object.assign(dictionary, {
    '開始不可':'Blocked', '制御スロット使用中':'Control slot in use',
    '開始するには、実行中の制御カードを停止してください。':'Stop the running control card before starting this card.',
    '接続を確認できません':'Connection unavailable', '再接続してからカードを開始してください。':'Reconnect before starting a card.',
    'パラメータの検証が必要です':'Parameters need validation', '今回の実行パラメータを確認してください。':'Check the parameters for this run.',
    '実行パラメータを検証中です。':'Checking run parameters.', '制御器が利用できないか使用中です':'Controller unavailable or busy',
    '稼働中ロックが1件あり、競合する操作がないことが必要です。':'Requires one running lock and no conflicting operation.',
    '別の制御操作が実行中です。':'Another control operation is running.', '稼働中ロックが必要です':'Running lock required',
    '対応するロックを先に開始してください。':'Start the compatible lock before this card.',
    '調整がすでに実行中です':'Adjustment already running', 'このカードはすでに実行中です':'This card is already running',
    'マップ JSON ↗':'Map JSON ↗', '復旧 JSON ↗':'Recovery JSON ↗'
  });
  const reverse=Object.fromEntries(Object.entries(dictionary).map(([ja,en])=>[en,ja]));
  Object.assign(reverse, {'Fresh controller sample satisfies its configured lock criteria':'新しい制御標本が設定されたロック条件を満たしています。','No unique active controller; physical output is unknown':'有効な制御記録が一意に確認できません。光出力は未確認です。','No fresh controller measurement':'新しい測定記録がありません。','Controller record is stale':'制御記録が古くなっています。','Fresh controller state':'制御器の最新記録。','Fresh data does not establish lock or output-off':'最新情報だけではロック成立・光出力OFFを確認できません。'});
  for(const [en,ja] of Object.entries(reverse))if(!dictionary[ja])dictionary[ja]=en;
  let language='ja'; try {language=(localStorage.getItem('ldcon.language')||'ja').startsWith('ja')?'ja':'en';}catch(_){}
  const records=new WeakMap();
  function translate(text){
    const source=reverse[text]||text;
    if(language==='ja')return reverse[text]||(text.includes(' · ')?text.split(' · ').map(part=>reverse[part]||part).join(' · '):text);
    if(dictionary[source])return dictionary[source];
    if(source.includes(' · '))return source.split(' · ').map(part=>dictionary[part]||part).join(' · ');
    const patterns=[[/^(\d+) 件$/,'$1 rows'],[/^現在の版: (.*)$/,'Current revision: $1'],[/^一括変更: (.*)$/,'Coupled change: $1'],[/^上限 (.*)$/,'Maximum $1'],[/^更新: (.*)$/,'Updated: $1'],[/^更新 · (.*)$/,'Updated · $1'],[/^接続済み · (.*)$/,'Connected · $1'],[/^(.*) 件 \/ (.*)$/,'$1 rows / $2'],[/^出力許可: 有効$/,'Output permission: enabled'],[/^出力許可: 無効$/,'Output permission: disabled'],[/^運用データ: (.*)$/,'Operational data: $1']];
    for(const [pattern,replacement] of patterns)if(pattern.test(source))return source.replace(pattern,replacement);
    return source;
  }
  function updateText(text){
    if(text.parentElement?.closest('script,style,pre,code,textarea, [data-no-translate]'))return;
    const current=text.nodeValue, record=records.get(text), source=record&&current===record.rendered?record.source:current;
    const trimmed=source.trim(), rendered=source.replace(trimmed,translate(trimmed));
    records.set(text,{source,rendered});if(current!==rendered)text.nodeValue=rendered;
  }
  function apply(root){
    if(root.nodeType===3){updateText(root);return;}
    if(!root.querySelectorAll)return;
    const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);while(walker.nextNode())updateText(walker.currentNode);
    for(const element of root.querySelectorAll('[placeholder]')){const initial=element.dataset.originalPlaceholder||element.getAttribute('placeholder');element.dataset.originalPlaceholder=initial;element.setAttribute('placeholder',translate(initial));}
  }
  function set(value,persist=true){
    language=value==='ja'?'ja':'en';document.documentElement.lang=language;
    if(persist)try{localStorage.setItem('ldcon.language',language);}catch(_){}
    apply(document.body);for(const button of document.querySelectorAll('[data-language]'))button.setAttribute('aria-pressed',String(button.dataset.language===language));
  }
  window.ldconTranslate=translate;
  for(const button of document.querySelectorAll('[data-language]'))button.onclick=()=>set(button.dataset.language);
  new MutationObserver(events=>{for(const event of events){if(event.type==='characterData')updateText(event.target);else for(const child of event.addedNodes)apply(child);}}).observe(document.body,{childList:true,characterData:true,subtree:true});
  window.addEventListener('storage',event=>{if(event.key==='ldcon.language')set(event.newValue,false);});
  set(language,false);
})();
