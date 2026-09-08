#!/usr/bin/env python3
"""Prepare a small source-only GitHub upload, without publishing anything."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    'README.md', 'LICENSE', 'CONTRIBUTING.md', 'CHANGELOG.md',
    'pyproject.toml', 'MANIFEST.in', '.gitignore', '.gitattributes',
    'toolchain.yaml', 'toolchain.lock.json', 'toolchain_cli.py',
)
TREES = {
    'cpp_toolchain_builder': {'.py', '.yaml'},
    'docs': {'.md'},
    'examples': {'.yaml', '.json', '.cpp'},
    'tests': {'.py', '.cpp'},
    'scripts': {'.py', '.sh'},
    '.github': {'.yml', '.yaml', '.md'},
}


def source_files():
    result = [ROOT / name for name in FILES]
    for folder, extensions in TREES.items():
        for path in (ROOT / folder).rglob('*'):
            if any(part in {'__pycache__', 'build', 'install', '.toolchain-work', '.toolchain-cache'} for part in path.relative_to(ROOT / folder).parts):
                continue
            if path.is_file() and (path.suffix in extensions or path.name == 'CMakeLists.txt'):
                result.append(path)
    for path in result:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f'Expected a regular source file: {path.relative_to(ROOT)}')
        if path.stat().st_size > 1024 * 1024:
            raise RuntimeError(f'Unexpectedly large source file: {path.relative_to(ROOT)}')
    return sorted(set(result))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force', action='store_true', help='Replace previously generated publication output')
    args = parser.parse_args()
    files = source_files()
    output = ROOT / 'output'
    target = output / 'github-source'
    archive = output / 'cpp-toolchain-builder-github.tar.gz'
    if (target.exists() or archive.exists()) and not args.force:
        parser.error('Publication output exists; use --force to regenerate it')
    if target.is_symlink() or archive.is_symlink():
        parser.error('Publication output must not be a symlink')
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    for source in files:
        destination = target / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        destination.chmod(0o644)
    with tarfile.open(archive, 'w:gz') as stream:
        def normalize(member):
            member.uid = member.gid = 0
            member.uname = member.gname = 'root'
            return member
        stream.add(target, arcname='cpp-toolchain-builder', filter=normalize)
    with archive.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    archive.with_name(archive.name + '.sha256').write_text(f'{digest}  {archive.name}\n')
    print(f'Prepared {len(files)} files in {target}')
    print(f'Source archive: {archive} ({archive.stat().st_size:,} bytes)')
    print('No GitHub repository, remote, commit, or release was created.')


if __name__ == '__main__':
    main()
