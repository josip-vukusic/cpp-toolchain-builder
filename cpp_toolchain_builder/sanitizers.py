"""Sanitizer profiles shared by library builds and SDK consumers."""

from .util import ToolchainError

SANITIZERS = {"asan-ubsan": "address,undefined", "tsan": "thread"}


def sanitizer_flags(profile: str | None) -> list[str]:
    if not profile:
        return []
    if profile not in SANITIZERS:
        raise ToolchainError(f"Unknown sanitizer profile: {profile}")
    return [f"-fsanitize={SANITIZERS[profile]}", "-fno-omit-frame-pointer", "-g"]
