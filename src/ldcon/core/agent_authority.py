"""Laser-agent authority: field classes, hard ceilings and a host-only approval ledger.

Three classes of configuration/runtime fields:

``free``       the laser agent may change them without asking (operational
               settings, quality thresholds, card operations).
``safety``     a safety range. The agent may change it only with an explicit
               approval that a human gave *directly* in the agent's own
               conversation (a Remote Control prompt). The approval is verified
               by the host against the prompt ledger, never by the agent.
``forbidden``  maintainer-only. No approval can move them through the agent:
               absolute/physical limits, device wiring and identity,
               calibration, firmware identity, interfaces and this table.

The ledger lives OUTSIDE the laser deployment root, so it is read-only inside
the resident sandbox (``/opt/ldcon`` is ro-bound; only the deployment root is
bind-writable). Only host processes (resident worker, dashboard) write it.
Relayed hub/Teams messages never appear in the ledger: they are not prompts of
the agent's own conversation, so they can never satisfy an approval.
"""
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import time
import uuid

FREE, SAFETY, FORBIDDEN = 'free', 'safety', 'forbidden'
CLASSES = (FREE, SAFETY, FORBIDDEN)
PROPOSAL_TTL_S = 1800.
APPROVAL_TTL_S = 7200.
MAX_QUOTE_BYTES = 8192

# Field names that denote a safety range wherever they occur under control
# settings or card parameters. Unknown names default to ``safety`` as well; the
# explicit FREE rules below are the only way to make a field free.
SAFETY_NAME = re.compile(r'limit|bound|range|threshold|tolerance|capture|acceptance|slew|step|safety|'
                         r'absolute|watchdog|timeout|deadline|fresh|interlock|integral|envelope|span|'
                         r'gate|guard|recover|hop|enabled|mode\b|direction|max_|min_|_max\b|_min\b|'
                         r'\bkp|\bki|\bkd|pid|excursion|levels|dwell|cap_|_cap\b|budget|margin|edge|'
                         r'window|dither|commission|feedforward|prior|observables|update_interval', re.I)

# (kind, pattern, class). Patterns are tuples of path segments: a literal, '*'
# for one segment, '**' for the rest. The first matching rule wins, so order is
# most specific first.
RULES = (
    # ---- laser definitions -------------------------------------------------
    # the ONE target source (2026-10-22): the value is free (bounded by target.allowed_change_hz / max_change_hz, which are
    # safety-class like every bound), so the tune.lock_target card can save it under a standing request.
    ('lasers', ('target', 'frequency_hz'), FREE),
    ('lasers', ('target', 'note'), FREE),
    # Human decision 2026-10-27 (target-ff design): the per-step maximum target shift is an OPERATING parameter the
    # study.target_response card adopts without approval. The physical limits (PZT/DAC range, allowed_change_hz) stay.
    ('lasers', ('target', 'max_change_hz'), FREE),
    ('lasers', ('target', 'allowed_change_hz', '**'), SAFETY),
    ('lasers', ('target', 'bounds_hz', '**'), SAFETY),
    ('lasers', ('schema_version',), FORBIDDEN),
    ('lasers', ('id',), FORBIDDEN),
    ('lasers', ('allow_output',), FORBIDDEN),
    ('lasers', ('agent_authority', '**'), FORBIDDEN),
    # Approved finite-measurement envelope: never a free candidate or monitor toggle.
    ('lasers', ('measurement_plan', '**'), SAFETY),
    ('lasers', ('interfaces', '**'), FORBIDDEN),
    ('lasers', ('telemetry', '**'), FORBIDDEN),
    ('lasers', ('devices',), FORBIDDEN),
    ('lasers', ('devices', '*'), FORBIDDEN),                      # add/remove a device
    ('lasers', ('devices', '*', 'type'), FORBIDDEN),
    ('lasers', ('devices', '*', 'connection', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'absolute_conditions', 'axes', '*', 'bounds', '**'), SAFETY),
    ('lasers', ('devices', '*', 'absolute_conditions', 'axes', '*', 'activation_range', '**'), SAFETY),
    ('lasers', ('devices', '*', 'absolute_conditions', 'axes', '*', 'max_step', '**'), SAFETY),
    ('lasers', ('devices', '*', 'absolute_conditions', 'axes', '*', 'max_slew_per_s', '**'), SAFETY),
    ('lasers', ('devices', '*', 'absolute_conditions', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings'), FORBIDDEN),  # replace the whole block
    # Human decision 2026-10-30: split Fine-input measurement gain is not an
    # output/safety bound. Preserve wiring, output factors and guard authority.
    ('lasers', ('devices', '*', 'control_settings', 'awg_sweep', 'adc_v_per_awg_v'), FREE),
    # native ESP32 current lock: physical identity, calibration and absolute envelope
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'firmware_version'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'writes_enabled'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'calibration', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'dac', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'adc', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'current_bounds_a', '**'), SAFETY),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'preserved', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'optical', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'cld_interlock', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'udp_gate', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'native', 'input', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'native', 'evidence'), FREE),
    # PI anti-windup allocation is an operating setting, not a physical output
    # safety range. The owner still proves the complete reachable envelope.
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'native', 'integral_limit_count'), FREE),
    # Human decisions 2026-10-27 (target-ff design, decisions 1 and 5): the per-step target cap, the FF scale (its
    # bounds stay safety through SAFETY_NAME) and the post-step integral recentre fraction are free.
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'native', 'target_ramp_step_hz'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'native', 'target_feedforward_scale'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'native', 'recentre_integral_fraction'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'native_current', 'revision'), FREE),
    # ECDL steady lock: wiring, calibration coefficients and commissioning evidence
    ('lasers', ('devices', '*', 'control_settings', 'tec_idn_contains'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'axes', '*', 'identity'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'axes', '*', 'unit'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'axes', '*', 'frequency_hz_per_unit'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'axes', '*', 'resolution'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'axes', '*', 'readback_tolerance'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'axes', '*', 'coefficient_source'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'axes', '*', 'revision'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'native_input', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'firmware_version'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'native_handover_verified'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'handover_evidence'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'wavelength_nm_per_count'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'counts_per_temperature_unit'), FORBIDDEN),
    # Dither pause (human request 2026-10-27 "しばらくディザを切ってみてください"): pausing only REMOVES a deliberate
    # modulation (the same ARB bank is rewritten with all-zero codes; name/frequency/carrier range unchanged), it
    # cannot widen any output envelope, so it is FREE and the rig owner adopts it live (LIVE_RULES owner_hot).
    # Every other dither leaf keeps its class: programming/telemetry/selector/verified/evidence FORBIDDEN below,
    # frequency/amplitude/window SAFETY through SAFETY_NAME.
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'dither', 'paused'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'dither', 'programming', '**'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'dither', 'telemetry_endpoint'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'dither', 'selector'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'dither', 'verified'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'dither', 'evidence'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'temperature_compensation', 'ohm_per_v'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'temperature_compensation', 'source'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'temperature_compensation', 'authorization'), FORBIDDEN),
    ('lasers', ('devices', '*', 'control_settings', 'slow_control', 'sensitivity'), FORBIDDEN),
    # standing quality authorization (position-free classification thresholds)
    ('lasers', ('devices', '*', 'control_settings', 'optical', 'single_mode_max', '*'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'window', 'warning_response'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'window', 'edge_response'), FREE),
    # Human decisions 2026-10-27 (target-ff design, decisions 1-4): the ECDL per-step target cap, the target slew,
    # the PZT FF scale, the LDC target-FF coefficient (and its sensitivity estimate) and the TEC->current FF
    # coefficient with its bounds are operating parameters a study adopts. The FF scale bounds, the LDC term's
    # max_abs_v / bounds_v_per_hz keep their safety class through SAFETY_NAME.
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'target_ramp_step_hz'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'target_slew_hz_per_s'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'target_feedforward_scale'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'ldc_target_feedforward', 'v_per_hz'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'ldc_target_feedforward', 'hz_per_v'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'steady_lock', 'tec_current_feedforward', '**'), FREE),
    ('lasers', ('devices', '*', 'control_settings', 'background_adjustments', 'feedforward', 'bounds', 'current_per_temperature', '**'), FREE),
    # A candidate never changes the human-approved autonomous envelope.
    ('lasers', ('devices', '*', 'control_settings', 'background_adjustments', '**'), SAFETY),
    # free operational leaves (the previous NORMAL set, plus bookkeeping text)
    ('lasers', ('name',), FREE),
    ('lasers', ('description',), FREE),
    ('lasers', ('**', 'target_frequency_hz'), FREE),
    ('lasers', ('**', 'target_wavelength_nm'), FREE),
    ('lasers', ('**', 'strategy'), FREE),
    ('lasers', ('**', 'tec_gain'), FREE),
    ('lasers', ('**', 'tec_ff_paused'), FREE),
    ('lasers', ('**', 'current_ff_enabled'), FREE),
    ('lasers', ('**', 'p_sample'), FREE),
    ('lasers', ('**', 'g_sample'), FREE),
    ('lasers', ('**', 'evidence'), FREE),
    ('lasers', ('**', 'revision'), FREE),
    ('lasers', ('**', 'reference_status'), FREE),
    ('lasers', ('**', 'interpretation'), FREE),
    ('lasers', ('**', 'setting_source'), FREE),
    ('lasers', ('**', 'approval'), FREE),
    ('lasers', ('**', 'settle_s'), FREE),
    ('lasers', ('**', 'supervision_interval_s'), FREE),
    ('lasers', ('**', 'measurement_filter_s'), FREE),
    ('lasers', ('**', 'error_deadband'), FREE),
    ('lasers', ('**', 'quantization_hysteresis'), FREE),
    # ---- assignments (per-laser card invocation parameters) ----------------
    ('assignments', ('schema_version',), FORBIDDEN),
    ('assignments', ('id',), FORBIDDEN),
    ('assignments', ('laser_id',), FORBIDDEN),
    ('assignments', ('decks',), FORBIDDEN),
    ('assignments', ('decks', '*'), FORBIDDEN),                   # add/remove a deck
    ('assignments', ('decks', '*', 'deck_id'), FORBIDDEN),
    ('assignments', ('decks', '*', 'parameters'), FORBIDDEN),     # replace every card at once
    ('assignments', ('decks', '*', 'parameters', '*'), FREE),     # add/remove one card's override block
    # window guard of the steady card and the midpoint card itself
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'enabled'), FREE),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'agent_enabled'), FREE),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'conditions', '**'), FREE),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'combine'), FREE),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'confirm_samples'), FREE),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'measurement_max_age_s'), FREE),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'dither_max_age_s'), FREE),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'cooldown_s'), FREE),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'max_per_hour'), FREE),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'card_id'), SAFETY),
    ('assignments', ('decks', '*', 'parameters', '*', 'window_guard', 'parameters', '**'), 'window_center'),
    ('assignments', ('decks', '*', 'parameters', '*', '**'), 'card_parameter'),
    # ---- shared deck definitions are never agent-editable -------------------
    ('interface-decks', ('**',), FORBIDDEN),
)

# tune.ecdl_window_center parameters: the traversal envelope is a safety range,
# the averaging/voting/timing details are free.
WINDOW_CENTER_FREE = frozenset(('settle_s', 'average_samples', 'average_max_wait_s', 'average_statistic',
                                'fast_settle_s', 'slow_observables', 'seed_ttl_s', 'vote_count',
                                'vote_window', 'position_warning_fraction', 'minimum_monotonicity',
                                'model_max_error_fraction', 'soft_quiet_s', 'max_soft_restudies_per_hour',
                                'measurement_max_age_s'))
WINDOW_CENTER_SAFETY = frozenset(('step_fraction', 'max_span_fraction', 'max_steps', 'timeout_s',
                                  'boundary_limits', 'urgent_limits', 'early_limits', 'observables',
                                  'require_single_mode', 'mode', 'position_offset_fraction',
                                  'initial_interior_search', 'controller_card_id', 'hop_threshold_hz',
                                  'temperature_tolerance'))
# card_parameter free names (everything else goes through SAFETY_NAME, then safety)
CARD_PARAMETER_FREE = frozenset(('sample_interval_s', 'settle_s', 'stable_s', 'stable_samples', 'approach_time_s',
                                 'average_samples', 'prompt', 'interval_s', 'lookback_s', 'provider', 'mode',
                                 'workspace', 'session_id', 'remote_control', 'request_timeout_s', 'agent_card_id',
                                 'webhook_env', 'message', 'title', 'datasource_uid', 'report_card_ids',
                                 'report_quantity_ids', 'reference_card_id', 'offset_range_mhz', 'strategy',
                                 'duration_s', 'target_frequency_hz', 'target_wavelength_nm', 'tec_ff_paused',
                                 'current_ff_enabled', 'tec_gain', 'save_settings', 'shape', 'sample_rate_hz',
                                 'optimize_update_interval', 'gain'))


def _match(pattern, path):
    i = 0
    for j, part in enumerate(pattern):
        if part == '**':
            if j == len(pattern) - 1:
                return True
            # '**' followed by a literal tail: match the tail at the end.
            tail = pattern[j+1:]
            return len(path) - i >= len(tail) and all(t == '*' or t == p for t, p in zip(tail, path[len(path)-len(tail):]))
        if i >= len(path) or (part != '*' and part != path[i]):
            return False
        i += 1
    return i == len(path)


def _window_center_class(key):
    if key in WINDOW_CENTER_FREE:
        return FREE
    if key in WINDOW_CENTER_SAFETY:
        return SAFETY
    return SAFETY


def classify(kind, path):
    """Return the class of one changed path (tuple of segments) for a document kind."""
    if not isinstance(path, tuple) or not path:
        return FORBIDDEN
    for rule_kind, pattern, result in RULES:
        if rule_kind == kind and _match(pattern, path):
            if result == 'window_center':
                # ('decks', i, 'parameters', card, 'window_guard', 'parameters', key, ...)
                return _window_center_class(path[6]) if len(path) > 6 else SAFETY
            if result == 'card_parameter':
                key = path[4]
                if key == 'background':return SAFETY
                # the midpoint card itself carries the same traversal envelope
                if key in WINDOW_CENTER_SAFETY:
                    return SAFETY
                if key in WINDOW_CENTER_FREE or key in CARD_PARAMETER_FREE:
                    return FREE
                return SAFETY if any(SAFETY_NAME.search(str(p)) for p in path[4:]) else FREE
            return result
    if kind == 'lasers':
        # Everything else is a control-setting leaf: a safety name, or unknown, needs approval.
        if len(path) >= 4 and path[0] == 'devices' and path[2] == 'control_settings':
            return SAFETY if any(SAFETY_NAME.search(str(p)) for p in path[3:]) else FREE
        return SAFETY
    if kind == 'runtime':
        # runtime patches of a running card: window_guard is the only guard today
        if path[0] in CARD_PARAMETER_FREE or path[0] in WINDOW_CENTER_FREE:
            return FREE
        if path[0] == 'window_guard':
            inner = path[1:]
            if not inner:
                return SAFETY
            if inner[0] == 'parameters':
                return _window_center_class(inner[1]) if len(inner) > 1 else SAFETY
            if inner[0] == 'card_id':
                return SAFETY
            return FREE
        return SAFETY if any(SAFETY_NAME.search(str(p)) for p in path) else FREE
    return SAFETY


# ---- live adoption classes for laser-definition paths ---------------------
# owner_hot  : the ECDL owner adopts it under the worker's lease at its boundary
# native_hot : the native current owner swaps policy live (policy_update)
# target_hot : the running lock card ramps its owner to the new laser target (bounded target slew)
# snapshot   : running cards keep the value read at start; next start uses it
# idle_only  : hard limits / identity: maintainer-only, needs idle (offline)
LIVE_RULES = (
    (('devices', '*', 'control_settings', 'background_adjustments', '**'), 'owner_hot'),
    # Rule M step 1 (2026-10-27): operating conditions of the ECDL rig are adopted live. The axis timing re-derives
    # the policies (max_step / slew) under prepare_limits; the sweep blocks and the compensation switch are copied
    # into the projection at the owner's safe boundary. They no longer need an instruments restart.
    (('devices', '*', 'control_settings', 'axes', '*', 'update_interval_s'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'axes', '*', 'settle_s'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'locked_sweep', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'sweep_api_defaults', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'temperature_compensation', 'enabled'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'temperature_compensation', 'minimum_interval_s'), 'owner_hot'),
    (('devices', '*', 'absolute_conditions', 'axes', '*', 'bounds', '**'), 'owner_hot'),
    (('devices', '*', 'absolute_conditions', 'axes', '*', 'activation_range', '**'), 'owner_hot'),
    (('devices', '*', 'absolute_conditions', 'axes', '*', 'max_step', '**'), 'owner_hot'),
    (('devices', '*', 'absolute_conditions', 'axes', '*', 'max_slew_per_s', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'current_bounds_a', '**'), 'native_hot'),
    (('target', 'frequency_hz'), 'target_hot'),
    (('devices', '*', 'control_settings', 'optical', 'single_mode_max', '*'), 'owner_hot'),
    # describe_changes permits only retirement; keep the routing consistent.
    (('devices', '*', 'control_settings', 'optical', 'warning_max', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'optical', 'classification_enabled'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'max_sample_age_s'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'current_safety', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'window', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'online_pid', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'entry_gate', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'supervision_interval_s'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'tec_dt_cap_s'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'target_feedforward_scale'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'target_feedforward_scale_bounds'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'target_feedforward_scale_bounds', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'target_ramp_step_hz'), 'owner_hot'),
    # target-ff design (2026-10-27): the slew, the LDC target-FF block and the TEC->current FF block are adopted by
    # the rig owner at its safe boundary (NativeLockOwner.HOT_PROFILE); a target ramp in flight defers them.
    (('devices', '*', 'control_settings', 'steady_lock', 'target_slew_hz_per_s'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'ldc_target_feedforward'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'ldc_target_feedforward', '**'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'tec_current_feedforward'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'steady_lock', 'tec_current_feedforward', '**'), 'owner_hot'),
    # dither pause (2026-10-27): rewritten in place by the rig owner at its safe boundary, also while the PI is armed
    (('devices', '*', 'control_settings', 'steady_lock', 'dither', 'paused'), 'owner_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'revision'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'activation_range_a', '**'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'activation_range_a'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'max_step_a'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'max_slew_a_per_s'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'external_modulation_step_slew_limited'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'update_interval_s'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'max_sample_age_s'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'missing_timeout_s'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'heartbeat_timeout_s'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'calibration', 'evidence'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'evidence'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'kp_count_per_nm'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'ki_count_per_nm_s'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'integral_limit_count'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'target_feedforward_scale'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'target_feedforward_scale_bounds'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'target_ramp_step_hz'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'recentre_integral_fraction'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'capture_hz'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'acceptance_hz'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'arm_enabled'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'interval_bounds_s', '**'), 'native_hot'),
    (('devices', '*', 'control_settings', 'native_current', 'native', 'interval_bounds_s'), 'native_hot'),
    # Rule M step 4 (2026-10-27): the FF sweep profile is adopted live by the native owner (policy_update with the
    # validate_profile proof; firmware_version must still equal the policy's, so that leaf is refused by the proof).
    (('devices', '*', 'control_settings', 'native_ff_sweep', '**'), 'native_hot'),
    # Rule M step 3: the CLD's operating settings are the laser system owner's second policy document, re-projected
    # to the CLD owner by the same policy_update decision path (no CLD restart). absolute_conditions stay idle_only
    # until the authority table is narrowed (step 6).
    (('devices', '*', 'control_settings', 'current', '**'), 'native_hot'),
    (('devices', '*', 'control_settings', 'temperature', '**'), 'native_hot'),
    (('devices', '*', 'control_settings', 'slow_control', '**'), 'native_hot'),
)


def live_class(kind, path):
    """How a changed laser-definition path reaches running processes."""
    if kind != 'lasers':
        return 'runtime'
    if classify(kind, path) == FORBIDDEN:
        return 'idle_only'
    for pattern, result in LIVE_RULES:
        if _match(pattern, path):
            return result
    return 'snapshot'


def changes(before, after, path=()):
    """Sorted leaf/structure differences; a missing key is reported at its own path.

    Equal-length lists are compared element-wise (index as a path segment);
    a list whose length changed is one structural change at its own path.
    """
    if isinstance(before, dict) and isinstance(after, dict):
        result = []
        for key in sorted(set(before) | set(after), key=str):
            if key not in before or key not in after:
                # An added or removed mapping is classified leaf by leaf, so a
                # new card block carrying a safety range is still a safety change.
                present = after.get(key) if key in after else before.get(key)
                if isinstance(present, dict) and present:
                    result.extend(changes(before.get(key, {}), after.get(key, {}), path + (str(key),)))
                else:
                    result.append(path + (str(key),))
            else:
                result.extend(changes(before[key], after[key], path + (str(key),)))
        return result
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after) and before != after \
            and any(isinstance(x, (dict, list)) for x in before + after):
        result = []
        for index, (x, y) in enumerate(zip(before, after)):
            result.extend(changes(x, y, path + (str(index),)))
        return result
    if type(before) in (int, float) and type(after) in (int, float):
        # JSON has one numeric type. A browser round trip from 0.0 to 0 must
        # not appear to change calibration/identity. Exact equality only: no
        # tolerance may hide a real edit, and booleans are not numbers here.
        return [] if math.isfinite(before) and math.isfinite(after) and before == after else [path]
    return [] if type(before) is type(after) and before == after else [path]


def _lookup(document, path):
    for key in path:
        if isinstance(document, dict) and key in document:
            document = document[key]
        elif isinstance(document, list) and key.isdigit() and int(key) < len(document):
            document = document[int(key)]
        else:
            return None, False
    return document, True


def describe_changes(kind, before, after):
    """Classified change records: path, class, old and new values."""
    rows = []
    for path in changes(before, after):
        old, had_old = _lookup(before, path)
        new, had_new = _lookup(after, path)
        if kind == 'lasers' and window_quality_path(path):
            # Standing quality authority covers finite thresholds, not removal
            # of the quality criterion or replacement with an arbitrary value.
            if not had_new or not _finite(new) or new <= 0:
                raise ValueError('Window quality threshold must be a positive finite number: ' + '.'.join(path))
        retired = (kind == 'lasers' and len(path) >= 5 and path[0] == 'devices' and
                   path[2:5] == ('control_settings', 'optical', 'warning_max'))
        if retired and had_new:
            raise ValueError('warning_max is retired; use the single optical quality limit')
        rows.append(dict(path='.'.join(path), **{'class': FREE if retired else classify(kind, path)},
                         old=old if had_old else None, new=new if had_new else None,
                         removed=not had_new, added=not had_old, live='owner_hot' if retired else live_class(kind, path)))
    return rows


def window_quality_path(path):
    return (len(path) == 6 and path[0] == 'devices' and
            path[2:5] == ('control_settings', 'steady_lock', 'window') and
            path[5] in ('warning_response', 'edge_response'))


def partition(rows):
    return {name: [r for r in rows if r['class'] == name] for name in CLASSES}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _check_ceiling(path, value, ceiling):
    values = value if isinstance(value, list) else [value]
    if not isinstance(ceiling, dict) or not values or any(not _finite(v) for v in values):
        raise ValueError('Ceiling for ' + path + ' applies to finite numbers only')
    lo, hi = ceiling.get('min'), ceiling.get('max')
    if 'within' in ceiling:
        lo, hi = ceiling['within']
    for v in values:
        if 'max_abs' in ceiling and abs(v) > ceiling['max_abs'] + 1e-15:
            raise ValueError(path + ' exceeds the maintainer ceiling |value| <= ' + str(ceiling['max_abs']))
        if lo is not None and v < lo - 1e-15:
            raise ValueError(path + ' is below the maintainer floor ' + str(lo))
        if hi is not None and v > hi + 1e-15:
            raise ValueError(path + ' exceeds the maintainer ceiling ' + str(hi))


def check_ceilings(kind, after, laser_document):
    """Hard limits no approval can exceed: the laser's ``agent_authority.ceilings``
    (maintainer-only section) plus built-in physical invariants.

    Ceilings are dotted paths of the *same* kind of document, for example
    ``devices.native_esp.control_settings.native_current.activation_range_a: {max_abs: 0.005}``
    or ``decks.0.parameters.steady-lock.window_guard.parameters.max_span_fraction: {max: 0.2}``.
    """
    authority = (laser_document or {}).get('agent_authority', {}) if isinstance(laser_document, dict) else {}
    if not isinstance(authority, dict):
        raise ValueError('agent_authority must be a mapping')
    ceilings = authority.get('ceilings', {}) if isinstance(authority, dict) else {}
    if not isinstance(ceilings, dict):
        raise ValueError('agent_authority.ceilings must be a mapping of dotted paths')
    for dotted, ceiling in ceilings.items():
        if not isinstance(dotted, str):
            raise ValueError('Ceiling paths must be dotted strings')
        prefix, _, rest = dotted.partition(':')
        if rest:
            if prefix != kind:
                continue
            dotted = rest
        elif kind != 'lasers':
            continue
        value, present = _lookup(after, tuple(dotted.split('.')))
        if present and value is not None:
            _check_ceiling(dotted, value, ceiling)
    if kind == 'lasers' and isinstance(after, dict):
        for name, device in (after.get('devices') or {}).items():
            native = (device.get('control_settings') or {}).get('native_current') if isinstance(device, dict) else None
            if isinstance(native, dict):
                bounds, activation = native.get('current_bounds_a'), native.get('activation_range_a')
                if isinstance(bounds, list) and isinstance(activation, list) and len(bounds) == 2 and len(activation) == 2 \
                        and all(_finite(v) for v in bounds + activation):
                    width = bounds[1] - bounds[0]
                    if not activation[0] <= 0 <= activation[1] or activation[0] < -width or activation[1] > width:
                        raise ValueError('devices.' + name + ' activation_range_a must contain zero and fit inside the absolute current bounds width')


class ApprovalError(ValueError):
    """A safety change was requested without a usable direct human approval."""


def approval_root(root, laser_id):
    """Host-only directory: outside the laser deployment root, read-only in the sandbox."""
    override = os.environ.get('LDCON_AGENT_APPROVALS_ROOT')
    root = Path(root).resolve()
    if override:
        base = Path(override)
    elif root.parent.name in ('deployments', 'lasers'):
        base = root.parent.parent / 'agent-approvals'
    else:
        base = root / 'agent-approvals'
    return base / laser_id


def _now():
    return time.time()


class ApprovalLedger:
    def __init__(self, root, laser_id, clock=_now):
        from ldcon.core.interface_schema import identifier
        identifier(laser_id)
        self.laser_id = laser_id
        self.directory = approval_root(root, laser_id)
        self.clock = clock

    # ---- storage ---------------------------------------------------------
    def _ensure(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o2770)
        (self.directory / 'proposals').mkdir(exist_ok=True, mode=0o2770)

    @contextmanager
    def _locked(self):
        self._ensure()
        with (self.directory / '.ledger.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def _append(self, name, record):
        self._ensure()
        path = self.directory / name
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(path, 0o660)
        except OSError:
            pass

    def _rows(self, name, limit=100000):
        path = self.directory / name
        if not path.is_file():
            return []
        rows = []
        with path.open('r', encoding='utf-8') as stream:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        return rows[-limit:]

    def _proposal_path(self, proposal_id):
        if not isinstance(proposal_id, str) or not re.fullmatch('[0-9a-f]{32}', proposal_id):
            raise ValueError('Invalid proposal id')
        return self.directory / 'proposals' / (proposal_id + '.json')

    def _write_proposal(self, value):
        path = self._proposal_path(value['id'])
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o660)
        temporary.replace(path)

    def proposal(self, proposal_id):
        path = self._proposal_path(proposal_id)
        if not path.is_file():
            raise ValueError('Unknown proposal')
        return json.loads(path.read_text(encoding='utf-8'))

    def proposals(self, limit=50):
        folder = self.directory / 'proposals'
        if not folder.is_dir():
            return []
        rows = []
        for path in sorted(folder.glob('*.json')):
            try:
                rows.append(json.loads(path.read_text(encoding='utf-8')))
            except ValueError:
                continue
        rows.sort(key=lambda r: r.get('created_unix_s', 0), reverse=True)
        return rows[:limit]

    # ---- prompts written by the host-side resident worker ------------------
    def record_prompt(self, event, kind):
        """Record identity and digest of one UserPromptSubmit; never its text."""
        if kind not in ('human', 'dispatch'):
            raise ValueError('Prompt kind must be human or dispatch')
        digest = event.get('prompt_sha256')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('Prompt digest required')
        record = dict(unix_s=self.clock(), kind=kind, prompt_id=event.get('prompt_id'),
                      prompt_sha256=digest, prompt_json_sha256=event.get('prompt_json_sha256'),
                      session_id=event.get('session_id'), epoch=event.get('epoch'))
        self._append('prompts.jsonl', record)
        return record

    def human_prompts(self, since_unix_s=0.):
        return [r for r in self._rows('prompts.jsonl') if r.get('kind') == 'human' and r.get('unix_s', 0) >= since_unix_s]

    # ---- proposals and approvals --------------------------------------------
    @staticmethod
    def normalize_scope(scope):
        if not isinstance(scope, dict) or scope.get('kind') not in ('lasers', 'assignments', 'runtime', 'devices'):
            raise ValueError('Scope kind must be lasers, assignments, runtime or devices')
        from ldcon.core.interface_schema import identifier
        result = dict(kind=scope['kind'])
        if scope['kind'] == 'runtime':
            job = scope.get('job_id')
            if not isinstance(job, str) or not re.fullmatch('[0-9a-f]{32}', job):
                raise ValueError('Runtime scope needs the running job id')
            result['job_id'] = job
            result['card_id'] = identifier(scope.get('card_id'))
        else:
            result['name'] = identifier(scope.get('name'))
        return result

    @staticmethod
    def normalize_changes(rows):
        if not isinstance(rows, list) or not rows or len(rows) > 64:
            raise ValueError('Expected 1–64 change records')
        result = []
        for row in rows:
            if not isinstance(row, dict) or set(row) - {'path', 'old', 'new'} or 'path' not in row or 'new' not in row:
                raise ValueError('Each change needs path, old and new')
            path = row['path']
            if not isinstance(path, str) or not path or len(path) > 512 or any(c in path for c in '\n\r\x00'):
                raise ValueError('Invalid change path')
            json.dumps(row.get('old'), allow_nan=False)
            json.dumps(row['new'], allow_nan=False)
            result.append(dict(path=path, old=row.get('old'), new=row['new']))
        result.sort(key=lambda r: r['path'])
        return result

    @staticmethod
    def _text(value, name, maximum):
        if not isinstance(value, str) or not value.strip() or len(value.encode()) > maximum or '\x00' in value:
            raise ValueError('Invalid ' + name)
        return value

    def propose(self, *, bridge_id, scope, changes, reason, classified=None):
        scope = self.normalize_scope(scope)
        changes = self.normalize_changes(changes)
        if classified is not None:
            forbidden = [r['path'] for r in classified if r['class'] == FORBIDDEN]
            if forbidden:
                raise ValueError('Maintainer-only fields cannot be approved through the agent: ' + ', '.join(forbidden))
        self._text(reason, 'reason', 4000)
        self._text(bridge_id, 'bridge id', 128)
        now = self.clock()
        value = dict(id=uuid.uuid4().hex, nonce=secrets.token_hex(3), laser_id=self.laser_id, bridge_id=bridge_id,
                     scope=scope, changes=changes, reason=reason, status='proposed',
                     created_unix_s=now, expires_unix_s=now + PROPOSAL_TTL_S,
                     human_name=None, quote=None, quote_sha256=None, prompt_id=None,
                     approved_unix_s=None, applied=[], )
        value['approval_instruction'] = (
            'Ask the human to answer in THIS conversation (not Teams, not the hub) with one message that '
            'contains the code ' + value['nonce'] + ', their name, and the decision. Example: '
            '"' + value['nonce'] + ': I, <name>, approve ' + '; '.join(
                r['path'] + ' ' + json.dumps(r['old']) + ' -> ' + json.dumps(r['new']) for r in changes[:3]) + '". '
            'Then call safety_change_approve with the exact message text as quote.')
        with self._locked():
            self._write_proposal(value)
            self._append('audit.jsonl', dict(unix_s=now, event='proposed', proposal_id=value['id'], bridge_id=bridge_id,
                                             scope=scope, changes=changes, reason=reason))
        return value

    def approve(self, proposal_id, *, quote, human_name, bridge_id):
        """Bind an approval to a prompt the human typed directly to this agent.

        The quote must be byte-identical to a prompt recorded by the host after
        the proposal was created, it must carry the proposal nonce, and it must
        not be a runtime dispatch. Relayed messages are not prompts and fail here.
        """
        quote = self._text(quote, 'quote', MAX_QUOTE_BYTES)
        human_name = self._text(human_name, 'human name', 200)
        self._text(bridge_id, 'bridge id', 128)
        now = self.clock()
        with self._locked():
            value = self.proposal(proposal_id)
            if value['status'] != 'proposed':
                raise ApprovalError('Proposal is not awaiting approval (status ' + value['status'] + ')')
            if value['bridge_id'] != bridge_id:
                raise ApprovalError('Proposal belongs to another resident')
            if now > value['expires_unix_s']:
                value['status'] = 'expired'
                self._write_proposal(value)
                raise ApprovalError('Proposal expired; propose again and ask for a fresh approval')
            if value['nonce'].lower() not in quote.lower():
                raise ApprovalError('The quoted message does not contain the proposal code ' + value['nonce'])
            if re.search(r'\b(?:not|never|do not|don\'t)\s+approve|reject|deny|却下|拒否|承認しない|不許可', quote, re.I):
                raise ApprovalError('The quoted message reads as a refusal, not an approval')
            candidates = {hashlib.sha256(quote.encode()).hexdigest(),
                          hashlib.sha256(quote.strip().encode()).hexdigest()}
            matches = [p for p in self.human_prompts(value['created_unix_s']) if p['prompt_sha256'] in candidates]
            if not matches:
                raise ApprovalError('No direct human message with this exact text was recorded in this conversation '
                                    'after the proposal. Approvals relayed through Teams/hub or paraphrased quotes do not count.')
            prompt = matches[-1]
            value.update(status='approved', human_name=human_name, quote=quote,
                         quote_sha256=prompt['prompt_sha256'], prompt_id=prompt.get('prompt_id'),
                         prompt_unix_s=prompt.get('unix_s'), approved_unix_s=now,
                         approval_expires_unix_s=now + APPROVAL_TTL_S)
            self._write_proposal(value)
            self._append('audit.jsonl', dict(unix_s=now, event='approved', proposal_id=value['id'], bridge_id=bridge_id,
                                             human_name=human_name, quote=quote, prompt_id=prompt.get('prompt_id'),
                                             prompt_unix_s=prompt.get('unix_s'), scope=value['scope'], changes=value['changes']))
        return value

    @staticmethod
    def _same(a, b):
        if _finite(a) and _finite(b):
            return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12)
        if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
            return all(ApprovalLedger._same(x, y) for x, y in zip(a, b))
        if isinstance(a, dict) and isinstance(b, dict) and set(a) == set(b):
            return all(ApprovalLedger._same(a[k], b[k]) for k in a)
        return type(a) is type(b) and a == b

    def verify(self, approval_id, *, scope, safety_rows):
        """Check that an approval covers exactly these safety changes; no state change."""
        if approval_id is None:
            raise ApprovalError('safety-class change requires approval_id from a direct human approval, or a maintainer reference')
        scope = self.normalize_scope(scope)
        value = self.proposal(approval_id)
        if value['status'] != 'approved':
            raise ApprovalError('Approval is not usable (status ' + value['status'] + ')')
        if self.clock() > value.get('approval_expires_unix_s', 0):
            raise ApprovalError('Approval expired; ask the human again')
        if value['scope'] != scope:
            raise ApprovalError('Approval scope differs from this request')
        wanted = {r['path']: r for r in safety_rows}
        approved = {r['path']: r for r in value['changes']}
        if set(wanted) != set(approved):
            raise ApprovalError('Approved paths differ from the requested safety changes: approved '
                                + ', '.join(sorted(approved)) + '; requested ' + ', '.join(sorted(wanted)))
        for path, row in wanted.items():
            if not self._same(row.get('old'), approved[path]['old']) or not self._same(row.get('new'), approved[path]['new']):
                raise ApprovalError('Approved values differ from the requested change at ' + path)
        return value

    def reserve(self, approval_id, *, decision_id, scope, safety_rows):
        """Bind a single-use approval to one durable pending transaction."""
        with self._locked():
            value = self.verify(approval_id, scope=scope, safety_rows=safety_rows)
            value['status'] = 'reserved'
            value['reserved_decision_id'] = decision_id
            self._write_proposal(value)
            self._append('audit.jsonl', dict(unix_s=self.clock(), event='reserved',
                proposal_id=approval_id, decision_id=decision_id, scope=scope))
        return value

    def abort_reserved(self, approval_id, *, decision_id, reason):
        """Record a proven non-commit without labelling the policy applied."""
        with self._locked():
            value = self.proposal(approval_id)
            if value.get('reserved_decision_id') != decision_id or value['status'] != 'reserved':
                raise ApprovalError('Approval is not reserved for this transaction')
            value['status'] = 'not_applied'
            value['transaction_reason'] = reason
            self._write_proposal(value)
            self._append('audit.jsonl', dict(unix_s=self.clock(), event='not_applied',
                proposal_id=approval_id, decision_id=decision_id, reason=reason))
        return value

    def finish_reserved(self, approval_id, *, decision_id, result):
        """Finish the exact already-authorized transaction, even after a crash."""
        with self._locked():
            value = self.proposal(approval_id)
            if value.get('reserved_decision_id') != decision_id:
                raise ApprovalError('Approval is reserved for a different transaction')
            if value['status'] == 'applied':
                return value
            if value['status'] != 'reserved':
                raise ApprovalError('Approval has no pending transaction')
            value['status'] = 'applied'
            value['applied'].append(dict(unix_s=self.clock(), result=result))
            self._write_proposal(value)
            self._append('audit.jsonl', dict(unix_s=self.clock(), event='applied',
                proposal_id=approval_id, decision_id=decision_id, result=result))
        return value

    def consume(self, approval_id, *, scope, safety_rows, result):
        """Mark an approval as applied (single use) after the trusted write succeeded."""
        with self._locked():
            value = self.verify(approval_id, scope=scope, safety_rows=safety_rows)
            value['status'] = 'applied'
            value['applied'].append(dict(unix_s=self.clock(), result=result))
            self._write_proposal(value)
            self._append('audit.jsonl', dict(unix_s=self.clock(), event='applied', proposal_id=approval_id,
                                             scope=value['scope'], changes=value['changes'], result=result,
                                             human_name=value['human_name'], quote=value['quote']))
        return value

    def audit(self, record):
        self._append('audit.jsonl', dict(unix_s=self.clock(), **record))

    def record_decision(self, *, scope, rows, decision, actor, revision_before, revision_after):
        """Durable authorization record an owner may consult (policy_update reads it)."""
        self._ensure()
        (self.directory / 'decisions').mkdir(exist_ok=True, mode=0o2770)
        value = dict(schema_version=1, id=uuid.uuid4().hex, laser_id=self.laser_id, unix_s=self.clock(), actor=actor,
                     scope=self.normalize_scope(scope), authority=decision.get('authority', 'free'),
                     approval_id=decision.get('approval_id'), human_name=decision.get('human_name'),
                     quote=decision.get('quote'), prompt_id=decision.get('prompt_id'),
                     maintainer_reference=decision.get('maintainer_reference'),
                     changes=[dict(path=r['path'], old=r.get('old'), new=r.get('new'), **{'class': r['class']}, live=r.get('live'))
                              for r in rows],
                     revision_before=revision_before, revision_after=revision_after)
        path = self.directory / 'decisions' / (value['id'] + '.json')
        temporary = path.with_name('.' + path.name + '.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=1)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o660)
        temporary.replace(path)
        return value

    def recent_audit(self, limit=50):
        return self._rows('audit.jsonl')[-limit:]


def authorize(ledger, *, kind, scope, rows, approval_id=None, maintainer_reference=None, maintainer_token=None):
    """Decide whether a classified change set may be written through the trusted path.

    Returns a record for the audit log. Raises ApprovalError/ValueError otherwise.
    ``maintainer_reference`` is the operator's own path (dashboard/CLI). When
    ``LDCON_MAINTAINER_TOKEN`` is configured for the dashboard, a maintainer
    override must also present that token; the agent sandbox never has it.
    """
    parts = partition(rows)
    record = dict(kind=kind, scope=scope, free=[r['path'] for r in parts[FREE]],
                  safety=[r['path'] for r in parts[SAFETY]], forbidden=[r['path'] for r in parts[FORBIDDEN]])
    if not parts[SAFETY] and not parts[FORBIDDEN]:
        record['authority'] = 'free'
        return record
    expected = os.environ.get('LDCON_MAINTAINER_TOKEN', '')
    if isinstance(maintainer_reference, str) and maintainer_reference.strip():
        if expected and not (isinstance(maintainer_token, str) and secrets.compare_digest(maintainer_token, expected)):
            raise ApprovalError('Maintainer override requires the configured maintainer token')
        record.update(authority='maintainer', maintainer_reference=maintainer_reference.strip()[:2000])
        return record
    if parts[FORBIDDEN]:
        raise ApprovalError('Maintainer-only fields changed (no approval can authorize them through the agent): '
                            + ', '.join(record['forbidden']))
    if ledger is None:
        raise ApprovalError('No approval ledger applies to this document; a maintainer reference is required')
    approval = ledger.verify(approval_id, scope=scope, safety_rows=parts[SAFETY])
    record.update(authority='human_approval', approval_id=approval_id, human_name=approval['human_name'],
                  quote=approval['quote'], prompt_id=approval['prompt_id'], approved_unix_s=approval['approved_unix_s'])
    return record


def rules_summary():
    """Human/agent readable table of the classification policy."""
    return dict(classes=dict(free='change without approval', safety='explicit direct human approval required',
                             forbidden='maintainer only; never through the agent'),
                live=dict(owner_hot='ECDL owner adopts under the running lease at its boundary',
                          native_hot='native current owner swaps policy live (policy_update)',
                          snapshot='running cards keep the start value; next start uses the new one',
                          idle_only='hard limits/identity: maintainer-only, offline'),
                live_rules=[dict(path='.'.join(p), live=c) for p, c in LIVE_RULES],
                window_center=dict(free=sorted(WINDOW_CENTER_FREE), safety=sorted(WINDOW_CENTER_SAFETY)),
                safety_name_pattern=SAFETY_NAME.pattern,
                rules=[dict(kind=k, path='.'.join(p), **{'class': c}) for k, p, c in RULES])
