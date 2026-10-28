#!/usr/bin/env python3
"""Compile real upstream ART/posting objects, optionally adding study-only counters.

Unused service and lazy-filter entry points are removed by the linker. There are
no replacement ART, posting, or filtering implementations in this probe.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def observe(source):
    start = source.index('int art_topk_iter(')
    end = source.index('// Recursively iterates over the tree', start)
    topk = source[start:end]
    if topk.count('q.pop();') != 2:
        raise ValueError('Instrumentation no longer matches the two locked top-k loops')
    topk = re.sub(r'q\.push\(([^;]+)\);',
        r'q.push(\1); ++art_probe_current.pushes; art_probe_current.peak_queue = std::max<uint64_t>(art_probe_current.peak_queue, q.size());', topk)
    topk = topk.replace('q.pop();', 'q.pop(); ++art_probe_current.popped;\n'
        '        if (n) { if (IS_LEAF(n)) ++art_probe_current.leaves; else ++art_probe_current.internal_nodes; }')
    topk = topk.replace('validate_and_add_leaf(l,', 'const bool study_added = validate_and_add_leaf(l,')
    topk = topk.replace('exact_leaf, results);', 'exact_leaf, results);\n'
        '            if (study_added) ++art_probe_current.accepted; else ++art_probe_current.rejected;')
    topk = topk.replace('exclude_leaves, results);', 'exclude_leaves, results);\n'
        '            if (study_added) ++art_probe_current.accepted; else ++art_probe_current.rejected;')
    topk = topk.replace('filter_result_iterator->reset();', '++art_probe_current.resets; filter_result_iterator->reset();')
    topk = topk.replace('std::chrono::system_clock::now()',
        '(++art_probe_current.clock_checks, std::chrono::system_clock::now())')
    source = source[:start] + topk + source[end:]
    source = source.replace('if(!posting_t::contains_atleast_one(leaf->values, allowed_doc_ids,',
        '++art_probe_current.filter_checks;\n        if(!posting_t::contains_atleast_one(leaf->values, allowed_doc_ids,')
    return '#include "art_metrics.h"\n' + source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', default=str(ROOT.parent / 'typesense'))
    parser.add_argument('--variant', required=True)
    parser.add_argument('--ref', help='Read art.cpp from a fixed Git ref; otherwise use working tree')
    parser.add_argument('--art-source', help='Candidate art.cpp without modifying a baseline build in progress')
    parser.add_argument('--observe', action='store_true')
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--sanitizers', default='address,undefined', choices=['address', 'address,undefined'])
    parser.add_argument('--cc', default='gcc')
    parser.add_argument('--cxx', default='g++')
    parser.add_argument('--static-sanitizers', action='store_true', help='Link the selected compiler sanitizer runtimes statically')
    parser.add_argument('--pic', action='store_true', help='Build position-independent diagnostic objects')
    parser.add_argument('--profile', action='store_true')
    parser.add_argument('--upstream-tests', action='store_true')
    args = parser.parse_args()
    source = Path(args.source).resolve()
    output = ROOT / 'builds' / args.variant
    output.mkdir(parents=True, exist_ok=True)
    # Dependencies were fetched at the upstream WORKSPACE's pinned versions.
    externals = list((ROOT / 'builds' / 'bazel').glob('*/external'))
    if len(externals) != 1:
        raise ValueError('Run the pinned Bazel build first to fetch its dependency headers')
    ext = externals[0]
    config = output / 'generated' / 'butil'
    config.mkdir(parents=True, exist_ok=True)
    (config / 'config.h').write_text('// From the upstream brpc config_h genrule.\n'
        '#ifndef BUTIL_CONFIG_H\n#define BUTIL_CONFIG_H\n#ifdef BRPC_WITH_GLOG\n#undef BRPC_WITH_GLOG\n#endif\n'
        '#define BRPC_WITH_GLOG 1\n#endif\n')
    code = subprocess.check_output(['git', 'show', args.ref + ':src/art.cpp'], cwd=source).decode() if args.ref else (source / 'src/art.cpp').read_text()
    if args.art_source:
        code = Path(args.art_source).read_text()
    upstream_code_hash = hashlib.sha256(code.encode()).hexdigest()
    (output / 'art.source.cpp').write_text(observe(code) if args.observe else code)
    gtest = ROOT / 'builds/test-deps/googletest/googletest'
    includes = [source / 'include', ROOT / 'bench', output / 'generated', ROOT / 'builds/core-glog',
                ext / 'com_github_google_glog/src', ext / 'for', ext / 'rocksdb/include',
                ext / 'com_github_brpc_brpc/src', ext / 'icu/icu4c/source/common']
    if args.upstream_tests:
        includes += [gtest, gtest / 'include']
    flags = ['-std=c++17', '-O2', '-g', '-Wno-deprecated-declarations', '-ffunction-sections', '-fdata-sections']
    if args.pic:
        flags += ['-fPIC']
    if args.sanitize:
        flags += ['-fsanitize=' + args.sanitizers, '-fno-omit-frame-pointer']
        if args.static_sanitizers:
            flags += ['-static-libasan']
            if 'undefined' in args.sanitizers:
                flags += ['-static-libubsan']
    if args.profile:
        flags += ['-pg']
    if args.upstream_tests:
        flags += ['-DART_PROBE_ARRAY_ONLY', '-DROOT_DIR="' + str(ROOT / 'builds/test-resources') + '/"']
    include_flags = [part for path in includes for part in ['-I', str(path)]]
    commands, objects, source_hashes = [], [], {}
    def run(command, log):
        commands.append(command)
        log.write(('\n' + ' '.join(command) + '\n').encode()); log.flush()
        subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True)
    with (output / 'build.log').open('wb') as log:
        paths = [output / 'art.source.cpp']
        for name in ['posting', 'posting_list', 'array', 'sorted_array', 'array_base', 'array_utils', 'thread_local_vars']:
            path = source / 'src' / (name + '.cpp')
            if args.ref:
                content = subprocess.check_output(['git', 'show', args.ref + ':src/' + name + '.cpp'], cwd=source)
                path = output / (name + '.cpp')
                path.write_bytes(content)
            paths.append(path)
        if args.upstream_tests:
            paths += [source / 'test/art_test.cpp', ROOT / 'patches/art_traversal_cutoff_test.cpp',
                      gtest / 'src/gtest-all.cc', gtest / 'src/gtest_main.cc']
        else:
            paths.append(ROOT / 'bench/art_probe.cpp')
        for path in paths:
            obj = output / (path.stem + '.o')
            source_hashes[str(path)] = study.sha256(path)
            run([args.cxx, *flags, *include_flags, '-c', str(path), '-o', str(obj)], log)
            objects.append(str(obj))
        obj = output / 'for.o'
        cflags = ['-std=gnu17', '-O2', '-g', '-ffunction-sections', '-fdata-sections']
        if args.pic:
            cflags += ['-fPIC']
        if args.sanitize:
            cflags += ['-fsanitize=' + args.sanitizers, '-fno-omit-frame-pointer']
        if args.profile:
            cflags += ['-pg']
        run([args.cc, *cflags,
             '-c', str(ext / 'for/for.c'), '-o', str(obj)], log)
        objects.append(str(obj))
        binary = output / ('art-tests' if args.upstream_tests else 'art-probe')
        run([args.cxx, *flags, '-Wl,--gc-sections', *objects, '-L', str(ROOT / 'builds/core-glog'),
             '-Wl,-rpath,' + str(ROOT / 'builds/core-glog'), '-lglog', '-pthread', '-o', str(binary)], log)
    study.save_json(output / 'manifest.json', dict(commands=commands, art_source_sha256=upstream_code_hash,
        ref=args.ref, art_source_override=args.art_source, instrumented=args.observe,
        sanitizers=args.sanitizers if args.sanitize else None,
        profile=args.profile, upstream_tests=args.upstream_tests, source_hashes=source_hashes,
        cc=study.checked_output(args.cc, '--version'), cxx=study.checked_output(args.cxx, '--version'),
        static_sanitizers=args.static_sanitizers,
        position_independent=args.pic,
        binary_sha256=study.sha256(binary), environment=study.environment(source),
        scope='Real ART and posting sources, materialized ID filter; lazy iterator/HTTP code is not linked.'))
    print(binary)


if __name__ == '__main__':
    main()
