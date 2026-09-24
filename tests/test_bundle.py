from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_product import ROOT, Workspace
from cpp_toolchain_builder.bundle import Bundle
from cpp_toolchain_builder.config import load_config
from cpp_toolchain_builder.inspection import archive, smoke, verify
from cpp_toolchain_builder.util import ToolchainError, read_json


class BundleTests(Workspace):
    def setUp(self):
        super().setUp()
        self.settings.update(compiler='toolchain', variants=['standard', 'asan', 'tsan'], lockfile='sources.lock.json')
        for name in ('clang', 'clang++', 'cmake'):
            (self.source / name).write_text('#!/bin/sh\nexit 0\n')
        self.compiler = {'name': 'compiler', 'version': '1', 'stage': 'core', 'requires': [],
            'source': {'path': str(self.source)}, 'build': {'system': 'copy', 'copies': [
                {'from': '${source}/' + name, 'to': 'bin/' + name, 'mode': '755'}
                for name in ('clang', 'clang++', 'cmake')]},
            'artifacts': ['bin/clang', 'bin/clang++', 'bin/cmake']}
        self.recipe.update(stage='library', depends_on=['compiler'], build={'system': 'custom', 'commands': [
            [sys.executable, '-c', 'import os; from pathlib import Path; '
             'Path(os.environ["TC_PREFIX"], "include/hello.h").write_text(os.environ["CFLAGS"])']]})
        self.write([self.compiler, self.recipe])
        self.bundle().fetch_all()

    def bundle(self, **options):
        return Bundle(load_config(self.path), quiet=True, **options)

    def test_config_validation(self):
        for variants in ([], 'standard', ['asan'], ['standard', 'standard'], ['standard', '../asan'], ['standard', 1]):
            with self.subTest(variants=variants):
                self.settings['variants'] = variants
                self.write([self.compiler, self.recipe])
                with self.assertRaisesRegex(ToolchainError, 'variants'):
                    load_config(self.path)
        self.settings['variants'] = ['standard', 'asan', 'tsan']
        for key, value in [('compiler_prefix', '/opt/old'), ('sanitizer', 'asan-ubsan'), ('compiler', 'system')]:
            with self.subTest(key=key):
                original = copy.deepcopy(self.settings)
                self.settings[key] = value
                self.write([self.compiler, self.recipe])
                with self.assertRaisesRegex(ToolchainError, 'Bundle variants require'):
                    load_config(self.path)
                self.settings = original

    def test_plan_is_read_only_and_shares_compiler_cache_lock(self):
        before = set(self.root.rglob('*'))
        code, out, err = self.cli('build', '--config', str(self.path), '--locked', '--dry-run', '--json')
        self.assertEqual(code, 0, err)
        plan = json.loads(out)['variants']
        self.assertEqual([r['name'] for r in plan['standard']], ['compiler', 'hello'])
        for name, flag in [('asan', '-fsanitize=address,undefined'), ('tsan', '-fsanitize=thread')]:
            self.assertEqual([r['name'] for r in plan[name]], ['hello'])
            self.assertIn('/standard/bin/clang', plan[name][0]['environment']['CC'])
            self.assertIn(flag, plan[name][0]['environment']['CFLAGS'])
        self.assertNotIn('-fsanitize=', plan['standard'][-1]['environment']['CFLAGS'])
        self.assertEqual(set(self.root.rglob('*')), before)
        bundle = self.bundle(locked=True)
        self.assertEqual(len({b.cache_path for b in bundle.builders.values()}), 1)
        self.assertEqual(len({b.lockfile for b in bundle.builders.values()}), 1)
        self.assertEqual(len({b.work for b in bundle.builders.values()}), 3)
        self.assertEqual(len({b.state_path for b in bundle.builders.values()}), 3)

    def test_build_skip_status_and_verify_all_variants(self):
        code, out, err = self.cli('build', '--config', str(self.path), '--locked', '--quiet', '--skip-doctor', '--json')
        self.assertEqual(code, 0, err)
        built = json.loads(out)['variants']
        self.assertEqual(built['standard']['built'], ['compiler', 'hello'])
        self.assertEqual(built['asan']['built'], ['hello'])
        self.assertEqual(built['tsan']['built'], ['hello'])
        bundle = self.bundle(locked=True)
        result = bundle.build()
        self.assertTrue(all(not r['built'] for r in result['variants'].values()))
        for arguments in [('inspect', 'verify', '--prefix', str(bundle.prefix)),
                          ('inspect', 'verify', '--config', str(self.path))]:
            code, out, err = self.cli(*arguments, '--json')
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['components_checked'], 4)
        code, out, err = self.cli('status', '--config', str(self.path), '--locked', '--json')
        self.assertEqual(code, 0, err)
        self.assertTrue(all(item['status'] == 'complete' for entries in json.loads(out).values() for item in entries))

    def test_resume_restores_environment_and_starts_pending_variant(self):
        marker = self.root / 'continue'
        self.recipe['build']['commands'].insert(0, [sys.executable, '-c',
            'import os,sys; from pathlib import Path; '
            f'sys.exit(7 if "address" in os.environ["CFLAGS"] and not Path({str(marker)!r}).exists() else 0)'])
        self.write([self.compiler, self.recipe])
        # Commands changed, but source identities and lock records did not.
        with self.assertRaisesRegex(ToolchainError, 'exited 7'):
            self.bundle(locked=True).build()
        marker.touch()
        with patch.dict(os.environ, {'PATH': '/different-shell:' + os.environ['PATH']}):
            resumed = self.bundle(locked=True, resume=True)
            result = resumed.build()['variants']
        self.assertEqual(result['standard']['built'], [])
        self.assertEqual(result['asan']['built'], ['hello'])
        self.assertEqual(result['tsan']['built'], ['hello'])
        self.assertTrue(verify(resumed.prefix)['passed'])

    def test_compiler_changes_invalidate_sanitizer_variants(self):
        before = self.bundle(locked=True).plan()
        self.compiler['description'] = 'Compiler recipe changed'
        self.write([self.compiler, self.recipe])
        after = self.bundle(locked=True).plan()
        for variant in ('standard', 'asan', 'tsan'):
            self.assertNotEqual(before[variant][-1]['fingerprint'], after[variant][-1]['fingerprint'])

    def test_relocation_activation_cmake_smoke_and_archive(self):
        bundle = self.bundle(locked=True)
        bundle.build()
        destination = self.root / 'relocated bundle'
        shutil.move(bundle.prefix, destination)
        self.assertTrue(verify(destination)['passed'])
        for variant in ('asan', 'tsan'):
            prefix = destination / variant
            for shell in ('bash', 'zsh'):
                if not shutil.which(shell):
                    continue
                script = 'set -eu; source "$1"; test "$CXX" = "$2/standard/bin/clang++"; test "$TOOLCHAIN_PREFIX" = "$3"; toolchain_deactivate'
                subprocess.run([shell, '-c', script, 'fixture', str(prefix / 'activate'), str(destination), str(prefix)], check=True)
            metadata = read_json(prefix / 'share/toolchain/manifest.json')['toolchain']['compiler']
            self.assertEqual(metadata['relative_prefix'], '../standard')
            cmake = shutil.which('cmake') or ('/opt/toolchain-v2/bin/cmake' if Path('/opt/toolchain-v2/bin/cmake').is_file() else None)
            if cmake:
                script = self.root / 'check.cmake'
                output = self.root / 'cmake-path.txt'
                script.write_text(f'include("{prefix}/share/toolchain/toolchain.cmake")\n'
                                  f'file(WRITE "{output}" "${{CMAKE_CXX_COMPILER}}")\n')
                subprocess.run([cmake, '-P', str(script)], check=True, capture_output=True)
                self.assertEqual(Path(output.read_text()).resolve(), destination / 'standard/bin/clang++')
            with patch('cpp_toolchain_builder.inspection.subprocess.run', return_value=Mock(returncode=0)) as run:
                report = smoke(prefix)
                self.assertEqual(report['compiler'], str(destination / 'standard/bin/clang++'))
                self.assertEqual(run.call_args_list[0].args[0][0], report['compiler'])
            with self.assertRaisesRegex(ToolchainError, 'whole bundle'):
                archive(prefix, self.root / 'wrong.tar.gz')
        target = self.root / 'sdk.tar.gz'
        archive(destination, target)
        with tarfile.open(target) as stream:
            for name in ('standard', 'asan', 'tsan'):
                self.assertIn(f'relocated bundle/{name}/activate', stream.getnames())
        (destination / 'tsan/include/hello.h').unlink()
        self.assertFalse(verify(destination)['passed'])
        with self.assertRaisesRegex(ToolchainError, 'incomplete'):
            archive(destination, self.root / 'incomplete.tar.gz')

    def test_rejects_conflicting_options_and_unlocked_build(self):
        for options in ({'compiler_prefix': '/compiler'}, {'sanitizer': 'tsan'},
                        {'prefix': str(self.root)}, {'work': str(self.root / 'prefix with spaces/nested')}):
            with self.subTest(options=options):
                with self.assertRaises(ToolchainError):
                    self.bundle(**options)
        with self.assertRaisesRegex(ToolchainError, '--locked'):
            self.bundle().build()
        with self.assertRaisesRegex(ToolchainError, 'No previous bundle'):
            self.bundle(resume=True)
        with self.assertRaisesRegex(ToolchainError, 'omit --library'):
            self.bundle().plan(['hello'])

    def test_missing_variant_and_wrong_profile_fail_verification(self):
        bundle = self.bundle(locked=True)
        bundle.write_metadata()
        self.assertFalse(verify(bundle.prefix)['passed'])
        bundle.build()
        path = bundle.prefix / 'tsan/share/toolchain/manifest.json'
        record = json.loads(path.read_text())
        record['toolchain']['sanitizer'] = 'asan-ubsan'
        path.write_text(json.dumps(record))
        self.assertFalse(verify(bundle.prefix)['passed'])

    def test_cli_inspection_inventory_and_bundle_archive(self):
        bundle = self.bundle(locked=True)
        bundle.build()
        for action in ('info', 'components', 'headers'):
            code, out, err = self.cli('inspect', action, '--prefix', str(bundle.prefix), '--json')
            self.assertEqual(code, 0, err)
            result = json.loads(out)
            if action == 'headers':
                self.assertEqual({entry['path'] for entry in result},
                                 {f'{name}/include/hello.h' for name in bundle.variants})
            elif action == 'components':
                self.assertEqual(set(result), set(bundle.variants))
            else:
                self.assertEqual(set(result['variants']), set(bundle.variants))
        code, out, err = self.cli('build', '--config', str(self.path), '--locked', '--quiet',
                                 '--skip-doctor', '--archive', '--json')
        self.assertEqual(code, 0, err)
        self.assertTrue(Path(json.loads(out)['distribution']['archive']).is_file())

    def test_resume_rejects_changed_completed_recipes_before_building(self):
        bundle = self.bundle(locked=True)
        bundle.build()
        state = (bundle.prefix / 'standard/share/toolchain/build-state.json').read_bytes()
        self.recipe['description'] = 'changed recipe'
        self.write([self.compiler, self.recipe])
        with self.assertRaisesRegex(ToolchainError, 'completed recipes have changed'):
            self.bundle(locked=True, resume=True).build()
        self.assertEqual((bundle.prefix / 'standard/share/toolchain/build-state.json').read_bytes(), state)

    def test_real_v3_plan_uses_existing_versions_without_old_sdk(self):
        config = load_config(ROOT / 'toolchain-v3.yaml')
        config.settings.update(prefix=str(self.root / 'v3'), work=str(self.root / 'v3-work'))
        bundle = Bundle(config, locked=True)
        plan = bundle.plan()
        self.assertEqual({name: len(entries) for name, entries in plan.items()}, {'standard': 37, 'asan': 30, 'tsan': 30})
        for variant in ('asan', 'tsan'):
            for entry in plan[variant]:
                self.assertNotIn('/opt/toolchain-v2', json.dumps(entry))
        self.assertEqual(bundle.builders['asan'].compiler_prefix, self.root / 'v3/standard')

    def test_relocated_cmake_files_compile_consumers_for_all_profiles(self):
        # A tiny fixture wraps an available compiler; no compiler/SDK source build.
        tools = Path('/opt/toolchain-v2/bin')
        if not all((tools / name).is_file() for name in ('clang', 'clang++', 'cmake')):
            self.skipTest('An installed Clang/CMake SDK is required for the consumer check')
        for name in ('clang', 'clang++', 'cmake'):
            extra = ' --gcc-toolchain=' + shlex.quote(str(tools.parent)) if name != 'cmake' else ''
            (self.source / name).write_text(f'#!/bin/sh\nexec {shlex.quote(str(tools / name))}{extra} "$@"\n')
        self.bundle().fetch_all()
        bundle = self.bundle(locked=True)
        bundle.build()
        destination = self.root / 'moved sdk'
        shutil.move(bundle.prefix, destination)
        consumer = self.root / 'consumer'
        consumer.mkdir()
        (consumer / 'main.cc').write_text('#include <vector>\nint main() { std::vector<int> v{42}; return v[0] != 42; }\n')
        (consumer / 'CMakeLists.txt').write_text('cmake_minimum_required(VERSION 3.20)\n'
            'project(consumer LANGUAGES CXX)\nadd_executable(consumer main.cc)\n')
        for variant in bundle.variants:
            build = consumer / variant
            subprocess.run([str(tools / 'cmake'), '-S', str(consumer), '-B', str(build),
                '-DCMAKE_BUILD_TYPE=Debug',
                f'-DCMAKE_TOOLCHAIN_FILE={destination / variant}/share/toolchain/toolchain.cmake'],
                check=True, capture_output=True, text=True)
            subprocess.run([str(tools / 'cmake'), '--build', str(build)], check=True, capture_output=True, text=True)
            self.assertTrue((build / 'consumer').is_file())
            cache = (build / 'CMakeCache.txt').read_text()
            compiler = next(line.split('=', 1)[1] for line in cache.splitlines() if line.startswith('CMAKE_CXX_COMPILER:'))
            self.assertEqual(Path(compiler).resolve(), destination / 'standard/bin/clang++')
            profile_flag = {'standard': None, 'asan': '-fsanitize=address,undefined', 'tsan': '-fsanitize=thread'}[variant]
            if profile_flag:
                self.assertIn(profile_flag, cache)
