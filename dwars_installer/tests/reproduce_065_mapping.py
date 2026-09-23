#!/usr/bin/env python3
"""Replay the literal inventory against original 0.6.4 and current pure mapper.

No HTTP, HA credentials or live inverter access. Baseline is a trusted local
source ZIP/directory. The bundled fixture is anonymised; --diagnostic can point
to a private exported diagnosis instead, and its IDs remain in your local report.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import types
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'dwars_installer'))
import oneshot_common as current


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--diagnostic', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.baseline.is_dir():
        source = (args.baseline / 'dwars_installer/oneshot_common.py').read_bytes()
    else:
        with zipfile.ZipFile(args.baseline) as archive:
            matches = [p for p in archive.namelist() if p.endswith('/dwars_installer/oneshot_common.py')]
            if len(matches) != 1:
                raise SystemExit('Expected exactly one baseline oneshot_common.py')
            source = archive.read(matches[0])
    baseline = types.ModuleType('trusted_local_baseline')
    exec(compile(source, 'baseline_oneshot_common.py', 'exec'), baseline.__dict__)
    if baseline.VERSION != '0.6.4':
        raise SystemExit('Use original 0.6.4 for this regression reproduction')
    if args.diagnostic:
        devices = json.loads(args.diagnostic.read_text())['devices']
    else:
        devices = [json.loads((Path(__file__).parent / 'fixtures/goodwe_mapping_194_entities.json').read_text())]
    profile = {'platform': 'goodwe', 'agent_options': {}, 'control_serial': '', 'expected_inverters': 0}
    try:
        baseline.bind_device(profile, devices)
    except baseline.Blocked as err:
        error = str(err)
        if 'Meer dan één passende entiteit voor active_power_total' not in error:
            raise AssertionError('Different baseline failure: ' + error)
    else:
        raise AssertionError('The supplied data did not reproduce the 0.6.4 bug')
    device, mapping = current.bind_device(profile, devices)
    if not mapping['grid_entity'].endswith('_active_power_total') or mapping['grid_entity'].endswith('_meter_active_power_total'):
        raise AssertionError('Unexpected automatic grid binding')
    result = {
        'baseline_version': baseline.VERSION,
        'baseline_source_sha256': hashlib.sha256(source).hexdigest(),
        'current_version': current.VERSION,
        'current_source_sha256': hashlib.sha256((ROOT / 'dwars_installer/oneshot_common.py').read_bytes()).hexdigest(),
        'fixture': 'private supplied diagnosis' if args.diagnostic else 'anonymised literal inventory',
        'device_count': len(devices),
        'entity_count': len(device['entities']),
        'baseline_result': {'status': 'blocked', 'message': error},
        'current_result': {'status': 'mapped', 'mapping_count': len(mapping), 'mapping': mapping},
        'limit': 'Pure mapper replay only; does not test live HA/Supervisor, control capabilities or telemetry.',
    }
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end='')


if __name__ == '__main__':
    main()
