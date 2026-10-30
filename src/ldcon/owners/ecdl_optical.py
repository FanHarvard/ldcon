"""Owner optical classification using calibrated position-free peaks only."""
from ldcon.core.contracts import DeviceError


def classify(frame, config):
    pattern=next((p for p in frame.patterns if p.interferometer==str(config['interferometer'])),None)
    if pattern is None:raise DeviceError('Configured interferometer is unavailable')
    if config.get('metric_method')!='phase_free_v1':
        raise DeviceError('Position-free optical configuration is required; fixed-window classification was retired')
    if any(k in ('A','B','C','F2') for k in set(config['single_mode_max'])):
        raise DeviceError('Position-free classification requires calibrated peak and width limits, not fixed windows or fringe fill')
    if config.get('classification_enabled',False):
        required={'side_peak_height_max','side_peak_prominence_max','main_fwhm_fraction'}
        if not required<=set(config['single_mode_max']):
            raise DeviceError('Position-free classification requires calibrated peak and width limits')
    from ldcon.owners.fringe_peaks import analyze
    try:
        analysis=analyze(pattern.pixels,config['period_reference_px'],config.get('period_tolerance_fraction',.12))
    except DeviceError as error:
        if str(error) in ('Invalid optical profile','No optical fringe contrast'):raise
        return dict(single_mode=False,mode_id='unclassified',quality_valid=pattern.quality=='valid',impurity=None,maintenance_required=True,metrics=None,classification_error=str(error))
    metrics=analysis['metrics']
    # A fixed frequency interval cannot identify a physical branch when TEC/PZT
    # or the requested target changes. Branch identity belongs to recorded sweeps.
    mode='unclassified'
    # Period/coverage qualification is measurement validity; near-main shoulders
    # are judged by their configured scalar limit, not an implicit zero limit.
    qualified=analysis.get('period_qualified',analysis['qualified'])
    clean=all(metrics[k]<=v for k,v in config['single_mode_max'].items())
    if analysis is not None:clean=clean and qualified and config.get('classification_enabled',False)
    sensitive=True
    if analysis is not None and config.get('classification_enabled',False):
        sensitive=max(.01,6*metrics['profile_noise_max'])<config['single_mode_max']['side_peak_prominence_max']
        clean=clean and sensitive
    warning=not sensitive or not qualified or any(metrics[k]>v for k,v in config['single_mode_max'].items())
    return dict(single_mode=clean,mode_id=mode,quality_valid=pattern.quality=='valid',
                impurity=metrics['F2'],maintenance_required=warning,metrics=metrics,fringe_analysis=analysis)
