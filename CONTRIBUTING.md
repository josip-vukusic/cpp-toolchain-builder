# Contributing

Bug reports, documentation fixes, and recipes with a concrete use case are welcome.
Keep changes focused on making a selected Linux C/C++ stack straightforward to
build and consume.

## Development setup

Use Linux with Python 3.12+, a C/C++ compiler, CMake, and Make. Install Zsh to exercise
both supported activation shells.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e . build
python -m unittest discover -s tests -v
```

The tests build small local fixtures and do not download GCC or LLVM. The consumer
tests need CMake and a host compiler; tests requiring an unavailable tool are
skipped locally. GitHub CI installs the required tools.

Before submitting a packaging change, also run:

```bash
python -m build
python scripts/check_wheel.py dist
```

## Commit messages

Use Conventional Commits: `feat: <message>` for new features, `fix: <message>` for
bug fixes, and `chore: <message>` for maintenance. Use `docs:` for documentation
and `test:` for tests. Keep the message concise and describe the change.

## Recipe changes

Use the [recipe guide](docs/recipes.md). Pin release sources with SHA-256 hashes or
Git commits, declare dependencies and expected artifacts, and retain upstream
license notices. Run the recipe in a dedicated prefix. Include the build command,
host platform, compiler selection, and a small consumer test in your pull request.
A successful dry run alone does not establish that a recipe builds.

Keep compatibility claims specific. Record a new platform or relocation target only
after testing the installed artifact there. Never add downloaded archives, source
caches, installed toolchains, or build directories to a pull request.

## Reporting problems

Use the issue forms with the command, minimal configuration, environment, and the
relevant log excerpt. Failed builds retain logs under the configured work directory.
Remove private URLs and credentials before sharing logs or recipes.

## Scope

The core workflow is a versioned installation directory, inspectable recipes,
explicit source identities, and straightforward activation. Large additions such
as a public package registry, dependency solver, or cross-compilation framework
need a concrete user problem and a maintenance plan before implementation.

Contributions are made under the project's [MIT license](LICENSE). Third-party
components retain their upstream licenses.
