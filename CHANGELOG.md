# Changelog

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
