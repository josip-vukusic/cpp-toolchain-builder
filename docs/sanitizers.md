# Sanitizer library builds

For a fresh combined SDK, [bundle mode](bundles.md) builds `standard`, `asan`,
and `tsan` together from `toolchain-v3.yaml`, with one compiler and one source
lock. The commands below remain supported for separate v2 installations.

An application built with ASan can report false container overflows when linked
to ordinary Protobuf libraries: the application's inline methods annotate memory,
but the uninstrumented parser does not update those annotations consistently.
The sanitizer project's [container-overflow documentation](https://github.com/google/sanitizers/wiki/AddressSanitizerContainerOverflow)
describes this mixed-instrumentation problem. Disabling `detect_container_overflow`
globally also hides real container errors.

Build instrumented dependencies in separate prefixes. ASan/UBSan and TSan each
need their own libraries and application build directory. The existing SDK supplies
Clang, its runtimes, GCC's standard library, and build tools; it is not rebuilt.

From the builder checkout, in a shell without an activated SDK:

```bash
source .venv/bin/activate
toolchain build --config toolchain-v2-asan.yaml --locked --jobs 8 --quiet

toolchain inspect verify --config toolchain-v2-asan.yaml --smoke
```

The [named ASan/UBSan configuration](../toolchain-v2-asan.yaml) reuses the versions
in `builtin:poc` and the release `toolchain.lock.json`. It uses `/opt/toolchain-v2`
for compiler tools and installs libraries into `install/toolchain-v2-asan`, with
build files in `.toolchain-work-asan`. Override `--compiler-prefix` if the validated
compiler lives elsewhere. XZ is configured with `--disable-sandbox` in sanitizer
builds because its Landlock sandbox is incompatible with instrumentation.

For TSan:

```bash
toolchain build --locked --sanitizer tsan \
  --compiler-prefix "$PWD/install/toolchain-v2" \
  --prefix ./install/toolchain-v2-tsan \
  --work .toolchain-work-tsan --jobs 8 --quiet

toolchain inspect verify --prefix ./install/toolchain-v2-tsan --smoke
```

These commands rebuild library recipes and copy data recipes. Top-level source
downloads are reused from the existing cache; upstream vendored dependencies may
still download. A narrower build can use `--library protobuf` for diagnostics, but
an application using CEL, Protovalidate, and other libraries needs those matching
dependencies too. Custom third-party recipes must forward the profile's compiler
and linker flags; the bundled Boost, Make, and CEL/Bazel adapters do so explicitly.
System dependencies and the external standard library are not rebuilt by these
profiles. This is not a claim of complete instrumentation of every dependency.

To resume the named ASan build:

```bash
toolchain resume --config toolchain-v2-asan.yaml --locked --jobs 8 --quiet
```

Use `build` for the first run of this named configuration, including when switching
from an earlier attempt configured with explicit command-line paths. For other
profiles, use the same command with `resume` in place of `build`, keeping the profile,
prefix, work path, and compiler prefix.
Profiles are recorded in build state and fingerprints. The builder refuses to mix
release, ASan/UBSan, and TSan libraries in a managed installation.

## OpenSSL's function-pointer check exception

The OpenSSL recipe declares a profile-specific override:

```yaml
sanitizer_overrides:
  asan-ubsan:
    compile_flags: [-fno-sanitize=function]
```

This disables UBSan's function-pointer type check only in OpenSSL's ASan/UBSan
compilation. ASan and the remaining UBSan checks stay enabled. The flag does not
propagate to other libraries or applications, or to release/TSan builds. See the
[recipe format](recipes.md#per-recipe-sanitizer-overrides) for the generic options.
This exception does not establish that a particular runtime report is a false
positive; rerun the application's sanitizer tests after rebuilding.

To update an already completed SDK, run from the builder checkout in the original
build shell, with the Python virtual environment active and no SDK activated:

```bash
source .venv/bin/activate
toolchain build --config toolchain-v2-asan.yaml --locked --jobs 8 --quiet &&
toolchain inspect verify --config toolchain-v2-asan.yaml --smoke
```

Use `build`, not `resume` or `--force`: it rebuilds changed recipes and their
declared dependents, retaining matching completed components. With the original
environment, this exception rebuilds OpenSSL, aws-crt-cpp,
aws-iot-device-sdk-cpp-v2, and aws-sdk-cpp. Changes to tracked environment variables
(including `PATH`) can cause additional rebuilds.

After the build and verification succeed, update the existing `/opt` copy:

```bash
sudo cp -a -- ./install/toolchain-v2-asan/. /opt/toolchain-v2-asan/ &&
sudo chown -hR --reference=/opt/toolchain-v2 /opt/toolchain-v2-asan &&
sudo chmod --reference=/opt/toolchain-v2 /opt/toolchain-v2-asan &&
toolchain inspect verify --prefix /opt/toolchain-v2-asan --smoke
```

The `/.` copies the SDK contents into the existing destination without nesting
another `toolchain-v2-asan` directory. Then rebuild/relink the application and
rerun its strict sanitizer tests.

## Application use

Activate the chosen library SDK. Its activation script selects the external compiler
and exports matching compile/link flags. Its generated CMake toolchain file also
sets those flags. Do not reuse a CMake cache from v1 or from a different profile.

For this checkout's `tomgate-site` application:

```bash
source /home/josip-vukusic/Projects/Private/cpp-toolchain-builder/install/toolchain-v2-asan/activate
cd /home/josip-vukusic/Projects/TE/tomgate-site
cmake -S . -B build-tv2-asan \
  -DCMAKE_BUILD_TYPE=Debug \
  -DCMAKE_TOOLCHAIN_FILE="$TOOLCHAIN_PREFIX/share/toolchain/toolchain.cmake" \
  -DENABLE_SANITIZERS=ON -DENABLE_TSAN=OFF
cmake --build build-tv2-asan -j8
ctest --test-dir build-tv2-asan --output-on-failure
```

For TSan, activate `toolchain-v2-tsan`, use `build-tv2-tsan`, and configure with
`-DENABLE_SANITIZERS=OFF -DENABLE_TSAN=ON`. Remove the old
`ASAN_OPTIONS=detect_container_overflow=0` override. Check that dependency paths in
the new CMake cache point to the matching library SDK, especially in projects that
have hardcoded hints to `/opt/toolchain-v1`.

`inspect verify --smoke` uses the recorded profile. With Protobuf installed, it
also compiles and runs a consumer that reserves a repeated field in instrumented
header code and grows it through the installed parser. For ASan this check forces
container-overflow detection on; it does not disable leak detection. It is a focused
regression check, not a substitute for application tests. LeakSanitizer cannot run
under ptrace: run verification outside debuggers or traced sandboxes.
