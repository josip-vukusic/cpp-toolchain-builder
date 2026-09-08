from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator


class ToolchainError(Exception):
    """An actionable error to report without a Python traceback."""


def digest(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def atomic_write(path: Path, text: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError) as exc:
        raise ToolchainError(f"Cannot read {path}: {exc}") from exc


def write_json(path: Path, data: Any, *, mode: int | None = None) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=mode)


def inside(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()):
        raise ToolchainError(f"Path must stay within {root}: {relative}")
    return path


def expand(value: str, variables: dict[str, str]) -> str:
    """Expand recipe ${variables}; leave ordinary shell/CMake dollars alone."""
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in variables:
            raise ToolchainError(f"Unknown recipe variable ${{{key}}}")
        return variables[key]
    return re.sub(r"\$\{([a-z][a-z0-9_]*)\}", replace, str(value))


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ToolchainError(f"Another operation is using {path.parent}; try again when it finishes") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
