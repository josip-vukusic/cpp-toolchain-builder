#!/usr/bin/env bash
# Executed only inside the disposable workspace prepared by record_demo.py.
set -euo pipefail
: "${TOOLCHAIN_DEMO_WORKSPACE:?Run this through scripts/record_demo.py}"
[[ "$PWD" == "$TOOLCHAIN_DEMO_WORKSPACE" ]]

scene() {
  printf '\033[2J\033[H# %s\n\n' "$1"
}

run() {
  printf '$ %s\n' "$1"
  sleep 0.8
  eval "$1"
  printf '\n'
  sleep 1.2
}

# Prerequisites and source downloads are prepared before recording starts.
source .venv/bin/activate

scene '1 / 4  BUILD THE LIBRARIES'
printf '# fmt + spdlog; host compiler; sources already cached.\n\n'
run 'toolchain build --config examples/library.yaml --jobs 4 --quiet --locked'
sleep 2

scene '2 / 4  COPY THE FOLDER'
run 'mkdir -p shared'
run 'cp -a install/example-libraries shared/toolchain'
printf '# Make the original path unavailable to check relocation.\n\n'
run 'mv install/example-libraries install/original-hidden'
test ! -e install/example-libraries
sleep 2

scene '3 / 4  ACTIVATE THE COPY'
printf '# Leave the builder environment; keep the host compiler and CMake.\n\n'
run 'deactivate'
run 'source shared/toolchain/activate'
run 'command -v toolchain || echo "No builder CLI on PATH"'
if command -v toolchain >/dev/null; then
  echo 'The consumer must not have the builder on PATH.' >&2
  exit 1
fi
sleep 2

scene '4 / 4  COMPILE AND RUN AN APPLICATION'
run 'cmake -S examples/hello -B build/hello --log-level=ERROR'
run 'cmake --build build/hello'
run './build/hello/hello'
printf '# Built and ran using the copied libraries.\n'
printf '# Same-host relocation; compatible Linux machines required.\n'
sleep 4
