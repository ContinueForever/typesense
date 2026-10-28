#!/usr/bin/env python3
"""Cache ONNX's pinned archives with system curl, retaining upstream hash checks.

Only the local dependency checkout's download locations change. Dependency
versions, hashes and C++ source are unchanged; use the same cache for both builds.
"""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def main():
    externals = list((ROOT / 'builds/bazel').glob('*/external/onnx_runtime'))
    if len(externals) != 1:
        raise RuntimeError('Expected the pinned ONNX dependency checkout')
    deps = externals[0] / 'cmake/deps.txt'
    original_path = ROOT / 'manifests/onnx-deps-original.txt'
    if original_path.exists():
        original = original_path.read_text()
    else:
        original = deps.read_text()
        if 'abseil_cpp;https://github.com/' not in original:
            raise ValueError('Expected unmodified upstream dependency URLs')
        original_path.write_text(original)
    cache = ROOT / 'builds/onnx-archives'
    cache.mkdir(exist_ok=True)
    entries = []
    for line in original.splitlines():
        if line and not line.startswith('#'):
            name, url, expected_sha1 = line.split(';')
            if url.startswith('https://github.com/'):
                entries.append(dict(name=name, url=url, sha1=expected_sha1,
                                    path=str(cache / (name + '-' + expected_sha1 + '.zip'))))
    def fetch(entry):
        path = Path(entry['path'])
        def valid():
            with path.open('rb') as stream:
                return hashlib.file_digest(stream, 'sha1').hexdigest() == entry['sha1']
        if not path.exists() or not valid():
            temporary = path.with_suffix('.part')
            command = ['curl', '--fail', '--silent', '--show-error', '--location', '--retry', '3',
                       '--connect-timeout', '15', '--max-time', '180', entry['url'], '--output', str(temporary)]
            result = subprocess.run(command, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(entry['name'] + ': ' + result.stderr)
            with temporary.open('rb') as stream:
                actual = hashlib.file_digest(stream, 'sha1').hexdigest()
            if actual != entry['sha1']:
                raise ValueError(entry['name'] + ': archive differs from the pinned upstream hash')
            temporary.replace(path)
        return dict(entry, sha256=study.sha256(path), bytes=path.stat().st_size)
    verified = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as workers:
        for entry in workers.map(fetch, entries):
            verified.append(entry)
            print(entry['name'] + ' verified', flush=True)
    replacements = {e['url']: e['path'] for e in verified}
    rewritten = original
    for url, local in replacements.items():
        rewritten = rewritten.replace(url, local)
    deps.write_text(rewritten)
    study.save_json(ROOT / 'manifests/onnx-download-cache.json', dict(
        purpose='Local download transport only; original pinned hashes remain enforced by CMake.',
        original_file_sha256=study.sha256(original_path), dependency_file=str(deps),
        rewritten_file_sha256=study.sha256(deps), archives=verified))
    print('Pinned dependency URLs now resolve to verified local archives.')


if __name__ == '__main__':
    main()
