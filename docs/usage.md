# Usage reference

Start with the [README quickstart](../README.md#quickstart). This page covers the larger preset, inspection, and custom recipes.

## Install and build the POC toolchain

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

toolchain recipes validate
toolchain doctor
toolchain plan

toolchain build --jobs 8 --quiet --archive
```

The repository's [toolchain.yaml](../toolchain.yaml) installs into
`./install/toolchain-v1`. This keeps an existing `/opt/toolchain-v1` available
for comparison. To use the POC's exact installation location, supply
`--prefix /opt/toolchain-v1` to both `doctor` and `build`; that directory must be
writable. A fresh compiler build can take hours. `doctor` reports missing host
packages and prints an installation command; it never changes system packages.

Install the dependencies reported by `doctor` on your host before starting the full build.

```sh
# Optional: acquire sources before starting compilation and freeze their identities.
toolchain fetch
# Use the source SHA-256 hashes and git commits recorded by fetch.
toolchain build --locked --jobs 8 --quiet --archive

# Repeating the same build resumes: successful, unchanged components are skipped.
toolchain build --jobs 8 --quiet
# Inspect progress or diagnose a failure.
toolchain status
# Build a component and its dependencies.
toolchain build --library fmt
```

`--quiet` keeps compiler output in `.toolchain-work/logs/<recipe>.log`; progress
and the failing command are still printed. `--force` reruns selected recipes and
their dependencies, keeping their build directories for incremental compilation.
Configuration, source, standard-library, prefix, and dependency changes invalidate
completed build records. Failed and interrupted recipes are never marked complete.
A prefix lock prevents simultaneous installations into the same toolchain.

`plan` and `build --dry-run` perform no downloads, builds, or filesystem writes.
`--jobs` controls build parallelism; LLVM link jobs are capped at two in the preset.
`--stdlib libc++` adds LLVM's libc++/libc++abi runtimes and selects libc++ for
third-party C++ builds. Its complete build is a separate validation target.

## Inspect and use a toolchain

```sh
toolchain inspect info --prefix ./install/toolchain-v1
toolchain inspect components --prefix ./install/toolchain-v1
toolchain inspect libs --prefix ./install/toolchain-v1
toolchain inspect bins --prefix ./install/toolchain-v1
toolchain inspect headers --prefix ./install/toolchain-v1
toolchain inspect find 'libantlr4*.a' --prefix ./install/toolchain-v1
# Check every expected component, then compile/link/run a C++20 consumer.
toolchain inspect verify --prefix ./install/toolchain-v1 --config toolchain.yaml --smoke

source ./install/toolchain-v1/activate
# Restore the environment without interfering with Python virtual environments.
toolchain_deactivate

cmake -S my-app -B my-app/build \
  -DCMAKE_TOOLCHAIN_FILE="$PWD/install/toolchain-v1/share/toolchain/toolchain.cmake"
```

Use `--json` on plan, status, doctor, recipe, inspection, build, and archive commands
for structured output. `inspect verify` and unsuccessful `inspect find` return a
nonzero exit code. Verification checks declared files and broken symlinks; it does
not prove every exported API or transitive ABI is correct. `--smoke` checks the C++20
runtime and links fmt/spdlog when present.

An installation records build status and source provenance in
`share/toolchain/manifest.json`, a readable `share/manifest.yaml`, a CycloneDX
component inventory in `share/sbom/bom.cdx.json`, and source license files under
`share/licenses/`. The inventory covers top-level recipes; it is not a complete
SBOM of dependencies fetched inside GCC, CMake, or Bazel.

```sh
toolchain archive --prefix ./install/toolchain-v1 --output output/toolchain.tar.gz
```

Archives include activation, CMake integration, metadata, and licenses, plus an
adjacent SHA-256 file. Existing output requires explicit `--force`. The activation
script and CMake prefix lookup support relocation, but some upstream binaries
and package files embed absolute paths. For deployment, build at the intended
installation prefix and use a compatible host distribution; this is not a
self-contained glibc/sysroot distribution.

## Extend and test recipes

```sh
# Export all 37 recipes into an editable, self-contained YAML file.
toolchain init my-toolchain.yaml --preset poc --name my-toolchain --prefix ./my-install

# Or start with an empty toolchain.
toolchain init small.yaml --name small --prefix ./small-install
toolchain add fmt --config small.yaml --version 11.2.0 \
  --url https://github.com/fmtlib/fmt/archive/refs/tags/11.2.0.tar.gz \
  --system cmake --option=-DFMT_TEST=OFF \
  --artifact lib/libfmt.a --artifact include/fmt/format.h

toolchain recipes show fmt --config small.yaml
toolchain test fmt --config small.yaml --keep

# Exercise a POC library with an already-built compiler, in a temporary prefix.
# Core recipes are replaced by the explicitly selected compiler installation.
toolchain test fmt --compiler-prefix /opt/toolchain-v1 --smoke --keep
```

`test` builds the requested recipe and all its dependencies in an isolated prefix.
Without `--compiler-prefix`, a POC recipe also builds its compiler dependencies.
Successful tests are deleted unless `--keep` is supplied. Failed tests retain their
files and logs automatically. Nothing is installed into the external compiler
prefix.

See [recipe format](recipes.md), [POC parity](poc-parity.md), and
[validation results](validation.md). The standard-library test suite runs with:

```sh
python -m unittest discover -s tests -v
```

## Cache and reproducibility

Configuration-relative defaults are `.toolchain-cache` for downloads and pristine
sources, `.toolchain-work` for private working copies and logs, and `output` for
archives. Override them with `--cache`, `--work`, and `--prefix`. Source acquisition
checks configured SHA-256 hashes, checks out the requested git ref before updating
submodules, rejects unsafe archive paths, and publishes completed caches atomically.
`toolchain.lock.json` records actual archive hashes and git commits. Keep it with
your build definition; `--locked` rejects changed or unrecorded sources. Branches
such as googleapis `master` are frozen when fetched, not automatically updated.

`--offline` restricts the CLI's **top-level source acquisition** to its cache.
GCC prerequisites, Arrow dependencies, and Bazel/Protovalidate vendoring may still
need network access. The preset does not claim a fully hermetic offline build.
YAML custom shell steps and downloaded project build scripts execute code; use
recipes and sources you trust.
