# Usage reference

Start with the [README quickstart](../README.md#quickstart). This page covers the larger preset, inspection, and custom recipes.

For one build containing `standard`, `asan`, and `tsan` variants, use
[`toolchain-v3.yaml`](../toolchain-v3.yaml). See [bundle commands](bundles.md) for
building from scratch, resuming, packaging, and installing all three together.

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

## Resume a build

`toolchain build` already skips successful, unchanged recipes. `toolchain resume`
also restores the build environment recorded in the installation's
`share/toolchain/build-state.json`. This lets you resume from another terminal or
virtual environment without a changed `PATH` triggering a compiler rebuild.

```sh
toolchain resume --locked --jobs 8 --quiet --archive
# Equivalent spelling:
toolchain build --resume --locked --jobs 8 --quiet --archive
# Inspect the commands without downloads, builds, or state changes:
toolchain resume --locked --dry-run
```

Pass the same configuration and location overrides as the original build, including
`--config`, `--prefix`, `--work`, `--compiler-prefix`, `--stdlib`, and `--lockfile`
where applicable. Keep the same `--locked` setting; omit it if the original build
was unlocked. You can change `--jobs`, `--quiet`, or select a component with
`--library NAME`. Packaging is optional; omit `--archive` if an archive already exists.

The saved values are `PATH`, `CC`, `CXX`, `CFLAGS`, `CXXFLAGS`, `CPPFLAGS`, `LDFLAGS`,
`CMAKE_PREFIX_PATH`, `PKG_CONFIG_PATH`, and `LD_LIBRARY_PATH`. Other environment
variables come from the current shell. Recipe-specific environment settings still
apply. Ordinary `build` uses the current environment; `resume` uses these recorded
values and rejects changes to completed recipes, sources, dependencies, or build
settings instead of silently rebuilding them. Use ordinary `build` for intentional
changes. Failed or interrupted recipes are retried, and missing declared artifacts
are rebuilt. `--force` cannot be combined with resume.

Older installations have no saved environment. The CLI first checks the current
environment, then tries the original `PATH` recorded in retained Autotools
`config.log` files. It accepts a recovered environment only when every selected
completed recipe's fingerprint matches. If that cannot be verified, use the
original shell and options, or ordinary `build`. A successful resume saves the
environment for subsequent runs. No project-specific resume script is needed.

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
runtime and links fmt/spdlog when present. When Protovalidate is installed, it also
uses the SDK's `protoc` to generate C++ and descriptors for a consumer importing
`buf/validate/validate.proto`.

For applications using ASan/UBSan or TSan, build matching dependencies in a separate
prefix with `--sanitizer`. See [sanitizer library builds](sanitizers.md) for commands,
consumer setup, and the Protobuf regression smoke check.

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
