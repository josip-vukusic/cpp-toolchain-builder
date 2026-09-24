# Standard, ASan/UBSan, and TSan in one bundle

Use [toolchain-v3.yaml](../toolchain-v3.yaml) to build all three variants from one
recipe collection and one source lock. `standard` describes uninstrumented
libraries, not the application's Debug/Release build type.

```text
install/toolchain-v3/
├── standard/   # Compiler, tools, normal libraries (37 components)
├── asan/       # ASan/UBSan libraries and data (30 components)
└── tsan/       # TSan libraries and data (30 components)
```

The compiler is built once in `standard`. Both sanitizer SDKs use that sibling
compiler; they do not depend on the existing `/opt/toolchain-v2`. Each variant has
its own activation script, CMake toolchain file, artifacts, logs, and checkpoints.
Sanitizer variants must remain alongside `standard` when copied or archived.

## Build from scratch

From the repository, in a shell without an activated SDK:

```bash
source .venv/bin/activate
toolchain build --config toolchain-v3.yaml --locked --jobs 8 --quiet &&
toolchain inspect verify --config toolchain-v3.yaml --smoke
```

The new prefix `install/toolchain-v3` and work directory `.toolchain-work-v3`
start fresh, so there is no need to delete v2. Existing downloads are reused from
`.toolchain-cache`. Builds run sequentially: standard, asan, tsan. `--jobs` controls
parallel compilation within the current variant, not three concurrent builds.
The full preset still needs the host dependencies reported by:

```bash
toolchain doctor --config toolchain-v3.yaml
```

The shared `toolchain.lock.json` preserves the previously selected source
versions/commits. Bundle builds require `--locked`. For a new configuration
without a lockfile, run `toolchain fetch --config <config>` first. Fetch visits
the shared sources once; upstream vendored downloads remain managed by their
respective build systems.

## Resume, inspect, and package

```bash
toolchain resume --config toolchain-v3.yaml --locked --jobs 8 --quiet
toolchain status --config toolchain-v3.yaml --locked
toolchain plan --config toolchain-v3.yaml --locked --json

toolchain inspect verify --config toolchain-v3.yaml --smoke
toolchain archive --prefix ./install/toolchain-v3 --output output/toolchain-v3.tar.gz
```

Resume restores the recorded environment, skips matching completed components,
retries interrupted work, and starts variants that have not run yet. Intentional
changes to completed recipes require `build` instead. Compiler recipe changes
invalidate sanitizer libraries too. Archives verify that every configured variant
is complete. An individual sanitizer directory cannot be packaged independently
of its sibling compiler.

`--prefix` and `--work` override the bundle's root directories; variant names are
appended automatically. `--cache`, `--lockfile`, `--stdlib`, and `--jobs` apply to
all variants. Bundle builds do not accept `--sanitizer`, `--compiler-prefix`, or
`--library`; existing single-SDK configurations retain those options.

## Install under /opt

After build and verification succeed, copy the entire bundle together:

```bash
sudo mkdir -p /opt/toolchain-v3 &&
sudo cp -a -- ./install/toolchain-v3/. /opt/toolchain-v3/ &&
sudo chown -hR --reference=/opt/toolchain-v2 /opt/toolchain-v3 &&
sudo chmod --reference=/opt/toolchain-v2 /opt/toolchain-v3 &&
toolchain inspect verify --prefix /opt/toolchain-v3 --smoke
```

The ownership commands use the existing v2 installation as a reference. The new
bundle does not need v2 at runtime. Compiler paths in the sanitizer activation
scripts, generated CMake files, and smoke verification resolve relative to their
new location. Full SDK smoke verification after copying is still necessary:
upstream packages may embed their own absolute paths, and these focused checks do
not exercise every tool or API.

## Use in an application

```bash
source /opt/toolchain-v3/standard/activate
# Or: source /opt/toolchain-v3/asan/activate
# Or: source /opt/toolchain-v3/tsan/activate

cmake -S . -B build-v3-standard \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN_PREFIX/share/toolchain/toolchain.cmake" \
  -DCMAKE_BUILD_TYPE=Debug
cmake --build build-v3-standard -j8
```

Use separate application build directories for each variant, such as
`build-v3-standard`, `build-v3-asan`, and `build-v3-tsan`. For Tomgate, retain
`-DENABLE_SANITIZERS=ON -DENABLE_TSAN=OFF` for ASan/UBSan and the opposite settings
for TSan. Generated SDK files supply matching compiler/linker instrumentation.
The recipe-scoped OpenSSL exception still applies only to its ASan/UBSan build.

## Configuration

```yaml
schema_version: 1
toolchain:
  name: toolchain-v3
  prefix: ./install/toolchain-v3
  work: .toolchain-work-v3
  cache: .toolchain-cache
  compiler: toolchain
  variants: [standard, asan, tsan]
  lockfile: toolchain.lock.json
recipe_files:
- builtin:poc
libraries: []
```

`standard` must be first; the optional `asan` and `tsan` entries must be unique.
The bundle derives profile-specific prefixes, work paths, and compiler settings.
The `asan` directory corresponds to the `asan-ubsan` sanitizer profile. Library
versions and per-recipe overrides remain in the one shared recipe collection.
