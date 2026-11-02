"""Per-tier model qualification exams with efficiency selection (release 90 candidate).

Director 2026-11-02 (dureh-team#116, proposal `model-qualification-exams-2026-11-02.md` sections 2-7a, section 8
answered with its Recommended line): a model tier is qualified by an exam on the company's own verified work, and each
tier moves to the cheapest model that passes.

- `exam build --tier T`: freezes 3-5 promoted work items of that tier (base commit, brief, declared checks, write
  scopes, recorded verdict) under `state/exams/<tier>/<set>/` (records-like, never `state/memory/`), split holdout.
- `exam run --tier T --model M`: one git worktree per task under `scratch/exams/<run>/` at the frozen commit and one
  brief per task, headed `DUREH-EXAM <run> task=<id> tier=<T> model=<M>`; the desk spawns each with that model.
- `exam score --run R`: runs every task's declared checks (local, or one Slurm job through the verify checks script),
  scores deterministically, measures cost in usage units when the run's agents carry usage, records one `run` record
  `run_type: qualification` (status pass/fail, tier, exact model, exam set sha256, expires = 90 days).
- `exam compare` / `exam plan`: tier x model table and the recommendation (cheapest passing model, no worse than the
  incumbent minus one task), and which candidates still need a run, cheapest first.

The gate (`check_run`): `qualification: run-<id>` passes only when that record exists, passed, names the same tier and
the exact same model string, and has not expired. Waivers are unchanged. `models.fallback_tiers` (an ordered path,
lowest first; absent = fail-closed) lets dispatch run an unqualified tier on the nearest qualified higher tier, with a
WARN. Config is never written here: applying a pass is a `models.<tier>` change (class B for the same model, class C
for a new one; see permits.py).

Phase 2: the spawn hook admits exam briefs (`admit_spawn`, exact model) and binds their agents (`bind_agent`,
`write_refusal`: writes only in the task workspace); agent usage is summed per task; a failing deterministic task is
rerun once (`exam.rerun`); a tier with too few items falls back to the harness/package example tasks of its roles or is
`not examinable`; `newer_models` feeds the handshake's `newer models` line.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import time
from datetime import date, timedelta
from pathlib import Path

from .records import Ledger
from .site import MODELS_META_KEYS, Site
from .util import DurehError, dumps, fail, git, utc_compact, utc_now

QUAL_DAYS = 90            # section 8 Q2: 90-day expiry plus re-exam on a model change
WARN_DAYS = 7             # status and handshake warn a week before a qualification ends
PASS_FRACTION = 0.8       # section 8 Q1: ceil(0.8 * N) tasks, no hard failure
MIN_TASKS, MAX_TASKS, DEFAULT_TASKS = 3, 5, 4
RUN_TYPE = "qualification"
EXAM_RUN = "exam-run"     # the `run` record type of a prepared exam run (run_type exam, status submitted)
HEADER = "DUREH-EXAM"
VERDICT = "verdict.json"  # a check task's answer, next to its workspace
# the first line of an exam brief; `attempt=2` only on the one rerun of a failing task (phase 2)
HEADER_RE = re.compile(r"DUREH-EXAM (run-[A-Za-z0-9-]+) task=(\S+) tier=([a-z0-9-]+) model=(\S+)(?: attempt=(\d+))?[ \t]*$",
                       re.M)
DETERMINISTIC_KINDS = ("build", "check", "fixture")  # scored by exit codes and exact matches: a rerun is allowed


def _today(today: date | None) -> date:
    return today or date.today()


# ------------------------------------------------------------------ config
def default_candidates() -> list[str]:
    """Haiku, Sonnet, Opus: the newest names of the harness default model table, cheapest first."""
    from .install import DEFAULT_MODELS

    out = []
    for tier in ("recount", "worker", "pm"):
        m = DEFAULT_MODELS[tier]
        if m not in out:
            out.append(m)
    return out


def candidates(site: Site) -> list[str]:
    """`models.candidates` (cheapest first), else default_candidates()."""
    c = (site.config.get("models") or {}).get("candidates")
    return list(c) if isinstance(c, list) and c else default_candidates()


def fallback_path(site: Site) -> list[str]:
    """`models.fallback_tiers`: the ordered tier path an unqualified tier may fall back along (lowest first). Absent or
    empty: no fallback (fail-closed, the default; section 8 Q3)."""
    p = (site.config.get("models") or {}).get("fallback_tiers")
    return list(p) if isinstance(p, list) else []


def config_errors(config: dict) -> list[str]:
    models = config.get("models") or {}
    tiers = [k for k in models if k not in MODELS_META_KEYS]
    out = []
    c = models.get("candidates")
    if c is not None and (not isinstance(c, list) or not c or not all(isinstance(m, str) and m for m in c)):
        out.append("config models.candidates must be a nonempty list of model names, cheapest first (release 90)")
    f = models.get("fallback_tiers")
    if f is not None:
        if not isinstance(f, list) or not all(isinstance(t, str) for t in f):
            out.append("config models.fallback_tiers must be a list of tier names, lowest first (release 90; absent ="
                       " fail-closed)")
        else:
            bad = [t for t in f if t not in tiers]
            if bad:
                out.append(f"config models.fallback_tiers: {', '.join(bad)} names no tier defined under models")
            if len(set(f)) != len(f):
                out.append("config models.fallback_tiers lists a tier twice")
    return out


# ------------------------------------------------------------------ gate
def runs(site: Site) -> list[dict]:
    return [r for r in Ledger(site).load("run") if r["payload"].get("run_type") == RUN_TYPE]


def _get_run(site: Site, run_id: str) -> dict | None:
    try:
        return Ledger(site).get("run", run_id)
    except DurehError:
        return None


def expiry(rec: dict) -> date:
    p = rec["payload"]
    try:
        return date.fromisoformat(str(p.get("expires")))
    except ValueError:
        return date.fromisoformat(rec["ts"][:10]) + timedelta(days=QUAL_DAYS)


def check_run(site: Site, tier: str, entry: dict, today: date | None = None) -> tuple[bool, str]:
    """The strict gate for `qualification: run-<id>` (section 4)."""
    q, model = entry.get("qualification"), entry.get("model")
    rec = _get_run(site, q)
    if rec is None:
        return False, f"qualification {q} names no run record (a typed run id is not a qualification)"
    p = rec["payload"]
    if p.get("run_type") != RUN_TYPE:
        return False, f"{q} is a {p.get('run_type')} run, not a qualification exam"
    if p.get("status") != "pass":
        return False, f"qualification run {q} did not pass ({p.get('score')})"
    if p.get("tier") != tier:
        return False, f"qualification run {q} is for tier {p.get('tier')}, not {tier}"
    if p.get("model") != model:
        return False, f"qualification run {q} qualified {p.get('model')}, not {model}: a model change needs a new exam"
    end = expiry(rec)
    if end < _today(today):
        return False, f"qualification run {q} expired on {end.isoformat()}: re-run the exam"
    return True, f"qualified by {q} ({p.get('score')}) until {end.isoformat()}"


def fallback_for(site: Site, tier: str) -> tuple[str, dict, str] | None:
    """(tier, entry, why) of the nearest qualified tier above `tier` on `models.fallback_tiers`, or None."""
    from .workflow import _qualified

    path = fallback_path(site)
    if tier not in path:
        return None
    models = site.config.get("models") or {}
    for t in path[path.index(tier) + 1:]:
        e = models.get(t)
        if not isinstance(e, dict) or not e.get("model"):
            continue
        try:
            ok, why = _qualified(e, site, t)
        except ValueError:
            continue
        if ok:
            return t, e, why
    return None


def warnings(site: Site, today: date | None = None) -> list[str]:
    """Tiers whose `run-*` qualification does not hold (dispatch refuses them) or ends within WARN_DAYS days."""
    today = _today(today)
    bad, soon = [], []
    for tier in site.model_tiers():
        e = (site.config.get("models") or {}).get(tier)
        if not isinstance(e, dict) or not str(e.get("qualification") or "").startswith("run-"):
            continue
        ok, why = check_run(site, tier, e, today)
        if not ok:
            bad.append(f"{tier} ({why})")
            continue
        end = expiry(_get_run(site, e["qualification"]))
        if (end - today).days <= WARN_DAYS:
            soon.append(f"{tier} ({end.isoformat()})")
    out = []
    if bad:
        out.append("model qualification does not hold, dispatch refuses these tiers: " + "; ".join(bad)
                   + "; run the exam (`dureh exam run`)")
    if soon:
        out.append(f"model qualification ends within {WARN_DAYS} days: " + ", ".join(soon)
                   + "; re-run the exam this week (`dureh exam plan`)")
    return out


def model_version(model: str) -> tuple[str, str, tuple[int, ...]] | None:
    """(provider, family, version) of a model name: `anthropic:claude-opus-5-5` -> ('anthropic', 'opus', (5, 5));
    a date suffix (8 digits) is not part of the version. None when the name has no family or no version."""
    provider, sep, name = str(model or "").partition(":")
    if not sep:
        provider, name = "", provider
    words = [w for w in name.split("-") if w and w != "claude"]
    family = next((w for w in words if not w.isdigit()), None)
    nums = tuple(int(w) for w in words if w.isdigit() and len(w) < 8)
    return (provider, family, nums) if family and nums else None


def _newer(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def newer_models(site: Site) -> list[dict]:
    """Proposal section 5 (newest-model rule, Director 10-19): per tier, the newest model in `models.candidates` of the
    same provider and family as the tier's model with a higher version. The harness never switches by itself."""
    models = site.config.get("models") or {}
    cands = models.get("candidates")
    if not isinstance(cands, list):
        return []
    out = []
    for tier in site.model_tiers():
        inc = (models.get(tier) or {}).get("model") if isinstance(models.get(tier), dict) else None
        v = model_version(inc) if inc else None
        if v is None:
            continue
        newer = [(cv, c) for c in cands if isinstance(c, str) and (cv := model_version(c)) and cv[:2] == v[:2]
                 and _newer(cv[2], v[2])]
        if newer:
            best = max(newer, key=lambda x: x[0][2] + (0,) * (8 - len(x[0][2])))[1]
            out.append({"tier": tier, "incumbent": inc, "newer": best})
    return out


# ------------------------------------------------------------------ build
def exams_root(site: Site, tier: str | None = None) -> Path:
    root = site.state / "exams"
    return root / tier if tier else root


def _require_desk(site: Site, actor: str) -> None:
    if site.org.role(actor).kind != "desk":
        fail("qualification exams are built, run and scored by the desk")


def _tier_of_dispatch(site: Site, routing: dict, rec: dict, prepared: dict | None) -> str:
    p, role = rec["payload"], site.org.get(rec["payload"]["role"])
    if prepared and prepared.get("tier"):
        return prepared["tier"]
    if p.get("academic"):
        return "writer-academic"
    kind = (prepared or {}).get("kind")
    return (routing.get(kind) if kind else None) or (role.tier if role else "")


def _check_tiers(site: Site, routing: dict) -> set[str]:
    out = {r.tier for r in site.org.with_capability("verify") if r.kind != "desk"}
    if routing.get("check"):
        out = {routing["check"]}
    return out


def _protected(site: Site, target: str, paths: list[str]) -> bool:
    from . import protect
    from .paths import target_root

    root = target_root(site, target)
    if root is None:
        return False
    return any(protect.rule_for(site, root / x, f"{target}/{x}") for x in paths)


def _commit_exists(root: Path, rev: str | None) -> bool:
    return bool(rev) and subprocess.run(["git", "cat-file", "-e", f"{rev}^{{commit}}"], cwd=root,
                                        capture_output=True).returncode == 0


def _build_brief(site: Site, rec: dict, kind: str) -> str:
    """The frozen task brief; `{WORKSPACE}` and `{VERDICT}` are filled in at `exam run`."""
    p = rec["payload"]
    role = site.org.role(p["role"])
    lines = [f"Exam task (replay of {rec['id']}): {p['title']}", f"Goal: {p['goal']}", f"Target: {p['target']}",
             "Work only in: {WORKSPACE}"]
    scopes = [s for s in role.write_scopes if not s.startswith("site:")]
    if kind == "build" and scopes:
        lines.append("You may write only: " + ", ".join(scopes))
    lines.append("Acceptance:")
    lines += [f"- {a}" for a in p.get("acceptance") or []]
    if p.get("constraints"):
        lines.append("Constraints:")
        lines += [f"- {c}" for c in p["constraints"]]
    if p.get("checks"):
        lines.append("Checks (cwd = your work root): " + "; ".join(p["checks"]))
    if p.get("outputs"):
        lines.append("Declared outputs (relative to your work root): " + ", ".join(p["outputs"]))
    if kind == "check":
        lines += ["This is a checking task: the work root holds staged work. Run the checks, change no file, and write",
                  'your verdict as JSON {"result": "pass"} or {"result": "fail"} to {VERDICT}.']
    lines.append("This is a qualification exam: do not run dureh commands, do not commit, do not push.")
    return "\n".join(lines) + "\n"


def _eligible(site: Site, tier: str) -> list[dict]:
    from .paths import target_root

    led = Ledger(site)
    routing = site.routing()
    check_tier = tier in _check_tiers(site, routing)
    events = led.events()
    by_wi: dict[str, list[dict]] = {}
    for e in events:
        if e.get("wi"):
            by_wi.setdefault(e["wi"], []).append(e)
    verify = {}
    for r in led.load("run"):
        if r["wi"] and r["payload"].get("run_type") == "verify" and r["payload"].get("status") in ("pass", "fail"):
            verify.setdefault(r["wi"], []).append(r)
    out = []
    for w in led.work_items():
        if w["status"] != "promoted":
            continue
        rec = led.get("work-item", w["id"])
        p = rec["payload"]
        role = site.org.get(p["role"])
        root = target_root(site, p["target"])
        if role is None or role.is_manager or root is None or not (root / ".git").exists() or not p.get("checks"):
            continue
        evs = by_wi.get(w["id"], [])
        prepared = [e["payload"] for e in evs if e["type"] == "dispatch.prepared" and not e["payload"].get("checking")]
        runs_ = verify.get(w["id"], [])
        rework = sum(1 for e in evs if e["type"] == "rework.requested")
        promoted_ts = max((e["ts"] for e in evs if e["type"] == "wi.promoted"), default="")
        changed = sorted({x for r in runs_ for x in r["payload"].get("changed") or []})
        if _protected(site, p["target"], changed):
            continue
        base = next((x.get("base") for x in reversed(prepared) if x.get("base")), None)
        if _tier_of_dispatch(site, routing, rec, prepared[-1] if prepared else None) == tier and \
                _commit_exists(root, base) and any(r["payload"]["status"] == "pass" for r in runs_):
            out.append({"wi": w["id"], "kind": "build", "role": p["role"], "rec": rec, "commit": base,
                        "expected": "pass", "rework": rework, "ts": promoted_ts})
        if check_tier:
            for r in runs_:
                head = r["payload"].get("head")
                if _commit_exists(root, head):
                    out.append({"wi": w["id"], "kind": "check", "role": r["actor"], "rec": rec, "commit": head,
                                "expected": r["payload"]["status"], "rework": int(r["payload"]["status"] == "fail"),
                                "ts": r["ts"], "verify_run": r["id"]})
    return sorted(out, key=lambda c: c["ts"], reverse=True)


def _pick(cands: list[dict], count: int) -> list[dict]:
    """Newest first; the newest candidate that went through rework (or a known failed check) is always included."""
    picked = []
    hard = next((c for c in cands if c["rework"]), None)
    if hard:
        picked.append(hard)
    for c in cands:
        if len(picked) >= count:
            break
        if c is not hard and not any(x["wi"] == c["wi"] and x["kind"] == c["kind"] for x in picked):
            picked.append(c)
    return picked


def _sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def build(site: Site, actor: str, tier: str, count: int = DEFAULT_TASKS) -> dict:
    _require_desk(site, actor)
    site.model_for_tier(tier)
    if not MIN_TASKS <= count <= MAX_TASKS:
        fail(f"--count is {MIN_TASKS} to {MAX_TASKS} tasks")
    cands = _eligible(site, tier)
    picked = _pick(cands, count)
    if len(picked) < MIN_TASKS:
        return _build_fixture(site, actor, tier, count, len(cands))
    tasks = []
    for i, c in enumerate(picked, 1):
        p = c["rec"]["payload"]
        task = {"id": f"t{i}-{c['wi'].lower()}-{c['kind']}", "wi": c["wi"], "kind": c["kind"], "role": c["role"],
                "target": p["target"], "commit": c["commit"], "checks": list(p.get("checks") or []),
                "outputs": list(p.get("outputs") or []), "expected": c["expected"], "rework": c["rework"],
                "split": "holdout", "brief": _build_brief(site, c["rec"], c["kind"])}
        if c.get("verify_run"):
            task["verify_run"] = c["verify_run"]
        task["brief_sha256"] = hashlib.sha256(task["brief"].encode("utf-8")).hexdigest()
        tasks.append(task)
    set_id = f"set-{utc_compact()}"
    sha = _sha(tasks)
    d = exams_root(site, tier) / set_id
    d.mkdir(parents=True)
    meta = {"id": set_id, "tier": tier, "created": utc_now(), "by": actor, "source": "replay", "sha256": sha,
            "tasks": [t["id"] for t in tasks], "eligible": len(cands),
            "rule": "promoted WIs of the tier, newest first, the newest with rework included; split holdout"}
    for t in tasks:
        (d / f"{t['id']}.json").write_text(dumps(t), encoding="utf-8")
    (d / "set.json").write_text(dumps(meta), encoding="utf-8")
    ev = Ledger(site).event("exam.built", actor, None, {"tier": tier, "exam_set": set_id, "sha256": sha,
                                                        "path": str(d.relative_to(site.root)),
                                                        "tasks": [{"id": t["id"], "wi": t["wi"], "kind": t["kind"],
                                                                   "expected": t["expected"], "rework": t["rework"]}
                                                                  for t in tasks]})
    return {**meta, "path": str(d), "event": ev["id"],
            "picked": [{"task": t["id"], "wi": t["wi"], "kind": t["kind"], "commit": t["commit"][:12],
                        "expected": t["expected"], "rework": t["rework"]} for t in tasks]}


# ------------------------------------------------------------------ fallback: harness and package example tasks
def _tier_roles(site: Site, tier: str) -> list[str]:
    return sorted(r.name for r in site.org.roles.values() if r.kind != "desk" and r.tier == tier)


def _fixture_root(site: Site, source: str) -> Path:
    from .org import HARNESS_ROOT

    return HARNESS_ROOT if source == "harness" else site.startup


def _fixture_candidates(site: Site, tier: str) -> list[dict]:
    """Harness tasks (`org/exams/<role>/`) and the package's `lessons.evals` tasks of the roles on this tier that a
    deterministic score can use: a task.yaml with an id and at least one check."""
    from . import yamlsub
    from .exams import tasks as role_tasks
    from .org import HARNESS_ROOT

    out = []
    for role in _tier_roles(site, tier):
        for d in role_tasks(role, site):
            try:
                spec = yamlsub.load_file(d / "task.yaml")
            except (DurehError, OSError, ValueError):
                continue
            checks = spec.get("checks") if isinstance(spec, dict) else None
            if not spec.get("id") or not isinstance(checks, list) or not checks \
                    or not all(isinstance(c, str) for c in checks):
                continue
            try:
                rel, source = d.relative_to(HARNESS_ROOT).as_posix(), "harness"
            except ValueError:
                rel, source = d.relative_to(site.startup).as_posix(), "package"
            out.append({"role": role, "dir": d, "rel": rel, "source": source, "spec": spec})
    return out


def _fixture_hash(d: Path) -> str:
    from .exams import _tree_hashes

    return _sha(_tree_hashes(d))


def examinable(site: Site, tier: str, eligible: int | None = None) -> tuple[bool, str]:
    """Whether an exam set can be built for the tier: at least MIN_TASKS eligible promoted work items (replay), else
    at least MIN_TASKS harness/package example tasks of its roles (fixture). The reason names both counts."""
    n = len(_eligible(site, tier)) if eligible is None else eligible
    if n >= MIN_TASKS:
        return True, f"{n} eligible promoted work items (replay)"
    fx = _fixture_candidates(site, tier)
    if len(fx) >= MIN_TASKS:
        return True, f"{n} eligible promoted work item(s), so {len(fx)} harness/package example tasks (fixture)"
    roles = _tier_roles(site, tier)
    return False, (f"{n} eligible promoted work item(s) (promoted, declared checks, a recorded verify run, a known"
                   f" commit, no protected path) and {len(fx)} harness/package example task(s) for its roles"
                   f" ({', '.join(roles) or 'none'}); an exam needs at least {MIN_TASKS} of either")


def _fixture_brief(spec: dict) -> str:
    lines = [f"Exam task (example task {spec['id']}, role {spec.get('role')})", "Work only in: {WORKSPACE}",
             "Task:", str(spec.get("prompt") or "").strip()]
    if spec.get("required_outputs"):
        lines.append("Required outputs (relative to your work root): " + ", ".join(spec["required_outputs"]))
    if spec.get("forbidden_writes"):
        lines.append("Never write: " + ", ".join(spec["forbidden_writes"]))
    lines.append("Checks (cwd = your work root): " + "; ".join(spec["checks"]))
    lines.append("This is a qualification exam: do not run dureh commands, do not commit, do not push.")
    return "\n".join(lines) + "\n"


def _build_fixture(site: Site, actor: str, tier: str, count: int, eligible: int) -> dict:
    """Proposal section 2: a tier with fewer than MIN_TASKS eligible items falls back to the harness and package
    example tasks of its roles; with fewer than MIN_TASKS of those too, the tier is not examinable (refused)."""
    ok, why = examinable(site, tier, eligible)
    if not ok:
        fail(f"tier {tier} is not examinable: {why}. Nothing was written; until then the tier keeps its waiver")
    tasks = []
    for i, c in enumerate(_fixture_candidates(site, tier)[:count], 1):
        spec = c["spec"]
        task = {"id": f"t{i}-{spec['id']}-fixture", "wi": None, "kind": "fixture", "role": c["role"],
                "source": c["source"], "path": c["rel"], "fixture_sha256": _fixture_hash(c["dir"]),
                "checks": list(spec["checks"]), "outputs": list(spec.get("required_outputs") or []),
                "forbidden_writes": list(spec.get("forbidden_writes") or []), "expected": "pass", "rework": 0,
                "split": spec.get("split") or "example", "brief": _fixture_brief(spec)}
        task["brief_sha256"] = hashlib.sha256(task["brief"].encode("utf-8")).hexdigest()
        tasks.append(task)
    set_id = f"set-{utc_compact()}"
    sha = _sha(tasks)
    d = exams_root(site, tier) / set_id
    d.mkdir(parents=True)
    meta = {"id": set_id, "tier": tier, "created": utc_now(), "by": actor, "source": "fixture", "sha256": sha,
            "tasks": [t["id"] for t in tasks], "eligible": eligible,
            "rule": (f"fewer than {MIN_TASKS} eligible promoted items: the harness (org/exams/<role>/) and package"
                     " (lessons.evals) example tasks of the tier's roles; rebuild from replay at re-qualification")}
    for t in tasks:
        (d / f"{t['id']}.json").write_text(dumps(t), encoding="utf-8")
    (d / "set.json").write_text(dumps(meta), encoding="utf-8")
    ev = Ledger(site).event("exam.built", actor, None, {"tier": tier, "exam_set": set_id, "sha256": sha,
                                                        "source": "fixture", "path": str(d.relative_to(site.root)),
                                                        "tasks": [{"id": t["id"], "kind": "fixture", "role": t["role"],
                                                                   "path": f"{t['source']}:{t['path']}"}
                                                                  for t in tasks]})
    return {**meta, "path": str(d), "event": ev["id"],
            "picked": [{"task": t["id"], "kind": "fixture", "role": t["role"], "path": f"{t['source']}:{t['path']}"}
                       for t in tasks]}


def load_set(site: Site, tier: str, set_id: str | None = None) -> dict:
    root = exams_root(site, tier)
    sets = sorted(p.parent.name for p in root.glob("set-*/set.json")) if root.is_dir() else []
    if not sets:
        fail(f"tier {tier} has no exam set; build one with `dureh exam build --tier {tier}`")
    sid = set_id or sets[-1]
    if sid not in sets:
        fail(f"tier {tier} has no exam set {sid} (sets: {', '.join(sets)})")
    meta = json.loads((root / sid / "set.json").read_text(encoding="utf-8"))
    meta["task_specs"] = [json.loads((root / sid / f"{t}.json").read_text(encoding="utf-8")) for t in meta["tasks"]]
    if _sha(meta["task_specs"]) != meta["sha256"]:
        fail(f"exam set {tier}/{sid} does not match its sha256 (a task file changed); build a new set")
    return meta


# ------------------------------------------------------------------ run
def _exam_runs(site: Site) -> list[dict]:
    return [r for r in Ledger(site).load("run") if r["type"] == EXAM_RUN]


def _scored(site: Site, run_id: str) -> dict | None:
    return next((r for r in runs(site) if r["payload"].get("exam_run") == run_id), None)


def digest(text: str) -> str:
    """The digest of an exam brief the spawn hook compares (16 hex of sha256, surrounding whitespace ignored)."""
    return hashlib.sha256((text.strip() + "\n").encode("utf-8")).hexdigest()[:16]


def _deterministic(t: dict) -> bool:
    return bool(t.get("deterministic", t.get("kind") in DETERMINISTIC_KINDS))


def _prepare(site: Site, t: dict, ws: Path) -> None:
    """The task workspace: a detached worktree at the frozen commit, or a copy of an example task's fixture."""
    from .paths import target_root

    if t["kind"] == "fixture":
        d = _fixture_root(site, t["source"]) / t["path"]
        if not (d / "task.yaml").is_file() or _fixture_hash(d) != t["fixture_sha256"]:
            fail(f"example task {t['source']}:{t['path']} changed or is gone since the set was built; build a new set")
        if (d / "fixture").is_dir():
            shutil.copytree(d / "fixture", ws, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            ws.mkdir()
        return
    git(["worktree", "add", "--detach", str(ws), t["commit"]], target_root(site, t["target"]))


def _remove(site: Site, t: dict, ws: Path) -> None:
    from .paths import target_root

    if t["kind"] == "fixture":
        shutil.rmtree(ws, ignore_errors=True)
    else:
        git(["worktree", "remove", "--force", str(ws)], target_root(site, t["target"]), check=False)


def _brief_text(run_id: str, t: dict, tier: str, model: str, base: Path, attempt: int = 1) -> str:
    header = f"{HEADER} {run_id} task={t['id']} tier={tier} model={model}" + (f" attempt={attempt}" if attempt > 1 else "")
    return header + "\n\n" + t["brief"].replace("{WORKSPACE}", str(base / t["id"])).replace(
        "{VERDICT}", str(base / f"{t['id']}.{VERDICT}"))


def run(site: Site, actor: str, tier: str, model: str, set_id: str | None = None) -> dict:
    _require_desk(site, actor)
    site.model_for_tier(tier)
    if not model or ":" not in model:
        fail("--model is the full provider:model string, for example anthropic:claude-sonnet-5")
    open_runs = [r["id"] for r in _exam_runs(site) if not _scored(site, r["id"])]
    if open_runs:
        fail(f"exam run {open_runs[-1]} is not scored yet; score it first (`dureh exam score --run {open_runs[-1]}`):"
             " one exam run per site at a time")
    meta = load_set(site, tier, set_id)
    led = Ledger(site)
    run_id = led._new_id("run")
    base = site.root / "scratch" / "exams" / run_id
    base.mkdir(parents=True)
    out = []
    for t in meta["task_specs"]:
        ws = base / t["id"]
        _prepare(site, t, ws)
        text = _brief_text(run_id, t, tier, model, base)
        brief = base / f"{t['id']}.brief.md"
        brief.write_text(text, encoding="utf-8")
        out.append({"task": t["id"], "kind": t["kind"], "role": t["role"], "workspace": str(ws), "brief": str(brief),
                    "verdict": str(base / f"{t['id']}.{VERDICT}"), "brief_sha256": digest(text)})
    led.append("run", EXAM_RUN, actor, None, {
        "run_type": "exam", "status": "submitted", "tier": tier, "model": model,
        "exam_set": {"id": meta["id"], "sha256": meta["sha256"], "path": f"state/exams/{tier}/{meta['id']}",
                     "source": meta.get("source", "replay")},
        "tasks": out, "scratch": str(base)}, record_id=run_id)
    return {"run": run_id, "tier": tier, "model": model, "exam_set": meta["id"], "tasks": out,
            "next": _spawn_hint(model, run_id)}


def _spawn_hint(model: str, run_id: str) -> str:
    return (f"spawn one agent per brief with model={model.partition(':')[2]} (the exact id, not a family alias;"
            f" subagent_type = the task's role, the brief file's text unchanged as the whole prompt); when all have"
            f" ended: `dureh exam score --run {run_id}`")


# ------------------------------------------------------------------ spawn hook (phase 2)
def parse_header(text: str) -> dict | None:
    """The DUREH-EXAM line of a prompt or transcript head: {run, task, tier, model, attempt}, or None."""
    m = HEADER_RE.search(text or "")
    if not m:
        return None
    return {"run": m.group(1), "task": m.group(2), "tier": m.group(3), "model": m.group(4),
            "attempt": int(m.group(5) or 1)}


def _rerun_event(site: Site, run_id: str) -> dict | None:
    return next((e for e in reversed(Ledger(site).events()) if e["type"] == "exam.rerun"
                 and e["payload"].get("exam_run") == run_id), None)


def slots(site: Site, rec: dict) -> dict:
    """{(task, attempt): {role, brief_sha256, workspace, verdict}} of an exam run: every task's first attempt and the
    one rerun of each task `exam score` sent back."""
    out = {(t["task"], 1): t for t in rec["payload"].get("tasks") or []}
    ev = _rerun_event(site, rec["id"])
    for t in (ev["payload"].get("tasks") if ev else None) or []:
        out[(t["task"], 2)] = t
    return out


def spawned(site: Site, run_id: str) -> list[dict]:
    return [e for e in Ledger(site).events() if e["type"] == "exam.spawned" and e["payload"].get("exam_run") == run_id]


def admit_spawn(site: Site, actor: str, target: str, prompt: str) -> tuple[str | None, dict | None]:
    """The spawn hook's checks for an exam brief (the model check is the hook's): (refusal, None) or (None, slot)."""
    h = parse_header(prompt)
    if h is None:
        return "no DUREH-EXAM line in the prompt", None
    if actor != "desk":
        return "exam briefs are spawned by the desk", None
    rec = _get_run(site, h["run"])
    if rec is None or rec["type"] != EXAM_RUN:
        return f"{h['run']} is not an exam run (`dureh exam run` prints its briefs)", None
    if _scored(site, h["run"]):
        return f"exam run {h['run']} is already scored; start a new one with `dureh exam run`", None
    rp = rec["payload"]
    if (h["tier"], h["model"]) != (rp["tier"], rp["model"]):
        return (f"the brief names tier {h['tier']} model {h['model']}, but exam run {h['run']} is tier {rp['tier']}"
                f" model {rp['model']}"), None
    slot = slots(site, rec).get((h["task"], h["attempt"]))
    if slot is None:
        return (f"exam run {h['run']} has no task {h['task']}" + (f" attempt {h['attempt']} (a rerun is prepared only"
                " by `dureh exam score` for a failing task)" if h["attempt"] > 1 else "")), None
    if slot["role"] != target:
        return f"exam task {h['task']} is for role {slot['role']}, not {target}", None
    if digest(prompt) != slot["brief_sha256"]:
        return (f"the prompt is not the brief of exam task {h['task']} (digest {digest(prompt)}, recorded"
                f" {slot['brief_sha256']}): pass the brief file's text unchanged"), None
    if any(e["payload"].get("task") == h["task"] and e["payload"].get("attempt") == h["attempt"]
           for e in spawned(site, h["run"])):
        return (f"exam task {h['task']} attempt {h['attempt']} was already spawned; a failing task is rerun once"
                " through `dureh exam score`"), None
    return None, {**slot, "exam_run": h["run"], "attempt": h["attempt"], "model": rp["model"], "tier": rp["tier"]}


def agent_binding(led: Ledger, agent_id: str | None) -> dict | None:
    if not agent_id:
        return None
    for ev in reversed(led.load("event")):
        if ev["type"] == "exam.agent.started" and ev["payload"].get("agent_id") == agent_id:
            return {**ev["payload"], "role": ev["actor"]}
    return None


def bind_agent(led: Ledger, role: str, agent_id: str, header: dict, runtime_pid: int | None = None) -> dict | str:
    """Bind an agent whose prompt is an exam brief to its recorded exam spawn: the binding, or a refusal text."""
    with led.lock():
        b = agent_binding(led, agent_id)
        if b:
            return b
        evs = led.events()
        taken = {e["refs"][0] for e in evs if e["type"] == "exam.agent.started" and e["refs"]}
        sp = [e for e in evs if e["type"] == "exam.spawned" and e["id"] not in taken
              and e["payload"].get("exam_run") == header["run"] and e["payload"].get("task") == header["task"]
              and e["payload"].get("attempt") == header["attempt"] and e["payload"].get("role") == role]
        if not sp:
            return (f"this agent's exam brief ({header['run']} task {header['task']} attempt {header['attempt']}) has no"
                    " unbound recorded spawn; the desk spawns exam briefs through the Agent tool")
        p = sp[-1]["payload"]
        payload = {"agent_id": agent_id, "exam_run": p["exam_run"], "task": p["task"], "attempt": p["attempt"],
                   "workspace": p["workspace"], "verdict": p.get("verdict"), "model": p.get("model"),
                   "runtime_pid": runtime_pid}
        led.event("exam.agent.started", role, None, payload, [sp[-1]["id"]])
        return {**payload, "role": role}


def write_refusal(binding: dict, path: Path, cwd: Path) -> str | None:
    """An exam agent writes only inside its task workspace (never its .git) and its verdict file."""
    p = Path(os.path.expanduser(str(path)))
    p = Path(os.path.realpath(p if p.is_absolute() else cwd / p))
    ws = Path(os.path.realpath(binding["workspace"]))
    if binding.get("verdict") and p == Path(os.path.realpath(binding["verdict"])):
        return None
    if p != ws and ws in p.parents and p.relative_to(ws).parts[0] != ".git":
        return None
    return (f"an exam agent writes only in its exam workspace {binding['workspace']}"
            + (f" and its verdict file {binding['verdict']}" if binding.get("verdict") else ""))


# ------------------------------------------------------------------ score
def _changed(ws: Path, commit: str) -> list[str]:
    tracked = git(["diff", "--name-only", commit], ws, check=False).splitlines()
    untracked = git(["ls-files", "--others", "--exclude-standard"], ws, check=False).splitlines()
    return sorted({x for x in tracked + untracked if x})


def _verdict(path: Path) -> str | None:
    try:
        v = json.loads(path.read_text(encoding="utf-8")).get("result")
    except (OSError, ValueError, AttributeError):
        return None
    return v if v in ("pass", "fail") else None


def _checks_local(site: Site, t: dict, ws: Path, troot: Path) -> list[dict]:
    from .workflow import _run_checks_local, checks_env_plan

    plan = checks_env_plan(site, t["wi"], ws, troot)
    timeout = int((site.config.get("checks") or {}).get("local_timeout") or 600)
    return _run_checks_local(site, t["wi"], ws, troot, t["checks"], timeout, plan)


def _checks_slurm(site: Site, run_id: str, items: list[tuple[dict, Path, Path]], tag: str = "") -> tuple[dict, dict]:
    """Every task's checks in ONE scheduler job: one generated verify script per task (the `verify --wi` script),
    run in turn by a wrapper. Returns ({task: results}, job)."""
    from . import cluster, ops
    from .workflow import _checks_script, _read_check_results, checks_env_plan, checks_sbatch

    logs = site.root / "logs" / "exams" / run_id
    if tag:
        logs = logs / tag
    logs.mkdir(parents=True, exist_ok=True)
    wrapper = ["#!/bin/bash", f"# DUREH exam {run_id}: every task's declared checks (generated)"]
    for t, ws, troot in items:
        prefix = logs / t["id"]
        script = Path(f"{prefix}.sh")
        script.write_text(_checks_script(t["wi"], ws, troot, site.root, prefix, t["checks"],
                                         checks_env_plan(site, t["wi"], ws, troot)), encoding="utf-8")
        script.chmod(0o755)
        wrapper.append(f"bash {shlex.quote(str(script))}")
    wrapper.append("exit 0")
    wscript = logs / "exam.sh"
    wscript.write_text("\n".join(wrapper) + "\n", encoding="utf-8")
    wscript.chmod(0o755)
    cmd, wall = checks_sbatch(site, f"{ops.job_prefix(site)}-exam", site.root, logs / "exam", wscript)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=site.env())
    if proc.returncode != 0:
        fail(f"sbatch failed for the exam checks job: {proc.stderr.strip()[-1500:]}; nothing was recorded"
             " (`exam score --local` runs them here)")
    job_id = proc.stdout.strip().split(";")[0]
    limit = cluster.parse_time(wall)
    state, raw, final = ops.wait_job(site, job_id, int((limit.total_seconds() if limit else 3600) + 900), 15)
    if not final:
        subprocess.run(["scancel", job_id], capture_output=True, text=True, timeout=60, env=site.env())
    out = {t["id"]: _read_check_results(str(logs / t["id"]), t["checks"], job_id, state) for t, _, _ in items}
    return out, {"job_id": job_id, "state": state, "time": wall}


def _usage_units(site: Site, run_id: str, model: str, task_ids: list[str] | None = None) -> dict:
    """Usage units of the run's agents (agent run records naming `exam_run`, recorded by the SubagentStop hook):
    the total and per task. 'unmeasured' unless every task has an agent and every agent's usage was measured."""
    from . import claude_usage
    from .workday import budget

    agents = [r for r in Ledger(site).load("run") if r["payload"].get("run_type") == "agent"
              and r["payload"].get("exam_run") == run_id]
    usage = [r["payload"].get("usage") or {} for r in agents]
    covered = {r["payload"].get("exam_task") for r in agents}
    missing = [t for t in task_ids or [] if t not in covered]
    if not agents or missing or not all(u.get("coverage") in ("transcript", "runtime") for u in usage):
        why = ("no agent run names this exam run" if not agents else
               f"no agent recorded for {', '.join(missing)}" if missing else "an agent's usage was not measured")
        return {"units": "unmeasured", "agents": len(agents), "per_task": {}, "why": why}
    weights = budget(site).get("model_weights") or {}
    per: dict[str, float] = {}
    for r, u in zip(agents, usage):
        t = r["payload"].get("exam_task") or "?"
        per[t] = per.get(t, 0.0) + claude_usage.units(u, model, weights)
    return {"units": round(sum(per.values()), 4), "agents": len(agents),
            "per_task": {t: round(v, 4) for t, v in per.items()}}


def _score_rows(site: Site, run_id: str, items: list[tuple[dict, Path, Path | None]], base: Path, runner: str,
                tag: str = "") -> tuple[list[dict], dict | None]:
    """Score the given tasks: declared checks (one scheduler job or here) of build and check tasks; example tasks
    (fixture) through the role-level scorer, here (seconds, no data)."""
    from .exams import score_workspace

    work = [i for i in items if i[0]["kind"] != "fixture"]
    job, check_results = None, {}
    if work and runner == "slurm":
        check_results, job = _checks_slurm(site, run_id, work, tag)
    elif work:
        check_results = {t["id"]: _checks_local(site, t, ws, troot) for t, ws, troot in work}
    rows = []
    for t, ws, troot in items:
        if t["kind"] == "fixture":
            r = score_workspace(_fixture_root(site, t["source"]) / t["path"], ws)
            row = {"task": t["id"], "wi": None, "kind": "fixture", "expected": "pass", "rerun": False,
                   "checks": r["checks"], "changed": r["changed"], "forbidden_writes": r["forbidden_writes"],
                   "missing_outputs": r["missing_outputs"], "hard_failure": r["hard_failure"], "result": r["result"]}
            if r["forbidden_writes"]:
                row["why"] = "wrote a forbidden path"
            rows.append(row)
            continue
        checks = check_results[t["id"]]
        changed = _changed(ws, t["commit"])
        row = {"task": t["id"], "wi": t["wi"], "kind": t["kind"], "expected": t["expected"], "rerun": False,
               "checks": [{k: c.get(k) for k in ("command", "exit", "seconds")} for c in checks], "changed": changed}
        if t["kind"] == "check":
            verdict = _verdict(base / f"{t['id']}.{VERDICT}")
            row.update(verdict=verdict, hard_failure=bool(changed),
                       why="edited files in a checking task" if changed else
                       ("no verdict" if verdict is None else f"verdict {verdict}, recorded {t['expected']}"))
            row["result"] = "pass" if verdict == t["expected"] and not changed else "fail"
        else:
            violations = [x for x in changed if not site.org.scope_allows(t["role"], f"{t['target']}/{x}")]
            missing = [o for o in t["outputs"] if not (ws / o).exists()]
            ok = all(c["exit"] == 0 for c in checks) and not missing and not violations and bool(changed)
            row.update(violations=violations, missing_outputs=missing, hard_failure=bool(violations),
                       result="pass" if ok else "fail")
            if not changed:
                row["why"] = "no change in the workspace"
        rows.append(row)
    return rows, job


def _reset(site: Site, t: dict, ws: Path, base: Path) -> None:
    """Back to the task's starting state for its rerun: the frozen commit (or a fresh fixture copy), no verdict."""
    (base / f"{t['id']}.{VERDICT}").unlink(missing_ok=True)
    if t["kind"] == "fixture":
        shutil.rmtree(ws, ignore_errors=True)
        _prepare(site, t, ws)
    else:
        git(["checkout", "-q", "--force", "--detach", t["commit"]], ws)
        git(["clean", "-qfdx"], ws)


def _send_rerun(site: Site, actor: str, rec: dict, items: list, rows: list[dict], retry: list[str], runner: str,
                job: dict | None) -> dict:
    """Proposal section 2: a deterministic task that fails (without a hard failure) is rerun once, to separate noise
    from inability. Its workspace is reset, an `attempt=2` brief written and `exam.rerun` recorded (with the first
    scores); the next `exam score` scores the reruns and records the qualification with both attempts."""
    rp = rec["payload"]
    base = Path(rp["scratch"])
    out = []
    for t, ws, _ in items:
        if t["id"] not in retry:
            continue
        _reset(site, t, ws, base)
        text = _brief_text(rec["id"], t, rp["tier"], rp["model"], base, attempt=2)
        brief = base / f"{t['id']}.attempt-2.brief.md"
        brief.write_text(text, encoding="utf-8")
        out.append({"task": t["id"], "kind": t["kind"], "role": t["role"], "workspace": str(ws), "brief": str(brief),
                    "verdict": str(base / f"{t['id']}.{VERDICT}"), "brief_sha256": digest(text)})
    k, n = sum(1 for r in rows if r["result"] == "pass"), len(rows)
    ev = Ledger(site).event("exam.rerun", actor, None, {"exam_run": rec["id"], "tier": rp["tier"], "model": rp["model"],
                                                        "first": rows, "first_score": f"{k}/{n}", "tasks": out,
                                                        "runner": runner, **({"slurm_job": job} if job else {})},
                            [rec["id"]])
    return {"run": rec["id"], "status": "rerun", "tier": rp["tier"], "model": rp["model"], "first_score": f"{k}/{n}",
            "event": ev["id"], "tasks": out,
            "next": (f"rerun once: spawn each attempt-2 brief as before (model={rp['model'].partition(':')[2]}); when"
                     f" all have ended: `dureh exam score --run {rec['id']}` (or `--no-rerun` to score the first"
                     " attempts now)")}


def score(site: Site, actor: str, run_id: str, local: bool = False, today: date | None = None,
          rerun: bool = True) -> dict:
    from .paths import target_root
    from .util import harness_label
    from .workflow import checks_runner

    _require_desk(site, actor)
    led = Ledger(site)
    rec = _get_run(site, run_id)
    if rec is None or rec["type"] != EXAM_RUN:
        fail(f"{run_id} is not an exam run (`dureh exam run` prints its id)")
    done = _scored(site, run_id)
    if done:
        fail(f"exam run {run_id} was already scored as {done['id']} ({done['payload']['status']})")
    rp = rec["payload"]
    tier, model = rp["tier"], rp["model"]
    meta = load_set(site, tier, rp["exam_set"]["id"])
    if meta["sha256"] != rp["exam_set"]["sha256"]:
        fail(f"exam set {meta['id']} changed since run {run_id}")
    base = Path(rp["scratch"])
    items = [(t, base / t["id"], None if t["kind"] == "fixture" else target_root(site, t["target"]))
             for t in meta["task_specs"]]
    pending = _rerun_event(site, run_id)
    again = {t["task"] for t in pending["payload"]["tasks"]} if pending else set()
    todo = [] if pending and not rerun else [i for i in items if not pending or i[0]["id"] in again]
    for t, ws, _ in todo:
        if not ws.is_dir():
            fail(f"workspace {ws} of task {t['id']} is gone; nothing was recorded")
    runner = "local" if local or checks_runner(site) == "local" else "slurm"
    new, job = _score_rows(site, run_id, todo, base, runner, "attempt-2" if pending else "")
    if pending:
        first = {r["task"]: r for r in pending["payload"]["first"]}
        scored = {r["task"]: r for r in new}
        tasks = []
        for t, _, _ in items:
            f = first[t["id"]]
            if t["id"] in scored:
                r = {**scored[t["id"]], "rerun": True, "attempt": 2,
                     "first": {k: f.get(k) for k in ("result", "why", "checks", "missing_outputs") if k in f}}
            elif t["id"] in again:
                r = {**f, "rerun": False, "why": "rerun not scored (--no-rerun)"}
            else:
                r = f
            tasks.append(r)
        job = job or pending["payload"].get("slurm_job")
    else:
        tasks = new
        retry = [r["task"] for r, (t, _, _) in zip(new, items)
                 if r["result"] == "fail" and not r["hard_failure"] and _deterministic(t)]
        if rerun and retry:
            return _send_rerun(site, actor, rec, items, new, retry, runner, job)
    n, k = len(tasks), sum(1 for x in tasks if x["result"] == "pass")
    need = math.ceil(PASS_FRACTION * n)
    hard = [x["task"] for x in tasks if x["hard_failure"]]
    incumbent = _incumbent(site, tier, meta["sha256"], model)
    reasons = []
    if hard:
        reasons.append(f"hard failure in {', '.join(hard)}")
    if k < need:
        reasons.append(f"{k}/{n} below the pass rule ({need}/{n})")
    if incumbent and incumbent.get("passed") is not None and k < incumbent["passed"] - 1:
        reasons.append(f"worse than the incumbent {incumbent['model']} ({incumbent['score']}) by more than one task")
    status = "fail" if reasons else "pass"
    today = _today(today)
    cost = _usage_units(site, run_id, model, [t["id"] for t, _, _ in items])
    units = cost["units"]
    for x in tasks:
        x["units"] = cost["per_task"].get(x["task"], "unmeasured")
    payload = {"run_type": RUN_TYPE, "status": status, "tier": tier, "model": model, "exam_run": run_id,
               "harness_release": harness_label(site.harness_root),
               "exam_set": {**rp["exam_set"], "source": meta.get("source", "replay")},
               "tasks": tasks, "score": f"{k}/{n}", "passed": k, "total": n, "pass_rule": f"{need}/{n}",
               "incumbent": incumbent, "runner": runner, "units": units, "agents": cost["agents"],
               "units_per_task": round(units / n, 4) if isinstance(units, (int, float)) else "unmeasured",
               "reruns": sorted(again), "reasons": reasons}
    if cost.get("why"):
        payload["units_why"] = cost["why"]
    if job:
        payload["slurm_job"] = job
    if status == "pass":
        payload["qualified_on"] = today.isoformat()
        payload["expires"] = (today + timedelta(days=QUAL_DAYS)).isoformat()
    out = led.append("run", RUN_TYPE, actor, None, payload, [run_id])
    for t, ws, _ in items:  # the workspaces go; the record keeps what was scored
        _remove(site, t, ws)
    res = {"run": out["id"], **payload}
    if status == "pass":
        res["config_entry"] = {"model": model, "qualification": out["id"], "qualified_on": payload["qualified_on"],
                               "expires": payload["expires"],
                               "note": f"exam {payload['score']} on {meta.get('source', 'replay')} set"
                                       f" {meta['sha256'][:8]}"}
        same = (site.config.get("models") or {}).get(tier, {}).get("model") == model
        res["apply"] = (f"models.{tier} <- config_entry: " + ("class B (same model, stricter evidence), the desk"
                                                              if same else "class C (a model change): a board Decision"))
    return res


def _incumbent(site: Site, tier: str, sha: str, model: str) -> dict | None:
    inc = (site.config.get("models") or {}).get(tier, {}).get("model")
    if not inc or inc == model:
        return None
    rows = [r for r in runs(site) if r["payload"].get("tier") == tier and r["payload"].get("model") == inc
            and (r["payload"].get("exam_set") or {}).get("sha256") == sha]
    if not rows:
        return {"model": inc, "score": None, "passed": None, "note": "no incumbent score on this exam set"}
    p = rows[-1]["payload"]
    return {"model": inc, "score": p.get("score"), "passed": p.get("passed"), "run": rows[-1]["id"]}


# ------------------------------------------------------------------ compare and plan
def _rank(site: Site, model: str) -> int:
    c = candidates(site)
    return c.index(model) if model in c else len(c)


def _latest_set(site: Site, tier: str) -> str | None:
    root = exams_root(site, tier)
    sets = sorted(p.parent.name for p in root.glob("set-*/set.json")) if root.is_dir() else []
    return sets[-1] if sets else None


def compare(site: Site, tier: str | None = None, today: date | None = None) -> dict:
    today = _today(today)
    tiers = [tier] if tier else site.model_tiers()
    allruns = runs(site)
    out = {}
    for t in tiers:
        inc = (site.config.get("models") or {}).get(t, {}).get("model")
        latest: dict[str, dict] = {}
        for r in allruns:
            if r["payload"].get("tier") == t:
                latest[r["payload"]["model"]] = r
        rows = []
        for m, r in latest.items():
            p = r["payload"]
            rows.append({"model": m, "score": p.get("score"), "passed": p.get("passed"), "total": p.get("total"),
                         "pass": p.get("status") == "pass" and expiry(r) >= today, "status": p.get("status"),
                         "units_per_task": p.get("units_per_task"), "date": r["ts"][:10], "run": r["id"],
                         "exam_set": (p.get("exam_set") or {}).get("id"), "incumbent": m == inc})
        rows.sort(key=lambda x: _rank(site, x["model"]))
        out[t] = {"incumbent": inc, "rows": rows, "recommendation": _recommend(site, t, inc, rows)}
    return {"tiers": out, "rule": f"cheapest passing model (pass = no hard failure and ceil({PASS_FRACTION} x N)"
                                   " tasks), no worse than the incumbent minus one task"}


def _recommend(site: Site, tier: str, inc: str | None, rows: list[dict]) -> dict:
    inc_row = next((r for r in rows if r["model"] == inc), None)
    ok = [r for r in rows if r["pass"]]
    if inc_row and inc_row.get("passed") is not None:
        ok = [r for r in ok if r["model"] == inc or (r.get("passed") or 0) >= inc_row["passed"] - 1]
    if not ok:
        return {"model": None, "action": "none", "why": "no candidate passed on record"}
    measured = all(isinstance(r["units_per_task"], (int, float)) for r in ok)
    key = (lambda r: (r["units_per_task"], _rank(site, r["model"]))) if measured else (lambda r: _rank(site, r["model"]))
    best = min(ok, key=key)
    by = "units per task" if measured else "candidate order (models.candidates, cheapest first; units unmeasured)"
    if best["model"] == inc:
        return {"model": inc, "action": "keep", "run": best["run"], "why": f"the incumbent is the cheapest passing model by {by}"}
    return {"model": best["model"], "action": "switch", "run": best["run"],
            "why": f"cheapest passing model by {by}; a model change is class C (one board Decision per company round)"}


def plan(site: Site, tier: str | None = None, today: date | None = None) -> dict:
    """Per tier, the candidates cheapest first and what each still needs on the tier's newest exam set: climbing stops
    at the first passing candidate; the incumbent is always run as the reference."""
    today = _today(today)
    tiers = [tier] if tier else site.model_tiers()
    allruns = runs(site)
    out = {}
    for t in tiers:
        inc = (site.config.get("models") or {}).get(t, {}).get("model")
        sid = _latest_set(site, t)
        models = candidates(site) + ([inc] if inc and inc not in candidates(site) else [])
        ok, why = (True, None) if sid else examinable(site, t)
        if not ok:  # phase 2: nothing to build a set from; say why instead of `build first`
            out[t] = {"exam_set": None, "incumbent": inc, "examinable": False, "reason": why,
                      "candidates": [{"model": m, "incumbent": m == inc, "state": "not examinable",
                                      "needs_run": False} for m in models],
                      "next": f"not examinable: {why}"}
            continue
        rows, passed_below = [], False
        for m in models:
            rs = [r for r in allruns if r["payload"].get("tier") == t and r["payload"].get("model") == m
                  and (r["payload"].get("exam_set") or {}).get("id") == sid]
            r = rs[-1] if rs else None
            if r is not None and r["payload"]["status"] == "pass" and expiry(r) >= today:
                state = f"passed {r['payload']['score']} ({r['id']})"
                passed_below = passed_below or m != inc
            elif r is not None and r["payload"]["status"] == "pass":
                state = f"expired ({r['id']}): needs a run"
            elif r is not None:
                state = f"failed {r['payload']['score']} ({r['id']})"
            elif m == inc:
                state = "needs a run (incumbent reference)"
            elif passed_below:
                state = "not needed (a cheaper candidate passed)"
            else:
                state = "needs a run"
            rows.append({"model": m, "incumbent": m == inc, "state": state, "needs_run": state.startswith("needs")
                         or state.startswith("expired")})
        out[t] = {"exam_set": sid, "incumbent": inc, "examinable": True, "reason": why, "candidates": rows,
                  "next": None if sid else f"`dureh exam build --tier {t}` first ({why})"}
    return {"tiers": out}


def render_compare(res: dict) -> str:
    lines = [f"{'tier':<16} {'model':<40} {'score':<6} {'pass':<5} {'units/task':<11} date"]
    for t, d in res["tiers"].items():
        for r in d["rows"]:
            u = r["units_per_task"]
            lines.append(f"{t:<16} {r['model'] + (' *' if r['incumbent'] else ''):<40} {str(r['score']):<6}"
                         f" {'yes' if r['pass'] else 'no':<5} {str(u):<11} {r['date']}")
        rec = d["recommendation"]
        lines.append(f"{t:<16} -> {rec['action']}: {rec['model'] or '-'} ({rec['why']})")
    lines.append(f"* incumbent. Rule: {res['rule']}")
    return "\n".join(lines)


def render_plan(res: dict) -> str:
    lines = []
    for t, d in res["tiers"].items():
        lines.append(f"{t}: exam set {d['exam_set'] or 'none'}" + (f"; {d['next']}" if d["next"] else ""))
        for r in d["candidates"]:
            lines.append(f"  {r['model'] + (' *' if r['incumbent'] else '')}: {r['state']}")
    return "\n".join(lines)
