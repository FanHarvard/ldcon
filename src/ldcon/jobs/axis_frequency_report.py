"""Offline calibration vector figures (ROOT/roothelper) and LuaLaTeX report."""
from array import array
from datetime import datetime
import json
import os
from pathlib import Path
import shutil

from ldcon.agents.report_pdf import initialize, build


def tex(value):
    return ''.join({'\\':r'\textbackslash{}','_':r'\_','%':r'\%','&':r'\&',
                    '#':r'\#','{':r'\{','}':r'\}','$':r'\$','^':r'\textasciicircum{}',
                    '~':r'\textasciitilde{}'}.get(c,c) for c in str(value))


def write_report(directory, result):
    import ROOT
    prefixes = [Path(os.environ['ROOTHELPER_PREFIX'])] if os.environ.get('ROOTHELPER_PREFIX') else []
    prefixes += [Path.home()/'opt/roothelper',Path.home()/'.local',Path('/home/FanLabAdmin/opt/roothelper')]
    prefix=next((p for p in prefixes if (p/'include/roothelper/roothelper.h').exists()),None)
    if prefix is None:raise RuntimeError('roothelper installation required; no alternate plotting backend')
    ROOT.gROOT.SetBatch(True)
    ROOT.gInterpreter.AddIncludePath(str(prefix/'include'))
    if not ROOT.gInterpreter.Declare('#include <roothelper/roothelper.h>'):
        raise RuntimeError('roothelper could not be loaded')
    output=initialize(Path(directory)/'report')
    (output/'analysis-input.json').write_text(json.dumps(result,indent=2,allow_nan=False)+'\n')
    ROOT.roothelper.Prepare()
    canvas=ROOT.roothelper.CreateCanvas('axis_frequency','Axis frequency calibration')
    graph=ROOT.TMultiGraph()
    reference=result['points'][0]['frequency_mean_hz'] if result['points'] else 0.
    retained=[]
    for direction,color in (('up',ROOT.kBlue+1),('down',ROOT.kRed+1)):
        points=[p for p in result['points'] if p['direction']==direction]
        if not points:continue
        x=array('d',[p['axis_value'] for p in points])
        y=array('d',[(p['frequency_mean_hz']-reference)/1e6 for p in points])
        g=ROOT.TGraph(len(x),x,y)
        g.SetTitle(direction.capitalize())
        g.SetLineColor(color)
        g.SetMarkerColor(color)
        g.SetLineWidth(1)
        graph.Add(g,'LP')
        retained.append(g)
    figure=''
    if retained:
        graph.SetTitle(';Axis position ('+result['axis']['unit']+');Frequency offset (MHz)')
        graph.Draw('A')
        canvas.Modified();canvas.Update()
        ROOT.roothelper.SetAxes(graph)
        legend=ROOT.roothelper.PutLegend()
        saver=ROOT.roothelper.DataSaver(str(output/'figures'),True)
        saver.WriteCanvas(canvas)
        del saver
        pdf=next((output/'figures').rglob('axis_frequency.pdf'))
        figure=r'\ReportFigure{'+str(pdf.relative_to(output))+r'}{Measured up/down calibration points. Frequency reference: '+f'{reference:.6g}'+r' Hz.}{fig:calibration}'
    fit=result.get('fit',{})
    notice = 'SIMULATION ONLY. Synthetic data; no hardware measurements. ' if result.get('context',{}).get('simulation') else ''
    body=r'''\documentclass{ldconreport}
\renewcommand{\reportshorttitle}{Axis frequency calibration}
\title{Axis frequency calibration}
\author{LDCon deterministic calibration card}
\date{'''+tex(datetime.now().astimezone().isoformat(timespec='seconds'))+r'''}
\begin{document}\maketitle
\ReportNotice{'''+notice+r'''Calibration proposal only. No calibration was automatically adopted.}
\section{Outcome}
Status: '''+tex(result['status'])+r'''. Unit: '''+tex(result['axis']['unit'])+r'''.
Signed slope (Hz per axis unit): '''+tex(fit.get('frequency_hz_per_unit','unavailable'))+r'''.
\section{Evidence and limitations}
Points: '''+str(len(result['points']))+r'''. Raw acquisition records: '''+str(len(result['raw']))+r'''.
Maximum residual (Hz): '''+tex(fit.get('max_residual_hz','unavailable'))+r'''.
Up/down difference includes both hysteresis and time drift (Hz): '''+tex(fit.get('hysteresis_or_drift_hz','unavailable'))+r'''.
Acquisition clock: '''+tex(result['clock'])+r'''. FF and competing-axis context,
quality metrics, rejected frames, parameters and original readings are retained
in analysis-input.json and the parent calibration artifact. No physical mode
identity is inferred from a frequency interval. A mode discontinuity invalidates
this local slope. A different FF configuration requires a separate calibration.
'''+figure+r'''
\end{document}
'''
    (output/'report.tex').write_text(body)
    pdf=build(output)
    shutil.copyfile(pdf,output/'report.pdf')
    return output/'report.pdf'
