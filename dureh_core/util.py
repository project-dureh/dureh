"""Shared helpers: errors, safe paths, create-only writes, JSON IO."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class DurehError(Exception):
    """A user-facing failure. The CLI prints it and exits nonzero."""


def fail(message: str) -> "None":
    raise DurehError(message)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"required file is missing: {path}")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        fail(f"cannot read valid JSON from {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"expected a JSON object in {path}")
    return value


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def write_create_only(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write a new file; never overwrite. fsync before returning."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        fail(f"refusing to overwrite existing file: {path}")
    except OSError as exc:
        fail(f"could not create {path}: {exc}")
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def write_replace(path: Path, text: str, mode: int = 0o644) -> None:
    """Atomically replace a harness-owned generated file."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with open(tmp, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def safe_relative(value: str) -> PurePosixPath:
    if not value or "\\" in value or "\x00" in value:
        fail(f"path must be a nonempty relative POSIX path: {value!r}")
    p = PurePosixPath(value)
    if p.is_absolute() or any(part in ("", ".", "..") for part in value.split("/")):
        fail(f"path must be a nonempty, normalized relative path: {value!r}")
    return p


def reject_symlinks(root: Path, rel: PurePosixPath, *, include_leaf: bool = True) -> Path:
    current = Path(root)
    parts = rel.parts if include_leaf else rel.parts[:-1]
    for part in parts:
        current = current / part
        if current.is_symlink():
            fail(f"symlink path component is not allowed: {current}")
    return Path(root).joinpath(*rel.parts)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git(args: list[str], cwd: Path, *, check: bool = True, env: dict | None = None) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        fail(f"git {' '.join(args)} failed to run: {exc}")
    if check and proc.returncode != 0:
        fail(f"git {' '.join(args)} failed in {cwd}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def git_revision(path: Path) -> str:
    """Return HEAD sha plus '+dirty' when the tree has uncommitted changes, or 'not-a-git-repo'."""
    path = Path(path)
    if not (path / ".git").exists():
        return "not-a-git-repo"
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True)
    if proc.returncode != 0:
        return "no-commit"
    sha = proc.stdout.strip()
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=path, capture_output=True, text=True).stdout.strip()
    return sha + ("+dirty" if dirty else "")


def is_within(path: Path, root: Path) -> bool:
    try:
        Path(path).relative_to(root)
        return True
    except ValueError:
        return False
