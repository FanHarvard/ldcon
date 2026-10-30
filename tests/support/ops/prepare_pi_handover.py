"""Reconstruct the isolated firmware candidate and prepare a coupled release bundle.

No device connections, systemd calls or flash commands. Build ldcon separately
from a reviewed commit with ops/build_release.py; never wheel this dirty tree.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'firmware/pi-bumpless-20261028'
SUITES = ('pid', 'pid_antiwindup', 'modulation_atomic', 'modulation_command',
          'analog_io', 'capabilities', 'telemetry_queue', 'logger_http', 'step_test', 'pi_handover')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest_files(path):
    files = json.loads(path.read_text())['files']
    if any(Path(k).is_absolute() or '..' in Path(k).parts for k in files):
        raise ValueError('Unsafe firmware manifest path')
    return files


def verify(root, files):
    for name, digest in files.items():
        if sha(root / name) != digest:
            raise ValueError('Firmware source changed: ' + name)


def prepare(source, destination, test=False, build=False, idf_export=None):
    baseline = manifest_files(PACKAGE / 'firmware-baseline-sha256.json')
    candidate = manifest_files(PACKAGE / 'firmware-candidate-sha256.json')
    verify(source, baseline)
    destination.mkdir(parents=True, exist_ok=False)
    tree = destination / 'firmware'
    for name in baseline:
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, target)
    patch = PACKAGE / 'firmware-candidate.patch'
    env=dict(os.environ,GIT_CEILING_DIRECTORIES=str(tree.parent))
    for key in ('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE'):env.pop(key,None)
    subprocess.run(['git','apply','--no-index',str(patch)],cwd=tree,env=env,check=True)
    verify(tree, candidate)
    if build and not test:
        raise ValueError('Building requires --test')
    if test:
        with (destination / 'firmware-tests.log').open('w') as log:
            for suite in SUITES:
                print('TEST', suite, file=log, flush=True)
                argv = [sys.executable, str(tree / ('cli/test/' + suite + '_reference.py'))]
                if suite == 'pi_handover':argv.append(str(destination / 'conformance.json'))
                subprocess.run(argv, cwd=tree, stdout=log, stderr=subprocess.STDOUT, check=True)
    if build:
        if idf_export is None or not idf_export.is_file():
            raise ValueError('Explicit installed ESP-IDF export.sh required')
        with (destination / 'firmware-build.log').open('w') as log:
            # Arguments are positional parameters, never interpolated shell code.
            subprocess.run(['bash', '-c', 'source "$1" && idf.py -B build-offline build',
                            'idf-build', str(idf_export)], cwd=tree, check=True,
                           stdout=log, stderr=subprocess.STDOUT)
    receipt = dict(prepared=True, tests_passed=bool(test), built=bool(build),
                   source=str(source), tree=str(tree), patch_sha256=sha(patch), hardware_operations=False)
    (destination / 'source-preparation.json').write_text(json.dumps(receipt, indent=2)+'\n')
    return receipt


def bundle(build_tree, destination, release=None):
    verify(build_tree, manifest_files(PACKAGE / 'firmware-candidate-sha256.json'))
    evidence = json.loads((PACKAGE / 'firmware-evidence.json').read_text())
    images = {'app': 'esp32servocon.bin', 'mode_reset': 'mode_reset.bin',
              'partition_table': 'partition_table/partition-table.bin'}
    destination.mkdir(parents=True, exist_ok=False)
    artifacts = {}
    for role, name in images.items():
        image = build_tree / 'build-offline' / name
        expected = evidence['artifacts']['firmware/build-offline/'+name]['sha256']
        # This versioned bundle is deliberately bound to the tested image.
        if sha(image) != expected:raise ValueError('Rebuilt image differs; review and re-freeze evidence: '+name)
        copy = destination / (role+'.bin');shutil.copy2(image, copy)
        artifacts[role] = dict(path=copy.name, sha256=sha(copy))
    inputs = json.loads((PACKAGE/'host-update-inputs.json').read_text())
    files = inputs['configuration']
    members = {}
    for name in inputs['wheel_members']:
        require_relative = Path(name)
        if require_relative.is_absolute() or '..' in require_relative.parts:
            raise ValueError('Unsafe host source path')
        package = 'experiment-core' if name.startswith('experiment_core/') else 'ldcon'
        members[name]=sha(ROOT/'packages'/package/'src'/name)
    for name in ('prepare_pi_handover.py','update_pi_handover.py','promote.py'):
        shutil.copy2(ROOT/'ops'/name,destination/name)
    document = dict(schema_version=1,laser_id='sr422',firmware_version=evidence['firmware_version'],
        source_firmware_version='2026.10.23-telemetry-batch',
        source_app=dict(bytes=804304,sha256='ba0e8d332fedc73bc5caad84d09543b323946a2b4277cb5668b0391324ce6d52'),
        device_host='192.168.1.9',
        device_mac='ec:c9:ff:b8:bc:20',
        serial_port='/dev/serial/by-path/pci-0000:0d:00.0-usb-0:6.3:1.1-port0',
        esptool_python='/home/kshu/.espressif/python_env/idf5.5_py3.12_env/bin/python',
        artifacts=artifacts,release=None if release is None else str(release.resolve()),
        required_wheel_members=members,configuration=files,resume_cards=False,
        tool_hashes={n:sha(destination/n) for n in ('update_pi_handover.py','promote.py')})
    (destination/'bundle.json').write_text(json.dumps(document,indent=2)+'\n')
    return dict(bundle=str(destination/'bundle.json'),sha256=sha(destination/'bundle.json'),
                hardware_operations=False,release_bound=release is not None)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('source');p.add_argument('--baseline',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--test',action='store_true')
    p.add_argument('--build',action='store_true');p.add_argument('--idf-export',type=Path)
    p=sub.add_parser('bundle');p.add_argument('--firmware-tree',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--release',type=Path)
    a=parser.parse_args()
    result=(prepare(a.baseline.resolve(),a.output.resolve(),a.test,a.build,a.idf_export)
            if a.action=='source' else bundle(a.firmware_tree.resolve(),a.output.resolve(),a.release))
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
