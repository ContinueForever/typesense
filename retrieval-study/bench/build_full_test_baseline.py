#!/usr/bin/env python3
"""Relink the complete C++ suite with pinned original ART and identical inputs.

Only ART is projected from git. All other engine and test objects, shared
libraries and compiler flags are reused from the completed candidate test build.
The new regression tests remain present and can be excluded explicitly when
running the original upstream suite. The live source checkout is never changed.
"""
import argparse
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study
from bench.build_server import state, dependency_state
from bench.validate_extended import verify_binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate-build', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--source', default=str(ROOT.parent / 'typesense'))
    args = parser.parse_args()
    if not args.label.replace('-', '').replace('_', '').isalnum():
        parser.error('Invalid label')
    candidate = Path(args.candidate_build).resolve()
    manifest = verify_binary(candidate / 'typesense-test', candidate / 'manifest.json')
    source = Path(args.source).resolve()
    before = state(source)
    if before != manifest['source_after']:
        raise RuntimeError('Current source differs from completed test build')
    out = ROOT / 'builds' / args.label
    out.mkdir(parents=True, exist_ok=False)
    command = manifest['command'][:]
    position = command.index('build')
    command[position:position + 2] = ['aquery', 'mnemonic("CppCompile", //:typesense-test)']
    command.extend(['--output=jsonproto', '--include_commandline'])
    env = dict(os.environ, PATH=str(ROOT / 'tools/bin') + ':/usr/local/bin:/usr/bin:/bin')
    with (out / 'compile-actions.json').open('w') as output, (out / 'query.log').open('w') as log:
        subprocess.run(command, cwd=source, env=env, stdout=output, stderr=log, check=True)
    actions = json.loads((out / 'compile-actions.json').read_text())['actions']
    matches = [action for action in actions if 'src/art.cpp' in action.get('arguments', [])]
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one ART compilation action')
    compile_command = matches[0]['arguments'][:]
    if '-DTEST_BUILD' not in compile_command or '-O0' not in compile_command:
        raise RuntimeError('Expected the original test build configuration')
    original = out / 'art-original.cpp'
    original.write_bytes(subprocess.check_output(['git', 'show', study.UPSTREAM_SHA + ':src/art.cpp'], cwd=source))
    obj = out / 'art-original.pic.o'
    compile_command[compile_command.index('-c') + 1] = str(original)
    compile_command[compile_command.index('-o') + 1] = str(obj)
    if '-MF' in compile_command:
        compile_command[compile_command.index('-MF') + 1] = str(out / 'art-original.pic.d')
    execution_root = (source / 'bazel-typesense').resolve()
    params = execution_root / 'bazel-out/k8-fastbuild/bin/typesense-test-2.params'
    link = shlex.split(params.read_text())
    (out / 'candidate-link.params').write_text(params.read_text())
    arts = [arg for arg in link if arg.endswith('/typesense-test/art.pic.o')]
    if len(arts) != 1:
        raise RuntimeError('Expected exactly one ART input in the full-suite link')
    hashes = {arg: study.sha256(execution_root / arg) for arg in link
              if arg.endswith(('.o', '.a', '.so')) and (execution_root / arg).is_file()}
    link[link.index(arts[0])] = str(obj)
    binary = out / 'typesense-test'
    link[link.index('-o') + 1] = str(binary)
    with (out / 'build.log').open('w') as log:
        subprocess.run(compile_command, cwd=execution_root, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        subprocess.run([compile_command[0], *link], cwd=execution_root, env=env,
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    after = state(source)
    dependencies = dependency_state()
    unchanged = (after == before and dependencies == manifest['build_compatibility_after'] and
        all(study.sha256(execution_root / path) == digest for path, digest in hashes.items()))
    result = dict(status='success' if unchanged else 'failed', provenance_valid=unchanged,
        target='typesense-test', binary_sha256=study.sha256(binary),
        source_before=before, source_after=after, source_mode='Pinned original ART projected; all other complete-suite inputs reused.',
        original_art_sha256=study.sha256(original), candidate_art_object_sha256=hashes[arts[0]],
        original_art_object_sha256=study.sha256(obj), candidate_build_manifest_sha256=study.sha256(candidate / 'manifest.json'),
        reused_link_input_hashes=hashes, replaced_link_input=arts[0],
        commands=[command, compile_command, [compile_command[0], *link]],
        build_compatibility_after=dependencies, contains_new_regression_tests=True,
        note='Use --gtest_filter=-ArtTraversalCutoffTest.* for the entire original upstream suite. This is a controlled relink, not a second pristine bazel build.')
    study.save_json(out / 'manifest.json', result)
    if not unchanged:
        raise RuntimeError('Inputs changed during original ART compile/relink')
    print(binary)


if __name__ == '__main__':
    main()
