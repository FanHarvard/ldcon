"""Offline staircase FF evidence report: ROOT/roothelper and LuaLaTeX only.

Plot raw target errors, never the fitted gain/offset residual. The report reads
only bounded local artifacts and cannot execute a card or adopt a coefficient.
"""
from array import array
from bisect import bisect_right
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil

from ldcon.agents.report_pdf import build, initialize
from ldcon.jobs.axis_frequency_report import tex


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _number(value):
    return f'{value:.6g}' if _finite(value) else 'unavailable'


def read_analysis(directory, relative):
    """Read a regular artifact inside this job; reject traversal and symlinks."""
    if not isinstance(relative, str) or not relative:
        raise ValueError('Missing local analysis artifact')
    relative = Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Analysis artifact must be local to this job')
    base = Path(directory).resolve(strict=True)
    path = base
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('Analysis artifact may not contain a symlink')
    path.resolve(strict=True).relative_to(base)
    if not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError('Expected a bounded regular analysis artifact')
    document = json.loads(path.read_text())
    if not isinstance(document, dict):
        raise ValueError('Analysis artifact must be an object')
    return document


def waveform(document):
    """Evaluate raw samples against hard command times, including response lag.

    Source level indices and programmed_offset_hz may incorporate fitted
    optical response delay/width. They must not shorten the plotted response.
    Lead/tail ramps and samples outside the explicit level record are omitted.
    """
    points = document.get('points') or {}
    times, waves = points.get('t_unix_s'), points.get('wavelength_nm')
    if not isinstance(times, list) or not isinstance(waves, list) or len(times) != len(waves):
        raise ValueError('Raw time/wavelength arrays are missing or inconsistent')
    if any(not _finite(t) for t in times) or any(b <= a for a,b in zip(times, times[1:])):
        raise ValueError('Acquisition timestamps are not strictly increasing')
    reference = (document.get('parameters') or {}).get('reference') or {}
    target = reference.get('lock_target_hz')
    if not _finite(target) or target <= 0:
        raise ValueError('Canonical target is unavailable')
    levels = document.get('levels')
    if not isinstance(levels, list) or not levels:
        raise ValueError('Hard command boundaries are unavailable')
    for i, level in enumerate(levels):
        if (not isinstance(level, dict) or any(not _finite(level.get(k)) for k in
                ('t_start_unix_s','t_end_unix_s','offset_hz'))
                or level['t_start_unix_s'] >= level['t_end_unix_s']
                or (i and level['t_start_unix_s'] < levels[i-1]['t_end_unix_s'])):
            raise ValueError('Hard command boundaries are invalid')
    starts = [level['t_start_unix_s'] for level in levels]
    valid = []
    for stamp, wave in zip(times, waves):
        index = bisect_right(starts, stamp)-1
        if index < 0 or stamp >= levels[index]['t_end_unix_s'] or not _finite(wave) or wave <= 0:
            continue
        valid.append((stamp, 299792458e9/wave-target-levels[index]['offset_hz']))
    return dict(points=valid, input_samples=len(times), excluded_samples=len(times)-len(valid))


def evidence(directory, result):
    """A report-specific allowlist: never copy arbitrary configuration/secrets."""
    records = []
    for index, trial in enumerate(result.get('trials', [])):
        condition = trial.get('condition') or {}
        candidate = trial.get('candidate') or {}
        evaluation = trial.get('evaluation') or {}
        levels = condition.get('levels_hz') or []
        levels = [v for v in levels if _finite(v)] if isinstance(levels, list) else []
        dwell = condition.get('dwell_s')
        repetitions = condition.get('repetitions')
        # Conditions carry the full exact sequence; do not infer a cycle by
        # equating the number of unique levels with the number of visits.
        cycle = condition.get('cycle_period_s')
        record = dict(index=index, id=str(trial.get('id', index)), role=str(trial.get('role', 'trial')),
            candidate={k:candidate[k] for k in ('id','ldc_ff_v_per_hz','pzt_ff_gain_scale','ff_gain_scale') if k in candidate},
            condition={k:condition[k] for k in ('id','dwell_s','edge_s','lead_s','tail_s','precondition_s',
                'cycle_period_s','repetitions','direction') if k in condition},
            tested_span_hz=max(levels)-min(levels) if levels else None,
            steps=len(levels), qualified=evaluation.get('qualified') is True,
            reasons=[str(v) for v in evaluation.get('reasons', [])],
            metrics={k:evaluation.get(k) for k in ('worst_settling_s','rms_error_hz','max_late_drift_hz_per_s')},
            frequency_continuity=(evaluation.get('frequency_continuity') or {}).get('status'),
            optical_quality=(evaluation.get('optical_quality') or {}).get('status'),
            source=None, time_base=None, completeness=None, waveform=None)
        if not _finite(cycle) and _finite(dwell) and type(repetitions) is int and repetitions > 0:
            cycle = len(levels)*dwell/repetitions
        record['cycle_period_s'] = cycle if _finite(cycle) and cycle > 0 else None
        record['cycle_frequency_hz'] = 1/cycle if _finite(cycle) and cycle > 0 else None
        try:
            document = read_analysis(directory, trial.get('analysis_file'))
            record.update(waveform=waveform(document), source=document.get('source'),
                time_base=document.get('time_base'), completeness=(document.get('completeness') or {}).get('complete'))
        except (OSError, ValueError, TypeError, KeyError):
            record['artifact_status'] = 'unavailable or invalid; no waveform was substituted'
        records.append(record)
    return records


def _root():
    import ROOT
    prefixes = [Path(os.environ['ROOTHELPER_PREFIX'])] if os.environ.get('ROOTHELPER_PREFIX') else []
    prefixes += [Path.home()/'opt/roothelper', Path.home()/'.local', Path('/home/FanLabAdmin/opt/roothelper')]
    prefix = next((p for p in prefixes if (p/'include/roothelper/roothelper.h').exists()), None)
    if prefix is None:
        raise RuntimeError('ROOT/roothelper required; no alternate plotting backend')
    ROOT.gROOT.SetBatch(True)
    ROOT.gInterpreter.AddIncludePath(str(prefix/'include'))
    if not ROOT.gInterpreter.Declare('#include <roothelper/roothelper.h>'):
        raise RuntimeError('roothelper unavailable')
    ROOT.roothelper.Prepare()
    return ROOT


def _figure(ROOT, saver, output, record, tolerance):
    points = (record.get('waveform') or {}).get('points') or []
    if not points:
        return ''
    name = 'sweep_ff_trial_' + str(record['index'])
    canvas = ROOT.roothelper.CreateCanvas(name, 'Staircase feedforward response')
    graph = ROOT.TMultiGraph()
    start = points[0][0]
    raw = ROOT.TGraph(len(points), array('d', [t-start for t,e in points]), array('d', [e/1e6 for t,e in points]))
    raw.SetTitle('Unfitted target error');raw.SetLineWidth(1);raw.SetMarkerStyle(20)
    raw.SetMarkerColor(ROOT.kBlue+1);graph.Add(raw, 'P')
    retained = [raw]
    if _finite(tolerance) and tolerance > 0:
        for sign, label in ((1, 'Positive evaluation limit'), (-1, 'Negative evaluation limit')):
            bound = ROOT.TGraph(2, array('d', [0, points[-1][0]-start]), array('d', [sign*tolerance/1e6]*2))
            bound.SetTitle(label);bound.SetLineWidth(1);bound.SetLineColor(ROOT.kRed+1);bound.SetLineStyle(2)
            graph.Add(bound, 'L');retained.append(bound)
    graph.SetTitle(';Time since first source sample (s);Unfitted target error (MHz)')
    y_values = [e/1e6 for _,e in points]
    if _finite(tolerance) and tolerance > 0:y_values += [-tolerance/1e6,tolerance/1e6]
    low, high = min(y_values), max(y_values)
    extent = high-low or max(abs(high),1.)
    # Reserve data-free range for automatic legend placement, not manual text
    # sizes/margins. Dense periodic peaks otherwise force an unreadable legend.
    graph.SetMinimum(low-.05*extent);graph.SetMaximum(high+.35*extent)
    graph.Draw('A');canvas.Modified();canvas.Update();ROOT.roothelper.SetAxes(graph)
    legend = ROOT.roothelper.PutLegend()
    saver.WriteCanvas(canvas)
    pdf = next((output/'figures').rglob(name+'.pdf'))
    caption = (f"Trial {record['id']}, {record['role']}. {len(points)} source samples; "
        f"{record['waveform']['excluded_samples']} entries outside commanded levels or invalid entries omitted. "
        f"First source timestamp {start:.6f}. Points are not joined across gaps. "
        'The dashed lines are the frozen evaluation criterion, not physical safety limits.')
    return r'\ReportFigure[width=\linewidth]{'+str(pdf.relative_to(output))+r'}{'+tex(caption)+r'}{fig:'+name+'}'


def render_source(result, records, figures, stamp):
    """Escaped English TeX, also testable without ROOT or physical interfaces."""
    sections = []
    for record, figure in zip(records, figures):
        condition, candidate = record['condition'], record['candidate']
        rows = [('Role / candidate', record['role']+' / '+str(candidate.get('id', 'unavailable'))),
            ('Condition', condition.get('id', 'unavailable')),
            ('Dwell / cycle period (s)', _number(condition.get('dwell_s'))+' / '+_number(record['cycle_period_s'])),
            ('Cycle frequency (Hz)', _number(record['cycle_frequency_hz'])),
            ('Tested span (MHz)', _number(record['tested_span_hz']/1e6 if record['tested_span_hz'] is not None else None)),
            ('LDC FF (V/Hz)', _number(candidate.get('ldc_ff_v_per_hz'))),
            ('PZT / native-current FF scale', _number(candidate.get('pzt_ff_gain_scale'))+' / '+_number(candidate.get('ff_gain_scale'))),
            ('Evidence qualification', 'qualified for this measured condition' if record['qualified'] else 'not qualified'),
            ('Frequency continuity / optical quality', str(record['frequency_continuity'] or 'unknown')+' / '+str(record['optical_quality'] or 'unknown')),
            ('Worst settling (s)', _number(record['metrics']['worst_settling_s'])),
            ('RMS error (Hz)', _number(record['metrics']['rms_error_hz'])),
            ('Maximum late-dwell drift (Hz/s)', _number(record['metrics']['max_late_drift_hz_per_s'])),
            ('Raw record complete', str(record['completeness'])),
            ('Source / clock', str(record['source'] or 'unavailable')+' / '+str(record['time_base'] or 'unavailable'))]
        table = '\n'.join(tex(k)+' & '+tex(v)+r' \\' for k,v in rows)
        reasons = '; '.join(record['reasons']) or 'See the machine-readable trial evaluation.'
        sections.append(r'\clearpage\section{Trial '+tex(record['id'])+'}\n'+
            r'\begin{tabularx}{\linewidth}{@{}lX@{}}\toprule'+'\n'+table+'\n'+r'\bottomrule\end{tabularx}'+'\n'+
            tex(reasons)+'\n'+(figure or tex(record.get('artifact_status', 'No finite raw waveform samples.'))))
    recommendations = []
    for recommendation in result.get('recommendations') or []:
        condition, candidate = recommendation.get('condition') or {}, recommendation.get('candidate') or {}
        coefficients = ', '.join(k+'='+_number(candidate[k]) for k in
            ('ldc_ff_v_per_hz','pzt_ff_gain_scale','ff_gain_scale') if k in candidate)
        recommendations.append(tex(condition.get('id', 'unavailable'))+' & '+tex(candidate.get('id', 'unavailable'))+
            ' & '+tex(_number(recommendation.get('worst_settling_s')))+' & '+
            tex(_number(recommendation.get('rms_error_hz')))+r' \\'+'\n'+
            r'\multicolumn{4}{p{0.94\linewidth}}{'+tex(coefficients+'. '+str(recommendation.get('application','recommended only'))+
            '. Pareto: '+str(recommendation.get('pareto'))+'. '+str(recommendation.get('evidence','')))+r'} \\'+'\n'+r'\midrule')
    selection = (r'\begin{longtable}{@{}llrr@{}}\toprule Condition & Candidate & Settling (s) & RMS (Hz) \\ \midrule'+'\n'+
        '\n'.join(recommendations)+r'\bottomrule\end{longtable}') if recommendations else 'No qualified recommendation was recorded.'
    return r'''\documentclass{ldconreport}
\renewcommand{\reportshorttitle}{Staircase FF optimization}
\title{Staircase feedforward optimization}
\author{LDCon deterministic sweep evaluator}
\date{'''+tex(stamp)+r'''}
\begin{document}\maketitle
\ReportNotice{Recommendations are conditional on the tested cadence and operating context.
This report does not adopt coefficients, change calibration, or establish a universal mode-hop-free range.}
\section{Outcome}
Status: '''+tex(result.get('status', 'unavailable'))+r'''.
The execution record reports applied: '''+tex(result.get('applied', False))+r'''.
Trial records: '''+str(len(records))+r'''. Qualified records: '''+str(sum(r['qualified'] for r in records))+r'''.
\section{Interpretation}
Dwell, step sequence, direction, cycle period, repeat count and preconditioning are separate experimental conditions.
Thermal carryover and slow drift may change the response; results at one cadence do not qualify another cadence.
A tested endpoint is not a measured mode-hop boundary. Missing data, truncated sweeps and unknown outcomes do not prove absence of a hop.
Optical purity and frequency continuity are distinct claims. Quality limits are evaluation criteria, not physical safety limits.
The plots show unfitted target error; gain or offset fitting is never used to hide feedforward error.
The command is assigned by ADC-fitted level boundaries at each raw acquisition time;
optical response delay and window width are not subtracted from the response.
Raw waveform artifacts and per-visit settling, residual, drift and uncertainty records remain in the parent trial artifacts.
\section{Selection and limitations}
Recommendations: '''+str(len(result.get('recommendations') or []))+r'''.
Use each recommendation only with its exact condition, source trials and qualification.
Do not combine independently successful coefficient bounds into an untested joint setting.
The report's analysis-input.json contains the plotted evidence and its qualifications.
'''+selection+'\n'+ '\n'.join(sections)+r'\end{document}'


def write_report(directory, result):
    records = evidence(directory, result)
    ROOT = _root()
    output = initialize(Path(directory)/'report')
    (output/'analysis-input.json').write_text(json.dumps(dict(status=result.get('status'),
        applied=result.get('applied', False), trials=records), indent=2, allow_nan=False)+'\n')
    tolerance = (result.get('parameters', {}).get('criteria') or {}).get('error_tolerance_hz')
    saver = ROOT.roothelper.DataSaver(str(output/'figures'), True)
    figures = [_figure(ROOT, saver, output, record, tolerance) for record in records]
    del saver
    stamp = datetime.now().astimezone().isoformat(timespec='seconds')
    (output/'report.tex').write_text(render_source(result, records, figures, stamp))
    pdf = build(output)
    shutil.copyfile(pdf, output/'report.pdf')
    return output/'report.pdf'
