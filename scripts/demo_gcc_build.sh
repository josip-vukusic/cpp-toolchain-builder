#!/usr/bin/env bash
# record_gcc_demo.py runs this in the repository root with an explicit builder.
set -u
: "${TOOLCHAIN_DEMO_BUILDER:?Run through scripts/record_gcc_demo.py}"
jobs=${TOOLCHAIN_DEMO_JOBS:-8}
printf '# Build a GCC SDK from pinned sources. Completed components can resume.\n'
printf '$ toolchain build --config examples/gcc-toolchain.yaml --jobs %s --quiet --locked\n' "$jobs"
SECONDS=0
"$TOOLCHAIN_DEMO_BUILDER" build --config examples/gcc-toolchain.yaml --jobs "$jobs" --quiet --locked
status=$?
printf '\nBuild exit code: %s; elapsed: %s seconds.\n' "$status" "$SECONDS"
exit "$status"
