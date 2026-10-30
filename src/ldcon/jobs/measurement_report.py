"""Offline vector time series and LuaLaTeX PDF for every measurement mode."""
from array import array
from datetime import datetime
import json
import math
import os
from pathlib import Path
import shutil
from ldcon.agents.report_pdf import initialize, build
from ldcon.jobs.axis_frequency_report import tex


def series(result):
    groups = []
    for index, step in enumerate(result.get('steps', [result])):
        data = {}
        for row in step.get('samples', []):
            for name, block in row.get('measurements', {}).items():
                stamp = block.get('acquired_unix_s')
                if block.get('valid') is not True or type(stamp) not in (float,int) or not math.isfinite(stamp):continue
                for field, value in block.get('values', {}).items():
                    if type(value) not in (float, int) or not math.isfinite(value):continue
                    data.setdefault((name, field), []).append((stamp, value))
        for (name, field), points in data.items():
            groups.append(dict(step=index, measurement=name, field=field, points=points))
    return groups


def write_report(directory, result):
    import ROOT
    prefixes = [Path(os.environ['ROOTHELPER_PREFIX'])] if os.environ.get('ROOTHELPER_PREFIX') else []
    prefixes += [Path.home()/'opt/roothelper', Path.home()/'.local', Path('/home/FanLabAdmin/opt/roothelper')]
    prefix = next((p for p in prefixes if (p/'include/roothelper/roothelper.h').exists()), None)
    if prefix is None:raise RuntimeError('ROOT/roothelper required; no alternate plotting backend')
    ROOT.gROOT.SetBatch(True)
    ROOT.gInterpreter.AddIncludePath(str(prefix/'include'))
    if not ROOT.gInterpreter.Declare('#include <roothelper/roothelper.h>'):raise RuntimeError('roothelper unavailable')
    ROOT.roothelper.Prepare()
    output = initialize(Path(directory)/'report')
    (output/'analysis-input.json').write_text(json.dumps(result, indent=2, allow_nan=False)+'\n')
    figures = []
    saver = ROOT.roothelper.DataSaver(str(output/'figures'), True)
    for index, group in enumerate(series(result)):
        points = group['points'];start = points[0][0];reference = 0.
        field = group['field'];unit = '1';scale = 1.
        if field == 'frequency_hz':reference=points[0][1];scale=1e6;unit='MHz'
        elif field == 'wavelength_nm':unit='nm'
        elif group['measurement']=='dither':unit='configured response unit'
        label = field.replace('_', ' ').capitalize()
        if reference:label = 'Frequency relative to first sample'
        canvas=ROOT.roothelper.CreateCanvas('measurement_'+str(index), label)
        graph=ROOT.TGraph(len(points),array('d',[t-start for t,v in points]),array('d',[(v-reference)/scale for t,v in points]))
        graph.SetTitle(';Time since first sample (s);'+label+' ('+unit+')')
        graph.SetLineWidth(1);graph.SetMarkerStyle(20);graph.Draw('AP')
        canvas.Modified();canvas.Update();ROOT.roothelper.SetAxes(graph)
        saver.WriteCanvas(canvas)
        pdf=next((output/'figures').rglob('measurement_'+str(index)+'.pdf'))
        caption=tex(f"Step {group['step']}, {group['measurement']}, {len(points)} source samples. First source timestamp {start}; frequency reference {reference} Hz. Points are not joined across missing data.")
        figures.append(r'\ReportFigure{'+str(pdf.relative_to(output))+r'}{'+caption+r'}{fig:measurement'+str(index)+'}')
    del saver
    body=r'''\documentclass{ldconreport}
\renewcommand{\reportshorttitle}{Change and measurement}
\title{Change and measurement}\author{LDCon deterministic measurement runner}
\date{'''+tex(datetime.now().astimezone().isoformat(timespec='seconds'))+r'''}
\begin{document}\maketitle
\ReportNotice{Measurement evidence only. No optimum or calibration is automatically adopted.}
\section{Outcome}
Status: '''+tex(result.get('status'))+r'''. Physical outcome: '''+tex(result.get('physical_outcome','See per-step receipts'))+r'''.
\section{Evidence}
Raw samples, source timestamps, selected measurements, baseline, waiting interval,
owner application receipts and rejected acquisition evidence are retained in measurement.json.
No agent is required to create this report. A successful write does not establish optical settling.
'''+ '\n'.join(figures)+r'\end{document}'
    (output/'report.tex').write_text(body)
    pdf=build(output);shutil.copyfile(pdf,output/'report.pdf')
    return output/'report.pdf'
