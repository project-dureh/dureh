"""Sanctioned container builds (UR-20261004-02).

`dureh container build` turns a tracked recipe `projects/<p>/code/containers/<name>.def` and a pinned module
revision (`git archive <rev>` of `modules/<m>`) into a new SIF in the site's container cache. The build runs only
as a scheduler job, submitted through `slurm submit` (records, logs, start check, WI binding); the session never
runs apptainer. The job writes `<cache>/.<target>.partial-<job>`, its sha256 and a provenance file, then moves the
partial to `<name>_<version>-<rev7>.sif` with `mv -n`: an existing image is never overwritten.
`dureh container status` reads the provenance once the job finished and appends one `container.built` record.

The cache directory is written by these jobs only: no role may write it through the hook (paths.classify zone
`container-cache`).
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path

from .records import Ledger
from .site import Site
from .util import DurehError, fail, git, sha256_file

RECIPE_RE = re.compile(r"^projects/([^/]+)/code/containers/([^/]+)\.def$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
DEFAULT_BUILD_ARGS = ["--fakeroot"]  # Longleaf, 2026-10-04 probe: works for glibc bases (root-mapped namespace)
DEFAULT_TIME = "02:00:00"
DEFAULT_MEM = "16G"
DEFAULT_CPUS = 2


def _cfg(site: Site) -> dict:
    return site.config.get("containers") or {}


def cache_dir(site: Site) -> Path | None:
    """The container cache: config `containers.cache`, else the site env var CAMRI_CONTAINER_CACHE
    (config `env.vars` or the process environment); relative paths are under the site. None when unset."""
    raw = _cfg(site).get("cache")
    if not raw:
        raw = ((site.config.get("env") or {}).get("vars") or {}).get("CAMRI_CONTAINER_CACHE") \
            or os.environ.get("CAMRI_CONTAINER_CACHE")
    if not raw:
        return None
    p = Path(site.expand(str(raw)))
    return p if p.is_absolute() else site.root / p


def _check_submitter(led: Ledger, actor: str, wi: str) -> dict:
    rec = led.get("work-item", wi)
    if actor not in ("pipeline-runner", "desk"):
        fail(f"container builds are submitted only by pipeline-runner (or the desk), not {actor}")
    if actor != "desk" and rec["payload"]["role"] != actor:
        fail(f"{wi} is not a {actor} work item")
    if led.wi_status(wi)["status"] not in ("dispatched", "rework"):
        fail(f"{wi} is not dispatched")
    return rec


def _module_version(mod: Path, rev: str) -> str | None:
    proc = subprocess.run(["git", "show", f"{rev}:pyproject.toml"], cwd=mod, capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    try:
        import tomllib

        return (tomllib.loads(proc.stdout).get("project") or {}).get("version")
    except Exception:  # noqa: BLE001 - malformed or old Python: fall back to the first version line
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', proc.stdout)
        return m.group(1) if m else None


def _annex_key_ok(key: str, data: bytes) -> bool:
    """Size and (SHA256 backends) digest of `data` match a git-annex key such as SHA256E-s774--<hex>.def."""
    import hashlib

    m = re.match(r"^([A-Z0-9]+)-s(\d+)--([^./]+)", key)
    if not m or int(m.group(2)) != len(data):
        return False
    if m.group(1).startswith("SHA256"):
        return hashlib.sha256(data).hexdigest() == m.group(3)
    return True  # other backends: size only


def _tracked_recipe(proj: Path, inner: str, refs: list[str]) -> dict | None:
    """The recipe as tracked at the first ref (WI base, then HEAD) whose entry matches the working tree:
    a plain git blob with the same bytes, or a git-annex file (locked symlink or unlocked pointer) whose key
    matches the local content. Returns ref, blob, annex key, the bytes, or None."""
    path = proj / inner
    for ref in refs:
        if not ref:
            continue
        parts = git(["ls-tree", ref, "--", inner], proj, check=False).split()
        if len(parts) < 3 or parts[1] != "blob":
            continue
        mode, blob = parts[0], parts[2]
        tracked = subprocess.run(["git", "cat-file", "blob", blob], cwd=proj, capture_output=True).stdout
        key, data = None, None
        if mode == "120000":
            link = tracked.decode("utf-8", "replace")
            if not path.is_symlink() or os.readlink(path) != link or "annex/objects/" not in link:
                continue
            real = path.resolve()
            annex = (proj / ".git" / "annex" / "objects").resolve()
            if not str(real).startswith(str(annex) + os.sep) or not real.is_file():
                fail(f"the annexed recipe {inner} has no local content; ask the data-steward to land it")
            key, data = real.name, real.read_bytes()
        elif path.is_symlink() or not path.is_file():
            continue
        elif tracked.startswith(b"/annex/objects/"):
            key, data = tracked.decode().strip().rsplit("/", 1)[-1], path.read_bytes()
        else:
            data = path.read_bytes()
            if data != tracked:
                continue
        if key is not None and not _annex_key_ok(key, data):
            continue
        return {"ref": git(["rev-parse", ref], proj), "blob": blob, "annex_key": key, "data": data}
    return None


def build(site: Site, actor: str, wi: str, recipe: str, module: str, rev: str, version: str | None = None,
          name: str | None = None, time: str | None = None, mem: str | None = None, dry_run: bool = False) -> dict:
    from .ops import slurm_submit
    from .workflow import _base_of

    led = Ledger(site)
    rec = _check_submitter(led, actor, wi)
    rel = os.path.normpath(recipe).replace(os.sep, "/")
    if Path(recipe).is_absolute():
        try:
            rel = Path(os.path.abspath(recipe)).relative_to(site.root).as_posix()
        except ValueError:
            fail(f"recipe must be inside the site: {recipe}")
    m = RECIPE_RE.match(rel)
    if not m or ".." in rel.split("/"):
        fail(f"recipe must be a site-relative projects/<project>/code/containers/<name>.def, not {recipe}")
    project, stem = m.group(1), m.group(2)
    target = rec["payload"].get("target")
    if target != f"projects/{project}":
        fail(f"{wi} targets {target}; a container build needs a WI for projects/{project}, the recipe's project")
    proj = site.root / "projects" / project
    recipe_path = site.root / rel
    if not recipe_path.is_file():
        fail(f"recipe is missing: {rel}")
    if not (proj / ".git").exists():
        fail(f"projects/{project} is not a git repository; the recipe must be tracked")
    inner = f"code/containers/{stem}.def"
    tracked = _tracked_recipe(proj, inner, [_base_of(site, wi) or "", "HEAD"])
    if tracked is None:
        fail(f"{rel} is not tracked with this content at the WI base or HEAD of projects/{project}; commit it first")
    if not re.match(r"^[A-Za-z0-9_.-]+$", module):
        fail(f"invalid module id: {module!r}")
    mod = site.root / "modules" / module
    if not mod.is_dir() or not (mod / ".git").exists():
        fail(f"module is missing or not a git repository: modules/{module}")
    full = git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], mod, check=False)
    if not re.match(r"^[0-9a-f]{40}$", full or ""):
        fail(f"revision {rev!r} does not resolve to a commit in modules/{module}")
    version = version or _module_version(mod, full)
    if not version:
        fail(f"no version in modules/{module}/pyproject.toml at {full[:7]}; pass --version")
    name = name or stem
    for label, value in (("name", name), ("version", version)):
        if not NAME_RE.match(value):
            fail(f"invalid {label} {value!r}: letters, digits and . _ + - only")
    cache = cache_dir(site)
    if cache is None:
        fail("no container cache configured: set config.json `containers.cache` (or env.vars CAMRI_CONTAINER_CACHE)")
    if not cache.is_dir():
        fail(f"container cache directory does not exist: {cache}")
    sif = f"{name}_{version}-{full[:7]}.sif"
    for existing in (cache / sif, cache / f"{sif}.provenance.json"):
        if existing.exists() or existing.is_symlink():
            fail(f"{existing} already exists; images are never overwritten (choose another --version or --name)")
    for run in led.load("run"):
        c = run["payload"].get("container") or {}
        if c.get("target") == sif and run["payload"].get("status") == "submitted" and run["payload"].get("job_id") \
                and not _built_event(led, run["id"]):
            state = _state(site, run["payload"]["job_id"])
            if state.split(" ")[0] in ("PENDING", "RUNNING", "CONFIGURING", "COMPLETING", "REQUEUED", "SUSPENDED"):
                fail(f"{sif} is already being built by job {run['payload']['job_id']} ({run['id']}, {state})")

    work = proj / "checkpoints" / f"{wi}-container-{sif[:-4]}"
    work.mkdir(parents=True, exist_ok=True)
    recipe_copy = work / f"{stem}.def"
    recipe_copy.write_bytes(tracked["data"])
    cfg = _cfg(site)
    build_args = [str(a) for a in (cfg.get("build_args") if cfg.get("build_args") is not None else DEFAULT_BUILD_ARGS)]
    import hashlib

    static = {"recipe": rel, "recipe_blob": tracked["blob"], "recipe_commit": tracked["ref"],
              "recipe_annex_key": tracked["annex_key"], "recipe_sha256": hashlib.sha256(tracked["data"]).hexdigest(), "project": f"projects/{project}",
              "module": module, "rev": full, "version": version, "name": name, "target": sif, "cache": str(cache),
              "wi": wi, "build_args": build_args}
    script = work / "build.sh"
    script.write_text(_job_script(site, work, recipe_copy, mod, full, cache, sif, static, build_args,
                                  time or cfg.get("time") or DEFAULT_TIME, mem or cfg.get("mem") or DEFAULT_MEM,
                                  int(cfg.get("cpus") or DEFAULT_CPUS)), encoding="utf-8")
    script.chmod(0o755)
    extra = {"container": {**static, "script_dir": str(work)}}
    return slurm_submit(site, actor, wi, str(script), dry_run, None, extra_payload=extra)


def _job_script(site: Site, work: Path, recipe_copy: Path, mod: Path, rev: str, cache: Path, sif: str,
                static: dict, build_args: list[str], time: str, mem: str, cpus: int) -> str:
    q = shlex.quote
    static_json = json.dumps(static, sort_keys=True)[:-1]  # closed by the job after its own fields
    module_load = _cfg(site).get("module", "apptainer")
    lines = [
        "#!/bin/bash",
        f"#SBATCH --time={time}",
        f"#SBATCH --mem={mem}",
        f"#SBATCH --cpus-per-task={cpus}",
        "# generated by `dureh container build`; do not edit (the run record holds this script's sha256)",
        "set -euo pipefail",
        f"JOB=${{SLURM_JOB_ID:?this script runs only as a scheduler job}}",
        f"WORK={q(str(work))}",
        f"CACHE={q(str(cache))}",
        f"TARGET=\"$CACHE\"/{q(sif)}",
        f"PARTIAL=\"$CACHE\"/{q('.' + sif + '.partial-')}\"$JOB\"",
        f"PROV=\"$CACHE\"/{q(sif + '.provenance.json')}",
        f"PROV_PARTIAL=\"$CACHE\"/{q('.' + sif + '.provenance.json.partial-')}\"$JOB\"",
        "CTX=\"$WORK/ctx-$JOB\"",
    ]
    if module_load:
        # the site's module only when apptainer is not already on PATH (a PATH the caller set wins)
        lines.append(f"command -v apptainer >/dev/null || module load {q(str(module_load))} >/dev/null 2>&1 || true")
    lines += [
        "command -v apptainer >/dev/null || { echo 'apptainer not found on this node' >&2; exit 2; }",
        "if [ -e \"$TARGET\" ] || [ -L \"$TARGET\" ]; then echo \"refusing: $TARGET exists\" >&2; exit 3; fi",
        # node-local scratch for the build; the image layer cache stays where the site's apptainer module puts it
        "export APPTAINER_TMPDIR=\"${TMPDIR:-$WORK/tmp-$JOB}\"",
        "mkdir -p \"$APPTAINER_TMPDIR\" \"$CTX\"",
    ]
    cachedir = _cfg(site).get("apptainer_cachedir")
    if cachedir:
        lines.append(f"export APPTAINER_CACHEDIR={q(site.expand(str(cachedir)))}")
    lines += [
        "export APPTAINER_CACHEDIR=\"${APPTAINER_CACHEDIR:-$WORK/apptainer-cache}\"",
        "mkdir -p \"$APPTAINER_CACHEDIR\"",
        f"git -C {q(str(mod))} archive {q(rev)} | tar -x -C \"$CTX\"",
        f"cp {q(str(recipe_copy))} \"$CTX\"/{q(recipe_copy.name)}",
        "cd \"$CTX\"",
        "AVER=$(apptainer --version | tr -d '\"\\\\')",
        f"apptainer build {' '.join(q(a) for a in build_args)} \"$PARTIAL\" {q(recipe_copy.name)}",
        "SHA=$(sha256sum \"$PARTIAL\" | cut -d' ' -f1)",
        "SIZE=$(stat -c %s \"$PARTIAL\")",
        f"STATIC={q(static_json)}",
        "printf '%s, \"job_id\": \"%s\", \"apptainer_version\": \"%s\", \"built_at\": \"%s\", \"host\": \"%s\","
        " \"sha256\": \"%s\", \"size\": %s}\\n' \"$STATIC\" \"$JOB\" \"$AVER\" \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
        " \"$(hostname)\" \"$SHA\" \"$SIZE\" > \"$PROV_PARTIAL\"",
        "mv -n \"$PARTIAL\" \"$TARGET\"",
        "if [ -e \"$PARTIAL\" ]; then echo \"refusing: $TARGET appeared meanwhile; left $PARTIAL\" >&2; exit 4; fi",
        "mv -n \"$PROV_PARTIAL\" \"$PROV\"",
        "if [ -e \"$PROV_PARTIAL\" ]; then echo \"refusing: $PROV exists; left $PROV_PARTIAL\" >&2; exit 5; fi",
        "echo \"built $TARGET sha256 $SHA\"",
        "",
    ]
    return "\n".join(lines)


def _state(site: Site, job: str) -> str:
    try:
        proc = subprocess.run(["sacct", "-n", "-P", "-X", "-j", str(job), "--format=State,ExitCode,Elapsed"],
                              capture_output=True, text=True, timeout=60, env=site.env())
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    line = (proc.stdout.strip().splitlines() or [""])[0]
    return line.split("|")[0] if line else "UNKNOWN"


def _built_event(led: Ledger, run_id: str) -> dict | None:
    return next((e for e in led.events() if e["type"] == "container.built" and run_id in e["refs"]), None)


def status(site: Site, actor: str, wi: str) -> dict:
    """Each container build of a WI: once its job finished, check the image against its provenance and append
    one `container.built` record (refs the run). Re-running is safe: a recorded build is reported, not re-recorded."""
    led = Ledger(site)
    led.get("work-item", wi)
    if actor not in ("pipeline-runner", "desk"):
        fail(f"container status is recorded only by pipeline-runner (or the desk), not {actor}")
    builds = []
    for run in led.load("run"):
        c = run["payload"].get("container")
        if run["wi"] != wi or not c or not run["payload"].get("job_id"):
            continue
        job = str(run["payload"]["job_id"])
        cache = Path(c["cache"])
        out = {"run": run["id"], "job_id": job, "target": str(cache / c["target"])}
        done = _built_event(led, run["id"])
        if done:
            builds.append({**out, "state": "recorded", "event": done["id"], "sha256": done["payload"].get("sha256")})
            continue
        prov_path = cache / f"{c['target']}.provenance.json"
        prov = None
        if prov_path.is_file():
            try:
                prov = json.loads(prov_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prov = None
        if prov is None or str(prov.get("job_id")) != job:
            state = _state(site, job)
            partial = cache / f".{c['target']}.partial-{job}"
            note = None
            if state.split(" ")[0] in ("COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"):
                note = "the job ended without this build's provenance; read its log" + \
                       (f"; a partial image was left at {partial}" if partial.exists() else "")
            builds.append({**out, "state": state, **({"note": note} if note else {})})
            continue
        sif = cache / c["target"]
        if not sif.is_file():
            builds.append({**out, "state": "broken", "note": f"provenance names {sif}, which is missing"})
            continue
        digest = sha256_file(sif)
        if digest != prov.get("sha256"):
            builds.append({**out, "state": "broken", "note": f"sha256 {digest} differs from the provenance {prov.get('sha256')}"})
            continue
        try:
            ev = led.event("container.built", actor, wi, {**prov, "provenance": str(prov_path), "path": str(sif),
                                                          "run": run["id"]}, [run["id"]])
        except DurehError as exc:
            builds.append({**out, "state": "unrecorded", "note": str(exc)})
            continue
        builds.append({**out, "state": "recorded", "event": ev["id"], "sha256": digest, "new": True})
    if not builds:
        fail(f"{wi} has no submitted container builds")
    return {"wi": wi, "builds": builds}
