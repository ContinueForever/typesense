#!/usr/bin/env python3
"""Build the upstream server with the project-local CI-compatible toolchain.

Keeps the exact command, before/after source state, log, status and binary hash.
No system package installation and no automatic source modification.
"""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def state(source):
    return {"head": subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=source, text=True).strip(),
            "status": subprocess.check_output(['git', 'status', '--porcelain'], cwd=source, text=True),
            "diff": subprocess.check_output(['git', 'diff', 'HEAD', '--'], cwd=source, text=True)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--label', required=True)
    parser.add_argument('--source', default=str(ROOT.parent / 'typesense'))
    parser.add_argument('--baseline', action='store_true', help='Require a clean locked upstream checkout')
    parser.add_argument('--jobs', type=study.positive, default=1)
    args = parser.parse_args()
    if not args.label.replace('-', '').replace('_', '').isalnum():
        parser.error('label must contain only letters, digits, hyphens and underscores')
    source = Path(args.source).resolve()
    before = state(source)
    if args.baseline and (before['head'] != study.UPSTREAM_SHA or before['status']):
        parser.error('baseline requires the exact clean upstream commit ' + study.UPSTREAM_SHA)
    output = ROOT / 'builds' / args.label
    output.mkdir(parents=True, exist_ok=False)
    tools = ROOT / 'tools'
    cc = tools / 'gcc10/usr/bin/gcc-10'
    cxx = tools / 'gcc10/usr/bin/g++-10'
    path = str(tools / 'bin') + ':/usr/local/bin:/usr/bin:/bin'
    command = [str(tools / 'bazel-6.2.0'), '--output_user_root=' + str(ROOT / 'builds/bazel'),
               '--host_jvm_args=-Xmx512m', 'build', '//:typesense-server', '--jobs=' + str(args.jobs),
               '--local_ram_resources=1536', '--repository_cache=' + str(ROOT / 'builds/repository-cache'),
               '--repo_env=CC=' + str(cc), '--repo_env=CXX=' + str(cxx), '--action_env=PATH=' + path,
               '--copt=-B' + str(tools / 'bin'), '--curses=no', '--color=no']
    log_path = ROOT / 'raw' / (args.label + '-build.log')
    metadata = {"started_at_utc": study.utc_now(), "command": command, "source_before": before,
                "compiler": study.checked_output(str(cxx), '--version'),
                "environment": study.environment(source), "status": "running",
                "log": str(log_path),
                "nested_parallelism": (tools / 'bin/nproc').read_text()}
    dependency_cache = ROOT / 'manifests/onnx-download-cache.json'
    if dependency_cache.exists():
        import json
        metadata['dependency_download_cache'] = json.loads(dependency_cache.read_text())
    study.save_json(output / 'manifest.json', metadata)
    env = dict(os.environ, CC=str(cc), CXX=str(cxx), PATH=path)
    try:
        with log_path.open('w') as log:
            result = subprocess.run(command, cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT)
        metadata['exit_code'] = result.returncode
        metadata['source_after'] = state(source)
        metadata['provenance_valid'] = metadata['source_after'] == before
        metadata['status'] = 'success' if result.returncode == 0 and metadata['provenance_valid'] else 'failed'
        if metadata['status'] == 'success':
            binary = output / 'typesense-server'
            shutil.copy2(source / 'bazel-bin/typesense-server', binary)
            metadata['binary_sha256'] = study.sha256(binary)
    except KeyboardInterrupt:
        metadata['status'] = 'interrupted'
        metadata['exit_code'] = 130
    except Exception as error:
        metadata['status'] = 'failed'
        metadata['exit_code'] = 1
        metadata['error'] = str(error)
    finally:
        metadata['finished_at_utc'] = study.utc_now()
        metadata['log'] = str(log_path)
        study.save_json(output / 'manifest.json', metadata)
    print(output)
    return 0 if metadata['status'] == 'success' else 1


if __name__ == '__main__':
    raise SystemExit(main())

