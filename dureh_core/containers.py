"""Sanctioned container builds (UR-20261004-02).

`dureh container build` turns a recipe and a pinned module revision (`git archive <rev>` of `modules/<m>`) into
a new SIF in the site's container cache. The recipe is the module's own `containers/<name>.def` inside that archive
(canonical, UR-20261004-03) or a tracked project-local `projects/<p>/code/containers/<name>.def` (`--recipe`).
The build runs only as a scheduler job, submitted through `slurm submit` (records, logs, start check, WI binding);
the session never runs apptainer. The job writes `<cache>/.<target>.partial-<job>`, its sha256 and a provenance
file, then moves the partial to `<name>_<version>-<rev7>.sif` with `mv -n`: an existing image is never overwritten.
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


def _module_recipe(mod: Path, module: str, rev: str, name: str) -> dict:
    """`containers/<name>.def` as it is inside `git archive <rev>` of the module (export-ignore and all):
    its bytes and blob. Refused when that rev has no such file."""
    import tarfile
    import io

    inner = f"containers/{name}.def"
    where = f"modules/{module}/{inner}"
    proc = subprocess.run(["git", "archive", "--format=tar", rev, "--", inner], cwd=mod, capture_output=True)
    data = None
    if proc.returncode == 0:
        try:
            with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
                member = next((m for m in tar.getmembers() if m.name == inner), None)
                if member is not None and member.isfile():
                    data = tar.extractfile(member).read()
        except tarfile.TarError:
            data = None
    if data is None:
        fail(f"{where} is not in modules/{module} at {rev[:7]} (`git archive {rev[:7]}`); commit the recipe in the "
             f"module first (py-writer, module WI), or pass --recipe projects/<p>/code/containers/<x>.def")
    blob = git(["rev-parse", "--verify", "--quiet", f"{rev}:{inner}"], mod, check=False) or None
    return {"path": where, "inner": inner, "blob": blob, "data": data}


def build(site: Site, actor: str, wi: str, recipe: str | None, module: str, rev: str, version: str | None = None,
          name: str | None = None, time: str | None = None, mem: str | None = None, dry_run: bool = False) -> dict:
    """Without `recipe` the recipe is the module's own `containers/<name or module>.def` at `rev` (UR-20261004-03):
    the WI may target that module or any project. With `recipe`, a project-local recipe (UR-20261004-02): the WI
    must target the recipe's project."""
    from .ops import slurm_submit
    from .workflow import _base_of

    led = Ledger(site)
    rec = _check_submitter(led, actor, wi)
    target = rec["payload"].get("target")
    if not re.match(r"^[A-Za-z0-9_.-]+$", module):
        fail(f"invalid module id: {module!r}")
    project = stem = tracked = None
    if recipe is not None:
        rel = os.path.normpath(recipe).replace(os.sep, "/")
        if Path(recipe).is_absolute():
            try:
                rel = Path(os.path.abspath(recipe)).relative_to(site.root).as_posix()
            except ValueError:
                fail(f"recipe must be inside the site: {recipe}")
        m = RECIPE_RE.match(rel)
        if not m or ".." in rel.split("/"):
            fail(f"recipe must be a site-relative projects/<project>/code/containers/<name>.def, not {recipe}"
                 " (omit --recipe to build the module's own containers/<name>.def at --rev)")
        project, stem = m.group(1), m.group(2)
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
    else:
        if target != f"modules/{module}" and not re.match(r"^projects/[^/]+$", target or ""):
            fail(f"{wi} targets {target}; a module-recipe build needs a WI for modules/{module} or for a project")
        if target.startswith("projects/"):
            project = target.split("/", 1)[1]
    mod = site.root / "modules" / module
    if not mod.is_dir() or not (mod / ".git").exists():
        fail(f"module is missing or not a git repository: modules/{module}")
    full = git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], mod, check=False)
    if not re.match(r"^[0-9a-f]{40}$", full or ""):
        fail(f"revision {rev!r} does not resolve to a commit in modules/{module}")
    name = name or stem or module
    if not NAME_RE.match(name):
        fail(f"invalid name {name!r}: letters, digits and . _ + - only")
    if recipe is None:
        mrec = _module_recipe(mod, module, full, name)
        stem = name
    version = version or _module_version(mod, full)
    if not version:
        fail(f"no version in modules/{module}/pyproject.toml at {full[:7]}; pass --version")
    if not NAME_RE.match(version):
        fail(f"invalid version {version!r}: letters, digits and . _ + - only")
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

    # project WIs keep the job directory in the project's untracked checkpoints/; a module WI's goes under the
    # site's logs/ (never into the module's git tree)
    if project is not None:
        work = site.root / "projects" / project / "checkpoints" / f"{wi}-container-{sif[:-4]}"
    else:
        work = site.root / "logs" / "containers" / f"{wi}-container-{sif[:-4]}"
    work.mkdir(parents=True, exist_ok=True)
    data = tracked["data"] if tracked else mrec["data"]
    recipe_copy = work / f"{stem}.def"
    recipe_copy.write_bytes(data)
    cfg = _cfg(site)
    build_args = [str(a) for a in (cfg.get("build_args") if cfg.get("build_args") is not None else DEFAULT_BUILD_ARGS)]
    import hashlib

    if tracked:
        rsrc = {"recipe_source": "project", "recipe": rel, "recipe_path": rel, "recipe_rev": tracked["ref"],
                "recipe_blob": tracked["blob"], "recipe_commit": tracked["ref"], "recipe_annex_key": tracked["annex_key"]}
        build_from = recipe_copy.name  # copied into the context root
    else:
        rsrc = {"recipe_source": "module", "recipe": mrec["path"], "recipe_path": mrec["path"], "recipe_rev": full,
                "recipe_blob": mrec["blob"], "recipe_commit": full, "recipe_annex_key": None}
        build_from = mrec["inner"]  # the archived file itself, so its %files paths resolve against the module root
    static = {**rsrc, "recipe_sha256": hashlib.sha256(data).hexdigest(),
              "project": f"projects/{project}" if project else None, "wi_target": target,
              "module": module, "rev": full, "version": version, "name": name, "target": sif, "cache": str(cache),
              "wi": wi, "build_args": build_args}
    script = work / "build.sh"
    script.write_text(_job_script(site, work, recipe_copy if tracked else None, build_from, mod, full, cache, sif,
                                  static, build_args, time or cfg.get("time") or DEFAULT_TIME,
                                  mem or cfg.get("mem") or DEFAULT_MEM, int(cfg.get("cpus") or DEFAULT_CPUS)),
                      encoding="utf-8")
    script.chmod(0o755)
    extra = {"container": {**static, "script_dir": str(work)}}
    return slurm_submit(site, actor, wi, str(script), dry_run, None, extra_payload=extra)


def _job_script(site: Site, work: Path, recipe_copy: Path | None, build_from: str, mod: Path, rev: str, cache: Path,
                sif: str, static: dict, build_args: list[str], time: str, mem: str, cpus: int) -> str:
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
    ]
    if recipe_copy is not None:
        lines.append(f"cp {q(str(recipe_copy))} \"$CTX\"/{q(recipe_copy.name)}")
    else:
        lines.append(f"test -f \"$CTX\"/{q(build_from)} || {{ echo 'recipe {build_from} missing from the archive' >&2; exit 6; }}")
    lines += [
        "cd \"$CTX\"",
        "AVER=$(apptainer --version | tr -d '\"\\\\')",
        f"apptainer build {' '.join(q(a) for a in build_args)} \"$PARTIAL\" {q(build_from)}",
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


# ------------------------------------------------------------------ retire (UR-20261004-03 item 3)

MANIFEST_GLOBS = ("camri-manifest.yaml", "*manifest*.yaml", "*manifest*.yml", "*manifest*.json")


def _manifests(site: Site) -> list[Path]:
    """The project manifests an image may be pinned in: each `projects/<p>/camri-manifest.yaml` (the one manifest
    the harness reads, see projectgen) and any other top-level `*manifest*.{yaml,yml,json}` of a project."""
    root = site.root / "projects"
    found: list[Path] = []
    if not root.is_dir():
        return found
    for proj in sorted(p for p in root.iterdir() if p.is_dir()):
        for pattern in MANIFEST_GLOBS:
            for f in sorted(proj.glob(pattern)):
                if f.is_file() and f not in found:
                    found.append(f)
    return found


def pinned_by(site: Site, digests: set[str]) -> list[dict]:
    """Manifests whose text contains one of the hex digests (bare or as `sha256:<hex>`, any case)."""
    out = []
    for f in _manifests(site):
        try:
            text = f.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        hits = sorted(d for d in digests if d in text)
        if hits:
            out.append({"manifest": f.relative_to(site.root).as_posix(), "sha256": hits})
    return out


def retire(site: Site, actor: str, sif_name: str, reason: str, dry_run: bool = False, wi: str | None = None) -> dict:
    """Remove an image nobody pins: refuse while any project manifest names its sha256; otherwise keep its
    provenance under `<cache>/retired/` (with retired_at, reason, by), delete the SIF and record
    `container.retired`. Only files inside the cache directory are touched."""
    from .util import utc_now, write_create_only, dumps

    if actor not in ("desk", "data-steward"):
        fail(f"container images are retired only by the desk or the data-steward, not {actor}")
    if not (reason or "").strip():
        fail("--reason is required")
    if not site.org.may_append(actor, "container.retired"):
        fail(f"role {actor!r} may not record container.retired (org/roles.yaml event_authority)")
    led = Ledger(site)
    if wi is not None:
        rec = led.get("work-item", wi)
        if actor != "desk" and rec["payload"]["role"] != actor:
            fail(f"{wi} is not a {actor} work item")
        if actor != "desk" and led.wi_status(wi)["status"] not in ("dispatched", "rework"):
            fail(f"{wi} is not dispatched")
    if "/" in sif_name or os.sep in sif_name or not NAME_RE.match(sif_name) or not sif_name.endswith(".sif"):
        fail(f"--sif takes an image file name in the cache, such as camri_2.0.0a1-2396359.sif (no paths): {sif_name!r}")
    cache = cache_dir(site)
    if cache is None:
        fail("no container cache configured: set config.json `containers.cache` (or env.vars CAMRI_CONTAINER_CACHE)")
    if not cache.is_dir():
        fail(f"container cache directory does not exist: {cache}")
    sif = cache / sif_name
    prov_path = cache / f"{sif_name}.provenance.json"
    retired_dir = cache / "retired"
    retired_prov = retired_dir / f"{sif_name}.provenance.json"
    has_sif = sif.is_file() and not sif.is_symlink()
    if sif.is_symlink() or (sif.exists() and not has_sif):
        fail(f"{sif} is not a regular file; retire handles images built into the cache only")
    prov = None
    if prov_path.is_file() and not prov_path.is_symlink():
        try:
            prov = json.loads(prov_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            fail(f"{prov_path} is not readable JSON; fix or remove it by hand")
    if not has_sif and prov is None:
        if retired_prov.exists():
            fail(f"{sif_name} is already retired ({retired_prov})")
        fail(f"unknown image {sif_name}: neither it nor its provenance is in {cache}")
    if retired_prov.exists() or retired_prov.is_symlink():
        fail(f"{retired_prov} already exists; a retired provenance is never overwritten")
    digests = set()
    measured = sha256_file(sif) if has_sif else None
    recorded = str((prov or {}).get("sha256") or "").lower() or None
    for d in (measured, recorded):
        if d and re.match(r"^[0-9a-f]{64}$", d):
            digests.add(d)
    if not digests:
        fail(f"no sha256 for {sif_name}: the image is missing and its provenance has none")
    scanned = _manifests(site)
    pins = pinned_by(site, digests)
    out = {"sif": str(sif), "sha256": measured or recorded, "manifests_scanned": len(scanned), "pinned_by": pins,
           "dry_run": dry_run}
    if measured and recorded and measured != recorded:
        out["note"] = f"the image's sha256 {measured} differs from its provenance {recorded}; both were checked"
    if pins:
        if dry_run:
            return {**out, "action": "refuse", "why": "pinned"}
        fail(f"{sif_name} is pinned (sha256 {out['sha256']}) by: " + ", ".join(p["manifest"] for p in pins)
             + "; unpin it in those manifests first (data-steward)")
    if dry_run:
        return {**out, "action": "retire", "would_write": str(retired_prov),
                "would_delete": [str(p) for p in (sif, prov_path) if p.exists()]}
    built = next((e for e in led.events() if e["type"] == "container.built"
                  and e["payload"].get("target") == sif_name and e["payload"].get("cache") == str(cache)), None)
    now = utc_now()
    keep = {**(prov or {"target": sif_name, "cache": str(cache), "sha256": out["sha256"]}),
            "retired_at": now, "reason": reason, "by": actor, **({"retired_sha256": measured} if measured else {})}
    if retired_dir.is_symlink() or (retired_dir.exists() and not retired_dir.is_dir()):
        fail(f"{retired_dir} must be a plain directory inside the cache")
    retired_dir.mkdir(exist_ok=True)
    write_create_only(retired_prov, dumps(keep).encode("utf-8"), 0o644)
    if has_sif:
        sif.unlink()
    if prov is not None:
        prov_path.unlink()
    ev = led.event("container.retired", actor, wi,
                   {"sif": sif_name, "cache": str(cache), "sha256": out["sha256"], "reason": reason, "by": actor,
                    "retired_at": now, "provenance": str(retired_prov), "deleted_image": has_sif,
                    "manifests_scanned": len(scanned)}, [built["id"]] if built else [])
    return {**out, "action": "retired", "event": ev["id"], "provenance": str(retired_prov)}
