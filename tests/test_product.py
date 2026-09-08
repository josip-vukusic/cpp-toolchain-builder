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
from unittest.mock import patch

import yaml

from cpp_toolchain_builder.cli import main
from cpp_toolchain_builder.config import load_config, resolve, save_yaml
from cpp_toolchain_builder.engine import Builder, Runner
from cpp_toolchain_builder.inspection import activation, archive, info, inventory, smoke, verify
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


class BuildTests(Workspace):
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
