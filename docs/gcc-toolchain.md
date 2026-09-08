# A GCC, CMake and spdlog SDK

The [GCC example definition](../examples/gcc-toolchain.yaml) builds a native
Linux x86_64 SDK with six components:

| Component | Version | Purpose |
| --- | --- | --- |
| GNU binutils | 2.45 | Assembler, linker, and archive tools |
| GCC | 15.2.0 | C and C++ compilers, with the C++ runtime |
| GNU Make | 4.4.1 | Runs the generated Makefiles |
| CMake | 4.1.1 | Configures and builds applications |
| fmt | 11.2.0 | Formatting library |
| spdlog | 1.15.3 | Logging library, linked with the same fmt installation |

GCC is built with its three-stage bootstrap and C/C++ frontends. The recipes
build the compiler first, then use its explicit `gcc`/`g++` paths to build Make,
CMake, and both libraries. The original 37-component GCC/LLVM preset is separate.

## Build the SDK

The example targets **Ubuntu 24.04 x86_64**. Building it requires Python 3.12+,
a working host C/C++ compiler, build tools, and development headers. On Ubuntu:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv build-essential flex bison texinfo curl \
  zlib1g-dev libssl-dev
```

A host CMake installation is not required: this recipe bootstraps CMake from
source before building the libraries. From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

toolchain doctor --config examples/gcc-toolchain.yaml
toolchain build --config examples/gcc-toolchain.yaml --locked --jobs 8 --quiet
toolchain inspect verify --prefix install/gcc-sdk --smoke
```

Reduce `--jobs` if you need to limit CPU or memory use. The first compiler build
is substantial; `--quiet` retains detailed logs under `.toolchain-work/gcc-sdk/logs/`.
The destination is `install/gcc-sdk`. If interrupted, repeat the same command;
completed recipes are skipped and a failed recipe can reuse its working directory.
Keep the configuration, environment, and paths unchanged when resuming.

The checked-in lockfile pins the six top-level source archives. GCC additionally
needs GMP, MPFR, MPC, ISL, and gettext source archives selected by its own
prerequisite script. This recipe retains them in `.toolchain-cache/imports/` and
GCC verifies them against its source-provided SHA-512 checksums before extraction.
The first run needs network access for archives that are not cached. Top-level
`--offline` does not itself disable downloads inside a recipe.

## Copy and use it

On a compatible Linux machine, place the completed SDK in a directory of your
choice. For example, after copying it into `toolchain/`:

```bash
source toolchain/activate
command -v g++ cmake make
g++ -dumpfullversion
cmake --version
```

Those commands should resolve inside the copied folder. You can now use ordinary
CMake commands. With the repository's small application available:

```bash
cmake -S examples/hello -B build/gcc-hello
cmake --build build/gcc-hello
./build/gcc-hello/hello
# The answer is 42
toolchain_deactivate
```

The receiving environment uses the SDK's GCC, CMake, Make, binutils, and libraries.
It does not need the Python builder to activate the SDK or build this application.
Use a fresh application build directory after relocating or switching toolchains.

**Host requirements remain:** a compatible x86_64 Linux runtime, shell utilities,
zlib, and the system C library headers and startup objects. On Ubuntu,
`libc6-dev` provides the C development files and `zlib1g` provides GCC's runtime
zlib dependency. This SDK does not bundle glibc or a sysroot;
copying it is not a guarantee of compatibility with every Linux distribution.

## Archive it

```bash
toolchain archive --prefix install/gcc-sdk --output output/gcc-sdk.tar.gz
```

Copy the archive and its `.sha256` file to the receiving machine, then:

```bash
sha256sum -c gcc-sdk.tar.gz.sha256
tar -xzf gcc-sdk.tar.gz
source gcc-sdk/activate
```

The archive includes the activation script, CMake integration, component metadata,
and collected license notices. The builder is MIT-licensed; the compiler, build
tools, and libraries retain their respective upstream licenses.
