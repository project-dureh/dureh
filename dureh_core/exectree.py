"""Pinned module execution trees for scheduler jobs (UR-20261004-04).

`dureh slurm submit --module <m>` runs a job against a frozen copy of `modules/<m>` instead of the live
checkout, so a promotion during the job cannot change the code under it (measured: job 3748401 started on
camri 1eeab74 while modules/camri moved to 8333b22 and 69f83b3).

- Revision: `--rev`, else the WI target project's manifest pin (`provenance.tool_versions.<m>` of
  `camri-manifest.yaml`, for example "2.0.0a1+2396359"); resolved to the full sha in modules/<m>.
- Tree: `git -C modules/<m> archive <rev> | tar -x` into `<site>/scratch/exec/<WI>-<m>-<rev7>-<ts>/`, then
  made read-only. No `git worktree`: nothing is written into the module's .git.
- Environment: `shared-venv` (the archived uv.lock is byte-identical to modules/<m>/uv.lock and
  modules/<m>/.venv exists: activate it and put the tree's package dir first on PYTHONPATH) or `rebuild`
  (the module's env_rebuild in the tree, into the writable sibling `<tree>.venv`, before the payload).
- The job wrapper exports the module's `root_env` variable (and `rev_env`, when config names one), or, only when
  no root_env is configured, the fallbacks DUREH_MODULE_ROOT and DUREH_MODULE_REV (Director 2026-10-04: variables
  a company's job scripts read carry the company's names, not DUREH's); it runs the
  original job script as a child, and removes the tree and `<tree>.venv` on exit.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .site import Site
from .util import fail, git

HEX = re.compile(r"^[0-9a-f]{7,40}$")
MANIFEST = "camri-manifest.yaml"


def exec_root(site: Site) -> Path:
    return site.root / "scratch" / "exec"


def module_entry(site: Site, module: str) -> dict:
    for e in site.config.get("modules") or []:
        if e.get("id") == module or e.get("path") == f"modules/{module}":
            return e
    return {}


def manifest_pin(site: Site, target: str | None, module: str) -> tuple[str | None, str | None]:
    """(rev, manifest path) from the WI target project's manifest: `provenance.tool_versions.<module>` as
    "<version>+<sha>", "<version>+g<sha>" or a bare sha. (None, path-or-None) when there is no usable pin."""
    from . import yamlsub
    from .paths import target_root

    if not target or not re.match(r"^projects/[^/]+$", target):
        return None, None
    root = target_root(site, target)
    if root is None or not (root / MANIFEST).is_file():
        return None, None
    rel = f"{target}/{MANIFEST}"
    try:
        data = yamlsub.load_file(root / MANIFEST) or {}
    except Exception as exc:  # noqa: BLE001 - an unreadable manifest is reported, not a crash
        fail(f"cannot read {rel}: {exc}")
    tv = ((data.get("provenance") or {}).get("tool_versions") or {}) if isinstance(data, dict) else {}
    value = tv.get(module) if isinstance(tv, dict) else None
    if isinstance(value, dict):
        value = value.get("rev")
    if not isinstance(value, str) or not value.strip():
        return None, rel
    token = value.strip().rsplit("+", 1)[-1]
    if token.startswith("g") and HEX.match(token[1:]):
        token = token[1:]
    return (token if HEX.match(token) else None), rel


def package_dir(tree: Path) -> Path:
    """The directory to put on PYTHONPATH: src/ for a src layout, else the parent of the first `packages`
    entry in pyproject.toml, else the tree root."""
    if (tree / "src").is_dir():
        return tree / "src"
    py = tree / "pyproject.toml"
    if py.is_file():
        m = re.search(r'^\s*packages\s*=\s*\[\s*["\']([^"\']+)["\']', py.read_text(errors="replace"), re.M)
        if m and "/" in m.group(1):
            return tree / m.group(1).rsplit("/", 1)[0]
    return tree


def env_mode(site: Site, mod: Path, tree: Path) -> str:
    from .workflow import stale_venv

    lock, live = tree / "uv.lock", mod / "uv.lock"
    if (lock.is_file() and live.is_file() and lock.read_bytes() == live.read_bytes()
            and (mod / ".venv").is_dir() and not stale_venv(mod)):
        return "shared-venv"
    return "rebuild"


def overriding_assignments(site: Site, script: Path, module: str, root_env: str | None) -> list[str]:
    """Job-script lines that set the module root themselves (they override the wrapper's export)."""
    pat = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    site_refs = ("$DUREH_SITE", "${DUREH_SITE}", str(site.root), *(str(r) for r in site.previous_roots()))
    out = []
    for n, line in enumerate(script.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        m = pat.match(line)
        if not m:
            continue
        name, value = m.group(1), m.group(2)
        live = f"modules/{module}" in value and any(s in value for s in site_refs)
        if live or (root_env and name == root_env):
            out.append(f"line {n}: {line.strip()}")
    return out


def _rmtree(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    subprocess.run(["chmod", "-R", "u+w", str(path)], check=False)
    shutil.rmtree(path, ignore_errors=True)


def prepare(site: Site, wi: str, target: str | None, module: str, rev: str | None, script: Path,
            dry_run: bool = False, items: list[str] | None = None, items_file: str | None = None,
            item_env: dict | None = None) -> dict:
    """Resolve the revision, extract the read-only tree and write the job wrapper. Returns the record's
    `module` block plus the wrapper path and warnings."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", module):
        fail(f"invalid module id {module!r}")
    mod = site.root / "modules" / module
    if not mod.is_dir() or not (mod / ".git").exists():
        fail(f"module is missing or not a git repository: modules/{module}")
    pin, manifest = manifest_pin(site, target, module)
    if rev is None and pin is None:
        where = f"{manifest} has no usable provenance.tool_versions.{module}" if manifest else \
            f"{wi} targets {target}, which has no {MANIFEST}"
        fail(f"no revision for modules/{module}: {where}; pass --rev <rev>")

    def resolve(r: str, what: str) -> str:
        full = git(["rev-parse", "--verify", "--quiet", f"{r}^{{commit}}"], mod, check=False)
        if not re.match(r"^[0-9a-f]{40}$", full or ""):
            fail(f"{what} {r!r} does not resolve to a commit in modules/{module}")
        return full

    pin_full = resolve(pin, f"manifest pin ({manifest})") if pin else None
    full = resolve(rev, "--rev") if rev is not None else pin_full
    entry = module_entry(site, module)
    root_env = entry.get("root_env")
    if root_env is not None and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(root_env)):
        fail(f"config modules[{module}].root_env is not a variable name: {root_env!r}")
    rev_env = entry.get("rev_env")
    if rev_env is not None and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", str(rev_env)):
        fail(f"config modules[{module}].rev_env is not a variable name: {rev_env!r}")
    if rev_env and not root_env:
        fail(f"config modules[{module}].rev_env needs root_env (without it the fallbacks DUREH_MODULE_ROOT and "
             "DUREH_MODULE_REV are exported)")
    env = {"root": root_env, "rev": rev_env} if root_env else {"root": "DUREH_MODULE_ROOT", "rev": "DUREH_MODULE_REV"}
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    tree = exec_root(site) / f"{wi}-{module}-{full[:7]}-{ts}"
    venv = Path(f"{tree}.venv")
    wrapper = Path(f"{tree}.sbatch")
    warnings = []
    for hit in overriding_assignments(site, script, module, root_env):
        warnings.append(f"{script.name} {hit} sets the module root itself and overrides the pinned tree; "
                        f"use ${{{env['root']}}} instead")
    if not root_env:
        warnings.append(f"config modules[{module}] has no root_env; only DUREH_MODULE_ROOT points at the pinned "
                        f"tree (the site may set root_env, e.g. \"CAMRI_PROJECT\" for camri)")
    block = {"name": module, "rev": full, "rev_override": bool(rev is not None and pin_full and full != pin_full),
             "tree": str(tree), "env_mode": None, "manifest_pin": pin_full, "manifest": manifest,
             "root_env": root_env, "rev_env": rev_env, "env": env}
    if dry_run:
        return {"module": block, "wrapper": str(wrapper), "warnings": warnings}
    exec_root(site).mkdir(parents=True, exist_ok=True)
    tree.mkdir()
    try:
        arch = subprocess.Popen(["git", "-C", str(mod), "archive", "--format=tar", full], stdout=subprocess.PIPE)
        tar = subprocess.run(["tar", "-x", "-C", str(tree)], stdin=arch.stdout, capture_output=True)
        arch.stdout.close()
        if arch.wait() != 0 or tar.returncode != 0:
            fail(f"git archive {full[:7]} | tar -x failed: {tar.stderr.decode(errors='replace').strip()}")
        mode = env_mode(site, mod, tree)
        rebuild = None
        if mode == "rebuild":
            from .workflow import _env_rebuild_command

            rebuild = _env_rebuild_command(site, f"modules/{module}")
            if not rebuild:
                fail(f"modules/{module} needs an environment rebuild for {full[:7]} (uv.lock differs from the live "
                     f"checkout, or no usable modules/{module}/.venv) and config has no env_rebuild for it; set "
                     f"`env_rebuild` on the modules/{module} entry of config.json")
        subprocess.run(["chmod", "-R", "a-w", str(tree)], check=True)
        block["env_mode"] = mode
        wrapper.write_text(_wrapper(site, script, mod, tree, venv, full, env, mode, rebuild, items, items_file,
                                    item_env))
        wrapper.chmod(0o755)
    except BaseException:
        cleanup(tree)
        raise
    return {"module": block, "wrapper": str(wrapper), "warnings": warnings}


def cleanup(tree: Path) -> None:
    """Remove a tree, its .venv (per array task: .venv-<i>) and its wrapper."""
    extra = sorted(tree.parent.glob(f"{tree.name}.venv-*")) if tree.parent.is_dir() else []
    for p in (tree, Path(f"{tree}.venv"), Path(f"{tree}.sbatch"), *extra):
        _rmtree(p)


def _wrapper(site: Site, script: Path, mod: Path, tree: Path, venv: Path, rev: str, env: dict,
             mode: str, rebuild: str | None, items: list[str] | None = None, items_file: str | None = None,
             item_env: dict | None = None) -> str:
    q = shlex.quote
    lines = script.read_text(encoding="utf-8", errors="replace").splitlines()
    first = lines[0] if lines and lines[0].startswith("#!") else ""
    interp = shlex.split(first[2:]) if first else ["/bin/bash"]
    directives = [ln for ln in lines if ln.strip().startswith("#SBATCH")]
    pkg = package_dir(tree)
    if items is None:
        out = ["#!/bin/bash", *directives,
               f"# Generated by `dureh slurm submit --module {mod.name}`: runs {script} against the read-only tree",
               f"# {tree} (git archive {rev}); the tree and its .venv are removed when the job exits.",
               f"DUREH_EXEC_TREE={q(str(tree))}",
               f"DUREH_EXEC_VENV={q(str(venv))}",
               '_dureh_exec_cleanup() { chmod -R u+w "$DUREH_EXEC_TREE" "$DUREH_EXEC_VENV" 2>/dev/null; '
               'rm -rf "$DUREH_EXEC_TREE" "$DUREH_EXEC_VENV"; }']
    else:  # array tasks share the tree (removed by `slurm wait|status` once every task ended, or `purge`);
        from .ops import item_lines  # each task removes only its own rebuilt .venv

        out = ["#!/bin/bash", *directives,
               f"# Generated by `dureh slurm submit --module {mod.name} --each`: runs {script} once per item against",
               f"# the read-only tree {tree} (git archive {rev}), shared by all array tasks.",
               *item_lines(items, items_file, item_env),
               f"DUREH_EXEC_TREE={q(str(tree))}",
               f"DUREH_EXEC_VENV={q(str(venv))}-\"$SLURM_ARRAY_TASK_ID\"",
               '_dureh_exec_cleanup() { chmod -R u+w "$DUREH_EXEC_VENV" 2>/dev/null; rm -rf "$DUREH_EXEC_VENV"; }']
    out += ["trap _dureh_exec_cleanup EXIT",
           "trap 'exit 143' TERM INT",
           f"export {env['root']}={q(str(tree))}"]
    if env.get("rev"):
        out.append(f"export {env['rev']}={q(rev)}")
    if mode == "shared-venv":
        out += [f"# env mode shared-venv: uv.lock at {rev[:7]} is identical to modules/{mod.name}/uv.lock",
                f"export VIRTUAL_ENV={q(str(mod / '.venv'))}",
                f"export PYTHONPATH={q(str(pkg))}\"${{PYTHONPATH:+:$PYTHONPATH}}\""]
    else:
        out += [f"# env mode rebuild: uv.lock at {rev[:7]} differs from modules/{mod.name}/uv.lock (or no usable .venv)",
                (f"export VIRTUAL_ENV={q(str(venv))}" if items is None else 'export VIRTUAL_ENV="$DUREH_EXEC_VENV"'),
                'export UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV"',
                f"( cd \"$DUREH_EXEC_TREE\" && {rebuild} ) || "
                "{ echo \"dureh: environment rebuild failed in $DUREH_EXEC_TREE\" >&2; exit 97; }"]
    out += ['export UV_PROJECT_ENVIRONMENT="$VIRTUAL_ENV"',
            "export UV_NO_SYNC=1  # never sync (and so never change) the environment from the job",
            'export PATH="$VIRTUAL_ENV/bin:$PATH"',
            " ".join(q(w) for w in interp) + f" {q(os.path.abspath(script))} \"$@\"",  # the job runs in its --chdir
            "exit $?"]
    return "\n".join(out) + "\n"


def purge_targets(site: Site, wi: str) -> list[Path]:
    root = exec_root(site)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.name.startswith(f"{wi}-"))
