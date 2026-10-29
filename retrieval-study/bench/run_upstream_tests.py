#!/usr/bin/env python3
"""Run the complete upstream binary with isolated working files and XML output.

Runfiles are staged without adding resource archives to the upstream checkout.
Models may be downloaded by upstream tests into their documented /tmp cache.
No test filter is applied unless explicitly requested and recorded.
"""
import argparse
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study
from bench.validate_extended import verify_binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True)
    parser.add_argument('--build-manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--source', default=str(ROOT.parent / 'typesense'))
    parser.add_argument('--filter', default='*')
    parser.add_argument('--timeout', type=study.positive, default=7200)
    args = parser.parse_args()
    manifest = verify_binary(args.binary, args.build_manifest)
    if manifest['target'] != 'typesense-test':
        parser.error('Requires the real complete upstream typesense-test binary')
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    workspace = ROOT / 'builds/test-runtime' / output.name
    workspace.mkdir(parents=True, exist_ok=False)
    source = Path(args.source).resolve()
    for file in (source / 'test').rglob('*'):
        if file.is_file():
            dest = workspace / file.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.symlink_to(file)
    resource = ROOT / 'tools/test-resources/tyrec-1-models.tar.gz'
    resource_manifest = json.loads((ROOT / 'manifests/upstream-test-resource.json').read_text())
    if study.sha256(resource) != resource_manifest['sha256']:
        raise RuntimeError('Model fixture archive differs from recorded hash')
    (workspace / 'test/resources/models.tar.gz').symlink_to(resource)
    (workspace / 'external').symlink_to(source / 'bazel-typesense/external', target_is_directory=True)
    # bazel-typesense is normally a symlink to the execroot. Resolve the actual
    # output base rather than depending on the checkout's directory basename.
    external = workspace / 'external'
    if not external.exists():
        external.unlink()
        external.symlink_to(source / 'bazel-out/../../../external', target_is_directory=True)
    if not external.exists():
        raise RuntimeError('Cannot locate Bazel external runfiles')
    temp = workspace / 'tmp'
    temp.mkdir()
    env = dict(os.environ, TEST_TMPDIR=str(temp))
    for name in list(env):
        if name.startswith('GTEST_') or name in ('TEST_SHARD_INDEX', 'TEST_TOTAL_SHARDS', 'TEST_SHARD_STATUS_FILE'):
            env.pop(name, None)
    binary = str(Path(args.binary).resolve())
    # cc_test defaults to shared dependencies. Preserve the generated relative
    # library layout when running the hash-verified archived executable.
    solib = source / 'bazel-bin/_solib_k8'
    archived_solib = Path(binary).parent / '_solib_k8'
    if solib.exists() and not archived_solib.exists():
        archived_solib.symlink_to(solib.resolve(), target_is_directory=True)
    runtime_libraries = {str(path.relative_to(solib)): study.sha256(path)
                         for path in solib.rglob('*') if path.is_file()} if solib.exists() else {}
    study.save_json(output / 'runtime-libraries.json', runtime_libraries)
    command = [binary, '--gtest_filter=' + args.filter, '--gtest_output=xml:' + str(output / 'results.xml')]
    listing = subprocess.run([binary, '--gtest_list_tests'], cwd=workspace, env=env,
                             capture_output=True, text=True, check=True)
    (output / 'test-list.txt').write_text(listing.stdout)
    study.save_json(output / 'run.json', dict(started_at_utc=study.utc_now(), command=command,
        workspace=str(workspace), temp=str(temp), timeout_seconds=args.timeout,
        filter=args.filter, model_resource=resource_manifest, build_binary_sha256=manifest['binary_sha256'],
        note='Unsharded upstream binary, upstream disabled tests stay disabled; no sanitizer flags added.'))
    study.save_json(output / 'build.json', manifest)
    started = time.monotonic()
    with (output / 'tests.log').open('w') as log:
        try:
            result = subprocess.run(command, cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT,
                                    timeout=args.timeout)
            exit_code, timed_out = result.returncode, False
        except subprocess.TimeoutExpired:
            exit_code, timed_out = None, True
    summary = dict(exit_code=exit_code, timed_out=timed_out, elapsed_seconds=time.monotonic() - started,
                   filter=args.filter, passed=False,
                   child_peak_rss_kib=resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss)
    after_libraries = {str(path.relative_to(solib)): study.sha256(path)
                       for path in solib.rglob('*') if path.is_file()} if solib.exists() else {}
    summary['runtime_libraries_unchanged'] = runtime_libraries == after_libraries
    if (output / 'results.xml').exists():
        root = ET.parse(output / 'results.xml').getroot()
        summary['xml'] = root.attrib
        summary['failures'] = [dict(suite=suite.get('name'), name=case.get('name'),
            details=[node.get('message', '') for node in case.findall('failure')])
            for suite in root.findall('testsuite') for case in suite.findall('testcase') if case.findall('failure')]
        summary['passed'] = (exit_code == 0 and not timed_out and summary['runtime_libraries_unchanged'] and int(root.get('failures', '0')) == 0
                             and int(root.get('errors', '0')) == 0)
    study.save_json(output / 'summary.json', summary)
    print(study.canonical({key: value for key, value in summary.items() if key != 'failures'}))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
