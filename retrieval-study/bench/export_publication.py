#!/usr/bin/env python3
"""Export reviewable research evidence, excluding caches, credentials and logs.

This prepares files only. It never commits, pushes, deletes a checkout, or changes
the upstream submodule. Large new JSONL evidence is losslessly compressed.
"""
import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
sys.path.insert(0, str(ROOT))
import study

EXCLUDED = {'builds', 'tools', 'data', '__pycache__', '.git', '.agents', '.aws', '.codex', '.publication', 'server-data'}
PRIVATE_NAMES = {'server.log', 'api-key', 'import.jsonl', 'tests.log'}
INTERRUPTED = 'retrieval-study/raw/extended-v31-baseline-3130-20261028/'
PATTERNS = [re.compile(pattern) for pattern in (
    r'gh[pousr]_[A-Za-z0-9]{30,}', r'github_pat_[A-Za-z0-9_]{50,}',
    r'AKIA[A-Z0-9]{16}', r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    r'(?im)^(?:export\s+)?(?:GITHUB_TOKEN|GH_TOKEN|AWS_SECRET_ACCESS_KEY|OPENAI_API_KEY)=[^\s<"\']{16,}',
    r'(?i)Authorization:\s*Bearer\s+[A-Za-z0-9._-]{24,}')]


def without_prose(value):
    if isinstance(value, dict):
        return {key: without_prose(item) for key, item in value.items()
                if key not in {'body', 'message', 'text', 'description', 'content'}}
    if isinstance(value, list):
        return [without_prose(item) for item in value]
    return value


def scan_line(text, path):
    if any(pattern.search(text) for pattern in PATTERNS):
        raise ValueError('Potential credential in export candidate: ' + path)


def validate(path, rel):
    if path.name.endswith('.jsonl.gz'):
        with gzip.open(path, 'rt') as stream:
            for line in stream:
                scan_line(line, rel)
                if line.strip():
                    json.loads(line)
        return
    with path.open('r', encoding='utf-8') as stream:
        for line in stream:
            scan_line(line, rel)
            if path.suffix == '.jsonl' and line.strip():
                json.loads(line)
    if path.suffix == '.json':
        value = json.loads(path.read_text())
        if path.name == 'server.json':
            command = value['command']
            if '--api-key' in command and command[command.index('--api-key') + 1] != '<ephemeral-key>':
                raise ValueError('Unredacted server command: ' + rel)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--status-manifest', required=True)
    args = parser.parse_args()
    dest = Path(args.destination).resolve()
    if dest == ROOT or ROOT in dest.parents:
        parser.error('Export destination must be outside retrieval-study')
    dest.mkdir(parents=True, exist_ok=False)
    prior = json.loads((PROJECT / 'publication-manifest.json').read_text())
    previous_paths = {record['path'] for record in prior['files']}
    audit_paths = set(prior.get('audit_metadata_with_downloaded_prose_omitted', []))
    candidates = [PROJECT / name for name in ('.gitignore', '.gitmodules', 'README.md',
                  'Typesense核心优化实施方案.md', 'Typesense实施检查清单.md')]
    omitted = []
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(PROJECT).as_posix()
        parts = path.relative_to(ROOT).parts
        reason = None
        if any(part in EXCLUDED for part in parts) or path.is_symlink():
            continue
        if rel.startswith(INTERRUPTED):
            reason = 'Incomplete run interrupted by host restart; retained locally and never counted as passing.'
        elif path.name in PRIVATE_NAMES or 'build' in path.name and path.suffix == '.log':
            reason = 'Private runtime/build log or regenerable server state.'
        elif path.suffix in ('.pyc', '.data', '.db') or path.name.startswith('core-profile.'):
            reason = 'Native profiling data or generated binary cache.'
        elif path.suffix == '.log' and rel not in previous_paths and not path.name.startswith((
                'study-tests', 'v31-linked', 'upstream-art', 'cutoff', 'tls', 'observation')):
            reason = 'Uncurated diagnostic log; selected results and checksums are published instead.'
        if reason:
            omitted.append(dict(path=rel, bytes=path.stat().st_size, reason=reason))
        else:
            candidates.append(path)
    records, transforms = [], []
    for path in sorted(candidates):
        rel = path.relative_to(PROJECT).as_posix()
        target_rel = rel
        transform = None
        if path.suffix == '.jsonl' and path.stat().st_size > 256 * 1024 and rel not in previous_paths:
            target_rel += '.gz'
            transform = 'gzip, lossless JSONL; study.jsonl reads either form'
        target = dest / target_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel in audit_paths:
            value = without_prose(json.loads(path.read_text()))
            value['publication_note'] = 'Downloaded prose omitted; original audit metadata and source links retained.'
            value['original_payload_sha256'] = study.sha256(path)
            study.save_json(target, value)
            transform = 'Downloaded prose omitted'
        elif transform:
            with path.open('rb') as source, target.open('wb') as output, \
                    gzip.GzipFile(filename='', fileobj=output, mode='wb', compresslevel=6, mtime=0) as zipped:
                shutil.copyfileobj(source, zipped)
        else:
            shutil.copy2(path, target)
        validate(target, target_rel)
        record = dict(path=target_rel, bytes=target.stat().st_size, sha256=study.sha256(target))
        if transform:
            record.update(source_path=rel, source_sha256=study.sha256(path), transformation=transform)
            transforms.append(dict(source=rel, target=target_rel, transformation=transform))
        records.append(record)
    status_path = Path(args.status_manifest)
    status = json.loads(status_path.read_text())
    publication = dict(created_at_utc=study.utc_now(), repository_url='https://github.com/ContinueForever/typesense',
        branch='main', scope='Research source, plans, patch, regression and controlled local evidence.',
        upstream=dict(path='typesense', type='git submodule', url='https://github.com/typesense/typesense.git', commit=study.UPSTREAM_SHA),
        checks=dict(credential_scan_findings=0, json_records_valid=True,
                    status_manifest=status_path.as_posix(), status_manifest_sha256=study.sha256(status_path)),
        validation_status=status, audit_metadata_with_downloaded_prose_omitted=sorted(audit_paths),
        excluded_directories=sorted(EXCLUDED), omitted_files=omitted, transformations=transforms, files=records)
    study.save_json(dest / 'publication-manifest.json', publication)
    print(study.canonical(dict(destination=str(dest), files=len(records),
        bytes=sum(record['bytes'] for record in records), transformed=len(transforms), credential_scan_findings=0)))


if __name__ == '__main__':
    main()
