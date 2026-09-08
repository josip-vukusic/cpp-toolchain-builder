# POC compatibility contract

The Python implementation retains all 37 distinct named components and their
release selections from the original POC. `cel-spec` is labeled `v0.24.0` to match
its actual configured ref instead of the contradictory display value `main`.
Googleapis and pahole use the original moving branch selections; the source
lockfile records what was actually downloaded.

The compatibility target is the installed compiler/library capabilities and
build options. It is not byte-for-byte reproducibility of a previous archive:
compiler host, paths, timestamps, moving branches, and upstream vendored dependencies
can change binary output. Every component has expected installed artifacts. SWIG,
previously a build-only prerequisite, is also installed in the product prefix.

## Preserved build choices

- Binutils: gold and default ld, LTO/plugins, shared tools, deterministic archives,
  threads, system zlib, and new dtags.
- GCC: native x86_64 GNU/Linux bootstrap, C/C++/Fortran, disabled multilib, the
  original libstdc++/PIE/LTO/plugin/debug settings; GMP and MPFR installed as well.
- GDB: Python, TUI, system readline/expat/zlib/lzma, Babeltrace, Intel PT, and the
  prefix-local gdbinit/pahole helpers.
- CMake: bootstrap, system curl, curses UI, and the original hardening flags.
- LLVM: GCC bootstrap compiler, gold/PIC, shared LLVM, RTTI/FFI, Clang tools,
  compiler-rt, lld, lldb, and libunwind; optional libc++/libc++abi.
- Third-party libraries: Clang, C++20, Release, PIC, static-library preference.
- Boost: static, Clang, C++20, zlib/bzip2, no ICU.
- AWS SDK: S3, STS, identity-management, IoT, IAM; CRT and IoT device SDK with OpenSSL.
- Arrow: static, CSV, Parquet, ZSTD, bundled xsimd/Thrift, and prefix Boost discovery.
- Protobuf: installed Abseil, zlib, no tests, WKT installation and inline-namespace
  validation. RE2 uses that same installed Abseil.
- CEL: the POC's Bazel query/build of non-test C++ libraries and public headers.
- Protovalidate: CMake vendoring with prefix Protobuf/Abseil/RE2, manual static archive
  installation, CEL internal headers, and generated CEL/Buf protobuf headers.

## Repairs needed for clean builds

The scripts contained state-dependent paths that could succeed on an existing
installation but fail in an empty prefix. The standalone recipes address these:

- Remove the missing external `util.sh` and OS-package script dependency; `doctor`
  performs host checks without installing anything.
- Deduplicate Protovalidate and use consistent googleapis/CEL-spec/Protovalidate
  names. Build RE2 and proto definitions before CEL.
- Replace the empty zlib download with the official fossils URL and download
  the raw toml++ header instead of a GitHub HTML page.
- Install googleapis `.proto` files without `rsync --delete` against the shared
  `include/google` tree, preserving Protobuf headers and WKTs.
- Disable ANTLR's actual `ANTLR_BUILD_CPP_TESTS` and `ANTLR_BUILD_SHARED` options,
  eliminating the undefined `GTEST_SHIM` and a shared target install failure.
- Pass PIC/Clang explicitly to Makefiles which override compiler environment
  variables. Select the toolchain GCC headers/runtime explicitly for Clang.
- Make source checkout/submodule order deterministic. Reject empty downloads,
  unsafe extraction paths, malformed configs, and missing installation artifacts.
- Remove OpenSSL's test for a macro as an exported symbol; install it consistently
  in `lib` so downstream discovery and artifact checks agree.
- Retain metadata across resumed builds rather than skipping it, and retain logs
  and a failed status when any build/install step fails.
- Copy generated Buf protobuf headers as well as CEL's internal and generated
  headers during Protovalidate installation.

The original full build also allowed upstream vendoring in Arrow, CEL/Bazel,
Protovalidate, AWS submodules, and GCC prerequisites. Those dependencies are not
all represented by the 37 top-level source lock entries. In particular, CEL's
Bazel build and Protovalidate's vendored CEL build can use different dependency
versions, as in the POC. A clean full build and application-level ABI/link tests
remain necessary before distributing the result.

Reference build definitions checked during migration:
[ANTLR 4.13.2](https://github.com/antlr/antlr4/blob/4.13.2/runtime/Cpp/runtime/CMakeLists.txt),
[Protobuf v6.31.1](https://github.com/protocolbuffers/protobuf/blob/v6.31.1/cmake/abseil-cpp.cmake),
[Protovalidate v1.0.0](https://github.com/bufbuild/protovalidate-cc/blob/v1.0.0/cmake/Deps.cmake),
[CEL v0.13.0](https://github.com/google/cel-cpp/blob/v0.13.0/MODULE.bazel).
