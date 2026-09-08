# Product evaluation — 2026-09-08

## Assessment

The tested workflow has practical value for teams that want to own and distribute
a selected Linux C++ development environment. A recipient can activate a prepared
installation and build an application with ordinary tools. The tests demonstrate
that workflow for the small example and selected uses of the preserved compiler
bundle. They do not measure customer demand or establish reliability for every
recipe in the full preset.

The 45 automated tests pass. A measured clean build of the small example took
14.69 seconds with four jobs and cached sources; that timing is specific to the
validation host.

The project is ready to present as an early open-source builder with a working
example and explicit support boundaries. A production claim for the entire
GCC/LLVM preset should wait for its full acceptance build.

## What was tested as a new user

1. Install the wheel into a fresh Python environment outside the checkout.
2. Export and validate the bundled recipes.
3. Build and verify a small library installation with a host compiler.
4. Build the documented fmt/spdlog example from pinned sources.
5. Archive the installation and check its checksum.
6. Copy it to a different directory, hide/remove its original prefix, disable
   networking, activate it, and compile and run an application.
7. Switch environments and restore the previous shell state.
8. Attempt activation with a missing external compiler and inspect the failure.
9. Use a copied, existing GCC/Clang SDK with its old path unavailable.

Detailed outcomes and the scope of earlier library builds are in
[validation](validation.md).

## Improvements made during the evaluation

| Friction | Change | User benefit |
| --- | --- | --- |
| The opening workflow required a large compiler build | Added a small fmt/spdlog C++ example and made it the README quickstart | A user can evaluate the workflow with an existing compiler |
| Library-only smoke verification demanded a bundled compiler | Verification uses the recorded host/external compiler | A valid small installation can be checked directly |
| Activation and generated CMake configuration could choose different compilers | Recorded compiler requirements are used consistently | Fewer accidental compiler/runtime changes between build methods |
| GCC-only activation did not explicitly select its compiler | Activation selects GCC/G++ when that is the bundled pair | A prepared GCC installation works through the same activation interface |
| An external compiler could be absent on the receiving machine | Activation reports the missing compiler and restores the previous environment | Failure happens before application configuration with a useful explanation |
| Generated files could retain private file permissions in an archive | Distribution permissions make files readable and installed tools executable | Another user can consume the extracted bundle |
| The small example bundled a second fmt through spdlog | spdlog uses the selected external fmt | The example demonstrates one consistent dependency choice |
| Public packaging and contribution paths were incomplete | Added MIT licensing, source packaging, CI, issue forms, contribution and release guides | New users can install, report failures, and understand the tested scope |

## Positioning supported by the tests

> Your C++ development environment, in one folder. Build it, archive it, share it,
> and activate it.

The benefit is a simple way to consume a selected stack, with a versioned artifact
under the team's control. Competing tools also support owned artifacts and
standalone deployment; the useful comparison is the effort required for the
specific workflow, not exclusive ownership of those capabilities.

The builder still requires someone to maintain recipes, validate upgrades, and
retain sources. Library-only bundles depend on their chosen compiler. Full SDKs
still depend on a compatible host runtime. Those requirements should stay visible
without dominating the initial quickstart.

## Next evidence to collect

Ask a few developers outside the project to complete the quickstart on their own
machines. Record successful attempts, time to the first working program, failure
messages, and whether they keep using the tool for a real project. Prioritize
improvements from those observations. Stars and downloads alone do not establish
that the product saves users time.

Before distributing a full preset as supported, complete the clean build and
receiving-platform checks in the [release guide](releasing.md).
