#!/usr/bin/env python3
"""Apply the reviewable patch to a projection, leaving the baseline source intact."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import study


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--source', default=str(ROOT.parent / 'typesense'))
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / 'src').mkdir()
    original = subprocess.check_output(['git', 'show', study.UPSTREAM_SHA + ':src/art.cpp'], cwd=args.source)
    (output / 'src/art.cpp').write_bytes(original)
    patch = ROOT / 'patches/0001-check-art-deadline-on-internal-nodes.patch'
    subprocess.run(['patch', '--batch', '--forward', '--strip=1', '--directory=' + str(output),
                    '--input=' + str(patch)], check=True)
    study.save_json(output / 'manifest.json', dict(base=study.UPSTREAM_SHA,
        patch_sha256=study.sha256(patch), art_sha256=study.sha256(output / 'src/art.cpp')))
    print(output / 'src/art.cpp')


if __name__ == '__main__':
    main()
