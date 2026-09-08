# Recipe format

For a walkthrough from download link to build commands and application linking,
see the [worked new-library example](../README.md#example-add-a-new-xy-lib-library).

```yaml
schema_version: 1
toolchain:
  name: example
  prefix: ./install
  work: .toolchain-work
  cache: .toolchain-cache
  stdlib: libstdc++
  jobs: 4
recipe_files: []
libraries:
  - name: fmt
    version: '11.2.0'
    stage: library
    source:
      url: https://github.com/fmtlib/fmt/archive/refs/tags/11.2.0.tar.gz
      sha256: bc23066d87ab3168f27cef3e97d545fa63314f5c79df5ea444d41d56f962c6af
    depends_on: []
    build:
      system: cmake
      options: [-DFMT_TEST=OFF]
    artifacts: [include/fmt/format.h, lib/libfmt.a]
```

All versions and git refs must be strings; quote numeric or date-like YAML values.
Recipes can live in `libraries` or files listed by `recipe_files`. Each recipe file
contains a `libraries` list. `builtin:poc` loads the packaged collection. Duplicate
names, unknown fields, dependency cycles, missing refs, invalid paths, and malformed
commands are errors. To customize built-in recipes, export them with
`toolchain init custom.yaml --preset poc` and edit that file.

`toolchain.compiler: toolchain` selects `<prefix>/bin/clang{,++}` for libraries.
The default `system` mode uses `CC`/`CXX` or `cc`/`c++`, optionally overridden by
`toolchain.cc`/`cxx`. `--compiler-prefix` uses an external Clang installation and
omits `stage: core` dependencies. `stage: data` is for files and prebuilt tools.
The supplied preset's core recipes target Linux x86_64; generic library recipes
can use the host compiler.

For a GCC-only SDK, the [GCC example](../examples/gcc-toolchain.yaml) builds GCC
first and sets each later recipe's `environment.CC` and `environment.CXX` to
`${prefix}/bin/gcc` and `${prefix}/bin/g++`. Its dependency chain builds Make and
CMake before the libraries. These explicit paths avoid selecting another compiler
from the host. The generated activation script recognizes the installed GCC.

## Sources

Specify exactly one of:

- `url`: HTTP(S) or `file:` URL, optionally `sha256` and `filename`.
- `git`: repository URL or local repository path, with `ref`; optionally
  `submodules: true`. Checkout happens before recursive submodule initialization.
- `path`: local source directory, archive, or single file, relative to its recipe
  file. Directory contents are fingerprinted and copied before building.

Archives unpack into an isolated directory. A single enclosing directory is
removed by default. Use `strip_root: false` to retain it, or `directory` to select
an explicit archive subtree. Plain files are copied under `filename`.
Prebuilt self-extracting binaries (such as Bazel) must set `unpack: false` to
preserve the executable instead of extracting its embedded ZIP. Nonempty
pre-downloaded archives can be placed in `<cache>/imports/<filename>` and are
checked against configured checksums before use. Never put build directories in
the source cache: recipes get their own copies under the work directory.

## Build systems

| System | Behavior |
| --- | --- |
| `cmake` | Configure with Release, static-library preference, PIC, and disabled tests; build and install with CMake. Library stage adds C++20 and the selected compiler. |
| `autotools` | Run configure with the prefix, then make and make install. Out-of-source by default; set `in_source: true` if required. |
| `make` | Run make and make install in the source directory with `PREFIX` and recipe options. |
| `copy` / `header-only` | Copy declared files/trees into the prefix; default copies `include/`. |
| `custom` | Execute the explicit `commands` list. |

`options` are individual argv entries, not a shell string. For CMake, they follow
the defaults, so later options can override them. `source_subdir` selects a nested
CMake/configure project. `targets` selects CMake/Make build targets. `install: false`
skips CMake install. `make_options` and `install_targets` customize Autotools;
`install_targets` also applies to Make. `prepare` and `after` accept steps before
and after the standard build.

Steps are argv lists or mappings with `run`, `shell`, or a built-in `hook`:

```yaml
build:
  system: custom
  commands:
    - run: ['${source}/bootstrap', '--prefix=${prefix}']
      cwd: '${build}'
    - [make, '-j${jobs}']
    - shell: 'install -Dm644 "$TC_SOURCE/special.h" "$TC_PREFIX/include/special.h"'
```

Each step starts a fresh process; shell environment changes do not persist to the
next step. Put multi-command shell logic in one YAML block. Shell steps use bash
with `-euo pipefail`. They use `$TC_PREFIX`, `$TC_SOURCE`, `$TC_BUILD`, `$TC_JOBS`,
etc., with normal shell quoting. The engine does not substitute into shell code.
Argv entries and environment values expand `${prefix}`, `${source}`, `${build}`,
`${work}`, `${cache}`, `${jobs}`, `${name}`, `${version}`, `${stdlib}`,
`${compiler_prefix}`, `${llvm_runtimes}`, and `${python}`. Default cwd is `${source}`.

```yaml
build:
  system: copy
  copies:
    - from: '${source}/google'
      to: include/google
      patterns: ['*.proto']
    - from: '${source}/my-tool'
      to: bin/my-tool
      mode: '755'
```

Copy destinations stay inside the prefix. Trees merge without deleting another
recipe's files. `patterns` filters files recursively. `artifacts` is a list of
prefix-relative glob patterns; each must match an existing path after installation
and before a completed build can be skipped. Declare library files **and** headers.
`requires` lists additional host executables for `doctor`. `environment` maps
variable names to strings, expanding the same recipe variables.

When every selected `stage: core` recipe explicitly declares `requires` (an empty
list is allowed), `doctor` checks those declarations and the build systems'
bootstrap tools instead of applying the original full preset's Ubuntu package
list. Header and library development packages still need to be documented by the
recipe author and are checked by the upstream configure step. A host CMake is not
required when an earlier recipe installs `bin/cmake` before the first CMake build.

Special Python adapters are deliberately limited to the bundled projects' unusual
installation steps. They cover GCC's math libraries, GDB setup, CMake bootstrap
flags, static zlib cleanup, Protobuf WKT/Abseil checks, Bazel setup, CEL's Bazel build,
and Protovalidate's manual library/generated-header installation. New conventional
libraries need YAML recipes, not new Python code.
