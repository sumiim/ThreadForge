"""Fail CI when shipped Worker code changes without a version bump."""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path


WORKER_PATHS = (
    "local-worker/pyproject.toml",
    "local-worker/src/",
    "pico-legacy-runtime/pico/",
    "agent-orchestrator/src/",
    "scripts/build-worker-installer.ps1",
    "scripts/worker-installer.nsi",
)


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True, encoding="utf-8").strip()


def package_version(raw: bytes) -> str:
    return str(tomllib.loads(raw.decode("utf-8"))["project"]["version"])


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", value)
    if match is None:
        raise ValueError(f"Worker version is not semantic: {value}")
    return tuple(int(item) for item in match.groups())


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else ""
    if not base:
        github_base = os.environ.get("GITHUB_BASE_REF", "").strip()
        base = f"origin/{github_base}" if github_base else "HEAD^"

    changed = git("diff", "--name-only", f"{base}...HEAD").splitlines()
    shipped_changes = [
        path
        for path in changed
        if any(path == prefix or path.startswith(prefix) for prefix in WORKER_PATHS)
    ]
    if not shipped_changes:
        print("No shipped Worker code changed.")
        return 0

    current = package_version(Path("local-worker/pyproject.toml").read_bytes())
    previous = package_version(
        subprocess.check_output(["git", "show", f"{base}:local-worker/pyproject.toml"])
    )
    source = Path("local-worker/src/threadforge_worker/__init__.py").read_text(encoding="utf-8")
    source_match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', source, re.MULTILINE)
    source_version = source_match.group(1) if source_match else ""
    if source_version != current:
        print(
            f"Worker package version {current} does not match runtime version {source_version or 'missing'}.",
            file=sys.stderr,
        )
        return 1
    if version_tuple(current) <= version_tuple(previous):
        print("Shipped Worker code changed without a higher version:", file=sys.stderr)
        for path in shipped_changes:
            print(f"- {path}", file=sys.stderr)
        print(f"Base version is {previous}; current version is {current}.", file=sys.stderr)
        return 1
    print(f"Worker version changed from {previous} to {current}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
