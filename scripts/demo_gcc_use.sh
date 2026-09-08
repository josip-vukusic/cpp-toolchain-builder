#!/usr/bin/env bash
# The Python recorder provides a disposable working directory and a mount namespace.
set -euo pipefail
: "${TOOLCHAIN_DEMO_WORKSPACE:?Run through scripts/record_gcc_demo.py}"
: "${TOOLCHAIN_DEMO_ORIGINAL:?Original SDK prefix is required}"
[[ "$PWD" == "$TOOLCHAIN_DEMO_WORKSPACE" ]]

scene() { printf '\033[2J\033[H# %s\n\n' "$1"; }
run() {
  printf '$ %s\n' "$1"
  sleep 0.8
  eval "$1"
  printf '\n'
  sleep 1.2
}

scene '1 / 3  COPY YOUR PREPARED SDK'
printf '# GCC, CMake, Make, binutils, fmt and spdlog are already built.\n\n'
run 'cp -a prepared-sdk/. toolchain'
# Hide the real original path, including its access through prepared-sdk.
# This bind mount exists only inside this process's private mount namespace.
mkdir original-unavailable
mount --bind original-unavailable "$TOOLCHAIN_DEMO_ORIGINAL"
test ! -e "$TOOLCHAIN_DEMO_ORIGINAL/bin/g++"
for directory in /usr/lib/gcc /usr/include/c++; do
  if [[ -d "$directory" ]]; then mount --bind original-unavailable "$directory"; fi
done
printf '# Original installation is now unavailable in this test.\n'
sleep 2

scene '2 / 3  ACTIVATE THE COPY'
# A minimal PATH has ordinary shell utilities, but no host development tools.
export PATH="$TOOLCHAIN_DEMO_WORKSPACE/base-bin"
! command -v g++ >/dev/null
! command -v cmake >/dev/null
! command -v make >/dev/null
printf '# Starting without a compiler, CMake or Make on PATH.\n\n'
run 'source toolchain/activate'
run 'command -v g++ cmake make'
for tool in gcc g++ cmake make as ld; do
  [[ "$(command -v "$tool")" == "$PWD/toolchain/bin/$tool" ]]
done
for helper in cc1 cc1plus collect2 as ld; do
  path=$(g++ -print-prog-name="$helper")
  if [[ "$path" != */* ]]; then path=$(command -v "$path"); fi
  case "$(/usr/bin/realpath "$path")" in
    "$PWD/toolchain/"*) ;;
    *) printf 'Compiler helper is outside the SDK: %s\n' "$path" >&2; exit 1 ;;
  esac
done
! command -v toolchain >/dev/null
run 'g++ -dumpfullversion; cmake --version | head -n 1; make --version | head -n 1'
sleep 2

scene '3 / 3  COMPILE WITH THE BUNDLED TOOLS'
run 'cmake -S hello -B build --log-level=ERROR'
run 'cmake --build build'
run './build/hello'
printf '# Compiler, CMake, Make and libraries came from the copied folder.\n'
printf '# Compatible Linux runtime and C library headers are still required.\n'
sleep 4
