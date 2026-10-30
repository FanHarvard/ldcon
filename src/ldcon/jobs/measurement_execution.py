"""Job boundary: durable measurement evidence before offline presentation."""
from pathlib import Path
import shutil
from ldcon.core.records import atomic_json, read_json
from ldcon.jobs.change_measure_adapter import run_card as run_measurement


def finish_report(directory, result):
    # Reporting cannot undo an owner-confirmed application or mask its failure.
    try:
        from ldcon.jobs.measurement_report import write_report
        pdf = write_report(directory, result)
        if pdf != directory/'report.pdf':
            shutil.copyfile(pdf, directory/'report.pdf')
        pdf = directory/'report.pdf'
        result['report'] = dict(status='created', path=str(pdf.relative_to(directory)))
    except Exception as error:
        result['report'] = dict(status='unavailable', reason=type(error).__name__)
    atomic_json(directory/'measurement.json', result)


def run_card(directory, laser, card, p, *, emit, **arguments):
    directory = Path(directory)
    artifact = directory/'measurement.json'
    def record(kind, value):
        if kind in ('change_measure_result', 'change_measure_batch_result'):
            atomic_json(artifact, value)
        emit(kind, value)
    try:
        result = run_measurement(directory, laser, card, p, emit=record, **arguments)
    except Exception:
        partial = read_json(artifact, None)
        if isinstance(partial, dict):
            try: finish_report(directory, partial)
            except Exception: pass  # Preserve the original physical failure.
        raise
    atomic_json(artifact, result)
    finish_report(directory, result)
    return dict(status=result['status'], physical_outcome=result.get('physical_outcome'),
                measurement_file='measurement.json', report=result.get('report'), steps=len(result.get('steps', [])))
