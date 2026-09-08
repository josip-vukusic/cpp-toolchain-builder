# Validation record — 2026-09-08

This page separates observed results from unvalidated configurations. Tests below
ran on one Ubuntu 24.04 x86_64 host. Network and mount namespaces isolate selected
consumer checks; they do not constitute testing on a different distribution.

## Product acceptance

| Check | Observed result |
| --- | --- |
| Existing automated suite | 40 tests passed before product changes |
| Suite including distribution regressions | 45 tests passed; includes actual C/C++ compilation |
| Fresh wheel in an isolated virtual environment, outside the checkout | Import, all 37 bundled recipes, local CMake build, and C++20 smoke test passed |
| First-time example source acquisition | fmt and spdlog downloaded into an empty cache; pinned hashes verified |
| README library example | Built fmt 11.2.0 and spdlog 1.15.3 using the host GCC 13.3.0 compiler |
| Library-only verification | Recorded host compiler selected automatically; fmt/spdlog compile, link, and run passed |
| Repeat the example build | Both unchanged recipes skipped |
| Archive distribution | SHA-256 validated; generated files readable and executable bits retained for recipients |
| Relocated archive with no builder on PATH | CMake discovery, compile, link, and run passed with the original prefix absent and networking disabled |
| Activation in a directory containing spaces | Passed in Bash and Zsh; deactivation restores the previous environment |
| Missing external compiler | Activation fails with an actionable message and restores the previous environment |
| Relocated 16-library consumer with an external compiler | Generated CMake integration passed with the original library prefix hidden and networking disabled |
| Preserved GCC/Clang SDK at a new path | fmt/spdlog consumer passed with the old compiler location hidden and networking disabled |

The preserved SDK also passed from a directory containing spaces.

The preserved SDK check used an existing binary artifact, not a fresh compiler
build. It used GCC 15.2.0, Clang 21.1.0, and CMake 4.1.1 from the copied bundle,
with the current builder-generated activation script. Hiding the original path
was confined to a temporary mount namespace; the original SDK was unchanged.
Only the tested compiler and example workflow are covered by this result.

### Fresh GCC SDK

The [six-component GCC example](gcc-toolchain.md) was subsequently built from
source on the same host, using eight jobs and a prefilled source cache. Networking
was disabled for the recorded build. This SDK contains GCC 15.2.0, CMake 4.1.1,
Make 4.4.1, binutils 2.45, fmt 11.2.0, and spdlog 1.15.3.

| Check | Observed result |
| --- | --- |
| Suite including SDK prerequisite regressions | 48 tests passed |
| Fresh GCC build | Three-stage bootstrap and stage comparison passed |
| Compiler selection during the remaining builds | Make, CMake, fmt, and spdlog built with the new GCC |
| Managed installation verification | All six components and C++20 fmt/spdlog compile/link/run passed |
| Copied SDK with no host build tools on PATH | GCC, CMake, Make, assembler, linker, and GCC helpers resolved inside the copy |
| Copied SDK consumer | CMake found fmt/spdlog inside the copy; application compiled and printed `The answer is 42` |

The copied-SDK check hid the original installation, `/usr/lib/gcc`, and
`/usr/include/c++` in a private mount namespace and disabled networking. It kept
the system C library development files available. GCC's helper uses system zlib;
CMake resolves the SDK's own C++ runtime after activation. This remains a
same-host relocation check, not a test on another distribution.

The successful recorded run took **55 minutes 21 seconds**, including GCC's
**49 minutes 34 seconds** and CMake's **5 minutes 24 seconds**. Binutils had
completed in **62 seconds** in the preceding run, before an isolated-environment
tar ownership issue was corrected. The installed SDK occupies about **1.83 GiB**.
These are measured build times with cached sources, not download-inclusive
estimates. [Recordings and reproduction instructions](gcc-demo.md) retain the
actual commands and timing. GCC's complete upstream test suite was not run.

Run the self-contained distribution regressions with:

```bash
python -m unittest discover -s tests -v
```

The tests in `tests/test_distribution.py` build a local library, archive it, delete
the original test prefix, and compile consumers from the relocated copy. The
real-library archive check is `bash scripts/check_example.sh`, after building and
archiving the README example. That script disables networking and removes the
original example prefix temporarily, restoring it on exit.

The GitHub workflow is prepared for Python 3.12–3.14 and a real-library consumer
job. It has not yet run on GitHub; local execution here used Python 3.12.3.

### Example size and build time

On the validation host, a fresh fmt/spdlog build with four jobs took **14.69 seconds**
with sources already downloaded. The installed example occupied about **2.6 MiB**
and its archive was about **477 KiB**. These are measurements of this small example
on one host, not estimates for the full GCC/LLVM preset.

## Earlier implementation validation

The earlier implementation checks used a development virtual environment, a
preserved original binary distribution, and previously downloaded source archives.
Those local artifacts are not included in the source release. The clean LLVM
checkout was checked against its release commit. The standalone builder does not
depend on the former `toolchain/` scripts directory.

## Results

| Check | Result |
| --- | --- |
| Recipe schema and dependency validation | All 37 POC recipes passed |
| Complete build plan after deleting the POC folder | Passed; no legacy builder dependency |
| Top-level source acquisition | All 37 sources fetched/verified |
| Locked, offline source acquisition | All 37 source identities match `toolchain.lock.json` and are cached |
| Automated Python tests at the earlier checkpoint | 40 passed |
| Fresh wheel installation outside the repository | Passed; all 37 recipes validated, isolated fmt build and C++20 smoke test passed |
| Real library recipe builds/installs | 16 components passed, listed below |
| Bazel installation | Passed; executable reports 8.4.1 |
| Googleapis and CEL-spec proto installation | Passed; existing Protobuf headers preserved |
| Protobuf import validation | `protoc` accepted CEL syntax/checked and google/rpc/status protos |
| C++20 compile/link/run smoke test | Passed with the new fmt/spdlog installation |
| CMake consumer using the generated toolchain file | Passed; exercised all 16 library components |
| Resume | Repeated completed builds skipped the unchanged recipes |
| Distribution archive | Produced and checked with an adjacent SHA-256 file |
| Existing POC inspection | 253 binaries, 702 libraries, 26,139 headers; compiler/tool versions detected |

The 16 library components are fmt, spdlog, gflags, bzip2, lz4, zstd, xz,
libmodbus, concurrentqueue, nlohmann-json, Abseil, zlib, Protobuf, ANTLR4, RE2,
and toml++. Together with Bazel, googleapis, and CEL-spec, **19 recipes** were
actually installed into `install/recipe-test`.

The real library builds used the existing `/opt/toolchain-v1` Clang 21.1.0 / GCC
15.2.0 compiler with a new, separate installation prefix. They do **not** count as
rebuilding GCC or LLVM. The test consumer is in `tests/consumer`; it checks CMake
package discovery, static linking, Protobuf serialization, RE2, ANTLR, logging,
formatting, TOML/JSON, queues, compression versions, and libmodbus allocation
without opening a network connection. ANTLR is isolated in its own translation
unit because its upstream headers undefine `EOF`, which affects nlohmann/json
when included afterward in the same translation unit.

The Python tests cover invalid configuration, duplicate names/keys, cycles,
dependency ordering/invalidation, path traversal, archive links, self-extracting
binaries, checksum mismatches, git ref locking, offline caches, partial/failing
builds, process return codes, prefix locks, resume, missing artifacts, side-effect
free dry runs, CLI parsing, actual local CMake compilation, activation/deactivation
in bash/zsh, malformed legacy manifests, and archive checksums.

## Full-build acceptance still required

A complete fresh GCC/LLVM + all-library toolchain build has **not** been run.
The initial full-build preflight on this Ubuntu host reports three missing
packages: `flex`, `gettext`, and `gawk`. The default native platform is Linux
x86_64; the full libc++ variant has not been compiled either.

The remaining compiled recipes—including the full AWS/Arrow/CEL/Protovalidate
stack—have their sources and build plans checked but have not passed an end-to-end
build in this session. Upstream vendored dependencies may still need network access
even though all top-level sources are cached. Do not treat schema checks, source
checks, or a successful dry run as proof that the full toolchain builds.

Run the acceptance build with:

```sh
. .venv/bin/activate
toolchain doctor
# Install the missing packages reported by doctor, then:
toolchain build --locked --jobs 8 --quiet --archive
toolchain inspect verify --prefix ./install/toolchain-v1 --config toolchain.yaml --smoke
```

Use `--prefix /opt/toolchain-v1` if you intentionally want to build into the old
installation location. Use the same prefix, standard-library setting, and source
lock for retries. A failure records the recipe name and log path; repeating the
build resumes successful components.

To repeat the library consumer check against the isolated test installation:

```sh
export LD_LIBRARY_PATH=/opt/toolchain-v1/lib:/opt/toolchain-v1/lib64
/opt/toolchain-v1/bin/cmake -S tests/consumer -B .toolchain-work/consumer \
  -DCMAKE_TOOLCHAIN_FILE="$PWD/install/recipe-test/share/toolchain/toolchain.cmake"
/opt/toolchain-v1/bin/cmake --build .toolchain-work/consumer --parallel 2
/opt/toolchain-v1/bin/ctest --test-dir .toolchain-work/consumer --output-on-failure
```
