"""Small Python adapters for projects without a standard install target.

Build flags and versions live in recipes; these adapters implement file handling
and discovery that cannot use a fixed argv list.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .util import ToolchainError, atomic_write, expand, inside, sha256

if TYPE_CHECKING:
    from .engine import Builder


def copy_tree(source: Path, target: Path, patterns: tuple[str, ...] | None = None) -> None:
    if not source.exists():
        raise ToolchainError(f"Install source is missing: {source}")
    if source.is_dir():
        if patterns:
            for file in source.rglob("*"):
                if file.is_file() and any(file.match(p) for p in patterns):
                    copy_tree(file, target / file.relative_to(source))
        else:
            shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def copy_files(builder: Builder) -> None:
    recipe = builder.current["build"]
    copies = recipe.get("copies", [{"from": "${source}/include", "to": "include"}])
    for item in copies:
        source = Path(expand(item["from"], builder.variables))
        target = inside(builder.prefix, item["to"])
        copy_tree(source, target, tuple(item["patterns"]) if item.get("patterns") else None)
        if "mode" in item:
            target.chmod(int(str(item["mode"]), 8))


def gcc_math(builder: Builder) -> None:
    source = Path(builder.variables["source"])
    for name in ("gmp", "mpfr"):
        directory = Path(builder.variables["build"]) / ("install-" + name)
        directory.mkdir(exist_ok=True)
        builder.runner([str(source / name / "configure"), "--build=x86_64-linux-gnu",
                        "--host=x86_64-linux-gnu", f"--prefix={builder.prefix}",
                        f"--with-gmp={builder.prefix}"], cwd=directory)
        builder.runner(["make", f"-j{builder.jobs}"], cwd=directory)
        builder.runner(["make", "install"], cwd=directory)


def cmake_flags(builder: Builder) -> None:
    atomic_write(Path(builder.variables["source"]) / "build-flags.cmake", '''set(CMAKE_SKIP_RPATH ON CACHE BOOL "Skip rpath" FORCE)
set(CMAKE_USE_RELATIVE_PATHS ON CACHE BOOL "Relative paths" FORCE)
set(CMAKE_C_FLAGS "-g -O2 -fstack-protector-strong -Wformat -Werror=format-security -Wdate-time -D_FORTIFY_SOURCE=2" CACHE STRING "C flags" FORCE)
set(CMAKE_CXX_FLAGS "-g -O2 -fstack-protector-strong -Wformat -Werror=format-security -Wdate-time -D_FORTIFY_SOURCE=2" CACHE STRING "C++ flags" FORCE)
set(CMAKE_SKIP_BOOTSTRAP_TEST ON CACHE BOOL "Skip bootstrap test" FORCE)
set(BUILD_CursesDialog ON CACHE BOOL "Curses UI" FORCE)
''')


def gdbinit(builder: Builder) -> None:
    prefix = str(builder.prefix)
    atomic_write(builder.prefix / "etc/gdb/gdbinit", f'''set print pretty on
set print object on
set print static-members on
set print vtbl on
set print demangle on
set demangle-style gnu-v3
set print sevenbit-strings off
add-auto-load-scripts-directory {prefix}/lib64
add-auto-load-safe-path {prefix}
python
import sys
sys.path.insert(0, {str(builder.prefix / 'share/pahole-gdb')!r})
import offsets
import pahole
end
''')


def zlib_static(builder: Builder) -> None:
    for directory in ("lib", "lib64"):
        for path in (builder.prefix / directory).glob("libz.so*"):
            path.unlink()


def protobuf_headers(builder: Builder) -> None:
    source = Path(builder.variables["source"]) / "src/google/protobuf"
    copy_tree(source, builder.prefix / "include/google/protobuf", ("*.proto",))
    for name in ("timestamp", "duration", "struct", "empty", "any"):
        if not (builder.prefix / f"include/google/protobuf/{name}.proto").exists():
            raise ToolchainError(f"Protobuf did not install {name}.proto")
    options = builder.prefix / "include/absl/base/options.h"
    if options.exists():
        match = re.search(r"#define\s+ABSL_OPTION_INLINE_NAMESPACE_NAME\s+(\w+)", options.read_text())
        library = builder.prefix / "lib/libprotobuf.a"
        output = builder.runner(["nm", "-C", str(library)], cwd=builder.prefix, capture=True)
        trains = set(re.findall(r"\bU absl::(lts_\d{8})::", output))
        if trains and (not match or trains != {match[1]}):
            raise ToolchainError(f"Protobuf/Abseil ABI mismatch: references {sorted(trains)}, headers declare {match[1] if match else 'unknown'}")


def bazel_wrapper(builder: Builder) -> None:
    atomic_write(builder.prefix / "etc/bazel-cpp.bazelrc", '''build --announce_rc
build --enable_bzlmod=false
build --incompatible_enable_cc_toolchain_resolution
build --extra_toolchains=@bazel_tools//tools/cpp:toolchain
build --features=static_link_cpp_runtimes
query --keep_going
test --test_output=errors
''')
    wrapper = builder.prefix / "bin/bazel-cpp"
    atomic_write(wrapper, '''#!/usr/bin/env bash
set -e
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$HERE/bazel" --bazelrc="$HERE/../etc/bazel-cpp.bazelrc" "$@"
''')
    wrapper.chmod(0o755)


def install_archives(paths: list[Path], prefix: Path) -> None:
    seen: dict[str, Path] = {}
    if not paths:
        raise ToolchainError("Build produced no static archives")
    for source in sorted(paths):
        if source.name in seen and sha256(source) != sha256(seen[source.name]):
            raise ToolchainError(f"Conflicting static archive names: {source} and {seen[source.name]}")
        copy_tree(source, prefix / "lib" / source.name)
        seen[source.name] = source


def cel_headers(source: Path, prefix: Path) -> None:
    for directory in ("base", "common", "eval", "extensions", "parser", "runtime", "internal"):
        if (source / directory).is_dir():
            copy_tree(source / directory, prefix / "include" / directory, ("*.h", "*.inc"))
    for name in ("common/arena.h", "common/data.h"):
        header = prefix / "include" / name
        if header.exists() and '"absl/base/nullability.h"' not in header.read_text():
            atomic_write(header, '#include "absl/base/nullability.h"\n' + header.read_text())


def cel_build(builder: Builder) -> None:
    source = Path(builder.variables["source"])
    bazel = str(builder.prefix / "bin/bazel")
    common = [bazel, f"--output_user_root={builder.variables['build']}/bazelroot"]
    query = 'kind("cc_library", //...:*) except attr("testonly", "1", //...:*)'
    targets = builder.runner([*common, "query", query], cwd=source, capture=True)
    targets = sorted(line for line in targets.splitlines() if line.startswith("//"))
    if not targets:
        raise ToolchainError("CEL: Bazel query returned no library targets")
    builder.runner([*common, "build", "-c", "opt", f"--jobs={builder.jobs}",
                    f"--action_env=CC={builder.env['CC']}", f"--action_env=CXX={builder.env['CXX']}",
                    "--check_direct_dependencies=off", *targets], cwd=source)
    output = builder.runner([*common, "info", "-c", "opt", "bazel-bin"], cwd=source, capture=True)
    candidates = [Path(line) for line in output.splitlines() if line.startswith("/")]
    if not candidates:
        raise ToolchainError("CEL: unable to locate Bazel output directory")
    binary = candidates[-1]
    archives = [p for directory in ("base", "common", "eval", "extensions", "parser", "runtime")
                for p in (binary / directory).rglob("lib*.a")]
    install_archives(archives, builder.prefix)
    cel_headers(source, builder.prefix)
    # Generated protobuf headers are part of the public CEL interface.
    for header in binary.rglob("*.pb.h"):
        relative = header.relative_to(binary)
        if "external" not in relative.parts:
            copy_tree(header, builder.prefix / "include" / relative)
        else:
            for root in ("google", "cel"):
                if root in relative.parts:
                    copy_tree(header, builder.prefix / "include" / Path(*relative.parts[relative.parts.index(root):]))
                    break
    # Protobuf normally installs utf8_range; older source combinations may
    # expose it only as a Bazel external dependency, as in the POC.
    if not (builder.prefix / "include/utf8_range.h").exists():
        for external in source.glob("bazel-*/external"):
            headers = list(external.glob("*/utf8_range.h")) + list(external.glob("*/third_party/utf8_range/utf8_range.h"))
            if headers:
                copy_tree(headers[0], builder.prefix / "include/utf8_range.h")
                break
    for library in binary.glob("external/**/libutf8_range*.a"):
        if not (builder.prefix / "lib" / library.name).exists():
            copy_tree(library, builder.prefix / "lib" / library.name)


def protovalidate_install(builder: Builder) -> None:
    source, build = Path(builder.variables["source"]), Path(builder.variables["build"])
    install_archives(list(build.rglob("lib*.a")), builder.prefix)
    copy_tree(source / "buf", builder.prefix / "include/buf", ("*.h",))
    cel = build / "_deps/cel_cpp-src"
    if not cel.is_dir():
        raise ToolchainError(f"Vendored CEL sources are missing: {cel}")
    cel_headers(cel, builder.prefix)
    # Preserve generated import paths (including buf/validate/validate.pb.h).
    for header in build.rglob("*.pb.h"):
        parts = header.relative_to(build).parts
        for root in ("buf", "cel", "google"):
            if root in parts:
                copy_tree(header, builder.prefix / "include" / Path(*parts[parts.index(root):]))
                break
    temporary = build / "installed-protos"
    temporary.mkdir(exist_ok=True)
    builder.runner([str(builder.prefix / "bin/protoc"), "-I", str(builder.prefix / "include"),
                    f"--cpp_out={temporary}", "cel/expr/syntax.proto", "cel/expr/checked.proto"], cwd=builder.prefix)
    for name in ("syntax", "checked"):
        copy_tree(temporary / f"cel/expr/{name}.pb.h", builder.prefix / f"include/cel/expr/{name}.pb.h")


HOOKS = {"copy_files": copy_files, "gcc_math": gcc_math, "cmake_flags": cmake_flags,
         "gdbinit": gdbinit, "zlib_static": zlib_static, "protobuf_headers": protobuf_headers,
         "bazel_wrapper": bazel_wrapper, "cel_build": cel_build, "protovalidate_install": protovalidate_install}
