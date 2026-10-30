"""Standalone, escaped HTML reports from recorded evidence only."""
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import stat


ARTIFACTS = ('grafana-dashboard.json', 'grafana-manifest.json', 'mode-map.json',
             'recovery.json', 'recovery-plan.json', 'recovery-test.json', 'operating-point-trace.json',
             'measurement.json', 'window-center.json', 'operation-record.json')
LABELS = {'id': '実行ID', 'set_name': 'レーザー', 'set_id': '割当ID', 'deck_id': 'デッキ',
          'created_utc': '開始日時 (UTC)', 'updated_utc': '記録更新 (UTC)', 'status': '実行状態',
          'controller_state': '制御状態', 'message': 'メッセージ', 'frequency_hz': '周波数 (Hz)',
          'wavelength_nm': '波長 (nm)', 'outputs': '出力の記録', 'parameters': '実行パラメーター',
          'card_results': 'カードの結果', 'error': 'エラー', 'reason': '理由', 'cards': 'カード',
          'finished_utc': '終了日時 (UTC)', 'template_revision': '共通コンテキストの版'}


def _read(path, limit=262144, tail=False):
    """Bound memory and refuse symbolic links/nonregular files, including a race."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (not tail and info.st_size > limit):
            return None
        offset = max(0, info.st_size - limit) if tail else 0
        stream.seek(offset)
        data = stream.read(limit).decode('utf-8', errors='replace')
        return data.partition('\n')[2] if offset else data


def evidence(directory):
    directory = Path(directory)
    result = {'events': [], 'artifacts': [], 'files': {}}
    for name in ('summary.json', 'agent-status.json', 'invocation.json',
                 'agent-instructions.md', *ARTIFACTS):
        text = _read(directory / name)
        if text is None:
            continue
        if name in ARTIFACTS:
            result['artifacts'].append(name)
        if name.endswith('.json'):
            try:
                result['files'][name] = json.loads(text)
            except (ValueError, RecursionError):
                continue
        elif name == 'agent-instructions.md':
            result['files'][name] = text
    text = _read(directory / 'telemetry.events.jsonl', tail=True)
    for line in (text or '').splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                result['events'].append(event)
        except (ValueError, RecursionError):
            continue
    result['events'] = result['events'][-100:]
    return result


def render(job, recorded=None):
    recorded = recorded if recorded is not None else job.get('report_evidence', {})
    esc = lambda value: html.escape(str(value), quote=True)
    budget = [3000]

    def value(data, depth=0):
        budget[0] -= 1
        if budget[0] < 0 or depth > 10:
            return '<span class="muted">表示上限。元の記録を確認してください。</span>'
        if data is None:
            return '<span class="muted">未記録</span>'
        if isinstance(data, bool):
            return 'true' if data else 'false'
        if isinstance(data, dict):
            if not data:
                return '<span class="muted">記録なし</span>'
            rows = []
            for key, item in list(data.items())[:200]:
                rows.append('<tr><th scope="row">' + esc(LABELS.get(key, key)) + '</th><td>' + value(item, depth + 1) + '</td></tr>')
            return '<table>' + ''.join(rows) + '</table>'
        if isinstance(data, list):
            if not data:
                return '<span class="muted">記録なし</span>'
            return '<ol>' + ''.join('<li>' + value(item, depth + 1) + '</li>' for item in data[:200]) + '</ol>'
        return esc(str(data)[:32000]).replace('\n', '<br>')

    def section(title, data, collapsed=False):
        if data is None or data == {} or data == []:
            return ''
        if collapsed:
            return '<details><summary>' + esc(title) + '</summary>' + value(data) + '</details>'
        return '<section><h2>' + esc(title) + '</h2>' + value(data) + '</section>'

    summary = {key: job[key] for key in ('id', 'set_name', 'set_id', 'deck_id', 'status',
               'controller_state', 'created_utc', 'updated_utc', 'finished_utc', 'message') if key in job}
    body = section('実行の概要', summary)
    body += section('カードと結果', recorded.get('files', {}).get('summary.json') or job.get('summary') or job.get('cards'))
    body += section('最後に記録された測定値', job.get('latest'))
    body += section('エージェントの応答・接続', job.get('agent'))
    body += section('実行中の操作', job.get('online_tuning'))
    events = recorded.get('events', [])
    body += section('イベント履歴（末尾100件まで）', events) if events else '<section><h2>イベント履歴</h2><p>表示できるイベント記録はありません。</p></section>'
    for name in ARTIFACTS:
        data = recorded.get('files', {}).get(name)
        if data is not None:
            body += section('成果物: ' + name, data, collapsed=True)
        if name in recorded.get('artifacts', []) and not job.get('archived') and (name == 'operation-record.json' or job.get('status') in ('succeeded', 'completed', 'completed_with_failures', 'completed_unsettled', 'failed', 'cancelled', 'interrupted', 'inconclusive')):
            body += '<p><a href="artifacts/' + name + '">元データをダウンロード: ' + name + '</a></p>'
    body += section('実行時パラメーター', recorded.get('files', {}).get('invocation.json') or job.get('parameters'), collapsed=True)
    body += section('実際に渡した共通コンテキスト', recorded.get('files', {}).get('agent-instructions.md'), collapsed=True)
    body += section('記録の詳細', {k: v for k, v in job.items() if k not in ('report_evidence', 'latest', 'summary', 'agent', 'parameters')}, collapsed=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return '''<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>LDCon 実行レポート</title>
<style>body{font:16px/1.65 system-ui,sans-serif;color:#19313d;background:#f2f5f7;max-width:1080px;margin:0 auto;padding:2rem 1rem}h1{font-size:2rem}h2{font-size:1.2rem}section,details{background:white;border:1px solid #d9e3e7;border-radius:12px;padding:1.2rem;margin:1rem 0;overflow:auto}table{border-collapse:collapse;width:100%;font-size:.9rem}th,td{text-align:left;vertical-align:top;border-bottom:1px solid #e5eaed;padding:.6rem;overflow-wrap:anywhere}th{min-width:10rem;color:#405965}td table{font-size:inherit}li{margin:.5rem 0}summary{cursor:pointer;font-weight:650}.muted,small{color:#556b76}a{color:#006f78}p{overflow-wrap:anywhere}@media(max-width:600px){body{padding:.8rem}th{min-width:0}th,td{padding:.3rem}section,details{padding:.6rem}}</style></head><body>
<header><small>LDCon · RUN REPORT</small><h1>実行レポート</h1><p>''' + esc(job.get('set_name') or job.get('set_id', '')) + ' · ' + esc(job.get('id', '')) + '''</p>
<p class="muted">保存された実行記録の表示です。現在の実機状態とは区別してください。実行中はページを再読み込みすると記録を更新できます。</p></header>''' + body + '<footer>表示生成 (UTC): ' + esc(stamp) + ' · 大きな記録は表示件数・サイズを制限しています。</footer></body></html>'
