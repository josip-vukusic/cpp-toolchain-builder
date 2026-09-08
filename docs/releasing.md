# Publishing a release

## Repository presentation

Suggested repository name: `cpp-toolchain-builder`.

Suggested description:

> Build, archive, and activate versioned Linux C/C++ development environments.

Suggested topics: `cpp`, `toolchain`, `gcc`, `clang`, `llvm`, `cmake`, `linux`, `sdk`,
`build-tools`, `developer-tools`.

The README introduces the product through the small library example. Keep its
support claims aligned with [validation](validation.md). The initial version is
an early public release; the complete GCC/LLVM preset is not yet an accepted
binary distribution.

## Prepare the source

```bash
python -m unittest discover -s tests -v
python -m build
python scripts/check_wheel.py dist
python scripts/prepare_publication.py
```

The last command creates `output/github-source/` and
`output/cpp-toolchain-builder-github.tar.gz` from an explicit list of source,
documentation, examples, tests, and GitHub configuration. It excludes virtual
environments, cached sources, installed SDKs, build output, and local tool state.
Use `--force` only to replace a previous generated publication directory/archive.
The source directory includes dotfiles such as `.github/` and `.gitignore`.

To publish from that clean directory, create an empty GitHub repository, then:

```bash
cd output/github-source
git init -b main
git add .
git diff --cached --stat
git commit -m "feat: add C++ toolchain builder CLI and recipes"
```

Add the remote URL shown by GitHub with `git remote add origin`, then run
`git push -u origin main`. The preparation command does not create a repository,
push code, or publish packages. The author identity for the commit comes from
your Git configuration.

The project uses MIT for its own code. Preserve any applicable prior attribution
and upstream notices. Generated toolchains contain components with separate
licenses; copying a few license files does not by itself establish that every
binary redistribution requirement has been met.

## Release artifacts

Attach the source distribution and Python wheel from `dist/` to the initial
GitHub release. Do not describe the wheel as a compiled GCC/LLVM SDK: it contains
the builder and recipes. Mark the initial GitHub release as a pre-release while
full-preset acceptance remains open. The existing package version is `1.0.0`.

CI is configured to check Python 3.12, 3.13, and 3.14 on Ubuntu 24.04, installs a wheel outside the
checkout, and exercises the small library bundle. Those runner results are only
available after the workflow runs on GitHub. The workflow uploads build artifacts
and does not publish a release or upload to PyPI.

## Acceptance for a compiled toolchain release

Before publishing a particular GCC/LLVM SDK as supported:

1. Build the entire selected configuration into an empty prefix and retain the
   recipe definition, source lock, logs, and component metadata.
2. Compile, link, and run representative application tests against that installation.
3. Archive it, check the SHA-256, and test the archive on each claimed receiving
   platform with its original installation path unavailable.
4. Exercise the included compiler, debugger, CMake packages, and required dynamic
   libraries. Test `libc++` separately from `libstdc++`.
5. Record host runtime prerequisites, CPU baseline, resource requirements, tested
   paths, and any tools supplied outside the bundle.
6. Retain the corresponding sources and required notices alongside your distribution.

For the small example, the repository supplies this acceptance command after
building and archiving the README paths:

```bash
bash scripts/check_example.sh
```

It temporarily moves the example's original prefix, restores it on exit, and
builds a fresh consumer from the archive in a network namespace. It requires
`unshare` with unprivileged user/network namespaces. On restricted CI runners,
the workflow creates the network namespace with `sudo unshare --net` and uses
`--isolated` only inside that namespace.

## Source retention

Keep the configuration and lockfile in version control. Back up downloaded sources,
patches, the build environment definition, and final archives separately. The
source lock fixes identities, while retained files protect against source removal.
Top-level `--offline` does not constrain independent downloads performed inside
GCC, CMake, or Bazel build steps.
