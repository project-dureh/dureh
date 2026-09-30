#!/usr/bin/env python3
"""Small Phase 1 DUREH CLI. Uses only the Python standard library."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class DurehError(Exception):
    pass


def fail(message: str) -> None:
    raise DurehError(message)


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        fail(f"required file is missing: {path}")
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid JSON from {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"expected a JSON object in {path}")
    return value


def safe_relative(value: str) -> PurePosixPath:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or any(part in ("", ".", "..") for part in p.parts):
        fail(f"path must be a nonempty, normalized relative path: {value!r}")
    if "\\" in value or "\x00" in value:
        fail("path contains a forbidden character")
    return p


def reject_symlinks(root: Path, rel: PurePosixPath, *, include_leaf: bool = True) -> Path:
    current = root
    parts = rel.parts if include_leaf else rel.parts[:-1]
    for part in parts:
        current = current / part
        if current.is_symlink():
            fail(f"symlink path component is not allowed: {current}")
    return root.joinpath(*rel.parts)


def package_manifest(startup: Path) -> dict:
    if startup.is_symlink() or not startup.is_dir():
        fail(f"startup package must be a directory: {startup}")
    manifest_path = startup / "startup.yaml"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        fail(f"required startup manifest is missing: {manifest_path}")
    # Package validation deliberately checks identity without pretending to parse all YAML.
    raw = manifest_path.read_text(encoding="utf-8")
    package_id = None
    name = None
    in_package = False
    for line in raw.splitlines():
        if line.strip() == "package:":
            in_package = True
            continue
        if line and not line[0].isspace():
            in_package = False
        if in_package:
            key, sep, value = line.strip().partition(":")
            if sep and key in {"id", "name", "version"}:
                value = value.strip().strip("\"'")
                if key == "id":
                    package_id = value
                elif key == "name":
                    name = value
    if not package_id or not name:
        fail(f"startup.yaml must declare package.id and package.name: {manifest_path}")
    return {"id": package_id, "name": name}


def copy_package(src: Path, dst: Path) -> None:
    for base, dirs, files in os.walk(src, followlinks=False):
        base_path = Path(base)
        rel = base_path.relative_to(src)
        out = dst / rel
        out.mkdir(parents=True, exist_ok=True)
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for directory in dirs:
            source_dir = base_path / directory
            if source_dir.is_symlink():
                fail(f"startup package may not contain symlinks: {source_dir}")
        for filename in sorted(files):
            source = base_path / filename
            if source.is_symlink():
                fail(f"startup package may not contain symlinks: {source}")
            if filename == ".DS_Store":
                continue
            target = out / filename
            shutil.copy2(source, target)


def install(args: argparse.Namespace) -> None:
    startup_arg = Path(args.startup).expanduser()
    if startup_arg.is_symlink():
        fail(f"startup package path may not be a symlink: {startup_arg}")
    startup = startup_arg.resolve()
    site_arg = Path(args.site).expanduser()
    if site_arg.is_symlink():
        fail(f"site path may not be a symlink: {site_arg}")
    site = site_arg.resolve()
    manifest = package_manifest(startup)
    if site.exists() and (not site.is_dir() or any(site.iterdir())):
        fail(f"site destination must be absent or empty: {site}")
    site.parent.mkdir(parents=True, exist_ok=True)
    site.mkdir(parents=True, exist_ok=True)
    package_target = site / "startup"
    try:
        package_target.mkdir()
        copy_package(startup, package_target)
        (site / "state" / "records" / "events").mkdir(parents=True)
        (site / "state" / "persona").mkdir(parents=True)
        (site / "state" / "memory").mkdir(parents=True)
        (site / "projects").mkdir()
        config = {"schema_version": 1, "site_root": str(site), "startup": {"id": manifest["id"], "revision": "local-unpinned"}}
        (site / "config.json").write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        private = {"schema_version": 1, "harness": {"revision": "working-tree"}, "startup": {"id": manifest["id"], "name": manifest["name"], "revision": "local-unpinned"}, "state": {"revision": "new-empty-state"}, "recorded_at": datetime.now(timezone.utc).isoformat()}
        deployment = site / "deployment.json"
        deployment.write_text(json.dumps(private, indent=2) + "\n", encoding="utf-8")
        deployment.chmod(0o600)
    except Exception:
        shutil.rmtree(site, ignore_errors=True)
        raise
    print(f"Installed startup package {manifest['id']} into {site}")
    print("Created fresh site state; no package state or recovery pointer was imported.")


def site_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir():
        fail(f"site directory does not exist: {root}")
    return root


def event_add(args: argparse.Namespace) -> None:
    root = site_root(args.site)
    if not args.kind.strip() or not args.actor.strip() or not args.wi.strip():
        fail("--kind, --wi, and --actor must be nonempty")
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        fail(f"--payload must be valid JSON: {exc}")
    if not isinstance(payload, dict):
        fail("--payload must be a JSON object")
    known_roles = set(DEFAULT_SCOPES)
    if args.actor not in known_roles:
        fail(f"unknown role: {args.actor!r}")
    if args.actor != "desk" and args.kind not in {"finding.created", "run.completed", "verification.recorded"}:
        fail(f"role {args.actor!r} is not authorized to append event kind {args.kind!r}")
    event_id = str(uuid.uuid4())
    event = {"schema_version": 1, "id": event_id, "kind": args.kind, "ts": datetime.now(timezone.utc).isoformat(), "actor": args.actor, "wi": args.wi, "payload": payload}
    directory = root / "state" / "records" / "events"
    if not directory.is_dir():
        fail(f"event store is missing; initialize the site first: {directory}")
    path = directory / f"{event_id}.json"
    encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        fail(f"could not append event without overwriting: {exc}")
    print(encoded, end="")


def event_query(args: argparse.Namespace) -> None:
    root = site_root(args.site)
    directory = root / "state" / "records" / "events"
    if not directory.is_dir():
        fail(f"event store is missing: {directory}")
    rows = []
    for path in sorted(directory.glob("*.json")):
        event = read_json(path)
        required = {"schema_version", "id", "kind", "ts", "actor", "wi", "payload"}
        if not required.issubset(event):
            fail(f"event is missing required fields: {path}")
        if args.wi is None or event["wi"] == args.wi:
            rows.append(event)
    print(json.dumps(rows, ensure_ascii=False, indent=2))


DEFAULT_SCOPES = {
    "desk": ["startup/**", "projects/**", "state/persona/**", "state/memory/**"],
    "py-writer": ["projects/*/modules/*/src/**", "projects/*/modules/*/tests/**"],
    "doc-writer": ["projects/*/docs/**", "projects/*/README.md"],
    "interpreter": ["projects/*/reports/interpretation/**"],
    "pipeline-runner": ["projects/*/derivatives/**", "projects/*/checkpoints/**"],
    "researcher": [],
    "checker": [],
}


def authorize_write(args: argparse.Namespace) -> None:
    root = site_root(args.site)
    rel = safe_relative(args.path)
    target = reject_symlinks(root, rel)
    scopes = DEFAULT_SCOPES.get(args.role)
    if scopes is None:
        fail(f"unknown role: {args.role}")
    allowed = any(fnmatch.fnmatchcase(rel.as_posix(), scope) for scope in scopes)
    if not allowed:
        fail(f"unauthorized write denied: role {args.role!r} cannot write {rel.as_posix()!r}")
    print(f"authorized: {args.role} -> {rel.as_posix()}")


def artifact_write(args: argparse.Namespace) -> None:
    root = site_root(args.site)
    rel = safe_relative(args.path)
    scopes = DEFAULT_SCOPES.get(args.role)
    if scopes is None:
        fail(f"unknown role: {args.role}")
    if not any(fnmatch.fnmatchcase(rel.as_posix(), scope) for scope in scopes):
        fail(f"unauthorized write denied: role {args.role!r} cannot write {rel.as_posix()!r}")
    target = reject_symlinks(root, rel, include_leaf=False)
    source = Path(args.input).expanduser()
    if source.is_symlink() or not source.is_file():
        fail(f"required input file is missing or is a symlink: {source}")
    try:
        contents = source.read_bytes()
    except OSError as exc:
        fail(f"cannot read required input file {source}: {exc}")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Recheck after creating parents; never follow a link or overwrite an artifact.
    reject_symlinks(root, rel, include_leaf=False)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        fail(f"refusing to overwrite existing artifact: {target}")
    except OSError as exc:
        fail(f"could not write artifact {target}: {exc}")
    print(f"wrote {len(contents)} bytes: {rel.as_posix()}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dureh", description="Phase 1 DUREH site and event tools")
    p.add_argument("--site", help="installed site directory")
    sub = p.add_subparsers(dest="command", required=True)
    install_p = sub.add_parser("install", help="install a startup package into a new site")
    install_p.add_argument("--startup", required=True)
    install_p.add_argument("--site", required=True)
    install_p.set_defaults(func=install)
    event = sub.add_parser("event", help="append or query site events")
    event_sub = event.add_subparsers(dest="event_command", required=True)
    add = event_sub.add_parser("add", help="append one immutable event")
    add.add_argument("--kind", required=True)
    add.add_argument("--wi", required=True)
    add.add_argument("--actor", required=True)
    add.add_argument("--payload", required=True)
    add.set_defaults(func=event_add)
    query = event_sub.add_parser("query", help="list events, optionally for one WI")
    query.add_argument("--wi")
    query.set_defaults(func=event_query)
    auth = sub.add_parser("authorize-write", help="check a role-scoped relative artifact path")
    auth.add_argument("--role", required=True)
    auth.add_argument("--path", required=True)
    auth.set_defaults(func=authorize_write)
    artifact = sub.add_parser("artifact", help="write a role-scoped artifact through the harness")
    artifact_sub = artifact.add_subparsers(dest="artifact_command", required=True)
    write = artifact_sub.add_parser("write", help="create a new, authorized artifact")
    write.add_argument("--role", required=True)
    write.add_argument("--path", required=True)
    write.add_argument("--input", required=True)
    write.set_defaults(func=artifact_write)
    return p


def main() -> int:
    try:
        args = parser().parse_args()
        if args.command != "install":
            if not args.site:
                fail("--site is required for this command")
            args.func(args)
        else:
            args.func(args)
        return 0
    except DurehError as exc:
        print(f"dureh: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"dureh: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
