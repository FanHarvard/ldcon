"""Named measurement configurations; presets never select a different algorithm.

Parameter names describe laser-owner capabilities, not vendor registers. Empty
changes are intentional: a template never supplies an unapproved physical step.
These explicit templates supply deck data; runtime normalization does not merge
operational defaults. Every generated step includes its history completion budget.
"""
import copy

PRESETS = {
    'observe': ('Observation only', (), {'wavelength': True}),
    'current-response': ('Current operating-point response', ('current',), {'wavelength': True, 'optical_quality': True}),
    'temperature-response': ('TEC operating-point response', ('temperature',), {'wavelength': True}),
    'piezo-response': ('PZT operating-point response', ('piezo',), {'wavelength': True}),
    'pi-comparison': ('PI stability comparison', ('kp_count_per_nm', 'ki_count_per_nm_s', 'integral_limit_count'), {'wavelength': True}),
    'target-response': ('Target step response', ('target_frequency_hz',), {'wavelength': True}),
    'centering-response': ('Outer-loop centering response', ('tec_gain', 'supervision_interval_s', 'piezo_center_dac'), {'wavelength': True}),
    'tec-current-feedforward': ('TEC to current feedforward comparison', ('current_per_temperature',), {'wavelength': True, 'optical_quality': True}),
    'piezo-current-feedforward': ('PZT/current paired-point measurement', ('piezo', 'current'), {'wavelength': True, 'optical_quality': True}),
}


def card(name, *, family='ecdl', optical_quality=False, dither=False):
    if family not in ('ecdl', 'dfb-dbr', 'ecdl-dlcpro'):
        raise ValueError('Unknown laser family')
    if name not in PRESETS:
        raise ValueError('Unknown measurement preset')
    if family == 'dfb-dbr' and name in ('piezo-response', 'piezo-current-feedforward', 'centering-response', 'tec-current-feedforward'):
        raise ValueError('This preset requires a PZT axis')
    title, _, flags = PRESETS[name]
    flags = dict(flags, optical_quality=bool(optical_quality and flags.get('optical_quality')), dither=bool(dither))
    return dict(id=name, operation='measure.change', uses=[], parameters=dict(
        changes=[], apply_mode='coordinated' if name == 'pi-comparison' else 'ordered',
        measurements=flags, baseline_s=2., wait_s=0., duration_s=10., history_wait_s=2.,
        minimum_samples=3, sample_interval_s=.1, measurement_max_age_s=2., max_sample_gap_s=1., timeout_s=300.))


def cards(family, *, optical_quality=False):
    excluded = ({'piezo-response','piezo-current-feedforward','centering-response','tec-current-feedforward'} if family=='dfb-dbr' else
                {'pi-comparison','centering-response','tec-current-feedforward','target-response'} if family=='ecdl-dlcpro' else set())
    names = [name for name in PRESETS if name not in excluded]
    result = [card(name, family=family, optical_quality=optical_quality) for name in names]
    result.append(dict(id='batch-measurement', operation='measure.batch', uses=[],
                       parameters=dict(steps=[], timeout_s=3600.)))
    return result


def describe():
    return [dict(id=name, title=title, suggested_parameters=list(names),
                 measurements=copy.deepcopy(flags), operation='measure.change',
                 authority='owner capability and admission required; preset grants no physical authority')
            for name, (title, names, flags) in PRESETS.items()]
