"""One configuration and source lock, with isolated SDK variants."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import Configuration
from .engine import BUILD_ENVIRONMENT_KEYS, Builder
from .util import ToolchainError, atomic_write, digest, exclusive_lock, read_json, write_json

PROFILES = {'standard': None, 'asan': 'asan-ubsan', 'tsan': 'tsan'}
BUNDLE_MANIFEST = 'share/toolchain/bundle.json'


def read_bundle(prefix: Path) -> dict | None:
    record = read_json(prefix / BUNDLE_MANIFEST)
    if record is None:
        return None
    if not isinstance(record, dict) or record.get('schema_version') != 1:
        raise ToolchainError(f'Invalid bundle manifest in {prefix}')
    variants = record.get('variants')
    if (not isinstance(variants, list) or not variants or variants[0] != 'standard'
            or any(not isinstance(name, str) or name not in PROFILES for name in variants)
            or len(set(variants)) != len(variants)):
        raise ToolchainError(f'Invalid bundle variants in {prefix}')
    return record


class Bundle:
    def __init__(self, config: Configuration, *, prefix=None, work=None, cache=None,
                 jobs=None, stdlib=None, compiler_prefix=None, offline=False,
                 quiet=False, locked=False, lockfile=None, resume=False, sanitizer=None):
        if compiler_prefix or sanitizer:
            raise ToolchainError('Bundle configurations select their own compiler and sanitizer; omit --compiler-prefix and --sanitizer')
        self.config = config
        self.prefix = config.location('prefix', prefix, './install')
        self.work = config.location('work', work, '.toolchain-work')
        self.cache_path = config.location('cache', cache, '.toolchain-cache')
        self.lockfile = (Path(lockfile).expanduser().resolve() if lockfile else
                         config.location('lockfile', None, str(config.path.with_suffix('.lock.json'))))
        self.variants = config.settings['variants']
        self.locked = locked
        self.resume = resume
        self.builders: dict[str, Builder] = {}
        for root in (self.prefix, self.work, self.cache_path):
            if root in {Path('/'), Path.home(), config.path.parent}:
                raise ToolchainError(f'Choose a dedicated bundle prefix/work/cache directory, not {root}')
        roots = (self.prefix, self.work, self.cache_path)
        for i, first in enumerate(roots):
            for second in roots[i + 1:]:
                if first.is_relative_to(second) or second.is_relative_to(first):
                    raise ToolchainError('Bundle prefix/work/cache directories must be separate and non-nested')
        if (self.prefix / 'share/toolchain/build-state.json').exists():
            raise ToolchainError('Bundle destination is an existing single SDK; choose a new prefix')
        record = read_bundle(self.prefix)
        if record and record['variants'] != self.variants:
            raise ToolchainError('Installed bundle variants differ; use a new bundle prefix')
        if resume and record is None:
            raise ToolchainError('No previous bundle build to resume; use toolchain build first')
        environment = os.environ.copy()
        if resume:
            saved = record.get('build_environment')
            if (not isinstance(saved, dict) or set(saved) != set(BUILD_ENVIRONMENT_KEYS)
                    or any(value is not None and not isinstance(value, str) for value in saved.values())):
                raise ToolchainError('Invalid saved build environment in bundle.json')
            for key, value in saved.items():
                if value is None:
                    environment.pop(key, None)
                else:
                    environment[key] = value
        self.environment = environment
        for name in self.variants:
            settings = dict(config.settings)
            settings.pop('variants')
            settings.update(name=f"{settings.get('name', 'toolchain')}-{name}",
                            prefix=str(self.prefix / name), work=str(self.work / name),
                            cache=str(self.cache_path), lockfile=str(self.lockfile))
            if name != 'standard':
                settings.update(compiler_prefix=str(self.prefix / 'standard'), sanitizer=PROFILES[name])
            child = Configuration(config.path, settings, config.recipes)
            builder = Builder(child, jobs=jobs, stdlib=stdlib, offline=offline,
                              quiet=quiet, locked=locked)
            if builder.prefix.parent != self.prefix or builder.work.parent != self.work:
                raise ToolchainError('Variant directories must stay inside the bundle prefix and work directory')
            builder.build_environment = environment.copy()
            # An interrupted bundle can contain variants that never started.
            builder.resume = resume and bool(builder.state.get('recipes'))
            if name != 'standard':
                builder.compiler_relative_prefix = '../standard'
            self.builders[name] = builder

    def plan(self, requested=None) -> dict[str, list[dict]]:
        if requested:
            raise ToolchainError('Bundle builds select complete variants; omit --library')
        plans = {}
        for name, builder in self.builders.items():
            if name != 'standard':
                builder.compiler_identity = digest({p['name']: p['fingerprint']
                    for p in plans['standard'] if self.config.recipes[p['name']].get('stage') == 'core'})
            plans[name] = builder.plan()
        return plans

    def fetch_all(self, requested=None) -> None:
        if requested:
            raise ToolchainError('Bundle fetch selects all shared sources; omit --library')
        # Every variant has identical source identities. Fetch just once.
        self.builders['standard'].fetch_all()

    def write_metadata(self) -> None:
        recipes = {name: {component: {'artifacts': self.config.recipes[component].get('artifacts', [])}
                         for component in builder.selected(None)} for name, builder in self.builders.items()}
        write_json(self.prefix / BUNDLE_MANIFEST, {
            'schema_version': 1, 'name': self.config.settings.get('name', self.prefix.name),
            'variants': self.variants, 'recipes': recipes,
            'stdlib': self.builders['standard'].stdlib,
            'build_environment': {key: self.environment.get(key) for key in BUILD_ENVIRONMENT_KEYS},
        }, mode=0o644)
        atomic_write(self.prefix / 'README.md',
            '# ' + self.config.settings.get('name', self.prefix.name) + '\n\n'
            'One compiler in `standard/`, with separate library variants: ' + ', '.join(self.variants) + '.\n\n'
            'Source `<bundle>/<variant>/activate`, or pass that variant\'s '
            '`share/toolchain/toolchain.cmake` to CMake. Standard supports Debug and Release consumers.\n\n'
            'Copy or archive this entire directory together. The sanitizer variants require the sibling '
            '`standard` directory. Verify after relocation with '
            '`toolchain inspect verify --prefix <bundle> --smoke`.\n', mode=0o644)

    def build(self, *, force=False, preflight=None) -> dict:
        if self.resume and force:
            raise ToolchainError('--force cannot be combined with resume; use toolchain build --force')
        if not self.locked:
            raise ToolchainError('Bundle builds require --locked so all variants use identical sources; run toolchain fetch first')
        # Validate all completed fingerprints before mutating any variant.
        self.plan()
        results = {}
        with exclusive_lock(self.prefix / 'share/toolchain/.build.lock'), exclusive_lock(self.work / '.build.lock'):
            self.write_metadata()
            for name, builder in self.builders.items():
                print(f'Building bundle variant: {name}', file=sys.stderr, flush=True)
                if preflight:
                    preflight(builder)
                results[name] = builder.build(force=force)
        return {'prefix': str(self.prefix), 'variants': results}
