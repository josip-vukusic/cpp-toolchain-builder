from __future__ import annotations

import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from cpp_toolchain_builder.cli import doctor, main
from cpp_toolchain_builder.config import load_config, resolve, save_yaml
from cpp_toolchain_builder.engine import Builder, Runner
from cpp_toolchain_builder.hooks import cel_build, copy_tree, install_archives, protovalidate_prepare, protovalidate_schemas
from cpp_toolchain_builder.inspection import activation, archive, info, inventory, protobuf_schema_smoke, smoke, verify
from cpp_toolchain_builder.sources import SourceCache, extract
from cpp_toolchain_builder.util import ToolchainError, exclusive_lock, read_json, sha256

ROOT = Path(__file__).resolve().parents[1]


class Workspace(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='toolchain-tests-')
        self.root = Path(self.temp.name)
        self.source = self.root / 'input with spaces'
        self.source.mkdir()
        (self.source / 'hello.h').write_text('#pragma once\n#define ANSWER 42\n')
        (self.source / 'LICENSE').write_text('Test fixture license\n')
        self.recipe = {'name': 'hello', 'version': '1.0', 'stage': 'data',
                       'source': {'path': str(self.source)}, 'build': {'system': 'copy',
                       'copies': [{'from': '${source}/hello.h', 'to': 'include/hello.h'}]},
                       'artifacts': ['include/hello.h']}
        self.path = self.root / 'toolchain.yaml'
        self.settings = {'name': 'test', 'prefix': 'prefix with spaces', 'work': 'work with spaces', 'cache': 'cache with spaces'}
        self.write([self.recipe])

    def tearDown(self):
        self.temp.cleanup()

    def write(self, recipes):
        save_yaml(self.path, {'schema_version': 1, 'toolchain': self.settings, 'libraries': recipes})

    def builder(self, **kwargs):
        return Builder(load_config(self.path), quiet=True, **kwargs)

    def cli(self, *args):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = main(list(args))
        return code, out.getvalue(), err.getvalue()


class DoctorTests(Workspace):
    def test_small_sdk_checks_declared_tools_without_full_preset_packages(self):
        self.recipe.update(stage='core', requires=['missing-bootstrap-tool'])
        self.write([self.recipe])
        with patch('cpp_toolchain_builder.cli.shutil.which', side_effect=lambda name, **kw:
                   None if name == 'missing-bootstrap-tool' else '/usr/bin/' + name):
            result = doctor(self.builder())
        self.assertEqual(result['problems'], ['Missing host executable: missing-bootstrap-tool'])
        self.assertNotIn('gawk', result['tools'])
        self.assertIsNone(result['dependency_install_hint'])

    def test_cmake_can_be_built_before_the_first_library(self):
        provider = copy.deepcopy(self.recipe)
        provider.update(name='cmake', stage='core', requires=['g++'], artifacts=['bin/cmake'])
        consumer = copy.deepcopy(self.recipe)
        consumer.update(depends_on=['cmake'], build={'system': 'cmake'})
        self.write([provider, consumer])
        with patch('cpp_toolchain_builder.cli.shutil.which', side_effect=lambda name, **kw:
                   None if name == 'cmake' else '/usr/bin/' + name):
            result = doctor(self.builder())
        self.assertTrue(result['passed'], result['problems'])
        self.assertNotIn('cmake', result['tools'])

    def test_cmake_cannot_bootstrap_itself_with_cmake(self):
        self.recipe.update(name='cmake', stage='core', requires=[],
                           build={'system': 'cmake'}, artifacts=['bin/cmake'])
        self.write([self.recipe])
        with patch('cpp_toolchain_builder.cli.shutil.which', side_effect=lambda name, **kw:
                   None if name == 'cmake' else '/usr/bin/' + name):
            result = doctor(self.builder())
        self.assertIn('Missing host executable: cmake', result['problems'])


class ConfigurationTests(Workspace):
    def test_duplicate_yaml_key(self):
        self.path.write_text('schema_version: 1\nschema_version: 1\n')
        with self.assertRaisesRegex(ToolchainError, 'Duplicate YAML key'):
            load_config(self.path)

    def test_unknown_build_option_is_error(self):
        self.recipe['build']['optoins'] = []
        self.write([self.recipe])
        with self.assertRaisesRegex(ToolchainError, 'unknown build fields'):
            load_config(self.path)

    def test_cycle_and_missing_dependency(self):
        with self.assertRaisesRegex(ToolchainError, 'cycle'):
            resolve({'a': {'depends_on': ['b']}, 'b': {'depends_on': ['a']}}, ['a'])
        with self.assertRaisesRegex(ToolchainError, 'Unknown'):
            resolve({'a': {'depends_on': ['missing']}}, ['a'])

    def test_shared_dependency_order(self):
        recipes = {'a': {}, 'b': {'depends_on': ['a']}, 'c': {'depends_on': ['a']}, 'd': {'depends_on': ['b', 'c']}}
        self.assertEqual(resolve(recipes, ['d']), ['a', 'b', 'c', 'd'])

    def test_duplicate_recipe(self):
        self.write([self.recipe, self.recipe])
        with self.assertRaisesRegex(ToolchainError, 'Duplicate recipe'):
            load_config(self.path)

    def test_malformed_recipe(self):
        for key, value in [('name', '../escape'), ('version', 1.0), ('depends_on', 'gcc')]:
            with self.subTest(key=key):
                recipe = copy.deepcopy(self.recipe)
                recipe[key] = value
                self.write([recipe])
                with self.assertRaises(ToolchainError):
                    load_config(self.path)

    def test_install_traversal_rejected(self):
        self.recipe['build']['copies'][0]['to'] = '../escape'
        self.write([self.recipe])
        with self.assertRaises(ToolchainError):
            load_config(self.path)

    def test_builtin_poc_coverage_and_critical_flags(self):
        config = load_config(ROOT / 'toolchain.yaml')
        expected = {'gcc', 'binutils', 'gdb', 'pahole-gdb', 'cmake', 'cppcheck', 'llvm', 'swig', 'boost', 'bzip2',
                    'gflags', 'fmt', 'spdlog', 'aws-sdk-cpp', 'aws-crt-cpp', 'aws-iot-device-sdk-cpp-v2', 'arrow',
                    'benchmark', 'googletest', 'protobuf', 'lz4', 'xz', 'zlib', 'zstd', 'openssl', 'libmodbus',
                    'bazel', 'googleapis', 'cel-spec', 'cel-cpp', 'abseil-cpp', 'antlr4', 're2', 'protovalidate-cc',
                    'tomlplusplus', 'concurrentqueue', 'nlohmann-json'}
        self.assertEqual(set(config.recipes), expected)
        plan = config.select()
        for dep in ('protobuf', 're2', 'antlr4', 'bazel', 'googleapis', 'cel-spec'):
            self.assertLess(plan.index(dep), plan.index('cel-cpp'))
        self.assertIn('--enable-bootstrap', config.recipes['gcc']['build']['options'])
        self.assertIn('--enable-languages=c,c++,fortran', config.recipes['gcc']['build']['options'])
        self.assertIn('-DARROW_PARQUET=ON', config.recipes['arrow']['build']['options'])
        arrow_options = config.recipes['arrow']['build']['options']
        self.assertIn('-DRapidJSON_SOURCE=BUNDLED', arrow_options)
        self.assertIn('-DCMAKE_FIND_PACKAGE_NO_PACKAGE_REGISTRY=ON', arrow_options)
        self.assertIn('-DCMAKE_FIND_USE_SYSTEM_PACKAGE_REGISTRY=OFF', arrow_options)
        self.assertIn('-DCMAKE_EXPORT_NO_PACKAGE_REGISTRY=ON', arrow_options)
        self.assertIn('-DANTLR_BUILD_CPP_TESTS=OFF', config.recipes['antlr4']['build']['options'])
        self.assertEqual(config.recipes['cel-spec']['source']['ref'], 'v0.24.0')
        self.assertTrue(all(r['artifacts'] for r in config.recipes.values()))
        self.assertNotIn('legacy', json.dumps(config.recipes))

    def test_libcxx_plan_and_jobs(self):
        builder = Builder(load_config(ROOT / 'toolchain.yaml'), jobs=3, stdlib='libc++')
        plan = builder.plan(['fmt'])
        llvm = next(p for p in plan if p['name'] == 'llvm')
        self.assertIn('-DLLVM_ENABLE_RUNTIMES=libunwind;libcxx;libcxxabi', llvm['commands'][0]['run'])
        self.assertIn('-stdlib=libc++', plan[-1]['environment']['CXXFLAGS'])
        self.assertIn('3', plan[-1]['commands'][1]['run'])

    def test_nested_cmake_project_runs_from_its_source_directory(self):
        self.recipe['build'] = {'system': 'cmake', 'source_subdir': 'runtime/Cpp'}
        self.write([self.recipe])
        plan = self.builder().plan()
        for command in plan[0]['commands']:
            self.assertTrue(command['cwd'].endswith('/source/runtime/Cpp'))


class SanitizerTests(Workspace):
    def test_invalid_recipe_sanitizer_overrides(self):
        self.recipe['stage'] = 'library'
        for overrides in ([], {'asan': {}}, {'asan-ubsan': []},
                          {'asan-ubsan': {'cflags': []}},
                          {'asan-ubsan': {'compile_flags': '-fno-sanitize=function'}},
                          {'asan-ubsan': {'link_flags': [7]}},
                          {'asan-ubsan': {'compile_flags': [' ']}}):
            with self.subTest(overrides=overrides):
                self.recipe['sanitizer_overrides'] = overrides
                self.write([self.recipe])
                with self.assertRaisesRegex(ToolchainError, 'sanitizer_overrides'):
                    load_config(self.path)
        self.recipe.update(stage='data', sanitizer_overrides={'asan-ubsan': {'compile_flags': ['-g']}})
        self.write([self.recipe])
        with self.assertRaisesRegex(ToolchainError, 'stage: library'):
            load_config(self.path)

    def test_recipe_overrides_follow_profile_flags_and_reach_build_adapters(self):
        self.recipe.update(stage='library', sanitizer_overrides={'asan-ubsan': {
            'compile_flags': ['-fno-sanitize=function', '-I${prefix}/custom include'],
            'link_flags': ['-Wl,--as-needed']}})
        for system in ('cmake', 'make', 'custom'):
            with self.subTest(system=system):
                self.recipe['build'] = {'system': system}
                if system == 'custom':
                    self.recipe['build']['commands'] = [['./Configure']]
                self.write([self.recipe])
                builder = self.builder(sanitizer='asan-ubsan', compiler_prefix='/compiler')
                step = builder.plan()[0]['commands'][0]['run']
                for key in ('CFLAGS', 'CXXFLAGS'):
                    flags = builder.env[key]
                    self.assertLess(flags.index('-fsanitize=address,undefined'), flags.index('-fno-sanitize=function'))
                    self.assertIn(str(builder.prefix / 'custom include'), flags)
                self.assertNotIn('-fno-sanitize=function', builder.env['LDFLAGS'])
                self.assertTrue(builder.env['LDFLAGS'].endswith('-Wl,--as-needed'))
                if system == 'cmake':
                    self.assertIn('-DCMAKE_C_FLAGS=' + builder.env['CFLAGS'], step)
                    self.assertIn('-DCMAKE_CXX_FLAGS=' + builder.env['CXXFLAGS'], step)
                    self.assertIn('-DCMAKE_EXE_LINKER_FLAGS=' + builder.env['LDFLAGS'], step)
                elif system == 'make':
                    for key in ('CFLAGS', 'CXXFLAGS', 'LDFLAGS'):
                        self.assertIn(key + '=' + builder.env[key], step)

    def test_openssl_override_scope_and_dependency_fingerprints(self):
        for profile in (None, 'tsan', 'asan-ubsan'):
            with self.subTest(profile=profile):
                config = load_config(ROOT / 'toolchain.yaml')
                with_override = Builder(config, prefix=str(self.root / 'sdk'), compiler_prefix='/compiler', sanitizer=profile)
                plan = with_override.plan(['aws-crt-cpp', 'fmt'])
                openssl = next(p for p in plan if p['name'] == 'openssl')
                for key in ('CFLAGS', 'CXXFLAGS'):
                    self.assertEqual('-fno-sanitize=function' in openssl['environment'][key], profile == 'asan-ubsan')
                for entry in plan:
                    if entry['name'] != 'openssl':
                        self.assertNotIn('-fno-sanitize=function', str(entry['environment']))
                # Compare against the old recipe: only the active profile and
                # its dependent libraries should be invalidated.
                config.recipes['openssl'].pop('sanitizer_overrides')
                without_override = Builder(config, prefix=str(self.root / 'sdk'), compiler_prefix='/compiler', sanitizer=profile)
                before = {p['name']: p['fingerprint'] for p in without_override.plan(['aws-crt-cpp', 'fmt'])}
                changed = {p['name'] for p in plan if before[p['name']] != p['fingerprint']}
                self.assertEqual(changed, {'openssl', 'aws-crt-cpp'} if profile == 'asan-ubsan' else set())
                self.assertNotIn('-fno-sanitize=function', activation(self.root, sanitizer=profile))

    def test_profile_settings_resolve_relative_to_config_and_allow_cli_overrides(self):
        self.settings.update(sanitizer='asan-ubsan', compiler_prefix='compiler', lockfile='release.lock.json')
        self.write([self.recipe])
        (self.root / 'release.lock.json').write_text('{"schema_version": 1, "sources": {}}')
        builder = self.builder(locked=True)
        self.assertEqual(builder.sanitizer, 'asan-ubsan')
        self.assertEqual(builder.compiler_prefix, self.root / 'compiler')
        self.assertEqual(builder.lockfile, self.root / 'release.lock.json')
        override = self.builder(sanitizer='tsan', compiler_prefix='/other/compiler', lockfile='/other/lock.json')
        self.assertEqual(override.sanitizer, 'tsan')
        self.assertEqual(override.compiler_prefix, Path('/other/compiler'))
        self.assertEqual(override.lockfile, Path('/other/lock.json'))

    def test_invalid_profile_settings_fail_during_config_loading(self):
        for key, value in [('sanitizer', 'address,thread'), ('sanitizer', []), ('compiler_prefix', 7), ('lockfile', [])]:
            with self.subTest(key=key, value=value):
                settings = {**self.settings, key: value}
                save_yaml(self.path, {'schema_version': 1, 'toolchain': settings, 'libraries': [self.recipe]})
                with self.assertRaises(ToolchainError):
                    load_config(self.path)

    def test_named_asan_profile_keeps_release_sources_and_excludes_compiler_build(self):
        config = load_config(ROOT / 'toolchain-v2-asan.yaml')
        release = load_config(ROOT / 'toolchain.yaml')
        self.assertEqual(config.recipes, release.recipes)
        builder = Builder(config, prefix=str(self.root / 'asan-sdk'), locked=True)
        self.assertEqual(builder.lockfile, ROOT / 'toolchain.lock.json')
        self.assertEqual(builder.compiler_prefix, Path('/opt/toolchain-v2'))
        self.assertEqual(builder.sanitizer, 'asan-ubsan')
        self.assertEqual(len(builder.selected(None)), 30)
        self.assertNotIn('llvm', builder.selected(None))
        for profile in ('asan-ubsan', 'tsan', None):
            candidate = Builder(release, prefix=str(self.root / 'sdk'), compiler_prefix='/compiler', sanitizer=profile)
            plan = candidate.plan(['xz'])
            configure = plan[-1]['commands'][0]['run']
            self.assertEqual('--disable-sandbox' in configure, bool(profile))
        self.assertNotIn('--disable-sandbox', release.recipes['xz']['build']['options'])

    def test_verify_profile_checks_all_libraries_but_not_external_compiler_artifacts(self):
        core = copy.deepcopy(self.recipe)
        core.update(name='compiler', stage='core', artifacts=['bin/clang'])
        self.settings.update(sanitizer='asan-ubsan', compiler_prefix='compiler-sdk')
        self.write([core, self.recipe])
        with patch('cpp_toolchain_builder.cli.verify', return_value={'passed': True}) as check:
            code, out, err = self.cli('inspect', 'verify', '--config', str(self.path))
        self.assertEqual(code, 0, err)
        self.assertEqual(check.call_args.args[0], self.root / 'prefix with spaces')
        self.assertEqual(set(check.call_args.args[1]), {'hello'})

    def test_profiles_require_separate_prefix_and_external_compiler(self):
        with self.assertRaisesRegex(ToolchainError, 'separate --prefix'):
            self.builder(sanitizer='asan-ubsan')
        with self.assertRaisesRegex(ToolchainError, 'separate --prefix'):
            self.builder(sanitizer='tsan', compiler_prefix=str(self.root / 'prefix with spaces'))
        with self.assertRaisesRegex(ToolchainError, 'Unknown sanitizer'):
            self.builder(sanitizer='address,thread')

    def test_profiles_cannot_mix_in_existing_installation(self):
        self.builder().build()
        with self.assertRaisesRegex(ToolchainError, 'Cannot mix'):
            self.builder(sanitizer='asan-ubsan', compiler_prefix='/compiler')
        state = self.root / 'prefix with spaces/share/toolchain/build-state.json'
        data = json.loads(state.read_text())
        data['sanitizer'] = 'asan-ubsan'
        state.write_text(json.dumps(data))
        with self.assertRaisesRegex(ToolchainError, 'Cannot mix'):
            self.builder()
        with self.assertRaisesRegex(ToolchainError, 'Cannot mix'):
            self.builder(sanitizer='tsan', compiler_prefix='/compiler')
        self.builder(sanitizer='asan-ubsan', compiler_prefix='/compiler')

    def test_flags_fingerprints_and_make_overrides(self):
        self.recipe.update(stage='library', build={'system': 'make', 'options': ['CFLAGS=-O2']})
        self.write([self.recipe])
        fingerprints = []
        for profile, flag in [('asan-ubsan', '-fsanitize=address,undefined'), ('tsan', '-fsanitize=thread')]:
            builder = self.builder(sanitizer=profile, compiler_prefix='/compiler')
            plan = builder.plan()
            fingerprints.append(plan[0]['fingerprint'])
            for key in ('CFLAGS', 'CXXFLAGS', 'LDFLAGS'):
                self.assertIn(flag, builder.env[key])
                assigned = [arg for arg in plan[0]['commands'][0]['run'] if arg.startswith(key + '=')]
                self.assertIn(flag, assigned[-1])
            self.assertNotIn('/compiler', builder.env['CMAKE_PREFIX_PATH'])
            self.assertNotIn('/compiler', builder.env['PKG_CONFIG_PATH'])
        self.assertNotEqual(*fingerprints)

    def test_boost_flags_are_forwarded_without_changing_release_recipe(self):
        config = load_config(ROOT / 'toolchain.yaml')
        builder = Builder(config, prefix=str(self.root / 'sdk'), compiler_prefix='/compiler', sanitizer='asan-ubsan')
        plan = builder.plan(['boost'])
        self.assertNotIn('llvm', [p['name'] for p in plan])
        command = next(c['shell'] for c in plan[-1]['commands'] if './b2' in c.get('shell', ''))
        self.assertIn('cxxflags=$CXXFLAGS', command)
        self.assertIn('linkflags=$LDFLAGS', command)
        self.assertNotIn('cxxflags=$CXXFLAGS', str(config.recipes['boost']))

    def test_activation_exports_and_restores_sanitizer_flags(self):
        target = self.root / 'activate'
        target.write_text(activation(self.root, sanitizer='asan-ubsan'))
        result = subprocess.run(['bash', '-c', '''set -eu
export CFLAGS=original-c CXXFLAGS=original-cxx LDFLAGS=original-link
source "$1"
[[ "$CFLAGS" == *-fsanitize=address,undefined* ]]
[[ "$CXXFLAGS" == *-fsanitize=address,undefined* ]]
[[ "$LDFLAGS" == *-fsanitize=address,undefined* ]]
toolchain_deactivate
[[ "$CFLAGS" == original-c && "$CXXFLAGS" == original-cxx && "$LDFLAGS" == original-link ]]
''', 'test', str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_cmake_and_metadata_record_the_profile(self):
        from cpp_toolchain_builder.inspection import write_metadata
        builder = self.builder(sanitizer='tsan', compiler_prefix='/compiler')
        write_metadata(builder)
        cmake = (builder.prefix / 'share/toolchain/toolchain.cmake').read_text()
        for variable in ('CMAKE_C_FLAGS_INIT', 'CMAKE_CXX_FLAGS_INIT', 'CMAKE_EXE_LINKER_FLAGS_INIT'):
            self.assertRegex(cmake, variable + r'.*-fsanitize=thread')
        manifest = read_json(builder.prefix / 'share/toolchain/manifest.json')
        self.assertEqual(manifest['toolchain']['sanitizer'], 'tsan')

    def test_cmake_overrides_and_bazel_actions_keep_instrumentation(self):
        self.recipe.update(stage='library', build={'system': 'cmake', 'options': ['-DCMAKE_CXX_FLAGS=-O2']})
        self.write([self.recipe])
        builder = self.builder(sanitizer='tsan', compiler_prefix='/compiler')
        plan = builder.plan()
        flags = [arg for arg in plan[0]['commands'][0]['run'] if arg.startswith('-DCMAKE_CXX_FLAGS=')]
        self.assertIn('-fsanitize=thread', flags[-1])
        binary = self.root / 'bazel-output'
        binary.mkdir()
        builder.runner = Mock(side_effect=['//common:example\n', '', str(binary) + '\n'])
        with patch('cpp_toolchain_builder.hooks.install_archives'), patch('cpp_toolchain_builder.hooks.cel_headers'):
            cel_build(builder)
        for call in builder.runner.call_args_list[1:]:
            self.assertIn('--copt=-fsanitize=thread', call.args[0])
            self.assertIn('--linkopt=-fsanitize=thread', call.args[0])


class SourceTests(Workspace):
    def test_tar_traversal_rejected(self):
        path = self.root / 'bad.tar'
        with tarfile.open(path, 'w') as out:
            member = tarfile.TarInfo('../escaped')
            member.size = 1
            out.addfile(member, io.BytesIO(b'x'))
        with self.assertRaises(tarfile.FilterError):
            extract(path, self.root / 'extracted', {})
        self.assertFalse((self.root / 'escaped').exists())

    def test_tar_symlink_escape_rejected(self):
        path = self.root / 'bad.tar'
        with tarfile.open(path, 'w') as out:
            member = tarfile.TarInfo('link')
            member.type = tarfile.SYMTYPE
            member.linkname = '/tmp'
            out.addfile(member)
        with self.assertRaises(tarfile.FilterError):
            extract(path, self.root / 'extracted', {})

    def test_zip_traversal_rejected(self):
        path = self.root / 'bad.zip'
        with zipfile.ZipFile(path, 'w') as out:
            out.writestr('../escaped', 'x')
        with self.assertRaises(ToolchainError):
            extract(path, self.root / 'extracted', {})

    def test_extract_isolated_archive_roots(self):
        for name in ('one', 'two'):
            path = self.root / f'{name}.zip'
            with zipfile.ZipFile(path, 'w') as out:
                out.writestr(f'{name}/include/{name}.h', 'x')
            extract(path, self.root / name, {})
            self.assertTrue((self.root / name / 'include' / f'{name}.h').is_file())

    def test_self_extracting_binary_stays_intact(self):
        path = self.root / 'binary'
        path.write_bytes(b'ELF executable prefix')
        with zipfile.ZipFile(path, 'a') as out:
            out.writestr('embedded/jdk', 'runtime')
        target = self.root / 'extracted'
        extract(path, target, {'unpack': False, 'filename': 'bazel'})
        self.assertEqual((target / 'bazel').read_bytes(), path.read_bytes())
        self.assertFalse((target / 'embedded').exists())

    def test_checksum_mismatch(self):
        source = self.source / 'hello.h'
        recipe = copy.deepcopy(self.recipe)
        recipe['source'] = {'url': source.as_uri(), 'filename': 'hello.h', 'sha256': '0' * 64}
        cache = SourceCache(self.root / 'cache', Runner(True))
        with self.assertRaisesRegex(ToolchainError, 'SHA-256 mismatch'):
            cache.fetch(recipe)

    def test_offline_missing_source(self):
        recipe = copy.deepcopy(self.recipe)
        recipe['source'] = {'url': 'https://example.invalid/source.tar.gz'}
        cache = SourceCache(self.root / 'cache', Runner(True), offline=True)
        with self.assertRaisesRegex(ToolchainError, 'not cached'):
            cache.fetch(recipe)

    def test_fetch_then_locked_offline(self):
        self.recipe['source'] = {'url': (self.source / 'hello.h').as_uri(), 'filename': 'hello.h'}
        self.write([self.recipe])
        self.builder().fetch_all()
        builder = self.builder(locked=True, offline=True)
        builder.build()
        self.assertTrue((builder.prefix / 'include/hello.h').is_file())

    def test_lock_rejects_changed_source(self):
        self.builder().fetch_all()
        (self.source / 'hello.h').write_text('changed')
        with self.assertRaisesRegex(ToolchainError, 'source differs'):
            self.builder(locked=True).build()

    def test_git_checkout_ref_and_lock(self):
        if not shutil.which('git'):
            self.skipTest('git unavailable')
        run = lambda *args: subprocess.run(['git', '-C', str(self.source), *args], check=True, capture_output=True)
        run('init')
        run('add', '.')
        run('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-m', 'fixture')
        commit = run('rev-parse', 'HEAD').stdout.decode().strip()
        self.recipe['source'] = {'git': str(self.source), 'ref': commit}
        self.write([self.recipe])
        self.builder().fetch_all()
        builder = self.builder(locked=True, offline=True)
        builder.build()
        self.assertEqual(builder.state['recipes']['hello']['source']['git_commit'], commit)


class ResumeTests(Workspace):
    def test_resume_restores_environment_and_retries_failed_recipe(self):
        consumer = copy.deepcopy(self.recipe)
        consumer.update(name='consumer', depends_on=['hello'], artifacts=['include/result.h'],
                        build={'system': 'custom', 'commands': [[sys.executable, '-c',
                            'import os, pathlib; p=pathlib.Path(os.environ["TC_PREFIX"]); '
                            'assert (p/"retry").exists(), "first attempt fails"; '
                            '(p/"include/result.h").write_text(os.environ["CXXFLAGS"]+"\\n"+'
                            'os.environ.get("CPPFLAGS", "unset"))']]})
        self.write([self.recipe, consumer])
        with patch.dict(os.environ, {'CXXFLAGS': '-DORIGINAL', 'PATH': '/original:' + os.environ['PATH']}):
            os.environ.pop('CPPFLAGS', None)
            first = self.builder()
            with self.assertRaises(ToolchainError):
                first.build()
            saved_path = os.environ['PATH']
        (first.prefix / 'retry').touch()
        with patch.dict(os.environ, {'CXXFLAGS': '-DNEW', 'CPPFLAGS': '-DNEW', 'PATH': '/different:' + os.environ['PATH']}):
            code, out, err = self.cli('resume', '--config', str(self.path), '--skip-doctor', '--quiet', '--json')
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['skipped'], ['hello'])
            self.assertEqual(json.loads(out)['built'], ['consumer'])
            self.assertEqual(os.environ['CXXFLAGS'], '-DNEW')
        self.assertEqual((first.prefix / 'include/result.h').read_text(), '-DORIGINAL\nunset')
        self.assertEqual(read_json(first.state_path)['build_environment']['PATH'], saved_path)

    def test_build_resume_flag_and_dry_run_do_not_write_state(self):
        first = self.builder()
        first.build()
        before = first.state_path.read_bytes()
        with patch.dict(os.environ, {'PATH': '/different:' + os.environ['PATH']}):
            code, _, err = self.cli('build', '--resume', '--config', str(self.path), '--dry-run')
            self.assertEqual(code, 0, err)
        self.assertEqual(first.state_path.read_bytes(), before)

    def test_resume_rejects_changed_completed_recipe_but_build_can_update(self):
        first = self.builder()
        first.build()
        before = first.state_path.read_bytes()
        self.recipe['version'] = '2.0'
        self.write([self.recipe])
        code, _, err = self.cli('resume', '--config', str(self.path), '--skip-doctor')
        self.assertEqual(code, 1)
        self.assertIn('completed recipes have changed: hello', err)
        self.assertEqual(first.state_path.read_bytes(), before)
        self.assertEqual(self.builder().build()['built'], ['hello'])

    def test_resume_rebuilds_missing_artifact(self):
        first = self.builder()
        first.build()
        (first.prefix / 'include/hello.h').unlink()
        self.assertEqual(self.builder(resume=True).build()['built'], ['hello'])

    def test_resume_requires_previous_state_and_rejects_force(self):
        code, _, err = self.cli('resume', '--config', str(self.path), '--skip-doctor')
        self.assertEqual(code, 1)
        self.assertIn('No previous build', err)
        self.assertFalse(self.builder().prefix.exists())
        code, _, err = self.cli('resume', '--config', str(self.path), '--force')
        self.assertEqual(code, 1)
        self.assertIn('--force cannot be combined', err)

    def test_legacy_resume_recovers_verified_path_from_config_log(self):
        with patch.dict(os.environ, {'PATH': '/original:' + os.environ['PATH']}):
            first = self.builder()
            first.build()
            original_path = os.environ['PATH']
        state = read_json(first.state_path)
        state.pop('build_environment')
        first.state_path.write_text(json.dumps(state))
        fingerprint = state['recipes']['hello']['fingerprint']
        log = first.work / 'build' / ('hello-' + fingerprint[:16]) / 'build/config.log'
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('\n'.join('PATH: ' + p for p in [str(first.prefix / 'bin'), *original_path.split(os.pathsep)]))
        before = first.state_path.read_bytes()
        with patch.dict(os.environ, {'PATH': '/new:' + os.environ['PATH']}):
            resumed = self.builder(resume=True)
            resumed.plan()
            self.assertEqual(resumed.build_environment['PATH'], original_path)
            self.assertEqual(first.state_path.read_bytes(), before)
            self.assertEqual(resumed.build()['skipped'], ['hello'])
        self.assertEqual(read_json(first.state_path)['build_environment']['PATH'], original_path)

    def test_legacy_resume_rejects_unverifiable_environment(self):
        first = self.builder()
        first.build()
        state = read_json(first.state_path)
        state.pop('build_environment')
        first.state_path.write_text(json.dumps(state))
        with patch.dict(os.environ, {'CXXFLAGS': '-DCHANGED'}):
            with self.assertRaisesRegex(ToolchainError, 'Cannot recover a matching environment'):
                self.builder(resume=True).plan()

    def test_resume_rejects_invalid_environment_record(self):
        first = self.builder()
        first.build()
        state = read_json(first.state_path)
        state['build_environment'] = {'PATH': ['invalid']}
        first.state_path.write_text(json.dumps(state))
        with self.assertRaisesRegex(ToolchainError, 'Invalid saved build environment'):
            self.builder(resume=True).plan()

    def test_resume_rejects_changed_source_and_lock_mode(self):
        self.builder().build()
        with self.assertRaisesRegex(ToolchainError, 'completed recipes have changed'):
            self.builder(resume=True, locked=True).plan()
        (self.source / 'hello.h').write_text('changed source')
        with self.assertRaisesRegex(ToolchainError, 'completed recipes have changed'):
            self.builder(resume=True).plan()

    def test_resume_checks_only_selected_dependencies(self):
        other = copy.deepcopy(self.recipe)
        other.update(name='other', version='1.0')
        self.write([self.recipe, other])
        self.builder().build()
        other['version'] = '2.0'
        self.write([self.recipe, other])
        self.assertEqual(self.builder(resume=True).build(['hello'])['skipped'], ['hello'])


class InstallCopyTests(Workspace):
    def test_protovalidate_installs_public_schemas_with_import_paths(self):
        build = self.root / 'build'
        base = build / '_deps/protovalidate-src/proto'
        schema = base / 'protovalidate/buf/validate/validate.proto'
        schema.parent.mkdir(parents=True)
        schema.write_text('public schema')
        schema.chmod(0o444)
        internal = base / 'protovalidate-testing/buf/validate/conformance/case.proto'
        internal.parent.mkdir(parents=True)
        internal.write_text('test schema')
        (schema.parent / 'README.md').write_text('not a proto')
        prefix = self.root / 'sdk'
        protovalidate_schemas(build, prefix)
        installed = prefix / 'include/buf/validate/validate.proto'
        self.assertEqual(installed.read_text(), 'public schema')
        self.assertEqual(list((prefix / 'include').rglob('*.proto')), [installed])
        self.assertFalse((installed.parent / 'README.md').exists())
        protovalidate_schemas(build, prefix)
        self.assertEqual(installed.read_text(), 'public schema')

    def test_missing_protovalidate_schema_is_an_install_error(self):
        with self.assertRaisesRegex(ToolchainError, 'Protovalidate public schemas are missing'):
            protovalidate_schemas(self.root / 'build', self.root / 'sdk')
        self.assertFalse((self.root / 'sdk').exists())

    def test_proto_consumer_rejects_generated_header_without_schema(self):
        prefix = self.root / 'sdk'
        header = prefix / 'include/buf/validate/validate.pb.h'
        header.parent.mkdir(parents=True)
        header.touch()
        with self.assertRaisesRegex(ToolchainError, 'missing .*validate.proto'):
            protobuf_schema_smoke(prefix, self.root, os.environ.copy())

    def test_readonly_headers_can_be_replaced_on_install_and_resume(self):
        for patterns in (None, ('*.h',)):
            with self.subTest(patterns=patterns):
                source = self.source / 'generated'
                source.mkdir(exist_ok=True)
                header = source / 'timeofday.pb.h'
                header.write_text('new generated header')
                header.chmod(0o555)
                target = self.root / ('filtered' if patterns else 'whole-tree')
                target.mkdir()
                old = target / header.name
                old.write_text('old generated header')
                old.chmod(0o555)
                copy_tree(source, target, patterns)
                self.assertEqual(old.read_text(), 'new generated header')
                self.assertEqual(old.stat().st_mode & 0o777, 0o755)
                self.assertEqual(header.stat().st_mode & 0o777, 0o555)
                copy_tree(source, target, patterns)
                self.assertEqual(old.read_text(), 'new generated header')
                header.chmod(0o755)

    def test_failed_copy_preserves_old_file_and_removes_temporary(self):
        target = self.root / 'installed.h'
        target.write_text('existing contents')
        target.chmod(0o444)
        def fail_copy(source, temporary):
            Path(temporary).write_text('partial contents')
            raise OSError('copy interrupted')
        with patch('cpp_toolchain_builder.hooks.shutil.copy2', side_effect=fail_copy):
            with self.assertRaisesRegex(OSError, 'copy interrupted'):
                copy_tree(self.source / 'hello.h', target)
        self.assertEqual(target.read_text(), 'existing contents')
        self.assertEqual(target.stat().st_mode & 0o777, 0o444)
        self.assertEqual(list(self.root.glob('.installed.h.*')), [])


class ArchiveInstallTests(Workspace):
    def make_archive(self, relative, contents):
        archive = self.source / relative
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(contents)
        return archive

    def test_cel_preserves_duplicate_basenames_and_pic_variants(self):
        archives = [
            self.make_archive('common/libexpr.a', b'common expression'),
            self.make_archive('common/ast/libexpr.a', b'ast expression'),
            self.make_archive('common/ast/libexpr.pic.a', b'PIC ast expression'),
        ]
        prefix = self.root / 'installed'
        install_archives(archives, prefix, source_root=self.source, namespace='cel')
        expected = {
            'libcel_common_expr.a': b'common expression',
            'libcel_common_ast_expr.a': b'ast expression',
            'libcel_common_ast_expr.pic.a': b'PIC ast expression',
        }
        self.assertEqual({p.name: p.read_bytes() for p in (prefix / 'lib').iterdir()}, expected)

    def test_flat_install_rejects_conflicts_before_copying(self):
        archives = [self.make_archive('a/libexpr.a', b'first'),
                    self.make_archive('b/libexpr.a', b'second')]
        prefix = self.root / 'installed'
        with self.assertRaisesRegex(ToolchainError, 'Conflicting static archive names'):
            install_archives(archives, prefix)
        self.assertFalse((prefix / 'lib').exists())

    def test_qualified_names_still_reject_ambiguous_paths(self):
        archives = [self.make_archive('common/ast/libexpr.a', b'first'),
                    self.make_archive('common_ast/libexpr.a', b'second')]
        prefix = self.root / 'installed'
        with self.assertRaisesRegex(ToolchainError, 'Conflicting static archive names'):
            install_archives(archives, prefix, source_root=self.source, namespace='cel')
        self.assertFalse((prefix / 'lib').exists())

    def test_identical_flat_duplicates_are_deduplicated(self):
        archives = [self.make_archive('a/libexpr.a', b'same'),
                    self.make_archive('b/libexpr.a', b'same')]
        prefix = self.root / 'installed'
        install_archives(archives, prefix)
        self.assertEqual((prefix / 'lib/libexpr.a').read_bytes(), b'same')
        self.assertEqual(len(list((prefix / 'lib').iterdir())), 1)


class BuildTests(Workspace):
    def test_protovalidate_probes_keep_checks_and_follow_project_standard(self):
        deps = self.source / 'cmake/Deps.cmake'
        deps.parent.mkdir()
        original = '''try_compile(ABSL_CAN_MOVE_ASSIGN_STATUS
    SOURCE_FROM_CONTENT probe.cc "static_assert(std::is_nothrow_move_assignable_v<absl::Status>);"
    CXX_STANDARD 17
    NO_CACHE
)
try_compile(PROTOBUF_HAS_CPPSTRINGTYPE
    SOURCE_FROM_CONTENT probe.cc "using Type = google::protobuf::FieldDescriptor::CppStringType;"
    CXX_STANDARD 17
    NO_CACHE
)
'''
        deps.write_text(original)
        builder = self.builder()
        builder.variables['source'] = str(self.source)
        protovalidate_prepare(builder)
        patched = deps.read_text()
        self.assertIn('static_assert(std::is_nothrow_move_assignable_v<absl::Status>);', patched)
        self.assertIn('google::protobuf::FieldDescriptor::CppStringType', patched)
        self.assertEqual(patched.count('CXX_STANDARD ${CMAKE_CXX_STANDARD}'), 2)
        protovalidate_prepare(builder)
        self.assertEqual(deps.read_text(), patched)
        deps.write_text('unexpected upstream layout')
        with self.assertRaisesRegex(ToolchainError, 'expected two C\\+\\+17 dependency probes'):
            protovalidate_prepare(builder)
        self.assertEqual(deps.read_text(), 'unexpected upstream layout')

    def test_boost_bootstrap_uses_base_python_from_virtualenv(self):
        virtualenv = self.root / 'python environment'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(virtualenv)], check=True)
        python = virtualenv / 'bin/python'
        base_prefix = subprocess.check_output(
            [str(python), '-c', 'import sys; print(sys.base_prefix)'], text=True).strip()
        bootstrap = self.source / 'bootstrap.sh'
        bootstrap.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > bootstrap-args.txt\n')
        bootstrap.chmod(0o755)
        builder = self.builder()
        builder.context(self.recipe, 'bootstrap-test')
        builder.variables['source'] = str(self.source)
        builder.env['TC_PYTHON'] = str(python)
        builder.env['PATH'] = str(python.parent) + os.pathsep + builder.env['PATH']
        recipe = load_config(ROOT / 'toolchain.yaml').recipes['boost']
        builder.run_step(recipe['build']['commands'][0])
        args = (self.source / 'bootstrap-args.txt').read_text().splitlines()
        self.assertIn('--with-python=' + str(python), args)
        self.assertIn('--with-python-root=' + base_prefix, args)
        self.assertNotIn('--with-python-root=' + str(virtualenv), args)

    def test_build_resume_and_missing_artifact(self):
        builder = self.builder()
        self.assertEqual(builder.build()['built'], ['hello'])
        self.assertEqual(self.builder().build()['skipped'], ['hello'])
        (builder.prefix / 'include/hello.h').unlink()
        self.assertEqual(self.builder().build()['built'], ['hello'])
        self.assertTrue((builder.prefix / 'share/licenses/hello/LICENSE').is_file())

    def test_changed_dependency_invalidates_consumer(self):
        consumer = copy.deepcopy(self.recipe)
        consumer.update(name='consumer', depends_on=['hello'])
        consumer['build']['copies'][0]['to'] = 'include/consumer.h'
        consumer['artifacts'] = ['include/consumer.h']
        self.write([self.recipe, consumer])
        self.builder().build(['consumer'])
        self.recipe['version'] = '2.0'
        self.write([self.recipe, consumer])
        self.assertEqual(self.builder().build(['consumer'])['built'], ['hello', 'consumer'])

    def test_source_change_invalidates_resume(self):
        self.builder().build()
        (self.source / 'hello.h').write_text('new content')
        builder = self.builder()
        self.assertEqual(builder.build()['built'], ['hello'])
        self.assertEqual((builder.prefix / 'include/hello.h').read_text(), 'new content')

    def test_failure_is_recorded_and_not_skipped(self):
        self.recipe['build'] = {'system': 'custom', 'commands': [[sys.executable, '-c', 'raise SystemExit(7)']]}
        self.write([self.recipe])
        builder = self.builder()
        with self.assertRaisesRegex(ToolchainError, 'exited 7'):
            builder.build()
        self.assertEqual(read_json(builder.state_path)['recipes']['hello']['status'], 'failed')
        self.assertIn('SystemExit', (builder.work / 'logs/hello.log').read_text())
        self.assertFalse(verify(builder.prefix)['passed'])

    def test_missing_install_output_fails(self):
        self.recipe['artifacts'] = ['include/missing.h']
        self.write([self.recipe])
        with self.assertRaisesRegex(ToolchainError, 'did not produce'):
            self.builder().build()

    def test_argv_is_not_a_shell(self):
        literal = '$(touch injected); spaces'
        self.recipe['build'] = {'system': 'custom', 'commands': [
            [sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])',
             '${prefix}/include/hello.h', literal]]}
        self.write([self.recipe])
        builder = self.builder()
        builder.build()
        self.assertEqual((builder.prefix / 'include/hello.h').read_text(), literal)
        self.assertFalse((builder.prefix / 'injected').exists())

    def test_lock_excludes_concurrent_build(self):
        builder = self.builder()
        with exclusive_lock(builder.prefix / 'share/toolchain/.build.lock'):
            with self.assertRaisesRegex(ToolchainError, 'Another operation'):
                builder.build()

    def test_dry_run_has_no_filesystem_side_effects(self):
        before = set(self.root.rglob('*'))
        code, out, err = self.cli('build', '--config', str(self.path), '--dry-run', '--json')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['plan'][0]['name'], 'hello')
        self.assertEqual(set(self.root.rglob('*')), before)

    def test_real_cmake_build_and_consumer(self):
        cmake = shutil.which('cmake') or ('/opt/toolchain-v1/bin/cmake' if Path('/opt/toolchain-v1/bin/cmake').exists() else None)
        if not cmake or not shutil.which('cc'):
            self.skipTest('CMake and cc are required')
        (self.source / 'hello.c').write_text('int answer(void){return 42;}\n')
        (self.source / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\nproject(hello C)\nadd_library(hello STATIC hello.c)\ninstall(TARGETS hello ARCHIVE DESTINATION lib)\n')
        self.recipe.update(stage='library', build={'system': 'cmake'}, artifacts=['lib/libhello.a'])
        self.write([self.recipe])
        env = {'PATH': str(Path(cmake).parent) + ':' + os.environ['PATH']}
        with patch.dict(os.environ, env):
            builder = self.builder(jobs=2)
            builder.build()
            consumer = self.root / 'consumer.c'
            consumer.write_text('int answer(void); int main(void){return answer()!=42;}\n')
            binary = self.root / 'consumer'
            subprocess.run(['cc', str(consumer), str(builder.prefix / 'lib/libhello.a'), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)

    def test_generated_cmake_file_uses_host_compiler_for_library_only_prefix(self):
        cmake = shutil.which('cmake') or ('/opt/toolchain-v1/bin/cmake' if Path('/opt/toolchain-v1/bin/cmake').exists() else None)
        if not cmake or not shutil.which('cc'):
            self.skipTest('CMake and cc required')
        builder = self.builder()
        builder.build()
        consumer = self.root / 'consumer'
        consumer.mkdir()
        (consumer / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.16)\nproject(consumer C)\nadd_executable(consumer main.c)\n')
        (consumer / 'main.c').write_text('int main(void){return 0;}\n')
        subprocess.run([cmake, '-S', str(consumer), '-B', str(consumer / 'build'),
                        '-DCMAKE_TOOLCHAIN_FILE=' + str(builder.prefix / 'share/toolchain/toolchain.cmake')],
                       check=True, capture_output=True)
        subprocess.run([cmake, '--build', str(consumer / 'build')], check=True, capture_output=True)


class InspectionAndCliTests(Workspace):
    def test_archive_metadata_and_checksum(self):
        builder = self.builder()
        builder.build()
        target = self.root / 'bundle.tar.gz'
        report = archive(builder.prefix, target)
        self.assertEqual(report['sha256'], sha256(target))
        with tarfile.open(target) as stream:
            members = stream.getnames()
            self.assertIn('prefix with spaces/include/hello.h', members)
            self.assertIn('prefix with spaces/share/sbom/bom.cdx.json', members)
        self.assertTrue(target.with_name(target.name + '.sha256').exists())
        with self.assertRaisesRegex(ToolchainError, 'exists'):
            archive(builder.prefix, target)

    def test_verify_broken_symlink(self):
        builder = self.builder()
        builder.build()
        (builder.prefix / 'lib/broken.so').symlink_to('absent.so')
        result = verify(builder.prefix)
        self.assertFalse(result['passed'])
        self.assertTrue(any('Broken symlink' in f for f in result['failures']))

    def test_legacy_manifest_and_inventory(self):
        prefix = self.root / 'legacy'
        (prefix / 'share').mkdir(parents=True)
        (prefix / 'share/manifest.yaml').write_text('build:\n  flags: |\n  -DINVALID\n')
        result = info(prefix)
        self.assertFalse(result['managed'])
        self.assertIn('legacy_manifest_warning', result)

    def test_activation_preserves_virtualenv_and_unset_variables(self):
        prefix = self.root / 'activation prefix'
        prefix.mkdir()
        (prefix / 'activate').write_text(activation(prefix))
        for shell in ('bash', 'zsh'):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                script = 'set -eu; unset CC CXX CFLAGS CXXFLAGS TOOLCHAIN_PREFIX LD_LIBRARY_PATH; orig=$PATH; VIRTUAL_ENV=sentinel; export VIRTUAL_ENV; source "$1"; source "$1"; test "$VIRTUAL_ENV" = sentinel; toolchain_deactivate; test "$PATH" = "$orig"; test "${CC+x}" != x; test "${LD_LIBRARY_PATH+x}" != x'
                subprocess.run([shell, '-c', script, 'fixture', str(prefix / 'activate')], check=True)

    def test_libcxx_activation_selects_runtime(self):
        prefix = self.root / 'libcxx'
        prefix.mkdir()
        (prefix / 'activate').write_text(activation(prefix, 'libc++'))
        result = subprocess.run(['bash', '-c', 'source "$1"; printf "%s" "$CXXFLAGS"', 'fixture', str(prefix / 'activate')],
                                text=True, capture_output=True, check=True)
        self.assertIn('-stdlib=libc++', result.stdout)

    def test_external_compiler_keeps_selected_library_tools_first(self):
        prefix, external = self.root / 'sdk', self.root / 'compiler'
        for root in (prefix, external):
            (root / 'bin').mkdir(parents=True)
            tool = root / 'bin/protoc'
            tool.write_text('#!/bin/sh\nexit 0\n')
            tool.chmod(0o755)
        for name in ('clang', 'clang++'):
            (external / 'bin' / name).symlink_to('/bin/true')
        (prefix / 'activate').write_text(activation(prefix, compiler={
            'mode': 'external', 'prefix': str(external), 'cc': 'clang', 'cxx': 'clang++'}))
        for shell in ('bash', 'zsh'):
            if not shutil.which(shell):
                continue
            script = 'set -eu; unset LD_LIBRARY_PATH; source "$1/activate"; test "$(command -v protoc)" = "$1/bin/protoc"; test "$CXX" = "$2/bin/clang++"; case "$LD_LIBRARY_PATH" in "$1/lib:$1/lib64:"*) ;; *) exit 1;; esac; toolchain_deactivate'
            subprocess.run([shell, '-c', script, 'fixture', str(prefix), str(external)], check=True)

    def test_archive_refuses_concurrent_install(self):
        builder = self.builder()
        builder.build()
        with exclusive_lock(builder.prefix / 'share/toolchain/.build.lock'):
            with self.assertRaisesRegex(ToolchainError, 'Another operation'):
                archive(builder.prefix, self.root / 'bundle.tar.gz')

    def test_cli_config_after_nouns_and_before_name(self):
        for argv in [('recipes', 'show', '--config', str(self.path), 'hello', '--json'),
                     ('recipes', 'show', 'hello', '--config', str(self.path), '--json')]:
            code, out, err = self.cli(*argv)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['name'], 'hello')

    def test_find_requires_name_and_no_match_exit(self):
        code, _, err = self.cli('inspect', 'find', '--prefix', str(self.root))
        self.assertEqual(code, 1)
        self.assertIn('requires', err)
        code, _, _ = self.cli('inspect', 'find', 'missing*', '--prefix', str(self.root))
        self.assertEqual(code, 1)

    def test_init_preset_and_refuse_overwrite(self):
        target = self.root / 'new.yaml'
        code, _, err = self.cli('init', str(target), '--preset', 'poc')
        self.assertEqual(code, 0, err)
        self.assertEqual(len(load_config(target).recipes), 37)
        code, _, _ = self.cli('init', str(target))
        self.assertEqual(code, 1)

    def test_add_then_build_local_recipe(self):
        self.write([])
        code, _, err = self.cli('add', 'hello', '--version', '1', '--path', str(self.source), '--system', 'custom',
                                '--command', 'cp "$TC_SOURCE/hello.h" "$TC_PREFIX/include/hello.h"', '--artifact', 'include/hello.h',
                                '--config', str(self.path))
        self.assertEqual(code, 0, err)
        self.builder().build()


if __name__ == '__main__':
    unittest.main()
