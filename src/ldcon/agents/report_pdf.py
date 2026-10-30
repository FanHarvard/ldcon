"""Initialize and compile trusted English report sources with LuaLaTeX.

This offline utility never imports WebRuntime, starts a card or accesses devices.
Only run it on report sources you have reviewed; no-shell-escape is not a TeX sandbox.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
from ldcon.core.paths import resources_dir
import shutil
import subprocess
import time

TEMPLATE = resources_dir() / 'report_template'


def instructions():
    import sys
    return ('Report format policy (2026-10-25): new study, diagnostic and maintenance '
            'reports use LuaLaTeX PDF, superseding old HTML-report instructions. '
            'Keep English editable TeX, original vector PDF figures, source data and a build receipt '
            'in this laser\'s reports directory. Embed PDF figures directly with includegraphics '
            'or ReportFigure, never rasterize vector originals. '
            'Use CERN ROOT and roothelper for report figures and analysis when installed, as required '
            'by card-system-design section J. Check root-config or ROOTSYS, and roothelper under '
            '~/.local and ~/opt (FanLabAdmin uses /home/FanLabAdmin/opt/roothelper). Read the '
            'roothelper AGENTS.md and use its automatic formatting and DataSaver; retain native ROOT '
            'objects. Create legends with roothelper PutLegend (transparent, no border); '
            'PlaceLegend alone only positions an existing legend. '
            'Use ProcessPDF for rotation normalization or other vector formats. '
            'Do not silently substitute another plotting backend; if unavailable, report the missing '
            'dependency before choosing a documented alternative. Use sentence-case axis titles '
            'with units in parentheses, without units on numeric ticks. '
            'Template resources: '+str(TEMPLATE)+'. Initialize a new empty report directory with '
            +sys.executable+' -m ldcon.report_pdf init <directory>; compile reviewed sources with '
            +sys.executable+' -m ldcon.report_pdf build <directory>. Compilation is offline, '
            'uses no shell escape, and is not a sandbox for untrusted TeX. Render and inspect '
            'all pages before delivery. Do not claim a PDF was produced if compilation failed. '
            'Keep historical HTML records intact. Existing Web pages remain views of evidence. '
            'Attachment delivery remains deferred; never claim a PDF was sent without a receipt.')


def initialize(directory):
    directory = Path(directory)
    if directory.exists() and any(directory.iterdir()):
        raise ValueError('Use a new or empty report directory; existing files are not overwritten')
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'figures').mkdir()
    for name in ('ldconreport.cls', 'report.tex'):
        shutil.copyfile(TEMPLATE / name, directory / name)
    return directory


def build(directory, *, timeout=120):
    directory = Path(directory).resolve(strict=True)
    for name in ('report.tex', 'ldconreport.cls'):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Expected local regular report source: ' + name)
    compiler = shutil.which('lualatex')
    if compiler is None:
        raise RuntimeError('LuaLaTeX is required; no HTML or raster fallback was generated')
    output = directory / 'build'
    output.mkdir(exist_ok=True)
    if output.is_symlink():
        raise ValueError('Build directory must not be a symlink')
    # Do not mistake a PDF left by an earlier successful build for this result.
    (output / 'report.pdf').unlink(missing_ok=True)
    # Lua font loading needs absolute read access to the installed TeX tree.
    # This is deliberately a trusted-source compiler, not a TeX sandbox.
    env = dict(os.environ, openin_any='a', openout_any='p',
               TEXMFVAR=str(output / 'tex-cache'), TEXMFCACHE=str(output / 'tex-cache'))
    command = [compiler, '-no-shell-escape', '-halt-on-error', '-interaction=nonstopmode',
               '-file-line-error', '-output-directory=build', 'report.tex']
    start = time.time()
    for pass_number in (1, 2):
        with (output / f'compile-{pass_number}.log').open('w') as log:
            subprocess.run(command, cwd=directory, env=env, check=True, timeout=timeout,
                           stdout=log, stderr=subprocess.STDOUT)
    pdf = output / 'report.pdf'
    if not pdf.is_file() or not pdf.read_bytes().startswith(b'%PDF-'):
        raise RuntimeError('Compiler did not produce a PDF')
    sources = [directory/'report.tex', directory/'ldconreport.cls']
    sources += sorted((directory/'figures').glob('*.pdf'))
    receipt = dict(schema_version=1, template_version='1.0', compiler='LuaLaTeX',
                   started_unix_s=start, finished_unix_s=time.time(),
                   sources={str(p.relative_to(directory)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
                   output_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
                   shell_escape=False, hardware_operations=False, visual_review='required')
    (output/'build-receipt.json').write_text(json.dumps(receipt, indent=2)+'\n')
    return pdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('init', 'build'))
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    print(initialize(args.directory) if args.action == 'init' else build(args.directory))


if __name__ == '__main__':
    main()
