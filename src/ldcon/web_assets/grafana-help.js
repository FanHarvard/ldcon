'use strict';
const grafanaHelp={
ja:{
 title:'カードの報告量からGrafana JSONを生成',
 operation:'選んだデッキの各カードが宣言する物理量を集め、Grafana用JSONを生成します。ECDLではPZT・フリンジ・ディザー、DFB/DBRでは測定電流・電流変調DAC・推定電流などが候補になります。ロックと並行実行でき、機器接続や出力変更は行いません。',
 selectionTitle:'カードと表示量の選択',
 selection:'未指定ならデッキの全報告量を集めます。カードIDと量IDで絞り込めます。同じ量は重複表示せず、提供元カードをmanifestに残します。明示した選択が利用できなければ理由付きエラーになり、自動収集で取得先が未対応の量は利用不可として記録します。空のリストは意図的に何も選ばない指定です。',
 referenceTitle:'実行時の基準値',
 reference:'基準波長または周波数を明示すれば優先します。未指定なら指定した稼働カードの目標値、次に割当の目標値を使います。確定した真空波長を編集可能なline_nmへ保存します。表示用許容差も同じ目標のスナップショットまたは明示値から取得し、装置固有の許容差や赤・緑の周波数閾値を共通値として埋め込みません。目標がなければ理由付きエラーになります。',
 layoutTitle:'配置と物理量の意味',
 layout:'選んだ量に応じた要約値と、制御器があれば状態タイムラインを表示し、詳しい時系列は4列に並べます。DAC count、測定電圧、測定電流、推定電流を区別します。TEC入力電圧は測定温度ではありません。機器の座標・単位はレーザー設定から読み、レーザー名や種類の固定判定でパネルを選びません。',
 limits:'フリンジ占有率、側峰、ディザー応答は条件付きの指標であり、普遍的な単一モード証明ではありません。ディザー応答は有効なfitだけを表示し、欠測をゼロにしません。物理量の契約と保存先・クエリは別モジュールです。acquire.metricsのVictoriaMetrics設定も利用できますが、SQLiteだけに保存された量はこのPrometheusダッシュボードでは取得不可と明示します。生成だけではGrafanaへ公開・インポートしません。',
 filesTitle:'保存される成果物',
 files:'manifestには確定した基準値と許容差、提供元カード、選択量の意味・単位、実際のクエリ、取得できない量と理由を記録します。実行条件と生成イベントは通常のjobログへ残ります。'
},
'zh-CN':{
 title:'根据卡片报告量生成Grafana JSON',
 operation:'收集所选卡组中各卡片声明的物理量，生成Grafana JSON。ECDL卡片提供PZT、条纹和抖动量；DFB/DBR卡片提供实测电流、电流调制DAC和估算电流。可与锁定并行运行，不连接设备或更改输出。',
 selectionTitle:'选择卡片和显示量',
 selection:'不指定时收集卡组的所有报告量，也可按卡片ID和量ID筛选。共享量去重，并在manifest中保留来源卡片。显式选择不可用的量会报告原因；自动收集中不支持的数据源会记录为不可用。空列表表示不选择任何量。',
 referenceTitle:'运行时的参考值',
 reference:'优先使用明确指定的参考波长或频率，否则采用所选运行卡片的目标值，再采用分配目标值。确定的真空波长保存为可编辑的line_nm。显示容差来自同一目标快照或显式值，不硬编码设备专属容差或红绿阈值。没有目标值时显示错误。',
 layoutTitle:'布局和物理意义',
 layout:'摘要和控制器时间线随所选量生成，详细时序采用四列布局。DAC计数、实测电压、实测电流和估算电流分开。TEC输入电压不是实测温度。设备坐标及单位来自激光器配置，不根据固定名称或类型选择面板。',
 limits:'条纹填充率、副峰和抖动响应是有条件的代理指标，并非普适的单模证明。仅显示有效拟合，缺失数据不会变成零。物理量契约与存储查询属于不同模块。可使用acquire.metrics的VictoriaMetrics绑定；仅SQLite的数据会明确标注为此Prometheus仪表板不支持。生成JSON不会自动导入或发布到Grafana。',
 filesTitle:'保存的文件',
 files:'manifest记录参考值及容差、来源卡片、所选量的意义和单位、实际查询，以及不可用数据源及原因。运行参数与事件保存在常规job日志中。'
}};
let grafanaHelpLanguage='en';for(const value of navigator.languages||[navigator.language]){const lang=value.toLowerCase();if(lang.startsWith('ja')){grafanaHelpLanguage='ja';break;}if(lang==='zh'||/^zh-(cn|sg|hans)/.test(lang)){grafanaHelpLanguage='zh-CN';break;}if(lang.startsWith('en'))break;}
document.documentElement.lang=grafanaHelpLanguage;
if(grafanaHelp[grafanaHelpLanguage])for(const element of document.querySelectorAll('[data-i18n]'))element.textContent=grafanaHelp[grafanaHelpLanguage][element.dataset.i18n]||element.textContent;
