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
from .sanitizers import sanitizer_flags
from .util import ToolchainError, digest, exclusive_lock, expand, inside, read_json, write_json


BUILD_ENVIRONMENT_KEYS = ("CC", "CXX", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS", "PATH",
                          "CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH", "LD_LIBRARY_PATH")


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
                 lockfile: str | None = None, resume: bool = False, sanitizer: str | None = None):
        self.config = config
        if "variants" in config.settings:
            raise ToolchainError("Bundle configurations must be expanded into variants before building")
        self.compiler_relative_prefix: str | None = None
        self.compiler_identity: str | None = None
        self.prefix = config.location("prefix", prefix, "./install")
        self.work = config.location("work", work, ".toolchain-work")
        self.cache_path = config.location("cache", cache, ".toolchain-cache")
        self.jobs = jobs or config.settings.get("jobs", min(os.cpu_count() or 1, 8))
        self.stdlib = stdlib or config.settings.get("stdlib", "libstdc++")
        self.compiler_prefix = (Path(compiler_prefix).expanduser().resolve() if compiler_prefix else
                                config.location("compiler_prefix", None, "") if config.settings.get("compiler_prefix") else None)
        self.sanitizer = sanitizer or config.settings.get("sanitizer")
        self.sanitizer_flags = sanitizer_flags(self.sanitizer)
        if self.sanitizer and (not self.compiler_prefix or self.prefix == self.compiler_prefix):
            raise ToolchainError("Sanitizer builds require --compiler-prefix and a separate --prefix for libraries")
        self.runner = Runner(quiet)
        self.cache = SourceCache(self.cache_path, self.runner, offline)
        self.lockfile = (Path(lockfile).expanduser().resolve() if lockfile else
                         config.location("lockfile", None, "") if config.settings.get("lockfile") else config.path.with_suffix(".lock.json"))
        self.locked = locked
        self.source_lock = read_json(self.lockfile, {"schema_version": 1, "sources": {}})
        if locked and not self.lockfile.is_file():
            raise ToolchainError(f"Lockfile missing: {self.lockfile}; run 'toolchain fetch' first")
        if not isinstance(self.source_lock, dict) or self.source_lock.get("schema_version") != 1 or not isinstance(self.source_lock.get("sources"), dict):
            raise ToolchainError(f"Invalid source lockfile: {self.lockfile}")
        self.state_path = self.prefix / "share/toolchain/build-state.json"
        self.state = read_json(self.state_path, {"schema_version": 1, "recipes": {}})
        self.check_sanitizer_prefix()
        self.resume = resume
        self.build_environment = os.environ.copy()
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

    def check_sanitizer_prefix(self) -> None:
        if self.state.get("recipes") and self.state.get("sanitizer") != self.sanitizer:
            raise ToolchainError("Cannot mix sanitizer profiles or release libraries in one prefix; choose a separate --prefix")
        if self.sanitizer and not self.state.get("recipes") and any(self.prefix.glob("lib*/lib*.a")):
            raise ToolchainError("Sanitizer destination contains untracked libraries; choose an empty --prefix")

    def selected(self, requested: list[str] | None) -> list[str]:
        names = self.config.select(requested)
        if self.compiler_prefix:
            names = [n for n in names if self.config.recipes[n].get("stage") != "core"]
        if not names:
            raise ToolchainError("No recipes selected")
        return names

    def _changed_completed(self, names: list[str]) -> list[str]:
        self.fingerprints.clear()
        changed = []
        for name in names:
            fingerprint = self.fingerprint(self.config.recipes[name])
            self.fingerprints[name] = fingerprint
            old = self.state["recipes"].get(name, {})
            if old.get("status") == "complete" and old.get("fingerprint") != fingerprint:
                changed.append(name)
        return changed

    def prepare_resume(self, requested: list[str] | None = None) -> None:
        if not self.resume:
            return
        names = self.selected(requested)
        if not any(name in self.state["recipes"] for name in names):
            raise ToolchainError("No previous build to resume for these recipes; use 'toolchain build' first")
        saved = self.state.get("build_environment")
        if saved is not None:
            if (not isinstance(saved, dict) or set(saved) != set(BUILD_ENVIRONMENT_KEYS)
                    or any(value is not None and not isinstance(value, str) for value in saved.values())):
                raise ToolchainError("Invalid saved build environment in build-state.json")
            for key, value in saved.items():
                if value is None:
                    self.build_environment.pop(key, None)
                else:
                    self.build_environment[key] = value
        else:
            # Older state files did not record the environment. Try the current
            # one, then PATH from retained Autotools logs. Never trust an inferred
            # PATH unless every selected completed fingerprint still matches.
            complete = [name for name in names if self.state["recipes"].get(name, {}).get("status") == "complete"]
            if not complete:
                raise ToolchainError("Older build state has no saved environment or completed recipes to verify; "
                                     "repeat the original 'toolchain build' command")
            if self._changed_completed(names):
                current_path = self.build_environment.get("PATH")
                roots = [self.prefix]
                if self.compiler_prefix:
                    roots.append(self.compiler_prefix)
                if self.config.settings.get("bootstrap_prefix"):
                    roots.append(Path(self.config.settings["bootstrap_prefix"]).expanduser().resolve())
                added = [str(root / "bin") for root in roots]
                for name in complete:
                    fingerprint = self.state["recipes"][name].get("fingerprint", "")
                    if len(fingerprint) != 64 or any(c not in "0123456789abcdef" for c in fingerprint):
                        continue
                    log = self.work / "build" / f"{name}-{fingerprint[:16]}" / "build/config.log"
                    if not log.is_file():
                        continue
                    paths = [line[6:] for line in log.read_text(errors="replace").splitlines() if line.startswith("PATH: ")]
                    if paths[:len(added)] != added or len(paths) <= len(added):
                        continue
                    self.build_environment["PATH"] = os.pathsep.join(paths[len(added):])
                    if not self._changed_completed(names):
                        break
                else:
                    if current_path is None:
                        self.build_environment.pop("PATH", None)
                    else:
                        self.build_environment["PATH"] = current_path
                    raise ToolchainError("Cannot recover a matching environment from older build state. "
                                         "Use the original shell and build options, or 'toolchain build' to rebuild changed components")
        changed = self._changed_completed(names)
        if changed:
            raise ToolchainError("Cannot resume: completed recipes have changed: " + ", ".join(changed)
                                 + ". Keep the original build options (including --locked), or use "
                                 "'toolchain build' to rebuild changed components")

    def environment(self, recipe: dict) -> dict[str, str]:
        env = self.build_environment.copy()
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
        if self.sanitizer and recipe.get("stage", "library") == "library":
            for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
                env[key] = (env.get(key, "") + " " + " ".join(self.sanitizer_flags)).strip()
            overrides = self.sanitizer_overrides(recipe)
            for key, field in (("CFLAGS", "compile_flags"), ("CXXFLAGS", "compile_flags"), ("LDFLAGS", "link_flags")):
                flags = [expand(flag, self.variables) for flag in overrides.get(field, [])]
                if flags:
                    env[key] += " " + shlex.join(flags)
            # The external SDK supplies build tools, not release dependencies.
            env["CMAKE_PREFIX_PATH"] = str(self.prefix)
            env["PKG_CONFIG_PATH"] = os.pathsep.join(str(self.prefix / d) for d in
                                                   ("lib/pkgconfig", "lib64/pkgconfig", "share/pkgconfig"))
            env["LIBRARY_PATH"] = os.pathsep.join(str(self.prefix / d) for d in ("lib", "lib64"))
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

    def sanitizer_overrides(self, recipe: dict) -> dict:
        return recipe.get("sanitizer_overrides", {}).get(self.sanitizer, {}) if self.sanitizer else {}

    def fingerprint(self, recipe: dict) -> str:
        environment = {key: self.build_environment.get(key, "") for key in BUILD_ENVIRONMENT_KEYS}
        # Inactive profile settings must not invalidate unrelated SDK builds.
        effective_recipe = {key: value for key, value in recipe.items() if key != "sanitizer_overrides"}
        overrides = self.sanitizer_overrides(recipe)
        if overrides:
            effective_recipe["sanitizer_overrides"] = {self.sanitizer: overrides}
        return digest({"engine": __version__, "recipe": effective_recipe, "source": source_identity(recipe),
                       "locked": self.source_lock["sources"].get(recipe["name"]) if self.locked else None,
                       "settings": self.config.settings, "prefix": str(self.prefix), "stdlib": self.stdlib,
                       **({"sanitizer": self.sanitizer} if self.sanitizer else {}),
                       "platform": [platform.system(), platform.machine()], "environment": environment,
                       "compiler_prefix": str(self.compiler_prefix),
                       **({"compiler_identity": self.compiler_identity} if self.compiler_identity else {}),
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
            if self.sanitizer and recipe.get("stage", "library") == "library":
                args += [f"-DCMAKE_C_FLAGS={self.env['CFLAGS']}", f"-DCMAKE_CXX_FLAGS={self.env['CXXFLAGS']}",
                         f"-DCMAKE_EXE_LINKER_FLAGS={self.env['LDFLAGS']}",
                         f"-DCMAKE_SHARED_LINKER_FLAGS={self.env['LDFLAGS']}"]
            result.append({"run": args, "cwd": source})
            command = ["cmake", "--build", "${build}", "--parallel", "${jobs}"]
            if build.get("targets"):
                command += ["--target", *build["targets"]]
            result.append({"run": command, "cwd": source})
            if build.get("install", True):
                result.append({"run": ["cmake", "--install", "${build}"], "cwd": source})
        elif system == "autotools":
            cwd = "${source}" if build.get("in_source") else "${build}"
            options = list(build.get("options", []))
            if self.sanitizer and recipe["name"] == "xz":
                # XZ's configure rejects sanitizer instrumentation with Landlock.
                options.append("--disable-sandbox")
            result += [{"run": [source + "/configure", "--prefix=${prefix}", *options], "cwd": cwd},
                       {"run": ["make", "-j${jobs}", *build.get("make_options", [])], "cwd": cwd},
                       {"run": ["make", *build.get("make_options", []), *build.get("install_targets", ["install"])], "cwd": cwd}]
        elif system == "make":
            options = ["PREFIX=${prefix}", *build.get("options", [])]
            if self.sanitizer and recipe.get("stage", "library") == "library":
                options += [f"{key}={self.env[key]}" for key in ("CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS")]
            result += [["make", "-j${jobs}", *options, *build.get("targets", [])],
                       ["make", *options, *build.get("install_targets", ["install"])]]
        elif system == "custom":
            result.extend(build["commands"])
            if self.sanitizer and recipe["name"] == "boost":
                # b2 does not consume the conventional compiler flag variables.
                result = [dict(step, shell=step["shell"].replace(
                    './b2 toolset=clang', 'args=("cflags=$CFLAGS" "cxxflags=$CXXFLAGS" "linkflags=$LDFLAGS $CXXFLAGS"); ./b2 toolset=clang'))
                    if isinstance(step, dict) and "./b2 toolset=clang" in step.get("shell", "") else step
                    for step in result]
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
        self.prepare_resume(requested)
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
        self.prepare_resume(requested)
        if self.compiler_prefix:
            for tool in ("clang", "clang++", "cmake"):
                if not os.access(self.compiler_prefix / "bin" / tool, os.X_OK):
                    raise ToolchainError(f"External toolchain is missing executable bin/{tool}: {self.compiler_prefix}")
        summary = {"built": [], "skipped": []}
        self.prefix.mkdir(parents=True, exist_ok=True)
        self.work.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(self.prefix / "share/toolchain/.build.lock"), exclusive_lock(self.work / ".build.lock"):
            self.state = read_json(self.state_path, {"schema_version": 1, "recipes": {}})
            self.check_sanitizer_prefix()
            self.prepare_resume(requested)
            if self.sanitizer:
                self.state["sanitizer"] = self.sanitizer
            self.state["build_environment"] = {key: self.build_environment.get(key) for key in BUILD_ENVIRONMENT_KEYS}
            write_json(self.state_path, self.state)
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
