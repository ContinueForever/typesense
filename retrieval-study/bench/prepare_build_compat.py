#!/usr/bin/env python3
"""Apply a recorded, const-correct elfutils fix for the local glibc 2.43 host.

This changes a dependency checkout only. Both engine variants must use the same
dependency file; the engine's pinned baseline remains clean.
"""
import difflib
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study

ARCHIVE_SHA256 = 'df76db71366d1d708365fc7a6c60ca48398f14367eb2b8954efc8897147ad871'
EDITS = [
    ('libcpu/riscv_disasm.c', '6732b1981da6d12dd3258700999f29dc3b51cdf0a1f4b8ca0f35fc9445231522',
     [('struct known_csrs *found = bsearch (&key, known,',
       'const struct known_csrs *found = bsearch (&key, known,')]),
    ('libdw/dwarf_getsrclines.c', 'a40844e8d56895c120acee0b373ad5018ee3b1fd44094b164a723c1878756e31',
     [('uint8_t *endp = memchr (', 'const uint8_t *endp = memchr (')]),
    ('src/readelf.c', 'f96641c5c6b5a546e74c1ebf0bead2fb97d4692ae7f888f5d60a0fde8b8ee79e',
     [('char *filename = strrchr (valuestr,', 'const char *filename = strrchr (valuestr,'),
      ('  Dwarf_Word val;\n  unsigned char *endp;\n  Elf_Data *data;',
       '  Dwarf_Word val;\n  const unsigned char *endp;\n  Elf_Data *data;'),
      ('unsigned char *endp = memchr (linep,', 'const unsigned char *endp = memchr (linep,')]),
]


def main():
    dependencies = list((ROOT / 'builds/bazel').glob('*/external/elfutils'))
    if len(dependencies) != 1:
        raise RuntimeError('Expected one pinned elfutils dependency checkout')
    dependency = dependencies[0]
    edits, diff, files = [], [], []
    for relative, expected, changes in EDITS:
        source = dependency / relative
        original = source.read_text()
        for old, new in changes:
            original = original.replace(new, old)
        if hashlib.sha256(original.encode()).hexdigest() != expected:
            raise ValueError('elfutils source does not match the pinned 0.191 file: ' + relative)
        patched = original
        for old, new in changes:
            patched = patched.replace(old, new)
        edits.append((source, patched))
        diff.extend(difflib.unified_diff(original.splitlines(True), patched.splitlines(True),
                    fromfile='a/' + relative, tofile='b/' + relative))
        files.append(dict(path=str(source), before_sha256=expected,
                          after_sha256=hashlib.sha256(patched.encode()).hexdigest()))
    patch = ROOT / 'patches/build-compat/elfutils-0.191-glibc-2.43-const.patch'
    patch.parent.mkdir(exist_ok=True)
    patch.write_text(''.join(diff))
    for source, patched in edits:
        source.write_text(patched)
    study.save_json(ROOT / 'manifests/build-compatibility.json', dict(
        recorded_at_utc=study.utc_now(), dependency='elfutils 0.191',
        archive_sha256=ARCHIVE_SHA256, patch=str(patch.relative_to(ROOT)),
        patch_sha256=study.sha256(patch),
        rationale='glibc 2.43 enables qualifier-generic bsearch/memchr with _GNU_SOURCE; '
                  'lookup results into constant CSR and DWARF data must retain const.',
        reference='https://sourceware.org/glibc/manual/2.43/html_node/Array-Search-Function.html',
        scope='Build compatibility only; no warning suppression or search algorithm change.',
        files=files))
    print('Applied and recorded the const-correct elfutils compatibility fix.')


if __name__ == '__main__':
    main()
