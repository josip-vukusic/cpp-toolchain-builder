#!/usr/bin/env bash
# Run after the README example has been built and archived.
set -euo pipefail

if [[ "${1:-}" != --isolated ]]; then
  # No network is available to any process in the acceptance test.
  exec unshare --user --map-root-user --net bash "$0" --isolated
fi

repo=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cmake_bin=$(command -v cmake)
ctest_bin=$(command -v ctest)
original="$repo/install/example-libraries"
archive="$repo/output/example-libraries.tar.gz"
[[ -d "$original" && -f "$archive" ]] || {
  echo 'Build examples/library.yaml and archive install/example-libraries first.' >&2
  exit 1
}
scratch=$(mktemp -d -t toolchain-consumer-XXXXXXXX)
restore() {
  if [[ -d "$scratch/original-hidden" ]]; then
    mv "$scratch/original-hidden" "$original"
  fi
  rm -rf "$scratch"
}
trap restore EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
mkdir -p "$scratch/another machine"
(cd "$(dirname "$archive")" && sha256sum -c "$(basename "$archive").sha256")
tar -xzf "$archive" -C "$scratch/another machine"
cp -R "$repo/examples/hello" "$scratch/consumer"
mv "$original" "$scratch/original-hidden"

# The receiving environment has no builder CLI/Python environment on PATH.
# The original prefix is absent, so absolute references cannot hide a failure.
env -i PATH=/usr/bin:/bin bash --noprofile --norc -c '
  set -eu
  cd "$1"
  source "$1/another machine/example-libraries/activate"
  "$2" -S "$1/consumer" -B "$1/build"
  "$2" --build "$1/build"
  "$3" --test-dir "$1/build" --output-on-failure
  "$1/build/hello"
  toolchain_deactivate
' acceptance "$scratch" "$cmake_bin" "$ctest_bin"
printf 'Passed: archive, relocation, activation, C++ build and run with networking disabled.\n'
