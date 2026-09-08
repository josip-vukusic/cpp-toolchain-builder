from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from importlib.resources import files
from pathlib import Path

import yaml

from . import __version__
from .config import SYSTEMS, load_config, read_yaml, save_yaml, validate_recipe
from .engine import Builder
from .hooks import HOOKS
from .inspection import archive, info, inventory, verify
from .util import ToolchainError, read_json

UBUNTU_PACKAGES = ['build-essential', 'git', 'curl', 'wget', 'bison', 'flex', 'texinfo', 'autoconf', 'automake',
                   'libtool', 'pkg-config', 'python3-dev', 'libexpat1-dev', 'libncurses-dev', 'libreadline-dev',
                   'zlib1g-dev', 'liblzma-dev', 'libcurl4-openssl-dev', 'libssl-dev', 'libffi-dev', 'libxml2-dev',
                   'libedit-dev', 'libbabeltrace-dev', 'libipt-dev', 'libpcre2-dev', 'gettext', 'gawk', 'patch', 'unzip', 'rsync']


def output(data, as_json=False):
    print(json.dumps(data, indent=2) if as_json else yaml.safe_dump(data, sort_keys=False).rstrip())


def make_builder(args, config=None):
    return Builder(config or load_config(Path(args.config)), **{key: getattr(args, key, None) for key in
                   ('prefix', 'work', 'cache', 'jobs', 'stdlib', 'compiler_prefix', 'offline', 'quiet', 'locked', 'lockfile')})


def doctor(builder, requested=None):
    names = builder.selected(requested)
    recipes = [builder.config.recipes[name] for name in names]
    core_recipes = [recipe for recipe in recipes if recipe.get('stage') == 'core']
    # Preserve the full preset's broad checks, while allowing small SDK recipes
    # to declare their actual bootstrap tools through the existing requires field.
    legacy_core = any('requires' not in recipe for recipe in core_recipes)
    problems = []
    if platform.system() != 'Linux':
        problems.append('The builder currently supports Linux hosts')
    if builder.config.settings.get('platform') == 'linux-x86_64' and platform.machine() not in {'x86_64', 'amd64'}:
        problems.append('The POC preset targets native Linux x86_64; it is not a cross compiler')
    commands = {'bash', 'make'}
    cmake_available = False
    needs_host_cmake = False
    for recipe in recipes:
        if recipe['build']['system'] == 'cmake' and not cmake_available:
            needs_host_cmake = True
        if 'bin/cmake' in recipe.get('artifacts', []):
            cmake_available = True
    if any('git' in builder.config.recipes[name]['source'] for name in names):
        commands.add('git')
    if legacy_core:
        commands.update({'gcc', 'g++', 'bison', 'flex', 'makeinfo', 'autoconf', 'automake', 'libtoolize', 'pkg-config', 'perl', 'wget', 'patch', 'm4'})
    elif needs_host_cmake:
        commands.add('cmake')
    for name in names:
        commands.update(builder.config.recipes[name].get('requires', []))
    builder.context(builder.config.recipes[names[-1]], 'preflight')
    locations = {command: shutil.which(command, path=builder.env['PATH']) for command in sorted(commands)}
    problems += [f'Missing host executable: {command}' for command, path in locations.items() if not path]
    if builder.compiler_prefix:
        for command in ('clang', 'clang++', 'cmake'):
            if not os.access(builder.compiler_prefix / 'bin' / command, os.X_OK):
                problems.append(f'External compiler prefix is missing bin/{command}')
    missing = []
    if legacy_core and Path('/etc/debian_version').exists() and shutil.which('dpkg-query'):
        for package in UBUNTU_PACKAGES:
            result = subprocess.run(['dpkg-query', '-W', '-f=${Status}', package], capture_output=True, text=True)
            if result.returncode or result.stdout.strip() != 'install ok installed':
                missing.append(package)
        if missing:
            problems.append('Missing build dependencies: ' + ', '.join(missing))
    writable = builder.prefix
    while not writable.exists():
        writable = writable.parent
    if not os.access(writable, os.W_OK):
        problems.append(f'Install prefix is not writable: {builder.prefix}; choose --prefix or arrange directory ownership')
    return {'passed': not problems, 'platform': platform.platform(), 'prefix': str(builder.prefix),
            'recipes': len(names), 'jobs': builder.jobs, 'free_disk_gib': round(shutil.disk_usage(writable).free / 1024**3, 1),
            'tools': locations, 'problems': problems,
            'dependency_install_hint': 'sudo apt-get install ' + ' '.join(missing) if missing else None,
            'notes': ['Full GCC/LLVM builds need substantial disk, RAM and time; reduce --jobs if memory is limited.',
                      'Source --offline controls the CLI cache; upstream CMake/Bazel/GCC steps can fetch their own dependencies.']}


def show_plan(builder, requested, as_json):
    plan = builder.plan(requested)
    if as_json:
        output({'prefix': str(builder.prefix), 'jobs': builder.jobs, 'stdlib': builder.stdlib, 'plan': plan}, True)
    else:
        print(f'Prefix: {builder.prefix}\nJobs: {builder.jobs}; standard library: {builder.stdlib}\n')
        for i, item in enumerate(plan, 1):
            print(f"{i:2}. {item['name']} {item['version']} ({item['system']})")
            for command in item['commands']:
                print('    ' + (shlex.join(command['run']) if 'run' in command else
                                  'Python adapter: ' + command['hook'] if 'hook' in command else command['shell']))
    return 0


def cmd_build(args):
    builder = make_builder(args)
    if args.dry_run:
        return show_plan(builder, args.library, args.json)
    if not args.skip_doctor:
        health = doctor(builder, args.library)
        if not health['passed']:
            output(health, args.json)
            return 1
    result = builder.build(args.library, args.force)
    if args.archive:
        destination = builder.config.location('output', None, 'output') / f'{builder.prefix.name}-{platform.machine()}-{builder.stdlib}.tar.gz'
        result['distribution'] = archive(builder.prefix, destination, args.force)
    result['prefix'] = str(builder.prefix)
    output(result, args.json)
    return 0


def cmd_test(args):
    config = load_config(Path(args.config))
    if args.dry_run:
        args.prefix = str(config.path.parent / '.test-preview/prefix')
        args.work = str(config.path.parent / '.test-preview/work')
        return show_plan(make_builder(args, config), [args.library], args.json)
    temporary = Path(tempfile.mkdtemp(prefix=f'toolchain-test-{args.library}-'))
    args.prefix, args.work = str(temporary / 'prefix'), str(temporary / 'work')
    builder = make_builder(args, config)
    success = False
    try:
        health = doctor(builder, [args.library])
        if not health['passed']:
            output(health, args.json)
            return 1
        result = builder.build([args.library])
        checks = verify(builder.prefix, run_smoke=args.smoke, stdlib=builder.stdlib, compiler_prefix=builder.compiler_prefix)
        result['verification'] = checks
        result['test_directory'] = str(temporary)
        output(result, args.json)
        success = checks['passed']
        return 0 if success else 1
    finally:
        if success and not args.keep:
            shutil.rmtree(temporary)
        else:
            print(f'Test files and logs retained: {temporary}', file=sys.stderr)


def cmd_init(args):
    path = Path(args.config).expanduser().resolve()
    if path.exists():
        raise ToolchainError(f'Configuration already exists: {path}')
    settings = {'name': args.name, 'version': '1', 'prefix': args.prefix, 'stdlib': 'libstdc++'}
    data = {'schema_version': 1, 'toolchain': settings, 'libraries': []}
    if args.preset == 'poc':
        settings.update(compiler='toolchain', platform='linux-x86_64', jobs=8)
        data['libraries'] = read_yaml(Path(str(files('cpp_toolchain_builder').joinpath('recipes/poc.yaml'))))['libraries']
    save_yaml(path, data)
    print(f'Created {path}')
    return 0


def cmd_recipes(args):
    config = load_config(Path(args.config))
    if args.action == 'show':
        if args.name not in config.recipes:
            raise ToolchainError('recipes show requires a known recipe name')
        output(config.recipes[args.name], args.json)
    elif args.action == 'validate':
        for recipe in config.recipes.values():
            for field in ('prepare', 'commands', 'after'):
                for step in recipe['build'].get(field, []):
                    if isinstance(step, dict) and 'hook' in step and step['hook'] not in HOOKS:
                        raise ToolchainError(f"{recipe['name']}: unknown hook {step['hook']}")
        if config.recipes:
            make_builder(args, config).plan()
        output({'valid': True, 'recipes': len(config.recipes)}, args.json)
    elif args.json:
        output(list(config.recipes.values()), True)
    else:
        for name in config.select():
            recipe = config.recipes[name]
            print(f"{name:28} {recipe['version']:25} {recipe['build']['system']}")
    return 0


def cmd_add(args):
    path = Path(args.config).expanduser().resolve()
    config = load_config(path)
    if args.name in config.recipes:
        raise ToolchainError(f'Recipe already exists: {args.name}')
    source = {'url': args.url} if args.url else {'git': args.git} if args.git else {'path': args.path}
    if args.ref:
        source['ref'] = args.ref
    if args.sha256:
        source['sha256'] = args.sha256
    recipe = {'name': args.name, 'version': args.recipe_version, 'source': source,
              'depends_on': args.depends_on or [], 'build': {'system': args.system, 'options': args.option or []},
              'artifacts': args.artifact or []}
    if args.command:
        recipe['build']['commands'] = [{'shell': command} for command in args.command]
    validate_recipe(recipe)
    for dependency in recipe['depends_on']:
        if dependency not in config.recipes:
            raise ToolchainError(f'Unknown dependency: {dependency}')
    data = read_yaml(path)
    data.setdefault('libraries', []).append(recipe)
    save_yaml(path, data)
    print(f'Added {args.name} to {path}')
    return 0


def cmd_inspect(args):
    prefix = Path(args.prefix or os.environ.get('TOOLCHAIN_PREFIX', '/opt/toolchain-v1')).expanduser().resolve()
    if args.action == 'info':
        output(info(prefix), args.json)
    elif args.action == 'verify':
        config = load_config(Path(args.config)) if args.config else None
        stdlib = args.stdlib or (config.settings.get('stdlib', 'libstdc++') if config else
                                read_json(prefix / 'share/toolchain/manifest.json', {}).get('toolchain', {}).get('stdlib', 'libstdc++'))
        result = verify(prefix, config.recipes if config else None, args.smoke, stdlib,
                        Path(args.compiler_prefix).resolve() if args.compiler_prefix else None)
        output(result, args.json)
        return 0 if result['passed'] else 1
    elif args.action == 'components':
        manifest = read_json(prefix / 'share/toolchain/manifest.json')
        if not manifest:
            raise ToolchainError(f'No managed manifest in {prefix}; use inspect info for a legacy installation')
        output(manifest['components'], args.json)
    else:
        if args.action == 'find' and not args.name:
            raise ToolchainError('inspect find requires a filename or glob')
        result = inventory(prefix, args.action, args.name if args.action == 'find' else None)
        if args.json:
            output(result, True)
        else:
            for entry in result:
                print(prefix / entry['path'])
        if args.action == 'find' and not result:
            return 1
    return 0


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('must be a positive integer')
    return number


def parser():
    root = argparse.ArgumentParser(prog='toolchain', description='Build, inspect and package native C/C++ toolchains')
    root.add_argument('--version', action='version', version=f'%(prog)s {__version__}')
    sub = root.add_subparsers(dest='command', required=True)

    def config_options(command):
        command.add_argument('--config', default='toolchain.yaml', help='YAML definition (default: ./toolchain.yaml)')
        command.add_argument('--json', action='store_true', help='Machine-readable output')

    def build_options(command, selection=True):
        config_options(command)
        for option in ('prefix', 'work', 'cache', 'lockfile', 'compiler-prefix'):
            command.add_argument('--' + option)
        command.add_argument('--jobs', '-j', type=positive)
        command.add_argument('--stdlib', choices=['libstdc++', 'libc++'])
        command.add_argument('--offline', action='store_true', help='Use cached top-level sources; upstream build downloads are separate')
        command.add_argument('--locked', action='store_true', help='Require the source identities recorded in the lockfile')
        command.add_argument('--quiet', action='store_true', help='Keep compiler output in logs')
        if selection:
            command.add_argument('--library', action='append', help='Build this recipe and dependencies; repeatable')

    build = sub.add_parser('build', help='Build selected recipes with automatic resume')
    build_options(build)
    build.add_argument('--dry-run', action='store_true', help='Print commands without writing files or downloading')
    build.add_argument('--force', action='store_true', help='Rerun all selected recipes and dependencies')
    build.add_argument('--skip-doctor', action='store_true', help='Skip host dependency checks')
    build.add_argument('--archive', action='store_true', help='Package after a successful build')
    build.set_defaults(func=cmd_build)
    plan = sub.add_parser('plan', help='Preview dependency order and build commands')
    build_options(plan)
    plan.set_defaults(func=lambda a: show_plan(make_builder(a), a.library, a.json))
    fetch = sub.add_parser('fetch', help='Cache sources and record digests/commits')
    build_options(fetch)
    fetch.set_defaults(func=lambda a: make_builder(a).fetch_all(a.library) or 0)
    health = sub.add_parser('doctor', help='Check host dependencies, platform, and permissions')
    build_options(health)
    def cmd_doctor(a):
        report = doctor(make_builder(a), a.library)
        output(report, a.json)
        return 0 if report['passed'] else 1
    health.set_defaults(func=cmd_doctor)
    test = sub.add_parser('test', help='Build a recipe into an isolated temporary prefix')
    test.add_argument('library')
    build_options(test, False)
    test.add_argument('--keep', action='store_true')
    test.add_argument('--dry-run', action='store_true')
    test.add_argument('--smoke', action='store_true', help='Compile, link and run a C++20 consumer')
    test.set_defaults(func=cmd_test)
    init = sub.add_parser('init', help='Create an editable toolchain definition')
    init.add_argument('config', nargs='?', default='toolchain.yaml')
    init.add_argument('--preset', choices=['empty', 'poc'], default='empty')
    init.add_argument('--name', default='my-toolchain')
    init.add_argument('--prefix', default='./install')
    init.set_defaults(func=cmd_init)
    recipes = sub.add_parser('recipes', help='List, show, or validate recipes')
    recipes.add_argument('action', choices=['list', 'show', 'validate'], nargs='?', default='list')
    recipes.add_argument('name', nargs='?')
    config_options(recipes)
    recipes.set_defaults(func=cmd_recipes)
    add = sub.add_parser('add', help='Add a library recipe')
    add.add_argument('name')
    config_options(add)
    add.add_argument('--version', dest='recipe_version', required=True)
    source = add.add_mutually_exclusive_group(required=True)
    for key in ('url', 'git', 'path'):
        source.add_argument('--' + key)
    for key in ('ref', 'sha256'):
        add.add_argument('--' + key)
    add.add_argument('--system', choices=sorted(SYSTEMS), default='cmake')
    for key in ('option', 'command', 'depends-on', 'artifact'):
        add.add_argument('--' + key, action='append')
    add.set_defaults(func=cmd_add)
    inspect = sub.add_parser('inspect', help='Inspect existing or newly built installations')
    inspect.add_argument('action', nargs='?', choices=['info', 'libs', 'bins', 'headers', 'find', 'components', 'verify'], default='info')
    inspect.add_argument('name', nargs='?')
    inspect.add_argument('--prefix')
    inspect.add_argument('--config', help='Expected recipes for complete or legacy verification')
    inspect.add_argument('--json', action='store_true')
    inspect.add_argument('--smoke', action='store_true')
    inspect.add_argument('--stdlib', choices=['libstdc++', 'libc++'])
    inspect.add_argument('--compiler-prefix')
    inspect.set_defaults(func=cmd_inspect)
    status = sub.add_parser('status', help='Show complete, failed, and pending recipes')
    build_options(status)
    def cmd_status(a):
        builder = make_builder(a)
        result = []
        for item in builder.plan(a.library):
            entry = builder.state['recipes'].get(item['name'], {})
            state = entry.get('status', 'pending')
            if state == 'complete' and entry.get('fingerprint') != item['fingerprint']:
                state = 'outdated'
            result.append({'name': item['name'], 'version': item['version'], 'status': state, 'log': entry.get('log')})
        output(result, a.json)
        return 0
    status.set_defaults(func=cmd_status)
    package = sub.add_parser('archive', help='Create a verified tar.gz and SHA-256 file')
    package.add_argument('--prefix', required=True)
    package.add_argument('--output', required=True)
    package.add_argument('--force', action='store_true')
    package.add_argument('--json', action='store_true')
    package.set_defaults(func=lambda a: output(archive(Path(a.prefix).resolve(), Path(a.output).resolve(), a.force), a.json) or 0)
    return root


def normalize_argv(argv):
    if argv == ['--recipes']:
        return ['recipes', 'list']
    if not argv or argv[0] not in {'recipes', 'inspect'}:
        return argv
    options, nouns, index = [], [], 0
    while index < len(argv):
        token = argv[index]
        if token in {'--config', '--prefix', '--stdlib', '--compiler-prefix'} and index + 1 < len(argv):
            options += argv[index:index + 2]
            index += 2
        else:
            nouns.append(token)
            index += 1
    return nouns + options


def main(argv=None):
    try:
        args = parser().parse_args(normalize_argv(list(sys.argv[1:] if argv is None else argv)))
        return args.func(args) or 0
    except KeyboardInterrupt:
        print('Interrupted. Build state and logs were retained; repeat the command to resume.', file=sys.stderr)
        return 130
    except (ToolchainError, OSError, yaml.YAMLError, ValueError, tarfile.TarError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 1
