# Changelog

## Unreleased

- Add single-configuration SDK bundles with `standard`, `asan`, and `tsan`
  variants, a shared compiler/source cache/lockfile, separate checkpoints,
  bundle resume/inspection/archives, and relocatable sibling compiler references.
  Provide `toolchain-v3.yaml` for a fresh build without changing v2 installations.
- Support per-recipe, per-profile sanitizer flag overrides. OpenSSL's ASan/UBSan
  recipe disables only the UBSan function-pointer check; consumer flags and
  release/TSan builds remain unchanged.
- Isolate Arrow's dependency lookup from CMake package registries and always
  vendor RapidJSON, avoiding stale package configs from earlier SDK builds.
- Add `toolchain-v2-asan.yaml` using the validated `/opt/toolchain-v2` compiler
  and the release source lock, with matching build/resume/verification commands.
  Disable XZ's incompatible Landlock sandbox only for sanitizer builds.
- Add separate `--sanitizer asan-ubsan` and `--sanitizer tsan` library builds using
  an existing compiler SDK, profile-aware activation/CMake metadata, and a
  Protobuf parser smoke test that keeps container-overflow detection enabled.
- Install Protovalidate's public `.proto` schemas alongside its generated headers,
  require the validation schema during verification, and smoke-test consumer imports.
- Add `toolchain resume` and `toolchain build --resume` to restore the recorded
  compiler environment, verify completed recipes, and continue interrupted builds.
  Recover older builds' PATH from retained configure logs when fingerprints match.
- Add a six-component GCC SDK example with CMake, Make, binutils, fmt, and spdlog,
  plus separate recordings of building the SDK and using a relocated copy.
- Let small SDK recipes declare their bootstrap tools without requiring all
  dependencies of the full preset. Recognize CMake built by an earlier recipe.

## 1.0.0 — Initial public preview

- Build native Linux C/C++ stacks from YAML recipes, including a 37-component
  GCC/LLVM preset and a small fmt/spdlog example.
- Inspect dependency plans, check host prerequisites, lock top-level sources,
  cache downloads, resume unchanged builds, and retain failure logs.
- Generate Bash/Zsh activation, CMake integration, component metadata, and
  archives with SHA-256 checksums.
- Record whether a compiler is bundled, external, or supplied by the host;
  use that selection consistently in activation and consumer verification.
- Exercise copied installations through automated C++ consumer tests.

The complete GCC/LLVM preset and its libc++ variant still require clean-build
acceptance. See [validation](docs/validation.md) for the tested scope.
