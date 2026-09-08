from __future__ import annotations

import contextlib
import datetime as dt
import os
import platform
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import Configuration
from .sources import SourceCache, source_identity
from .util import ToolchainError, digest, exclusive_lock, expand, inside, read_json, write_json


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class Runner:
    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.log: Path | None = None
        self.env: dict[str, str] | None = None

    def __call__(self, argv: list[str], *, cwd: Path, env: dict | None = None, capture: bool = False) -> str:
        command = shlex.join(str(x) for x in argv)
        if not self.quiet:
            print(f"  $ {command}", file=sys.stderr, flush=True)
        stream = self.log.open("a", encoding="utf-8") if self.log else contextlib.nullcontext()
        with stream as log:
            if log:
                log.write(f"\n[{now()}] {cwd}\n$ {command}\n")
                log.flush()
            try:
                process = subprocess.Popen(argv, cwd=cwd, env=env or self.env,
                                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, errors="replace", start_new_session=True)
            except OSError as exc:
                raise ToolchainError(f"Cannot run {argv[0]}: {exc}") from exc
            output = []
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    if log:
                        log.write(line)
                        log.flush()
                    if capture:
                        output.append(line)
                    elif not self.quiet:
                        print(line, end="", file=sys.stderr, flush=True)
                code = process.wait()
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                raise
            finally:
                process.stdout.close()
            if code:
                detail = f"; log: {self.log}" if self.log else ""
                if capture:
                    detail += "\n" + "".join(output)[-3000:]
                raise ToolchainError(f"Command exited {code}: {command}{detail}")
            return "".join(output)


def artifacts_missing(prefix: Path, patterns: list[str]) -> list[str]:
    return [pattern for pattern in patterns if not any(p.exists() for p in prefix.glob(pattern))]


class Builder:
    def __init__(self, config: Configuration, *, prefix: str | None = None,
                 work: str | None = None, cache: str | None = None, jobs: int | None = None,
                 stdlib: str | None = None, compiler_prefix: str | None = None,
                 offline: bool = False, quiet: bool = False, locked: bool = False,
                 lockfile: str | None = None):
        self.config = config
        self.prefix = config.location("prefix", prefix, "./install")
        self.work = config.location("work", work, ".toolchain-work")
        self.cache_path = config.location("cache", cache, ".toolchain-cache")
        self.jobs = jobs or config.settings.get("jobs", min(os.cpu_count() or 1, 8))
        self.stdlib = stdlib or config.settings.get("stdlib", "libstdc++")
        self.compiler_prefix = Path(compiler_prefix).expanduser().resolve() if compiler_prefix else None
        self.runner = Runner(quiet)
        self.cache = SourceCache(self.cache_path, self.runner, offline)
        self.lockfile = Path(lockfile).resolve() if lockfile else config.path.with_suffix(".lock.json")
        self.locked = locked
        self.source_lock = read_json(self.lockfile, {"schema_version": 1, "sources": {}})
        if locked and not self.lockfile.is_file():
            raise ToolchainError(f"Lockfile missing: {self.lockfile}; run 'toolchain fetch' first")
        if not isinstance(self.source_lock, dict) or self.source_lock.get("schema_version") != 1 or not isinstance(self.source_lock.get("sources"), dict):
            raise ToolchainError(f"Invalid source lockfile: {self.lockfile}")
        self.state_path = self.prefix / "share/toolchain/build-state.json"
        self.state = read_json(self.state_path, {"schema_version": 1, "recipes": {}})
        self.fingerprints: dict[str, str] = {}
        self.sources: dict[str, Path] = {}
        self.current: dict = {}
        self.variables: dict[str, str] = {}
        self.env: dict[str, str] = {}
        for root in (self.prefix, self.work, self.cache_path):
            if root in {Path("/"), Path.home(), config.path.parent}:
                raise ToolchainError(f"Choose a dedicated prefix/work/cache directory, not {root}")
        for first, second in ((self.prefix, self.work), (self.prefix, self.cache_path), (self.work, self.cache_path)):
            if first.is_relative_to(second) or second.is_relative_to(first):
                raise ToolchainError("Install prefix, work and cache directories must be separate, non-nested directories")

    def selected(self, requested: list[str] | None) -> list[str]:
        names = self.config.select(requested)
        if self.compiler_prefix:
            names = [n for n in names if self.config.recipes[n].get("stage") != "core"]
        if not names:
            raise ToolchainError("No recipes selected")
        return names

    def environment(self, recipe: dict) -> dict[str, str]:
        env = os.environ.copy()
        roots = [self.prefix]
        if self.compiler_prefix:
            roots.append(self.compiler_prefix)
        bootstrap = self.config.settings.get("bootstrap_prefix")
        if bootstrap:
            roots.append(Path(bootstrap).expanduser().resolve())
        for variable, paths in {
            "PATH": [str(p / "bin") for p in roots],
            "LD_LIBRARY_PATH": [str(p / d) for p in roots for d in ("lib", "lib64")],
            "LIBRARY_PATH": [str(p / d) for p in roots for d in ("lib", "lib64")],
            "CMAKE_PREFIX_PATH": [str(p) for p in roots],
            "PKG_CONFIG_PATH": [str(p / d) for p in roots for d in ("lib/pkgconfig", "lib64/pkgconfig", "share/pkgconfig")],
        }.items():
            env[variable] = os.pathsep.join(paths + ([env[variable]] if env.get(variable) else []))
        if recipe.get("stage", "library") == "library":
            if self.compiler_prefix or self.config.settings.get("compiler", "system") == "toolchain":
                compiler = self.compiler_prefix or self.prefix
                env["CC"], env["CXX"] = str(compiler / "bin/clang"), str(compiler / "bin/clang++")
                gcc_flag = f"--gcc-toolchain={shlex.quote(str(compiler))}" if self.stdlib == "libstdc++" else "-stdlib=libc++"
            else:
                env["CC"] = self.config.settings.get("cc", env.get("CC", "cc"))
                env["CXX"] = self.config.settings.get("cxx", env.get("CXX", "c++"))
                gcc_flag = "-stdlib=libc++" if self.stdlib == "libc++" else ""
            env["CFLAGS"] = f"-isystem {shlex.quote(str(self.prefix / 'include'))} -fPIC " + env.get("CFLAGS", "")
            env["CXXFLAGS"] = f"-isystem {shlex.quote(str(self.prefix / 'include'))} -fPIC {gcc_flag} " + env.get("CXXFLAGS", "")
        env["CMAKE_BUILD_PARALLEL_LEVEL"] = str(self.jobs)
        env["MAKEFLAGS"] = f"-j{self.jobs}"
        # Shell recipes use these explicit environment variables, with normal shell quoting.
        env.update({f"TC_{key.upper()}": value for key, value in self.variables.items()})
        env.update({key: expand(value, self.variables) for key, value in recipe.get("environment", {}).items()})
        return env

    def context(self, recipe: dict, fingerprint: str) -> None:
        self.current = recipe
        root = self.work / "build" / f"{recipe['name']}-{fingerprint[:16]}"
        compiler = self.compiler_prefix or self.prefix
        self.variables = {
            "name": recipe["name"], "version": recipe["version"], "prefix": str(self.prefix),
            "work": str(self.work), "cache": str(self.cache_path), "source": str(root / "source"),
            "build": str(root / "build"), "jobs": str(self.jobs), "stdlib": self.stdlib,
            "compiler_prefix": str(compiler),
            "llvm_runtimes": "libunwind" + (";libcxx;libcxxabi" if self.stdlib == "libc++" else ""),
            "python": sys.executable,
        }
        self.env = self.environment(recipe)
        self.runner.env = self.env

    def fingerprint(self, recipe: dict) -> str:
        environment = {key: os.environ.get(key, "") for key in
                       ("CC", "CXX", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "PATH", "CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH", "LD_LIBRARY_PATH")}
        return digest({"engine": __version__, "recipe": recipe, "source": source_identity(recipe),
                       "locked": self.source_lock["sources"].get(recipe["name"]) if self.locked else None,
                       "settings": self.config.settings, "prefix": str(self.prefix), "stdlib": self.stdlib,
                       "platform": [platform.system(), platform.machine()], "environment": environment,
                       "compiler_prefix": str(self.compiler_prefix),
                       "dependencies": {d: self.fingerprints.get(d, str(self.compiler_prefix)) for d in recipe.get("depends_on", [])}})

    def commands(self, recipe: dict) -> list:
        build = recipe["build"]
        result = list(build.get("prepare", []))
        system = build["system"]
        source = "${source}" + ("/" + build["source_subdir"] if build.get("source_subdir") else "")
        if system == "cmake":
            args = ["cmake", "-S", source, "-B", "${build}", "-G", "Unix Makefiles",
                    "-DCMAKE_INSTALL_PREFIX=${prefix}", "-DCMAKE_PREFIX_PATH=${prefix}",
                    "-DCMAKE_INSTALL_LIBDIR=lib", "-DCMAKE_BUILD_TYPE=Release",
                    "-DBUILD_SHARED_LIBS=OFF", "-DBUILD_TESTING=OFF", "-DCMAKE_POSITION_INDEPENDENT_CODE=ON"]
            if recipe.get("stage", "library") == "library":
                args += ["-DCMAKE_CXX_STANDARD=20", f"-DCMAKE_C_COMPILER={self.env['CC']}",
                         f"-DCMAKE_CXX_COMPILER={self.env['CXX']}", "-DCMAKE_REQUIRED_INCLUDES=${prefix}/include"]
            args += build.get("options", [])
            result.append({"run": args, "cwd": source})
            command = ["cmake", "--build", "${build}", "--parallel", "${jobs}"]
            if build.get("targets"):
                command += ["--target", *build["targets"]]
            result.append({"run": command, "cwd": source})
            if build.get("install", True):
                result.append({"run": ["cmake", "--install", "${build}"], "cwd": source})
        elif system == "autotools":
            cwd = "${source}" if build.get("in_source") else "${build}"
            result += [{"run": [source + "/configure", "--prefix=${prefix}", *build.get("options", [])], "cwd": cwd},
                       {"run": ["make", "-j${jobs}", *build.get("make_options", [])], "cwd": cwd},
                       {"run": ["make", *build.get("make_options", []), *build.get("install_targets", ["install"])], "cwd": cwd}]
        elif system == "make":
            options = ["PREFIX=${prefix}", *build.get("options", [])]
            result += [["make", "-j${jobs}", *options, *build.get("targets", [])],
                       ["make", *options, *build.get("install_targets", ["install"])]]
        elif system == "custom":
            result.extend(build["commands"])
        elif system in {"copy", "header-only"}:
            result.append({"hook": "copy_files"})
        result.extend(build.get("after", []))
        return result

    def run_step(self, step: list | dict) -> None:
        if isinstance(step, list):
            step = {"run": step}
        cwd = Path(expand(step.get("cwd", "${source}"), self.variables))
        if "hook" in step:
            from .hooks import HOOKS
            if step["hook"] not in HOOKS:
                raise ToolchainError(f"Unknown build hook: {step['hook']}")
            HOOKS[step["hook"]](self)
        elif "shell" in step:
            # Deliberately no templating here: shell scripts use $TC_PREFIX etc.
            self.runner(["bash", "-euo", "pipefail", "-c", step["shell"]], cwd=cwd, env=self.env)
        else:
            self.runner([expand(arg, self.variables) for arg in step["run"]], cwd=cwd, env=self.env)

    def plan(self, requested: list[str] | None = None) -> list[dict]:
        result = []
        for name in self.selected(requested):
            recipe = self.config.recipes[name]
            fp = self.fingerprint(recipe)
            self.fingerprints[name] = fp
            self.context(recipe, fp)
            commands = []
            for entry in self.commands(recipe):
                if isinstance(entry, list):
                    entry = {"run": entry}
                entry = dict(entry)
                entry["cwd"] = expand(entry.get("cwd", "${source}"), self.variables)
                if "run" in entry:
                    entry["run"] = [expand(x, self.variables) for x in entry["run"]]
                commands.append(entry)
            result.append({"name": name, "version": recipe["version"], "system": recipe["build"]["system"],
                           "depends_on": recipe.get("depends_on", []), "source": recipe["source"],
                           "artifacts": recipe.get("artifacts", []), "commands": commands,
                           "environment": {k: self.env[k] for k in ("CC", "CXX", "CFLAGS", "CXXFLAGS") if k in self.env},
                           "fingerprint": fp})
        return result

    def fetch_one(self, recipe: dict) -> tuple[Path, dict]:
        lock = self.source_lock["sources"].get(recipe["name"]) if self.locked else None
        if self.locked and lock is None:
            raise ToolchainError(f"{recipe['name']}: missing from lockfile {self.lockfile}")
        source, record = self.cache.fetch(recipe, lock)
        if not self.locked:
            # Merge under a lock so parallel fetch/build commands cannot lose entries.
            with exclusive_lock(self.lockfile.with_suffix(self.lockfile.suffix + ".lock")):
                current = read_json(self.lockfile, {"schema_version": 1, "sources": {}})
                current["sources"][recipe["name"]] = record
                write_json(self.lockfile, current)
                self.source_lock = current
        return source, record

    def fetch_all(self, requested: list[str] | None = None) -> None:
        for name in self.selected(requested):
            print(f"Fetching {name}", file=sys.stderr, flush=True)
            self.fetch_one(self.config.recipes[name])

    def build(self, requested: list[str] | None = None, force: bool = False) -> dict:
        from .inspection import write_metadata
        names = self.selected(requested)
        if self.compiler_prefix:
            for tool in ("clang", "clang++", "cmake"):
                if not os.access(self.compiler_prefix / "bin" / tool, os.X_OK):
                    raise ToolchainError(f"External toolchain is missing executable bin/{tool}: {self.compiler_prefix}")
        summary = {"built": [], "skipped": []}
        self.prefix.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.prefix / "share/toolchain/.build.lock"), exclusive_lock(self.work / ".build.lock"):
            self.state = read_json(self.state_path, {"schema_version": 1, "recipes": {}})
            for index, name in enumerate(names, 1):
                recipe = self.config.recipes[name]
                fp = self.fingerprint(recipe)
                self.fingerprints[name] = fp
                self.context(recipe, fp)
                old = self.state["recipes"].get(name, {})
                if not force and old.get("status") == "complete" and old.get("fingerprint") == fp and not artifacts_missing(self.prefix, recipe.get("artifacts", [])):
                    print(f"[{index}/{len(names)}] {name} {recipe['version']}: up to date", file=sys.stderr, flush=True)
                    summary["skipped"].append(name)
                    continue
                print(f"[{index}/{len(names)}] Building {name} {recipe['version']}", file=sys.stderr, flush=True)
                self.runner.log = self.work / "logs" / f"{name}.log"
                self.runner.log.parent.mkdir(parents=True, exist_ok=True)
                self.state["recipes"][name] = {"name": name, "version": recipe["version"], "fingerprint": fp,
                    "status": "building", "started_at": now(), "log": str(self.runner.log),
                    "artifacts": recipe.get("artifacts", []), "depends_on": recipe.get("depends_on", [])}
                write_json(self.state_path, self.state)
                try:
                    cached, provenance = self.fetch_one(recipe)
                    source = Path(self.variables["source"])
                    if not source.exists():
                        source.parent.mkdir(parents=True, exist_ok=True)
                        staging = source.with_name("source.partial")
                        if staging.exists():
                            shutil.rmtree(staging)
                        shutil.copytree(cached, staging, symlinks=True)
                        staging.replace(source)
                    self.sources[name] = source
                    Path(self.variables["build"]).mkdir(parents=True, exist_ok=True)
                    for directory in ("bin", "lib", "lib64", "include", "share", "etc"):
                        (self.prefix / directory).mkdir(exist_ok=True)
                    for step in self.commands(recipe):
                        self.run_step(step)
                    missing = artifacts_missing(self.prefix, recipe.get("artifacts", []))
                    if missing:
                        raise ToolchainError(f"{name}: install did not produce: {', '.join(missing)}")
                    self.state["recipes"][name].update(status="complete", completed_at=now(), source=provenance)
                    self.collect_licenses(name, source)
                    summary["built"].append(name)
                except BaseException as exc:
                    self.state["recipes"][name].update(status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed", error=str(exc))
                    raise
                finally:
                    write_json(self.state_path, self.state)
                    write_metadata(self)
            self.runner.log = None
            write_metadata(self)
        return summary

    def collect_licenses(self, name: str, source: Path) -> None:
        for pattern in ("*", "*/*"):
            for path in source.glob(pattern):
                if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "COPYRIGHT")):
                    target = self.prefix / "share/licenses" / name / path.relative_to(source)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, target)
