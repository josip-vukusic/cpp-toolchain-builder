from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from .util import ToolchainError, atomic_write, inside

SYSTEMS = {"cmake", "autotools", "make", "copy", "header-only", "custom"}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]*$")


class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader: UniqueLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ToolchainError("YAML mapping keys must be strings")
        if key in result:
            raise ToolchainError(f"Duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)


def read_yaml(path: Path) -> dict:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ToolchainError(f"Cannot load configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ToolchainError(f"Expected a YAML mapping in {path}")
    return data


def save_yaml(path: Path, data: dict) -> None:
    atomic_write(path, yaml.safe_dump(data, sort_keys=False))


def strings(value: Any, label: str) -> None:
    if not isinstance(value, list) or any(not isinstance(x, str) for x in value):
        raise ToolchainError(f"{label} must be a list of strings (quote versions and options)")


def steps(items: Any, label: str) -> None:
    if not isinstance(items, list):
        raise ToolchainError(f"{label} must be a list")
    for step in items:
        if isinstance(step, list):
            strings(step, label)
            if not step:
                raise ToolchainError(f"{label}: empty command")
        elif isinstance(step, dict):
            kinds = set(step) & {"run", "shell", "hook"}
            if len(kinds) != 1 or set(step) - {"run", "shell", "hook", "cwd"}:
                raise ToolchainError(f"{label}: use one of run, shell, hook, with optional cwd")
            if "run" in step:
                strings(step["run"], label)
                if not step["run"]:
                    raise ToolchainError(f"{label}: empty command")
            elif not isinstance(step[next(iter(kinds))], str):
                raise ToolchainError(f"{label}: step value must be a string")
            if "cwd" in step and not isinstance(step["cwd"], str):
                raise ToolchainError(f"{label}: cwd must be a string")
        else:
            raise ToolchainError(f"{label}: command must be an argv list or a step mapping")


def validate_recipe(recipe: dict) -> None:
    name = recipe.get("name", "")
    if not isinstance(name, str) or not NAME.fullmatch(name) or name in {".", ".."}:
        raise ToolchainError(f"Invalid recipe name: {name!r}")
    if not isinstance(recipe.get("version"), str) or not recipe["version"]:
        raise ToolchainError(f"{name}: version must be a nonempty quoted string")
    unknown = set(recipe) - {"name", "version", "stage", "source", "depends_on", "build", "artifacts", "environment", "requires", "description"}
    if unknown:
        raise ToolchainError(f"{name}: unknown recipe fields: {', '.join(sorted(unknown))}")
    source = recipe.get("source")
    if not isinstance(source, dict) or len(set(source) & {"url", "git", "path"}) != 1:
        raise ToolchainError(f"{name}: specify exactly one source.url, source.git, or source.path")
    unknown = set(source) - {"url", "git", "path", "ref", "sha256", "filename", "directory", "strip_root", "submodules", "unpack"}
    if unknown:
        raise ToolchainError(f"{name}: unknown source fields: {', '.join(sorted(unknown))}")
    for key in ("url", "git", "path", "ref", "sha256", "filename", "directory"):
        if key in source and (not isinstance(source[key], str) or not source[key]):
            raise ToolchainError(f"{name}: source.{key} must be a nonempty string")
    if "git" in source and not source.get("ref"):
        raise ToolchainError(f"{name}: git sources need a ref (tag, branch, or commit)")
    if "sha256" in source and not re.fullmatch(r"[0-9a-fA-F]{64}", source["sha256"]):
        raise ToolchainError(f"{name}: invalid SHA-256")
    if "filename" in source and Path(source["filename"]).name != source["filename"]:
        raise ToolchainError(f"{name}: source.filename must be a basename")
    if "directory" in source:
        inside(Path("/recipe"), source["directory"])
    if "strip_root" in source and not isinstance(source["strip_root"], bool):
        raise ToolchainError(f"{name}: source.strip_root must be true or false")
    if "unpack" in source and not isinstance(source["unpack"], bool):
        raise ToolchainError(f"{name}: source.unpack must be true or false")
    if "submodules" in source and not isinstance(source["submodules"], bool):
        raise ToolchainError(f"{name}: source.submodules must be true or false")
    strings(recipe.get("depends_on", []), f"{name}.depends_on")
    strings(recipe.get("requires", []), f"{name}.requires")
    if recipe.get("stage", "library") not in {"core", "library", "data"}:
        raise ToolchainError(f"{name}: stage must be core, library, or data")
    environment = recipe.get("environment", {})
    if not isinstance(environment, dict) or any(not isinstance(v, str) for v in environment.values()):
        raise ToolchainError(f"{name}: environment must map names to strings")
    build = recipe.get("build", {})
    if not isinstance(build, dict) or build.get("system") not in SYSTEMS:
        raise ToolchainError(f"{name}: unsupported build.system; expected {', '.join(sorted(SYSTEMS))}")
    unknown = set(build) - {"system", "options", "prepare", "commands", "after", "copies", "source_subdir", "targets", "install_targets", "make_options", "in_source", "install"}
    if unknown:
        raise ToolchainError(f"{name}: unknown build fields: {', '.join(sorted(unknown))}")
    for field in ("install", "in_source"):
        if field in build and not isinstance(build[field], bool):
            raise ToolchainError(f"{name}: build.{field} must be true or false")
    for field in ("options", "make_options", "install_targets", "targets"):
        strings(build.get(field, []), f"{name}.build.{field}")
    for field in ("prepare", "commands", "after"):
        steps(build.get(field, []), f"{name}.build.{field}")
    if build["system"] == "custom" and not build.get("commands"):
        raise ToolchainError(f"{name}: custom recipes need build.commands")
    if "source_subdir" in build:
        inside(Path("/recipe"), build["source_subdir"])
    if not isinstance(build.get("copies", []), list):
        raise ToolchainError(f"{name}: build.copies must be a list")
    for item in build.get("copies", []):
        if not isinstance(item, dict) or not {"from", "to"} <= set(item):
            raise ToolchainError(f"{name}: copies need from/to paths")
        if not isinstance(item["from"], str) or not isinstance(item["to"], str):
            raise ToolchainError(f"{name}: copies from/to must be strings")
        if "patterns" in item:
            strings(item["patterns"], f"{name}.build.copies.patterns")
        inside(Path("/prefix"), item["to"])
    artifacts = recipe.get("artifacts", [])
    strings(artifacts, f"{name}.artifacts")
    for pattern in artifacts:
        inside(Path("/prefix"), pattern)


def resolve(recipes: dict[str, dict], requested: list[str]) -> list[str]:
    result: list[str] = []
    visiting: list[str] = []
    done: set[str] = set()

    def visit(name: str) -> None:
        if name in done:
            return
        if name in visiting:
            raise ToolchainError("Dependency cycle: " + " -> ".join(visiting + [name]))
        if name not in recipes:
            raise ToolchainError(f"Unknown recipe/dependency: {name}")
        visiting.append(name)
        for dependency in recipes[name].get("depends_on", []):
            visit(dependency)
        visiting.pop()
        done.add(name)
        result.append(name)

    for name in requested:
        visit(name)
    return result


@dataclass
class Configuration:
    path: Path
    settings: dict
    recipes: dict[str, dict]

    def select(self, requested: list[str] | None = None) -> list[str]:
        return resolve(self.recipes, requested or list(self.recipes))

    def location(self, field: str, override: str | None, default: str) -> Path:
        path = Path(override or self.settings.get(field, default)).expanduser()
        return (path if path.is_absolute() else self.path.parent / path).resolve()


def load_config(path: Path) -> Configuration:
    path = path.expanduser().resolve()
    data = read_yaml(path)
    unknown = set(data) - {"schema_version", "toolchain", "libraries", "recipe_files"}
    if unknown:
        raise ToolchainError(f"Unknown configuration fields: {', '.join(sorted(unknown))}")
    if data.get("schema_version") != 1:
        raise ToolchainError("Expected schema_version: 1; use 'toolchain init --preset poc' for the standalone format")
    settings = data.get("toolchain", {})
    if not isinstance(settings, dict):
        raise ToolchainError("toolchain must be a mapping")
    if settings.get("stdlib", "libstdc++") not in {"libstdc++", "libc++"}:
        raise ToolchainError("toolchain.stdlib must be libstdc++ or libc++")
    if "jobs" in settings and (type(settings["jobs"]) is not int or settings["jobs"] < 1):
        raise ToolchainError("toolchain.jobs must be a positive integer")
    if not NAME.fullmatch(str(settings.get("name", "toolchain"))):
        raise ToolchainError("Invalid toolchain.name")
    for field in ("prefix", "work", "cache", "output", "bootstrap_prefix", "cc", "cxx"):
        if field in settings and not isinstance(settings[field], str):
            raise ToolchainError(f"toolchain.{field} must be a string")
    recipes: dict[str, dict] = {}
    includes = data.get("recipe_files", [])
    strings(includes, "recipe_files")
    documents = []
    for reference in includes:
        if reference == "builtin:poc":
            location = Path(str(files("cpp_toolchain_builder").joinpath("recipes/poc.yaml")))
        elif reference.startswith("builtin:"):
            raise ToolchainError(f"Unknown built-in recipe collection: {reference}")
        else:
            location = path.parent / reference
        documents.append((read_yaml(location), location.parent))
    documents.append((data, path.parent))
    for document, base in documents:
        entries = document.get("libraries", [])
        if not isinstance(entries, list):
            raise ToolchainError("libraries must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise ToolchainError("Each library must be a mapping")
            recipe = copy.deepcopy(entry)
            validate_recipe(recipe)
            name = recipe["name"]
            if name in recipes:
                raise ToolchainError(f"Duplicate recipe: {name}")
            if "path" in recipe["source"]:
                recipe["source"]["path"] = str((base / Path(recipe["source"]["path"]).expanduser()).resolve())
            recipes[name] = recipe
    resolve(recipes, list(recipes))
    return Configuration(path, settings, recipes)
