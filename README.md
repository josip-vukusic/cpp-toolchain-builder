# C++ Toolchain Builder

C++ Toolchain Builder assembles compilers and libraries from YAML recipes into a
versioned installation directory. Keep a known-working stack, share it with your
team, and choose when to upgrade. Developers use the resulting installation with
ordinary build tools; they do not need the Python builder to use a prepared bundle.

```bash
source /path/to/toolchain/activate
cmake -S my-app -B build
cmake --build build
```

Built for teams maintaining native **Linux C/C++** development and CI environments.
You can build a small library bundle with your existing compiler, or build the
bundled GCC/LLVM preset together with its dependencies.

[Quickstart](#quickstart) · [Sharing](#share-an-installation) ·
[Full toolchain](#build-the-full-toolchain) · [Write a recipe](#create-your-own-recipes) ·
[Custom library example](#example-add-a-new-xy-lib-library) ·
[Validation](docs/validation.md)


## Current scope

This is an early public release. The builder and library distribution workflow
have automated and real-library tests on **Ubuntu 24.04 x86_64**. The bundled preset
has 37 recipes, including GCC 15.2.0, LLVM 21.1.0, Boost, Arrow, AWS libraries, and
Protobuf. **A clean build of that entire preset has not yet been validated.**

A copied bundle needs a compatible CPU architecture, Linux runtime, and any
external tools it was built to use. Library-only bundles still need a compatible
compiler. Some upstream tools embed absolute paths; relocation support is tested
per bundle, and is not a promise of portability across every Linux distribution.
See the [test results and known limits](docs/validation.md).

## Quickstart

Start with two small libraries and a runnable C++ example. This uses your host
compiler and does not rebuild GCC or LLVM.

Requirements: **Linux, Python 3.12+, a C++20 compiler, CMake, and Make**. The first
installation and source download need network access. On Ubuntu 24.04, the basic
packages are `python3-venv build-essential cmake`.

From a checkout or extracted source release:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

toolchain doctor --config examples/library.yaml
toolchain build --config examples/library.yaml --jobs 4 --quiet

source ./install/example-libraries/activate
cmake -S examples/hello -B build/hello
cmake --build build/hello
./build/hello/hello
# The answer is 42

toolchain inspect verify --prefix ./install/example-libraries --smoke
toolchain_deactivate
```

The [example definition](examples/library.yaml) builds fmt and spdlog, using the
same fmt installation for both. Its install, cache, and working directories stay
inside the checkout. Paths in a recipe definition are relative to that definition.
`toolchain_deactivate` restores your previous environment and leaves your Python
virtual environment alone. Use a fresh CMake build directory when switching SDKs.

## Share an installation

```bash
toolchain archive --prefix ./install/example-libraries \
  --output output/example-libraries.tar.gz
```

Keep the archive and its adjacent `.sha256` file. On a compatible machine, copy
both files into a directory, then:

```bash
sha256sum -c example-libraries.tar.gz.sha256
tar -xzf example-libraries.tar.gz
source ./example-libraries/activate
```

You can now build applications using that installation. This example still uses
the host compiler and CMake; a complete toolchain bundle can include those tools.
No package server or builder CLI is needed just to activate a bundle. Each bundle's
`README.md` and `share/toolchain/manifest.json` describe its compiler requirements.

For explicit CMake integration, use the generated file:

```bash
cmake -S my-app -B build \
  -DCMAKE_TOOLCHAIN_FILE=/path/to/toolchain/share/toolchain/toolchain.cmake
```

The installation includes component versions, source identities, collected
third-party license notices, and a CycloneDX component inventory. The inventory
covers top-level recipes; it does not enumerate every upstream vendored dependency.

## Build the full toolchain

The root [toolchain.yaml](toolchain.yaml) selects the original 37-component preset.
Review the [preset's build choices and validation boundaries](docs/poc-parity.md)
before starting. A fresh compiler build is substantial work and can take hours.

```bash
toolchain recipes validate
toolchain doctor
toolchain plan

# After addressing any host dependencies reported by doctor:
toolchain fetch --locked
toolchain build --locked --jobs 8 --quiet --archive
```

The default destination is `./install/toolchain-v1`. `doctor` checks prerequisites
and prints installation hints; it does not modify system packages. The checked-in
lockfile pins top-level sources. Omit `--locked` when intentionally creating or
updating your own source lock, and retain the resulting file.

Repeat the same build command to resume. Inspect progress with `toolchain status`;
compiler logs are under `.toolchain-work/logs/`. Use `--library NAME` to select a
component and its dependencies, or `--stdlib libc++` to select the separate libc++
variant, which still needs full-build validation.

`--offline` restricts the builder's top-level downloads to its cache. Upstream
build steps can have their own downloads; this is not a fully offline rebuild
guarantee. See [usage and source retention](docs/usage.md).

## Create your own recipes

A recipe tells the builder where to get a library, how to build it, and which
files should exist afterward. Most libraries need only YAML; you do not need to
write Python or modify the builder.

### 1. Create a toolchain definition

```bash
toolchain init my-toolchain.yaml --name my-sdk --prefix ./install/my-sdk
```

This creates a configuration that uses your host compiler. Its installation and
working directories are relative to `my-toolchain.yaml`.

### 2. Add a library recipe

Open `my-toolchain.yaml` and replace `libraries: []` with this example:

```yaml
libraries:
  - name: fmt
    version: '11.2.0'
    source:
      url: https://github.com/fmtlib/fmt/archive/refs/tags/11.2.0.tar.gz
      sha256: bc23066d87ab3168f27cef3e97d545fa63314f5c79df5ea444d41d56f962c6af
    build:
      system: cmake
      options:
        - -DFMT_TEST=OFF
    artifacts:
      - include/fmt/format.h
      - lib/libfmt.a
```

To adapt it for another library:

- Change `name`, the quoted `version`, and the source URL. Set `sha256` to the hash
  of that exact archive; use `sha256sum downloaded-archive.tar.gz` to calculate it
  and compare with an upstream checksum when one is provided. Changing `version`
  alone does not change the URL or Git ref.
- Choose the upstream build system. `cmake` configures, builds, and installs for
  you. `autotools`, `make`, `header-only`, `copy`, and `custom` are also supported.
- Put project-specific flags in `build.options`, one argument per list item.
  CMake recipes default to Release, C++20, PIC, and static libraries where supported.
- List the installed headers and libraries in `artifacts`, relative to the install
  directory. A build fails if any declared artifact is missing.

For a local project, replace the entire `source` mapping with
`source: {path: ./my-library}`. For Git, use a repository and an explicit ref, for
example `source: {git: 'https://github.com/fmtlib/fmt.git', ref: '11.2.0'}`.

### 3. Validate, test, and build

```bash
toolchain recipes validate --config my-toolchain.yaml
toolchain plan --config my-toolchain.yaml
toolchain doctor --config my-toolchain.yaml

# Build in a temporary prefix and compile/run a small C++ consumer.
toolchain test fmt --config my-toolchain.yaml --smoke

# Install into the directory chosen in step 1.
toolchain build --config my-toolchain.yaml --library fmt --jobs 4 --quiet
source ./install/my-sdk/activate
```

`validate` checks the recipe definition; `plan` previews commands without building.
`test` performs a real build and checks the declared artifacts. Successful temporary
tests are removed unless you pass `--keep`; failed tests retain their files and
logs. The generic `--smoke` check exercises C++20 and fmt/spdlog when present. For
another library, also compile and run a small application that uses its API.

Once the recipe works, run `toolchain fetch --config my-toolchain.yaml` and keep
the generated `my-toolchain.lock.json`. Future builds can use `--locked` to require
those recorded source identities.

### Example: add a new `xy-lib` library

Suppose you need a library that has no existing recipe. Its upstream documentation
says to download a release, run `make`, and install with `make PREFIX=... install`.
Here is how to turn those instructions into a recipe.

`xy-lib` is fictional: replace the example URL, checksum, flags, filenames, and API
below with the values from your library. The `example.com` URL is a placeholder,
not a downloadable package.

**Find the source and build instructions.** For this example, assume the library:

- Has a release archive called `xy-lib-1.2.3.tar.gz`.
- Builds a static library at `build/libxy.a` using `make`.
- Installs `include/xy/xy.hpp` and `lib/libxy.a` under the requested prefix.

Download the actual release archive and calculate its checksum:

```bash
# Replace this URL with the real release download link.
curl -fL https://example.com/releases/xy-lib-1.2.3.tar.gz -o xy-lib-1.2.3.tar.gz
sha256sum xy-lib-1.2.3.tar.gz
```

**Write the recipe.** Save this as `xy-toolchain.yaml`, replacing the URL and
`REPLACE_WITH_SHA256` with the actual URL and 64-character checksum:

```yaml
schema_version: 1
toolchain:
  name: xy-sdk
  prefix: ./install/xy-sdk
  jobs: 4
libraries:
  - name: xy-lib
    version: '1.2.3'
    source:
      url: https://example.com/releases/xy-lib-1.2.3.tar.gz
      sha256: REPLACE_WITH_SHA256
    build:
      system: custom
      commands:
        - shell: |
            make -j"$TC_JOBS" \
              CC="$CC" CXX="$CXX" \
              CFLAGS="$CFLAGS" CXXFLAGS="$CXXFLAGS"
            make PREFIX="$TC_PREFIX" install
    artifacts:
      - include/xy/xy.hpp
      - lib/libxy.a
```

The builder downloads and extracts the archive, then runs the commands in a private
copy of the source directory. `$TC_PREFIX` is the installation directory and
`$TC_JOBS` is the requested parallelism. Passing `CC`, `CXX`, and the flags to Make
keeps the build on the selected compiler. Installation goes into the SDK directory;
do not add `sudo` to the recipe.

If you already have the source locally, replace the entire `source` block with
`source: {path: ./xy-lib}`. This lets you try the recipe before hosting a release.
To add it to an existing toolchain instead, copy the `- name: xy-lib` item into
that configuration's `libraries` list.
When using the full preset's bundled compiler, add `depends_on: [llvm]` to the
new recipe so selecting `--library xy-lib` includes its compiler dependency.

**Test and install it.** After replacing the placeholders:

```bash
toolchain recipes validate --config xy-toolchain.yaml
toolchain plan --config xy-toolchain.yaml --library xy-lib
toolchain doctor --config xy-toolchain.yaml --library xy-lib
toolchain test xy-lib --config xy-toolchain.yaml --keep
toolchain build --config xy-toolchain.yaml --library xy-lib --quiet
toolchain inspect verify --prefix ./install/xy-sdk
```

The checks above confirm that the recipe builds and installs the declared files.
Test the actual library API with a small consumer too, as shown below. If a build
fails, inspect `.toolchain-work/logs/xy-lib.log`, adjust the recipe, and repeat the
build. Temporary `test` runs print their own retained log directory.

<details>
<summary>If the library uses CMake, Autotools, or only headers</summary>

Keep the source and version, then replace the `build` block and adjust `artifacts`
to match what that library actually installs. The option names here are illustrative;
check the upstream documentation for the real flags.

**CMake:** the builder supplies configure, build, and install commands. You only
need the library's options:

```yaml
build:
  system: cmake
  options:
    - -DXY_BUILD_TESTS=OFF
    - -DXY_BUILD_EXAMPLES=OFF
```

If the upstream CMake project is in a subdirectory, also set
`source_subdir: path/to/cmake-project` under `build`.

**Autotools:** for a release archive with a `configure` script:

```yaml
build:
  system: autotools
  options:
    - --disable-shared
    - --enable-static
```

The builder runs `configure --prefix=...`, `make`, and `make install`. If the
project requires configuration inside its source tree, add `in_source: true`.

**Header-only:** copy headers without compiling. Replace the recipe's `build`
and `artifacts` fields with:

```yaml
build:
  system: header-only
  copies:
    - from: '${source}/include/xy'
      to: include/xy
artifacts:
  - include/xy/xy.hpp
```

</details>

<details>
<summary>If the library has no install command</summary>

Keep `system: custom` and install the files explicitly after building:

```yaml
build:
  system: custom
  commands:
    - shell: |
        make -j"$TC_JOBS" CC="$CC" CXX="$CXX" \
          CFLAGS="$CFLAGS" CXXFLAGS="$CXXFLAGS"
    - run: [install, -Dm644, '${source}/build/libxy.a', '${prefix}/lib/libxy.a']
    - run: [install, -Dm644, '${source}/include/xy/xy.hpp', '${prefix}/include/xy/xy.hpp']
```

Use `${source}` and `${prefix}` in `run` argument lists. Inside `shell` blocks,
use quoted shell variables such as `"$TC_SOURCE"` and `"$TC_PREFIX"`. Each step
starts a new process; put related `cd`, variable assignments, and commands in the
same shell block when they need to share state.

</details>

**Link the installed library into your application.** If upstream provides a CMake
package and exported target, use its documented `find_package` and target names.
For this fictional library, assume there is no CMake package. Create
`xy-app/CMakeLists.txt`:

```cmake
cmake_minimum_required(VERSION 3.18)
project(xy_app LANGUAGES CXX)
find_path(XY_INCLUDE_DIR NAMES xy/xy.hpp REQUIRED)
find_library(XY_LIBRARY NAMES xy REQUIRED)
add_executable(xy_app main.cpp)
target_compile_features(xy_app PRIVATE cxx_std_20)
target_include_directories(xy_app PRIVATE "${XY_INCLUDE_DIR}")
target_link_libraries(xy_app PRIVATE "${XY_LIBRARY}")
```

Create `xy-app/main.cpp`, replacing the example API call with one from your library:

```cpp
#include <xy/xy.hpp>

int main() {
    return xy::add(20, 22) == 42 ? 0 : 1;
}
```

Then activate your SDK, build, and run the application:

```bash
source ./install/xy-sdk/activate
cmake -S xy-app -B build/xy-app
cmake --build build/xy-app
./build/xy-app/xy_app
toolchain_deactivate
```

If the static library depends on other libraries, link those dependencies too.
`depends_on` controls recipe build order; it does not add link libraries to your
application automatically.

### Dependencies and reusable recipe files

Add `depends_on: [fmt]` to a recipe that needs fmt, and include both recipes in
the configuration. The builder installs dependencies first and makes the prefix
available to build tools. You still need the upstream options that select those
dependencies; [the spdlog example](examples/library.yaml) demonstrates this.

To share recipes across configurations, move the `libraries:` block into a file
such as `recipes/my-libraries.yaml`. Reference it from your main configuration:

```yaml
recipe_files:
  - recipes/my-libraries.yaml
libraries: []
```

Keep the existing `schema_version` and `toolchain` settings in the main file.
Each included recipe file contains its own `libraries:` list. Define each recipe
name only once; paths to local sources are relative to the file containing the recipe.

To customize the bundled compiler stack, export an editable copy with
`toolchain init team.yaml --preset poc --name team-sdk --prefix ./install/team-sdk`.
See the [full recipe reference](docs/recipes.md) for custom commands, file copying,
environment variables, and additional build-system options.

## Contributing

Bug reports, minimal reproductions, and tested recipes are welcome. The
[contributing guide](CONTRIBUTING.md) explains how to run the tests and report a
build failure. The [release guide](docs/releasing.md) covers publication and the
checks required before describing a toolchain bundle as supported.

## License

The builder is available under the [MIT license](LICENSE), including for commercial
use. Compilers and libraries built with it retain their own upstream licenses;
the builder's license does not relicense those components.
