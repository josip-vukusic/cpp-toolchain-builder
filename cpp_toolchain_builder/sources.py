from __future__ import annotations

import os
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

from .util import ToolchainError, digest, exclusive_lock, inside, read_json, sha256, write_json


def local_signature(path: Path) -> str:
    if not path.exists():
        raise ToolchainError(f"Local source does not exist: {path}")
    if path.is_file():
        return sha256(path)
    return digest([(str(p.relative_to(path)), os.readlink(p) if p.is_symlink() else sha256(p))
                   for p in sorted(path.rglob("*"))
                   if ".git" not in p.relative_to(path).parts and (p.is_file() or p.is_symlink())])


def source_identity(recipe: dict) -> dict:
    result = {"name": recipe["name"], "version": recipe["version"], "source": recipe["source"]}
    if "path" in recipe["source"]:
        result["local_digest"] = local_signature(Path(recipe["source"]["path"]))
    return result


def extract(archive: Path, destination: Path, source: dict) -> None:
    """Extract into an isolated directory, rejecting traversal and special files."""
    destination.mkdir(parents=True, exist_ok=True)
    if not source.get("unpack", True):
        filename = source.get("filename") or Path(urllib.parse.urlparse(source.get("url", "")).path).name or archive.name
        shutil.copy2(archive, destination / filename)
        return
    with tempfile.TemporaryDirectory(prefix="extract-", dir=destination.parent) as temporary:
        root = Path(temporary)
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as stream:
                for member in stream.infolist():
                    inside(root, member.filename)
                    mode = member.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise ToolchainError(f"Archive contains a zip symlink: {member.filename}")
                stream.extractall(root)
                for member in stream.infolist():
                    target = inside(root, member.filename)
                    if target.is_file():
                        target.chmod(0o755 if (member.external_attr >> 16) & 0o111 else 0o644)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as stream:
                # Python 3.12's data filter checks links, paths and file types.
                stream.extractall(root, filter="data")
        else:
            filename = source.get("filename") or Path(urllib.parse.urlparse(source.get("url", "")).path).name or archive.name
            shutil.copy2(archive, destination / filename)
            return
        selected = root
        if source.get("directory"):
            selected = inside(root, source["directory"])
            if not selected.is_dir():
                raise ToolchainError(f"Archive has no directory {source['directory']}")
        elif source.get("strip_root", True):
            entries = list(root.iterdir())
            if len(entries) == 1 and entries[0].is_dir() and not entries[0].is_symlink():
                selected = entries[0]
        for child in selected.iterdir():
            shutil.move(str(child), destination / child.name)


def download(url: str, destination: Path) -> None:
    if urllib.parse.urlparse(url).scheme not in {"https", "http", "file"}:
        raise ToolchainError(f"Unsupported download URL: {url}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(3):
        temporary = destination.with_name(destination.name + ".part")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "cpp-toolchain-builder/1.0"})
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
                expected = response.headers.get("Content-Length")
            if not temporary.stat().st_size:
                raise ToolchainError(f"Empty download: {url}")
            if expected and temporary.stat().st_size != int(expected):
                raise ToolchainError(f"Incomplete download: {url}")
            temporary.replace(destination)
            return
        except (OSError, ValueError, ToolchainError) as exc:
            last_error = exc
            temporary.unlink(missing_ok=True)
            if attempt < 2:
                time.sleep(attempt + 1)
    raise ToolchainError(f"Download failed for {url}: {last_error}")


class SourceCache:
    def __init__(self, root: Path, run: Callable, offline: bool = False):
        self.root, self.run, self.offline = root, run, offline

    def fetch(self, recipe: dict, locked: dict | None = None) -> tuple[Path, dict]:
        identity = source_identity(recipe)
        if locked is not None and locked.get("identity") != digest(identity):
            raise ToolchainError(f"{recipe['name']}: source differs from lockfile; run fetch to update it")
        key = f"{recipe['name']}-{digest(identity)[:20]}"
        target = self.root / "sources" / key
        metadata = self.root / "records" / f"{key}.json"
        with exclusive_lock(self.root / "locks" / f"{key}.lock"):
            record = read_json(metadata)
            compatible = not locked or (record and all(record.get(k) == locked.get(k) for k in ("sha256", "git_commit")))
            if record and target.is_dir() and compatible:
                return target, record
            source = recipe["source"]
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="fetch-", dir=self.root) as temporary:
                staging = Path(temporary) / "source"
                record = {"identity": digest(identity), "source": source}
                if "path" in source:
                    local = Path(source["path"])
                    if local.is_dir():
                        shutil.copytree(local, staging, symlinks=True, ignore=shutil.ignore_patterns(".git"))
                    else:
                        staging.mkdir()
                        extract(local, staging, source)
                    record["sha256"] = identity["local_digest"]
                elif "git" in source:
                    if self.offline:
                        raise ToolchainError(f"{recipe['name']}: git source is not cached; run fetch without --offline")
                    ref = locked.get("git_commit") if locked else source["ref"]
                    self.run(["git", "init", str(staging)], cwd=self.root)
                    self.run(["git", "-C", str(staging), "remote", "add", "origin", source["git"]], cwd=self.root)
                    self.run(["git", "-C", str(staging), "fetch", "--depth", "1", "origin", ref], cwd=self.root)
                    self.run(["git", "-C", str(staging), "checkout", "--detach", "FETCH_HEAD"], cwd=self.root)
                    if source.get("submodules"):
                        self.run(["git", "-C", str(staging), "submodule", "update", "--init", "--recursive", "--depth", "1"], cwd=self.root)
                    record["git_commit"] = self.run(["git", "-C", str(staging), "rev-parse", "HEAD"], cwd=self.root, capture=True).strip()
                else:
                    filename = source.get("filename") or Path(urllib.parse.urlparse(source["url"]).path).name
                    filename = filename or recipe["name"]
                    archive = self.root / "downloads" / f"{digest(source)[:20]}-{filename}"
                    expected = (locked or {}).get("sha256") or source.get("sha256")
                    imported = self.root / "imports" / filename
                    if not archive.exists() and imported.is_file() and imported.stat().st_size:
                        if expected and sha256(imported).lower() != expected.lower():
                            raise ToolchainError(f"{recipe['name']}: imported archive checksum mismatch: {imported}")
                        archive.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(imported, archive)
                    if not archive.exists():
                        if self.offline:
                            raise ToolchainError(f"{recipe['name']}: archive is not cached: {filename}; run fetch first")
                        download(source["url"], archive)
                    if not archive.stat().st_size:
                        raise ToolchainError(f"Empty cached archive; remove it and retry: {archive}")
                    record["sha256"] = sha256(archive)
                    if expected and record["sha256"].lower() != expected.lower():
                        raise ToolchainError(f"{recipe['name']}: SHA-256 mismatch for {archive}; expected {expected}, got {record['sha256']}")
                    extract(archive, staging, source)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.rmtree(target)
                staging.replace(target)
                write_json(metadata, record)
            return target, record
