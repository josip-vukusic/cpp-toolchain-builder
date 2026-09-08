"""Consumer acceptance checks: build, archive, relocate, activate, and compile."""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cpp_toolchain_builder.config import load_config, save_yaml
from cpp_toolchain_builder.engine import Builder
from cpp_toolchain_builder.inspection import activation, archive, verify


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='toolchain-distribution-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cmake = shutil.which('cmake')
        if not self.cmake and Path('/opt/toolchain-v1/bin/cmake').is_file():
            self.cmake = '/opt/toolchain-v1/bin/cmake'
        if not self.cmake or not shutil.which('g++'):
            self.skipTest('CMake and g++ are required for consumer acceptance tests')
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'answer.hpp').write_text('#pragma once\nint answer();\n')
        (self.source / 'answer.cpp').write_text('int answer() { return 42; }\n')
        (self.source / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.16)
project(answer LANGUAGES CXX)
add_library(answer STATIC answer.cpp)
install(TARGETS answer ARCHIVE DESTINATION lib)
install(FILES answer.hpp DESTINATION include)
''')
        self.config = self.root / 'toolchain.yaml'
        save_yaml(self.config, {'schema_version': 1, 'toolchain': {
            'name': 'acceptance', 'prefix': 'original SDK', 'cc': shutil.which('gcc'),
            'cxx': shutil.which('g++')}, 'libraries': [{
                'name': 'answer', 'version': '1', 'source': {'path': str(self.source)},
                'build': {'system': 'cmake'}, 'artifacts': ['lib/libanswer.a', 'include/answer.hpp']}]})
        with patch.dict(os.environ, {'PATH': str(Path(self.cmake).parent) + ':' + os.environ['PATH']}):
            self.builder = Builder(load_config(self.config), quiet=True, jobs=2)
            self.builder.build()

    def test_library_only_smoke_uses_recorded_host_compiler(self):
        result = verify(self.builder.prefix, run_smoke=True)
        self.assertTrue(result['passed'], result['failures'])
        self.assertEqual(result['smoke']['compiler'], shutil.which('g++'))

    def test_archive_is_readable_and_consumable_after_relocation(self):
        executable = self.builder.prefix / 'bin/private-mode-tool'
        executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o700)
        destination = self.root / 'sdk.tar.gz'
        archive(self.builder.prefix, destination)
        moved = self.root / 'another machine'
        moved.mkdir()
        with tarfile.open(destination) as stream:
            for member in stream.getmembers():
                if member.isfile():
                    self.assertEqual(member.mode & 0o444, 0o444, member.name)
                if member.name.endswith('/private-mode-tool'):
                    self.assertEqual(member.mode & 0o555, 0o555)
            stream.extractall(moved, filter='data')
        prefix = moved / self.builder.prefix.name
        # Ensure accidental references to the original installation cannot succeed.
        shutil.rmtree(self.builder.prefix)
        self.assertTrue(verify(prefix, run_smoke=True)['passed'])
        consumer = self.root / 'consumer'
        consumer.mkdir()
        (consumer / 'main.cpp').write_text('#include <answer.hpp>\nint main(){return answer()!=42;}\n')
        (consumer / 'CMakeLists.txt').write_text('''cmake_minimum_required(VERSION 3.16)
project(consumer LANGUAGES CXX)
add_executable(consumer main.cpp)
find_library(ANSWER_LIBRARY answer REQUIRED)
target_link_libraries(consumer PRIVATE "${ANSWER_LIBRARY}")
''')
        for shell in ('bash', 'zsh'):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell):
                build = self.root / ('consumer-' + shell)
                # No Python, source tree, package registry, or builder CLI is used
                # by this consumer. Header lookup must come from activation.
                script = '''set -eu
source "$1/activate"
"$2" -S "$3" -B "$4" -DCMAKE_CXX_STANDARD=20 -DCMAKE_LIBRARY_PATH="$1/lib"
"$2" --build "$4"
"$4/consumer"
toolchain_deactivate
'''
                result = subprocess.run([shell, '-c', script, 'acceptance', str(prefix), self.cmake,
                                         str(consumer), str(build)], capture_output=True, text=True,
                                        env={**os.environ, 'PATH': '/usr/bin:/bin', 'CFLAGS': '', 'CXXFLAGS': ''})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                cache = (build / 'CMakeCache.txt').read_text()
                self.assertIn('CMAKE_CXX_COMPILER:FILEPATH=' + shutil.which('g++'), cache)

    def test_external_compiler_failure_restores_environment(self):
        compiler = {'mode': 'external', 'prefix': str(self.root / 'missing compiler'),
                    'cc': 'clang', 'cxx': 'clang++'}
        target = self.root / 'activate-external'
        target.write_text(activation(self.builder.prefix, compiler=compiler))
        script = '''set -eu
old_path=$PATH
export CXX=original-cxx
if source "$1"; then exit 9; fi
test "$PATH" = "$old_path"
test "$CXX" = original-cxx
'''
        result = subprocess.run(['bash', '-c', script, 'acceptance', str(target)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('compiler unavailable', result.stderr)

    def test_bundled_gcc_activation_selects_gcc_and_restores_previous_compiler(self):
        prefix = self.root / 'gcc bundle'
        (prefix / 'bin').mkdir(parents=True)
        for name in ('gcc', 'g++'):
            (prefix / 'bin' / name).symlink_to(shutil.which(name))
        target = prefix / 'activate'
        target.write_text(activation(prefix, compiler={'mode': 'bundled', 'prefix': str(prefix), 'cc': 'gcc', 'cxx': 'g++'}))
        script = '''set -eu
export CC=original-cc CXX=original-cxx
source "$1/activate"
test "$CC" = "$1/bin/gcc"
test "$CXX" = "$1/bin/g++"
toolchain_deactivate
test "$CC" = original-cc
test "$CXX" = original-cxx
'''
        subprocess.run(['bash', '-c', script, 'acceptance', str(prefix)], check=True)


if __name__ == '__main__':
    unittest.main()
