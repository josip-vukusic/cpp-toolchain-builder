#!/usr/bin/env python3
"""Render a captured SDK build, or record copying and using the completed SDK."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import shutil
import tempfile

from record_demo import ANSI, COLS, ROWS, ROOT, capture, render


def save_recording(events, duration, basename, title):
    output = ROOT / 'docs/media'
    output.mkdir(parents=True, exist_ok=True)
    header = {'version': 2, 'width': COLS, 'height': ROWS, 'duration': duration,
              'idle_time_limit': 2.5, 'title': title,
              'env': {'SHELL': '/bin/bash', 'TERM': 'dumb'}}
    (output / f'{basename}.cast').write_text('\n'.join(json.dumps(item) for item in [header, *events]) + '\n')
    text = ANSI.sub('', ''.join(event[2] for event in events)).replace('\r\n', '\n')
    (output / f'{basename}.txt').write_text(text)
    return output


def build_recording(args):
    rows = [json.loads(line) for line in args.cast.read_text().splitlines()]
    events = [row for row in rows[1:] if row[1] == 'o']
    text = ''.join(row[2] for row in events)
    if 'Build exit code: 0;' not in text:
        raise SystemExit('A successful capture with its exit code and elapsed time is required')
    # Captions and paths are normalized for publication; compiler/CLI output is retained.
    for row in events:
        row[2] = row[2].replace(str(ROOT), '$PROJECT').replace(
            '# Build a GCC SDK from cached sources; all components compiled now.',
            '# Build a GCC SDK from cached sources. Completed components can resume.')
    duration = rows[0].get('duration', events[-1][0] + 4.5)
    jobs = re.search(r'--jobs (\d+)', text)
    requirements = f'{jobs.group(1)} build jobs' if jobs else 'Source build'
    output = save_recording(events, duration, 'gcc-build', 'Build a GCC, CMake and spdlog SDK')
    playback = render(events, duration, output, args.font, args.platform,
                      basename='gcc-build', subtitle='Build GCC, CMake and spdlog from source.',
                      requirements=requirements,
                      limitations='Long compilation pauses shortened | Actual elapsed time is shown in the terminal')
    print(f'Build GIF: {playback:.1f}s; recorded run: {duration:.1f}s')


def record_build(args):
    destination = ROOT / '.toolchain-work/gcc-sdk-capture'
    destination.mkdir(parents=True, exist_ok=True)
    index = 1 + len(list(destination.glob('build-*.cast')))
    args.cast = destination / f'build-{index}.cast'
    host_tools = str(args.host_tools.resolve()) + ':' if args.host_tools else ''
    env = {'HOME': str(Path.home()), 'PATH': host_tools + '/usr/bin:/bin',
           'LANG': 'C.UTF-8', 'TERM': 'dumb', 'NO_COLOR': '1', 'PYTHONUNBUFFERED': '1',
           'TAR_OPTIONS': '--no-same-owner', 'TOOLCHAIN_DEMO_JOBS': str(args.jobs),
           'TOOLCHAIN_DEMO_BUILDER': str(ROOT / '.venv/bin/toolchain')}
    argv = ['bash', '--noprofile', '--norc', str(ROOT / 'scripts/demo_gcc_build.sh')]
    if args.offline:
        argv = ['unshare', '--user', '--map-root-user', '--net', *argv]
    capture(argv, ROOT, env, recording=args.cast)
    build_recording(args)


def use_recording(args):
    prefix = args.prefix.resolve()
    manifest = json.loads((prefix / 'share/toolchain/manifest.json').read_text())
    if manifest['toolchain']['compiler']['mode'] != 'bundled':
        raise SystemExit('This recording requires a completed SDK with a bundled compiler')
    for name in ('gcc', 'g++', 'cmake', 'ctest', 'make', 'as', 'ld'):
        if not os.access(prefix / 'bin' / name, os.X_OK):
            raise SystemExit(f'SDK is missing executable bin/{name}')
    workspace = Path(tempfile.mkdtemp(prefix='gcc-sdk-demo-'))
    (workspace / 'prepared-sdk').symlink_to(prefix, target_is_directory=True)
    shutil.copytree(ROOT / 'examples/hello', workspace / 'hello')
    base = workspace / 'base-bin'
    base.mkdir()
    # No compiler, CMake, Make, Python or builder is exposed on the receiving PATH.
    for name in ('bash', 'sh', 'dirname', 'sleep', 'head', 'uname', 'pwd', 'sed', 'grep',
                 'cat', 'mkdir', 'rm', 'rmdir', 'cp', 'mv', 'touch', 'date', 'true', 'false'):
        source = shutil.which(name)
        if source:
            (base / name).symlink_to(source)
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TERM': 'dumb', 'NO_COLOR': '1',
           'TOOLCHAIN_DEMO_WORKSPACE': str(workspace), 'TOOLCHAIN_DEMO_ORIGINAL': str(prefix)}
    events, duration = capture(
        ['unshare', '--user', '--map-root-user', '--mount', '--net', 'bash', '--noprofile',
         '--norc', str(ROOT / 'scripts/demo_gcc_use.sh')], workspace, env)
    copied = workspace / 'toolchain'
    cache = (workspace / 'build/CMakeCache.txt').read_text()
    for entry in (f'CMAKE_CXX_COMPILER:FILEPATH={copied}/bin/g++',
                  f'CMAKE_MAKE_PROGRAM:FILEPATH={copied}/bin/make',
                  f'fmt_DIR:PATH={copied}/lib/cmake/fmt',
                  f'spdlog_DIR:PATH={copied}/lib/cmake/spdlog'):
        if entry not in cache:
            raise RuntimeError(f'Consumer used an unexpected tool/package: {entry}; retained {workspace}')
    if 'The answer is 42' not in ''.join(row[2] for row in events):
        raise RuntimeError('The consumer did not produce the expected output')
    output = save_recording(events, duration, 'gcc-use', 'Copy, activate and use a GCC SDK')
    playback = render(events, duration, output, args.font, args.platform,
                      basename='gcc-use', subtitle='Copy your SDK. Activate it. Build your application.',
                      requirements='GCC + CMake + Make included',
                      limitations='Same-host relocation | Compatible Linux runtime and C library headers required')
    print(f'Use GIF: {playback:.1f}s. All consumer tools and packages resolved inside the copy.')
    shutil.rmtree(workspace)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font', type=Path, default=Path('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'))
    release = platform.freedesktop_os_release()
    parser.add_argument('--platform', default=f'{release["NAME"]} {release["VERSION_ID"]} {platform.machine()}')
    sub = parser.add_subparsers(dest='mode', required=True)
    record = sub.add_parser('build', help='Run the real SDK build, record it, and render the result')
    record.add_argument('--jobs', type=int, default=8)
    record.add_argument('--host-tools', type=Path, help='Optional directory with bootstrap tools such as flex')
    record.add_argument('--offline', action='store_true', help='Disable networking; all sources must already be cached')
    build = sub.add_parser('render-build')
    build.add_argument('cast', type=Path)
    use = sub.add_parser('use')
    use.add_argument('--prefix', type=Path, default=ROOT / 'install/gcc-sdk')
    args = parser.parse_args()
    if not args.font.is_file():
        parser.error('A monospace TrueType font is required; use --font /path/to/font.ttf')
    from PIL import Image  # Check the optional rendering dependency before a long build.
    if args.mode == 'build' and (args.jobs < 1 or not (ROOT / '.venv/bin/toolchain').is_file()):
        parser.error('Use a positive job count and install the builder in .venv first')
    if args.mode == 'build':
        record_build(args)
    elif args.mode == 'render-build':
        build_recording(args)
    else:
        use_recording(args)


if __name__ == '__main__':
    main()
