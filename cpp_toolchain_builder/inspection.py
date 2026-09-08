from __future__ import annotations

import fnmatch
import gzip
import json
import os
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from .util import ToolchainError, atomic_write, exclusive_lock, read_json, sha256, write_json

if TYPE_CHECKING:
    from .engine import Builder


def compiler_configuration(builder: Builder) -> dict:
    """Record the compiler consumers need, including dependencies outside the SDK."""
    root = builder.compiler_prefix or builder.prefix
    for cc, cxx in (("clang", "clang++"), ("gcc", "g++")):
        if (root / "bin" / cc).is_file() and (root / "bin" / cxx).is_file():
            return {"mode": "external" if builder.compiler_prefix else "bundled",
                    "prefix": str(root), "cc": cc, "cxx": cxx}
    return {"mode": "system", **{
        key.lower(): shutil.which(builder.env.get(key, default), path=builder.env.get("PATH"))
        or builder.env.get(key, default) for key, default in (("CC", "cc"), ("CXX", "c++"))}}


def activation(prefix: Path, stdlib: str = "libstdc++", compiler: dict | None = None) -> str:
    variables = ["PATH", "LD_LIBRARY_PATH", "CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH", "CC", "CXX", "CFLAGS", "CXXFLAGS", "TOOLCHAIN_PREFIX"]
    lines = ['# Source this file in bash or zsh.',
             'if command -v toolchain_deactivate >/dev/null 2>&1; then toolchain_deactivate; fi']
    for key in variables:
        lines += [f'if [ "${{{key}+x}}" = x ]; then _TC_HAD_{key}=1; _TC_OLD_{key}=${{{key}}}; else _TC_HAD_{key}=0; fi']
    lines += ['if [ -n "${ZSH_VERSION:-}" ]; then', '  _TC_FILE="${(%):-%x}"', 'else',
              '  _TC_FILE="${BASH_SOURCE[0]}"', 'fi',
              'export TOOLCHAIN_PREFIX="$(cd "$(dirname "$_TC_FILE")" && pwd)"', 'unset _TC_FILE',
              'export PATH="$TOOLCHAIN_PREFIX/bin:$PATH"',
              'export LD_LIBRARY_PATH="$TOOLCHAIN_PREFIX/lib:$TOOLCHAIN_PREFIX/lib64${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"',
              'export CMAKE_PREFIX_PATH="$TOOLCHAIN_PREFIX${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"',
              'export PKG_CONFIG_PATH="$TOOLCHAIN_PREFIX/lib/pkgconfig:$TOOLCHAIN_PREFIX/lib64/pkgconfig:$TOOLCHAIN_PREFIX/share/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"']
    # Keep an external compiler dependency explicit; never silently fall back to a
    # different C++ runtime when a library-only archive is activated.
    if compiler and compiler['mode'] == 'external':
        lines += [f'_TC_COMPILER_ROOT={shlex.quote(compiler["prefix"])}',
                  'export PATH="$TOOLCHAIN_PREFIX/bin:$_TC_COMPILER_ROOT/bin${_TC_OLD_PATH:+:$_TC_OLD_PATH}"',
                  'export LD_LIBRARY_PATH="$TOOLCHAIN_PREFIX/lib:$TOOLCHAIN_PREFIX/lib64:$_TC_COMPILER_ROOT/lib:$_TC_COMPILER_ROOT/lib64${_TC_OLD_LD_LIBRARY_PATH:+:$_TC_OLD_LD_LIBRARY_PATH}"',
                  f'export CC="$_TC_COMPILER_ROOT/bin/{compiler["cc"]}" CXX="$_TC_COMPILER_ROOT/bin/{compiler["cxx"]}"']
    elif compiler and compiler['mode'] == 'system':
        lines += [f'export CC={shlex.quote(compiler["cc"])} CXX={shlex.quote(compiler["cxx"])}']
    else:
        lines += ['_TC_COMPILER_ROOT="$TOOLCHAIN_PREFIX"',
                  'if [ -x "$TOOLCHAIN_PREFIX/bin/clang++" ]; then',
                  '  export CC="$TOOLCHAIN_PREFIX/bin/clang" CXX="$TOOLCHAIN_PREFIX/bin/clang++"',
                  'elif [ -x "$TOOLCHAIN_PREFIX/bin/g++" ]; then',
                  '  export CC="$TOOLCHAIN_PREFIX/bin/gcc" CXX="$TOOLCHAIN_PREFIX/bin/g++"', 'fi']
    # Quoted paths are interpreted by CMake/Autotools when consuming *FLAGS.
    lines += [r'export CFLAGS="-isystem \"$TOOLCHAIN_PREFIX/include\" ${CFLAGS:-}"',
              r'export CXXFLAGS="-isystem \"$TOOLCHAIN_PREFIX/include\" ${CXXFLAGS:-}"']
    if stdlib == 'libc++':
        lines += ['export CXXFLAGS="-stdlib=libc++ $CXXFLAGS"']
    else:
        lines += ['if [ -n "${_TC_COMPILER_ROOT:-}" ] && [ -x "$_TC_COMPILER_ROOT/bin/clang++" ] && [ -x "$_TC_COMPILER_ROOT/bin/g++" ]; then',
                  '  export CXXFLAGS="--gcc-toolchain=\\"$_TC_COMPILER_ROOT\\" $CXXFLAGS"', 'fi']
    lines += ['unset _TC_COMPILER_ROOT', 'toolchain_deactivate() {']
    for key in variables:
        lines += [f'  if [ "$_TC_HAD_{key}" = 1 ]; then export {key}="$_TC_OLD_{key}"; else unset {key}; fi',
                  f'  unset _TC_HAD_{key} _TC_OLD_{key}']
    lines += ['  unset -f toolchain_deactivate', '}']
    if compiler:
        lines += ['if ! command -v "$CC" >/dev/null 2>&1 || ! command -v "$CXX" >/dev/null 2>&1; then',
                  '  printf "Toolchain compiler unavailable: %s / %s. See this installation\047s README.md.\\n" "$CC" "$CXX" >&2',
                  '  toolchain_deactivate', '  return 1', 'fi']
    return "\n".join(lines) + "\n"


def write_metadata(builder: Builder) -> None:
    from .engine import now
    settings = builder.config.settings
    entries = list(builder.state["recipes"].values())
    complete = [entry for entry in entries if entry["status"] == "complete"]
    compiler = compiler_configuration(builder)
    manifest = {"schema_version": 1, "toolchain": {"name": settings.get("name", "toolchain"),
                "version": str(settings.get("version", "1")), "prefix": str(builder.prefix),
                "stdlib": builder.stdlib, "platform": platform.platform(), "arch": platform.machine(),
                "compiler": compiler}, "generated_at": now(), "components": entries}
    write_json(builder.prefix / "share/toolchain/manifest.json", manifest, mode=0o644)
    atomic_write(builder.prefix / "share/manifest.yaml", yaml.safe_dump(manifest, sort_keys=False), mode=0o644)
    write_json(builder.prefix / "share/sbom/bom.cdx.json", {"bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
               "components": [{"type": "library", "name": entry["name"], "version": entry["version"],
                               **({"hashes": [{"alg": "SHA-256", "content": entry["source"]["sha256"]}]}
                                  if entry.get("source", {}).get("sha256") else {})} for entry in complete]}, mode=0o644)
    atomic_write(builder.prefix / "activate", activation(builder.prefix, builder.stdlib, compiler), mode=0o644)
    root = compiler.get('prefix', str(builder.prefix)) if compiler['mode'] == 'external' else '${TOOLCHAIN_PREFIX}'
    cc_path = root + '/bin/' + compiler['cc'] if compiler['mode'] != 'system' else compiler['cc']
    cxx_path = root + '/bin/' + compiler['cxx'] if compiler['mode'] != 'system' else compiler['cxx']
    flags = '-stdlib=libc++' if builder.stdlib == 'libc++' else (
        f'--gcc-toolchain=\\"{root}\\"' if compiler['mode'] != 'system' and compiler['cxx'] == 'clang++'
        and (Path(compiler['prefix']) / 'bin/g++').is_file() else '')
    def cmake_literal(value: str) -> str:
        # Preserve only the generated relocation variable, never caller CMake syntax.
        return value.replace('\\', '\\\\').replace('"', '\\"').replace(';', '\\;').replace('$', '\\$').replace('\\${TOOLCHAIN_PREFIX}', '${TOOLCHAIN_PREFIX}')
    atomic_write(builder.prefix / 'share/toolchain/toolchain.cmake', f'''get_filename_component(TOOLCHAIN_PREFIX "${{CMAKE_CURRENT_LIST_DIR}}/../.." ABSOLUTE)
set(CMAKE_C_COMPILER "{cmake_literal(cc_path)}" CACHE FILEPATH "")
set(CMAKE_CXX_COMPILER "{cmake_literal(cxx_path)}" CACHE FILEPATH "")
list(PREPEND CMAKE_PREFIX_PATH "${{TOOLCHAIN_PREFIX}}")
set(CMAKE_CXX_STANDARD 20 CACHE STRING "")
set(CMAKE_POSITION_INDEPENDENT_CODE ON CACHE BOOL "")
set(CMAKE_CXX_FLAGS_INIT "{flags}")
list(APPEND CMAKE_BUILD_RPATH "${{TOOLCHAIN_PREFIX}}/lib" "${{TOOLCHAIN_PREFIX}}/lib64" "{cmake_literal(root)}/lib" "{cmake_literal(root)}/lib64")
''', mode=0o644)
    requirement = ('The compiler is included in this installation.' if compiler['mode'] == 'bundled' else
                   f'This is a library-only installation. It requires the external compiler at `{compiler["prefix"]}`.'
                   if compiler['mode'] == 'external' else
                   f'This is a library-only installation. It requires host compilers `{compiler["cc"]}` and `{compiler["cxx"]}`.')
    atomic_write(builder.prefix / 'README.md', f"# {settings.get('name', 'Toolchain')}\n\n"
                 f"{len(complete)} components installed. Standard library: {builder.stdlib}.\n\n"
                 f"{requirement}\n\n"
                 "Source `activate` in bash/zsh; run `toolchain_deactivate` to restore your environment.\n\n"
                 "Use `share/toolchain/toolchain.cmake` with CMake. The Python builder is not needed to use "
                 "this installation. If installed, it can check the bundle with "
                 "`toolchain inspect verify --prefix <this-directory> --smoke`.\n\n"
                 "Component versions and source identities are in `share/toolchain/manifest.json`; "
                 "collected third-party notices are in `share/licenses/`. The component inventory is not "
                 "a complete inventory of upstream vendored dependencies.\n\n"
                 "Use a compatible Linux architecture and distribution. Host runtime libraries such as "
                 "glibc, curl and Python are not bundled. Some upstream tools embed their installation "
                 "prefix; a successful relocation test for one bundle does not prove all recipes relocate.\n", mode=0o644)


def inventory(prefix: Path, kind: str = "all", pattern: str | None = None) -> list[dict]:
    if not prefix.is_dir():
        raise ToolchainError(f"Toolchain prefix does not exist: {prefix}")
    roots = {"libs": [prefix / "lib", prefix / "lib64"], "bins": [prefix / "bin"],
             "headers": [prefix / "include"]}.get(kind, [prefix])
    entries = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if not path.is_file() and not path.is_symlink():
                continue
            if kind == "libs" and not (path.name.endswith(".a") or ".so" in path.name or path.name.endswith(".dylib")):
                continue
            if kind == "bins" and not (os.access(path, os.X_OK) or path.is_symlink()):
                continue
            relative = str(path.relative_to(prefix))
            if pattern and not (fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern)):
                continue
            entry = {"path": relative, "size": path.lstat().st_size}
            if path.is_symlink():
                entry.update(target=os.readlink(path), broken=not path.exists())
            entries.append(entry)
    return entries


def info(prefix: Path) -> dict:
    if not prefix.is_dir():
        raise ToolchainError(f"Toolchain prefix does not exist: {prefix}")
    manifest = read_json(prefix / "share/toolchain/manifest.json")
    result = {"prefix": str(prefix), "managed": bool(manifest), "manifest": manifest}
    legacy = prefix / "share/manifest.yaml"
    if not manifest and legacy.exists():
        try:
            result["legacy_manifest"] = yaml.safe_load(legacy.read_text())
        except yaml.YAMLError:
            result["legacy_manifest_warning"] = "The legacy manifest is malformed YAML; file inventory remains available."
    result["counts"] = {kind: len(inventory(prefix, kind)) for kind in ("bins", "libs", "headers")}
    result["tools"] = {}
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = f"{prefix}/lib:{prefix}/lib64" + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    for name in ("gcc", "g++", "clang", "cmake", "gdb", "ld", "protoc"):
        executable = prefix / "bin" / name
        if not executable.is_file():
            continue
        record = {"path": str(executable)}
        try:
            probe = subprocess.run([str(executable), "--version"], capture_output=True, text=True, env=env, timeout=10)
            lines = (probe.stdout or probe.stderr).strip().splitlines()
            record["version" if probe.returncode == 0 else "error"] = lines[0] if lines else f"exit {probe.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            record["error"] = str(exc)
        result["tools"][name] = record
    return result


def smoke(prefix: Path, stdlib: str = "libstdc++", compiler_prefix: Path | None = None) -> dict:
    metadata = read_json(prefix / "share/toolchain/manifest.json", {}).get("toolchain", {}).get("compiler", {})
    compiler_root = compiler_prefix or (Path(metadata['prefix']) if metadata.get('mode') == 'external' else prefix)
    compiler = compiler_root / "bin/clang++"
    if not compiler.is_file():
        compiler = compiler_root / "bin/g++"
    if not compiler.is_file():
        if compiler_prefix or metadata.get('mode') in {'bundled', 'external'}:
            raise ToolchainError(f"No C++ compiler in {compiler_root}/bin")
        candidate = metadata.get('cxx') or os.environ.get('CXX', 'c++')
        resolved = shutil.which(candidate)
        if not resolved:
            raise ToolchainError(f"Host C++ compiler unavailable: {candidate}")
        compiler = Path(resolved)
    env = os.environ.copy()
    roots = [prefix, compiler_root]
    env["LD_LIBRARY_PATH"] = ":".join(str(p / d) for p in roots for d in ("lib", "lib64")) + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    flags = ["-std=c++20", "-pthread", "-isystem", str(prefix / "include")]
    if compiler.name == "clang++":
        if stdlib == "libc++":
            flags += ["-stdlib=libc++"]
        elif (compiler_root / "bin/g++").is_file():
            flags += [f"--gcc-toolchain={compiler_root}"]
    includes = '#include <iostream>\n#include <vector>\n#include <string>\n#include <thread>\n'
    body = 'std::vector<std::string> v{"toolchain"}; std::thread t([]{}); t.join(); std::cout << v.at(0);'
    libraries = []
    if (prefix / "include/fmt/format.h").is_file():
        includes += '#include <fmt/format.h>\n'
        body += 'if (fmt::format("{}", 42) != "42") return 2;'
        libraries += ["-lfmt"]
    if (prefix / "include/spdlog/spdlog.h").is_file():
        includes += '#include <spdlog/spdlog.h>\n'
        body += 'spdlog::set_level(spdlog::level::off); spdlog::info("test");'
        libraries.insert(0, "-lspdlog")
        for targets in prefix.glob("lib*/cmake/spdlog/*Targets.cmake"):
            if "SPDLOG_FMT_EXTERNAL" in targets.read_text():
                flags += ["-DSPDLOG_FMT_EXTERNAL"]
                break
    with tempfile.TemporaryDirectory(prefix="toolchain-smoke-") as temporary:
        directory = Path(temporary)
        source, binary = directory / "smoke.cpp", directory / "smoke"
        source.write_text(includes + "\nint main(){" + body + "}\n")
        command = [str(compiler), *flags, str(source), "-L" + str(prefix / "lib"), "-L" + str(prefix / "lib64"), *libraries, "-o", str(binary)]
        for argv in (command, [str(binary)]):
            try:
                completed = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ToolchainError(f"Smoke test could not run: {exc}") from exc
            if completed.returncode:
                raise ToolchainError(f"Smoke test failed: {shlex.join(argv)}\n{completed.stderr[-4000:]}")
    return {"passed": True, "compiler": str(compiler), "standard": "c++20", "libraries": libraries}


def verify(prefix: Path, recipes: dict | None = None, run_smoke: bool = False,
           stdlib: str = "libstdc++", compiler_prefix: Path | None = None) -> dict:
    from .engine import artifacts_missing
    if not prefix.is_dir():
        raise ToolchainError(f"Toolchain prefix does not exist: {prefix}")
    failures = []
    state = read_json(prefix / "share/toolchain/build-state.json")
    checks = recipes or ({n: e for n, e in state["recipes"].items()} if state else {})
    if not checks:
        failures.append("No managed manifest; pass --config to verify expected component artifacts")
    for name, recipe in checks.items():
        failures += [f"{name}: missing {path}" for path in artifacts_missing(prefix, recipe.get("artifacts", []))]
        if state and name in state["recipes"] and state["recipes"][name].get("status") != "complete":
            failures.append(f"{name}: build status is {state['recipes'][name].get('status')}")
    failures += [f"Broken symlink: {item['path']} -> {item['target']}" for item in inventory(prefix) if item.get("broken")]
    result = {"prefix": str(prefix), "passed": not failures, "components_checked": len(checks), "failures": failures}
    if run_smoke:
        try:
            result["smoke"] = smoke(prefix, stdlib, compiler_prefix)
        except ToolchainError as exc:
            failures.append(str(exc))
            result["passed"] = False
    return result


def archive(prefix: Path, destination: Path, force: bool = False) -> dict:
    if not prefix.is_dir():
        raise ToolchainError(f"Prefix does not exist: {prefix}")
    with exclusive_lock(prefix / "share/toolchain/.build.lock"):
        return _archive(prefix, destination, force)


def _archive(prefix: Path, destination: Path, force: bool = False) -> dict:
    if not prefix.is_dir():
        raise ToolchainError(f"Prefix does not exist: {prefix}")
    if destination.resolve().is_relative_to(prefix.resolve()):
        raise ToolchainError("Archive output must be outside the install prefix")
    if destination.exists() and not force:
        raise ToolchainError(f"Archive exists: {destination}; use --force to replace it")
    result = verify(prefix)
    if not result["passed"]:
        raise ToolchainError("Cannot archive an incomplete toolchain: " + "; ".join(result["failures"][:10]))
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".archive-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as output, gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as stream:
                def normalize(member: tarfile.TarInfo) -> tarfile.TarInfo | None:
                    if member.name.endswith("/.build.lock"):
                        return None
                    member.uid = member.gid = 0
                    member.uname = member.gname = "root"
                    # SDK artifacts must remain readable after extraction by a
                    # different user; preserve executable bits on installed tools.
                    if member.isdir():
                        member.mode |= 0o555
                    elif member.isfile():
                        member.mode |= 0o444
                        if member.mode & 0o111:
                            member.mode |= 0o111
                    return member
                stream.add(prefix, arcname=prefix.name, filter=normalize)
        os.chmod(temporary, 0o644)
        Path(temporary).replace(destination)
    finally:
        Path(temporary).unlink(missing_ok=True)
    checksum = sha256(destination)
    atomic_write(destination.with_name(destination.name + ".sha256"), f"{checksum}  {destination.name}\n", mode=0o644)
    return {"archive": str(destination), "sha256": checksum, "size": destination.stat().st_size}
