#!/usr/bin/env python3
"""Record the real example in a clean Linux workspace and render its README GIF.

Recording uses the standard library; GIF rendering additionally needs Pillow and
a monospace TrueType font. No terminal-recording service or upload is involved.
"""
from __future__ import annotations

import argparse
import codecs
import errno
import fcntl
import json
import os
from pathlib import Path
import platform
import pty
import re
import shutil
import struct
import subprocess
import termios
import time

ROOT = Path(__file__).resolve().parents[1]
COLS, ROWS = 100, 23
CLEAR = '\x1b[2J\x1b[H'
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def record(workspace, cmake, venv):
    """Run real commands in a PTY with networking disabled and record output only."""
    shutil.copytree(ROOT / 'examples', workspace / 'examples')
    (workspace / '.venv').symlink_to(venv, target_is_directory=True)
    downloads = workspace / '.toolchain-cache/downloads'
    downloads.mkdir(parents=True)
    for archive in ('fmt-11.2.0.tar.gz', 'spdlog-1.15.3.tar.gz'):
        cached = list((ROOT / '.toolchain-cache/downloads').glob(f'*-{archive}'))
        if not cached:
            raise RuntimeError('Cache the example first: toolchain fetch --config examples/library.yaml --locked')
        for path in cached:
            shutil.copy2(path, downloads / path.name)
    host_bin = workspace / 'host-bin'
    host_bin.mkdir()
    (host_bin / 'cmake').symlink_to(cmake)
    (workspace / 'home').mkdir()
    env = {
        'PATH': f'{host_bin}:/usr/bin:/bin', 'HOME': str(workspace / 'home'),
        'LANG': 'C.UTF-8', 'TERM': 'dumb', 'NO_COLOR': '1', 'CLICOLOR': '0',
        'CC': '/usr/bin/gcc', 'CXX': '/usr/bin/g++',
        'TOOLCHAIN_DEMO_WORKSPACE': str(workspace),
    }
    return capture(
        ['unshare', '--user', '--map-root-user', '--net', 'bash', '--noprofile',
         '--norc', str(ROOT / 'scripts/demo.sh')], workspace, env)


def capture(argv, workspace, env, *, recording=None):
    """Capture real output from a command in a terminal, retaining its timing."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=workspace, env=env, stdin=subprocess.DEVNULL, stdout=slave, stderr=slave,
    )
    os.close(slave)
    events = []
    stream = recording.open('w') if recording else None
    if stream:
        stream.write(json.dumps({'version': 2, 'width': COLS, 'height': ROWS,
                                 'idle_time_limit': 2.5, 'env': {'SHELL': '/bin/bash', 'TERM': 'dumb'}}) + '\n')
        stream.flush()
    decoder = codecs.getincrementaldecoder('utf-8')()
    try:
        while True:
            try:
                data = os.read(master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not data:
                break
            text = decoder.decode(data)
            events.append([round(time.monotonic() - started, 6), 'o', text])
            if stream:
                stream.write(json.dumps(events[-1]) + '\n')
                stream.flush()
            print(text, end='', flush=True)
        if process.wait() != 0:
            raise RuntimeError(f'Demo failed; build logs retained in {workspace}')
    finally:
        if stream:
            stream.close()
        if process.poll() is None:
            process.terminate()
            process.wait()
        os.close(master)
    return events, round(time.monotonic() - started, 6)


def screen(text):
    """Decode the demo's plain terminal output (clear/home, CRLF, SGR)."""
    text = ANSI.sub('', text.rsplit(CLEAR, 1)[-1]).replace('\r\n', '\n')
    if '\x1b' in text or '\r' in text or '\b' in text:
        raise RuntimeError('Unexpected terminal controls; inspect the recording before rendering')
    lines = []
    for line in text.split('\n'):
        lines.extend([line[i:i + COLS] for i in range(0, len(line), COLS)] or [''])
    if len(lines) > ROWS:
        raise RuntimeError('Demo output exceeds the frame; shorten commands or increase ROWS')
    return lines


def render(events, duration, output, font_path, platform_label, *, basename='workflow',
           subtitle='Build libraries. Copy the folder. Activate. Compile.',
           requirements='Host compiler + CMake required',
           limitations='Same-host relocation | Compatible Linux runtime required | Pauses shortened'):
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(str(font_path), 17)
    small = ImageFont.truetype(str(font_path), 14)
    title = ImageFont.truetype(str(font_path), 21)
    width, height = 1104, 690
    background, panel = '#0d1420', '#121e2e'
    ink, muted, accent = '#e4ecf5', '#9bafc7', '#74e0c0'
    frames, durations = [], []
    text = ''
    for index, (at, _, chunk) in enumerate(events):
        text += chunk
        next_at = events[index + 1][0] if index + 1 < len(events) else duration
        if next_at - at < 0.08 and index + 1 < len(events):
            continue  # Coalesce rapid PTY chunks, keeping every byte of output.
        lines = screen(text)
        frame = Image.new('RGB', (width, height), background)
        draw = ImageDraw.Draw(frame)
        draw.text((30, 21), 'C++ Toolchain Builder', font=title, fill=ink)
        draw.text((30, 54), subtitle, font=small, fill=muted)
        draw.rounded_rectangle((20, 91, width - 20, 614), radius=14, fill=panel)
        for row, line in enumerate(lines):
            color = accent if line.startswith('$ ') or line == 'The answer is 42' else (
                muted if line.startswith('#') else ink)
            draw.text((38, 111 + row * 21), line, font=font, fill=color)
        draw.text((30, 633), platform_label + ' | ' + requirements, font=small, fill=ink)
        draw.text((30, 658), limitations, font=small, fill=muted)
        frames.append(frame.quantize(colors=64))
        durations.append(max(100, round(min(next_at - at, 2.5) * 100) * 10))
    durations[-1] = 4500
    frames[0].save(output / f'{basename}.gif', save_all=True, append_images=frames[1:],
                   duration=durations, loop=0, optimize=True)
    frames[-1].convert('RGB').save(output / f'{basename}.png')
    return round(sum(durations) / 1000, 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cmake', default=shutil.which('cmake'), help='Host CMake executable')
    parser.add_argument('--venv', type=Path, default=ROOT / '.venv', help='Existing builder virtual environment')
    parser.add_argument('--font', type=Path, default=Path('/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'))
    args = parser.parse_args()
    if not args.cmake or not Path(args.cmake).is_file():
        parser.error('Install CMake or pass --cmake /path/to/cmake')
    if not (args.venv / 'bin/activate').is_file() or not args.font.is_file():
        parser.error('An installed builder virtual environment and monospace font are required')
    from PIL import Image  # Fail before building if the rendering dependency is missing.
    import tempfile

    workspace = Path(tempfile.mkdtemp(prefix='cpp-demo-'))
    events, duration = record(workspace, Path(args.cmake).resolve(), args.venv.resolve())
    transcript = ANSI.sub('', ''.join(event[2] for event in events)).replace('\r\n', '\n')
    assert 'The answer is 42' in transcript and 'No builder CLI on PATH' in transcript
    assert not (workspace / 'install/example-libraries').exists()
    cache = (workspace / 'build/hello/CMakeCache.txt').read_text()
    for package in ('fmt', 'spdlog'):
        assert f'{package}_DIR:PATH={workspace}/shared/toolchain/' in cache
    assert 'CMAKE_CXX_COMPILER:FILEPATH=/usr/bin/g++' in cache
    output = ROOT / 'docs/media'
    output.mkdir(parents=True, exist_ok=True)
    label = f'{platform.freedesktop_os_release()["NAME"]} {platform.freedesktop_os_release()["VERSION_ID"]} {platform.machine()}'
    playback = render(events, duration, output, args.font, label)
    header = {'version': 2, 'width': COLS, 'height': ROWS, 'duration': duration,
              'idle_time_limit': 2.5, 'title': 'C++ Toolchain Builder: build, copy, activate, compile',
              'env': {'SHELL': '/bin/bash', 'TERM': 'dumb'}}
    (output / 'workflow.cast').write_text('\n'.join(json.dumps(item) for item in [header, *events]) + '\n')
    (output / 'workflow.txt').write_text(transcript)
    print(f'\nValidated relocated fmt/spdlog packages and host GCC. Recording: {duration:.1f}s; GIF: {playback:.1f}s.')
    print(f'Artifacts: {output}; GIF: {(output / "workflow.gif").stat().st_size:,} bytes')
    shutil.rmtree(workspace)


if __name__ == '__main__':
    main()
