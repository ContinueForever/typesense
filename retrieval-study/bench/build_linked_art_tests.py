#!/usr/bin/env python3
"""Link ART regressions against the completed server's real production objects.

Reuses every production link input except the server main, and compiles tests
with the recorded ART compile flags. This is a focused integration binary, not
the whole upstream C++ suite. No filtering implementation is substituted.
"""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study
from bench.build_server import dependency_state, state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--server-build', required=True)
    parser.add_argument('--compile-action', required=True)
    parser.add_argument('--variant', required=True)
    parser.add_argument('--source', default=str(ROOT.parent / 'typesense'))
    args = parser.parse_args()
    if not args.variant.replace('-', '').isalnum():
        parser.error('variant must contain only letters, digits and hyphens')
    source = Path(args.source).resolve()
    build = Path(args.server_build).resolve()
    manifest = json.loads((build / 'manifest.json').read_text())
    before = state(source)
    if manifest['status'] != 'success' or not manifest['provenance_valid']:
        raise ValueError('A completed, provenance-checked server build is required')
    if before != manifest['source_after'] or dependency_state() != manifest['build_compatibility_after']:
        raise ValueError('Production objects must still correspond to the recorded source and dependency state')
    if study.sha256(build / 'typesense-server') != manifest['binary_sha256']:
        raise ValueError('Recorded server binary hash differs')
    action = json.loads(Path(args.compile_action).read_text())['actions']
    if len(action) != 1 or action[0]['mnemonic'] != 'CppCompile':
        raise ValueError('Expected one recorded ART compile action')
    command = action[0]['arguments']
    if command[command.index('-c') + 1] != 'src/art.cpp':
        raise ValueError('Compile action must be the server ART object')
    compiler, flags = command[0], command[1:]
    flags = flags[:]
    for option in ['-c', '-o', '-MF']:
        position = flags.index(option)
        del flags[position:position + 2]
    flags = [f for f in flags if f != '-MD' and not f.startswith('-frandom-seed=')]
    output = ROOT / 'builds' / args.variant
    output.mkdir(parents=True, exist_ok=False)
    externals = list((ROOT / 'builds/bazel').glob('*/execroot/__main__'))
    if len(externals) != 1:
        raise ValueError('Expected the same pinned Bazel execution root')
    execution_root = externals[0]
    params = execution_root / 'bazel-out/k8-fastbuild/bin/typesense-server-2.params'
    original_params = params.read_text()
    (output / 'server-link-original.params').write_text(original_params)
    link = shlex.split(original_params)
    mains = [arg for arg in link if arg.endswith('/typesense_server.pic.o')]
    if len(mains) != 1:
        raise ValueError('Expected exactly one production server main object')
    position = link.index(mains[0])
    link.remove(mains[0])
    gtest = ROOT / 'builds/test-deps/googletest/googletest'
    include = ['-I' + str(gtest), '-I' + str(gtest / 'include'),
               '-DROOT_DIR="' + str(ROOT / 'builds/test-resources') + '/"']
    inputs = [source / 'test/art_test.cpp', ROOT / 'patches/art_traversal_cutoff_test.cpp',
              gtest / 'src/gtest-all.cc', source / 'test/main.cpp', source / 'test/test_tmp_path.cpp']
    commands, objects = [], []
    metadata = dict(source_before=before, server_manifest_sha256=study.sha256(build / 'manifest.json'),
        production_server_sha256=manifest['binary_sha256'],
        compile_action_sha256=study.sha256(Path(args.compile_action)),
        source_hashes={str(p): study.sha256(p) for p in inputs},
        production_link_inputs={arg: study.sha256(execution_root / arg) for arg in link
            if arg.endswith(('.o', '.a', '.so')) and (execution_root / arg).is_file()},
        scope='Focused ART tests with real production objects and filter iterator; not the entire C++ suite.')
    metadata.update(status='running', commands=commands)
    study.save_json(output / 'manifest.json', metadata)
    env = dict(__import__('os').environ)
    env['PATH'] = str(ROOT / 'tools/bin') + ':/usr/local/bin:/usr/bin:/bin'
    with (output / 'build.log').open('w') as log:
        def run(argv):
            commands.append(argv)
            result = subprocess.run(argv, cwd=execution_root, env=env, stdout=log, stderr=subprocess.STDOUT)
            if result.returncode:
                metadata.update(status='failed', exit_code=result.returncode)
                study.save_json(output / 'manifest.json', metadata)
                print('Integration test build failed; see ' + str(output / 'build.log'), file=sys.stderr)
                raise SystemExit(result.returncode)
        for path in inputs:
            obj = output / (path.stem + '.o')
            run([compiler, *flags, *include, '-c', str(path), '-o', str(obj)])
            objects.append(str(obj))
        link[position:position] = objects
        binary = output / 'art-integration-tests'
        link[link.index('-o') + 1] = str(binary)
        run([compiler, *link])
    metadata.update(status='success', commands=commands, source_after=state(source), binary_sha256=study.sha256(binary),
                    build_compatibility=dependency_state())
    metadata['provenance_valid'] = (metadata['source_after'] == before and
                                  metadata['build_compatibility'] == manifest['build_compatibility_after'])
    study.save_json(output / 'manifest.json', metadata)
    if not metadata['provenance_valid']:
        raise RuntimeError('Source changed while linking integration tests')
    print(binary)


if __name__ == '__main__':
    main()
