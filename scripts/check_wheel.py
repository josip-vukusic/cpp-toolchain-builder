#!/usr/bin/env python3
"""Install and exercise a wheel in a fresh venv outside the source checkout."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dist', type=Path)
    parser.add_argument('--wheelhouse', type=Path, help='Install dependencies offline from this directory')
    args = parser.parse_args()
    wheels = list(args.dist.resolve().glob('cpp_toolchain_builder-*.whl'))
    if len(wheels) != 1:
        parser.error(f'Expected one builder wheel in {args.dist}, found {len(wheels)}')
    env = os.environ.copy()
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    with tempfile.TemporaryDirectory(prefix='toolchain-wheel-') as temporary:
        root = Path(temporary)
        environment = root / 'venv'
        venv.EnvBuilder(with_pip=True).create(environment)
        python = environment / 'bin/python'
        cli = environment / 'bin/toolchain'

        def run(argv):
            result = subprocess.run([str(a) for a in argv], cwd=root, env=env, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if result.returncode:
                raise RuntimeError(result.stdout)
            return result.stdout

        options = ['--no-index', '--find-links', str(args.wheelhouse.resolve())] if args.wheelhouse else []
        run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-cache-dir', *options, wheels[0]])
        imported = run([python, '-c', 'import cpp_toolchain_builder; print(cpp_toolchain_builder.__file__)']).strip()
        if not Path(imported).is_relative_to(environment):
            raise RuntimeError(f'Imported outside fresh environment: {imported}')
        print(run([cli, '--version']).strip())
        run([cli, 'init', 'preset.yaml', '--preset', 'poc'])
        validation = json.loads(run([cli, 'recipes', 'validate', '--config', 'preset.yaml', '--json']))
        if validation != {'valid': True, 'recipes': 37}:
            raise RuntimeError(f'Unexpected preset validation: {validation}')
        run([cli, 'plan', '--config', 'preset.yaml', '--json'])
        source = root / 'source'
        source.mkdir()
        (source / 'tiny.c').write_text('int tiny(void){return 42;}\n')
        (source / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\nproject(tiny C)\nadd_library(tiny STATIC tiny.c)\ninstall(TARGETS tiny ARCHIVE DESTINATION lib)\n')
        run([cli, 'init', 'local.yaml'])
        run([cli, 'add', 'tiny', '--config', 'local.yaml', '--version', '1', '--path', source,
             '--artifact', 'lib/libtiny.a'])
        run([cli, 'build', '--config', 'local.yaml', '--offline', '--quiet'])
        verification = json.loads(run([cli, 'inspect', 'verify', '--prefix', root / 'install', '--smoke', '--json']))
        if not verification['passed']:
            raise RuntimeError(str(verification))
        print('Passed: fresh wheel import, 37 bundled recipes, local CMake build and C++20 smoke test.')


if __name__ == '__main__':
    main()
