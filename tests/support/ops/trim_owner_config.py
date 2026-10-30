"""Show (and, on request, apply) the rule M layer-1 trim of an owner config.json (steps 3-4, 2026-10-27).

    python3 ops/trim_owner_config.py /opt/ldcon/var/device-owners/esp32servocon-sr1092/config.json --dry-run
    python3 ops/trim_owner_config.py /opt/ldcon/var/device-owners/cld1015-M01343851/config.json --dry-run
    python3 ops/trim_owner_config.py <config.json> --apply      # writes <config.json>.bak-<UTC stamp> first

Native owner (esp32servocon): ``native_current`` and ``native_ff_sweep`` are layer-2 copies of the laser yaml (the
owner ignores them since step 4 and audits ``layer1_copy_ignored``); the physical block
``physical: {firmware_version, dac, adc, calibration}`` is kept (taken from the copy when absent).
CLD owner (cld1015): the ``controller`` block keeps the physical facts only (``current_limits_a`` as the device
rating, design Q1; ``modulation_role``; ``ld_enable_writes_enabled``); the operating conditions, limits and write
flags come from the laser system owner's projection. A ``projection_sources`` entry is proposed when absent: the
principal and key_env are the laser's ``connection.projection`` (sr1092: ``laser-sr1092`` / ``LDCON_PROJECTION_SR1092``);
the CLD owner's unit must then carry that key in its environment.

The tool never touches state files, journals or units; the human runs it, reviews the diff and restarts the owner.
"""
import argparse
import copy
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for candidate in (REPO / 'packages' / 'ldcon' / 'src', REPO / 'packages' / 'experiment-core' / 'src'):
    if candidate.is_dir() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

NATIVE_COPIES = ('native_current', 'native_ff_sweep')
PHYSICAL_KEYS = ('firmware_version', 'dac', 'adc', 'calibration')
CLD_LAYER1 = ('current_limits_a', 'modulation_role', 'ld_enable_writes_enabled')


def kind_of(config):
    if config.get('driver') == 'esp32servocon' and (config.get('native_current') is not None or config.get('native_assembly') is not None):
        return 'native'
    if isinstance(config.get('controller'), dict) and str((config.get('identity') or {}).get('model', '')).upper() == 'CLD1015':
        return 'cld'
    raise ValueError('Not a native (esp32servocon) or CLD1015 owner configuration')


def trim(config, *, principal=None, key_env=None):
    """(trimmed config, report) without writing anything. The report lists removed and added paths."""
    config = copy.deepcopy(config)
    kind = kind_of(config)
    removed, added, notes = [], [], []
    if kind == 'native':
        if config.get('physical') is None:
            source = config.get('native_current') or {}
            missing = [key for key in PHYSICAL_KEYS if key not in source]
            if missing:
                raise ValueError('Cannot derive the physical block: native_current lacks ' + ', '.join(missing))
            config['physical'] = {key: copy.deepcopy(source[key]) for key in PHYSICAL_KEYS}
            added.append('physical')
        for key in NATIVE_COPIES:
            if key in config:
                removed.append(key)
                del config[key]
        if config.get('native_assembly') is None:
            notes.append('native_assembly.laser_file is required: it is the layer-2 policy source at start')
        else:
            notes.append('the owner adopts control_settings.native_current / native_ff_sweep of %s at start'
                         % config['native_assembly']['laser_file'])
    else:
        controller = config.get('controller') or {}
        for key in sorted(controller):
            if key not in CLD_LAYER1:
                removed.append('controller.' + key)
                del controller[key]
        notes.append('controller.current_limits_a stays as the diode device rating (design Q1, human to confirm)')
        if not config.get('projection_sources'):
            if config.get('system_id') and principal and key_env:
                config['projection_sources'] = {principal: dict(system_id=config['system_id'], key_env=key_env, channels=[0])}
                added.append('projection_sources.' + principal)
            else:
                notes.append('projection_sources is absent: the owner derives it from system_id %r (principal laser-<slug>, key '
                             'LDCON_PROJECTION_<SLUG>, channel 0, audited at start); pass --principal/--key-env to write the explicit entry'
                             % config.get('system_id'))
    return config, dict(kind=kind, removed=removed, added=added, notes=notes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('config', type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--dry-run', action='store_true', help='print what would change (default)')
    group.add_argument('--apply', action='store_true', help='write the trimmed file after a .bak copy')
    parser.add_argument('--principal', help='CLD only: projection principal to add (e.g. laser-sr1092)')
    parser.add_argument('--key-env', help='CLD only: environment variable holding the projection key (e.g. LDCON_PROJECTION_SR1092)')
    args = parser.parse_args(argv)
    original = json.loads(args.config.read_text(encoding='utf-8'))
    trimmed, report = trim(original, principal=args.principal, key_env=args.key_env)
    print('%s owner configuration: %s' % (report['kind'], args.config))
    for path in report['removed']:
        print('  remove  %s' % path)
    for path in report['added']:
        print('  add     %s' % path)
    for note in report['notes']:
        print('  note    %s' % note)
    if not report['removed'] and not report['added']:
        print('  nothing to trim')
    if args.apply:
        if trimmed == original:
            return 0
        backup = args.config.with_name(args.config.name + '.bak-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
        backup.write_text(args.config.read_text(encoding='utf-8'), encoding='utf-8')
        args.config.write_text(json.dumps(trimmed, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
        print('  written %s (backup %s)' % (args.config, backup))
    else:
        print('  dry run: nothing written (use --apply)')
        print(json.dumps(trimmed, indent=2, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
