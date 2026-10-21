"""`verify --self`: harness self-test plus site consistency checks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .org import HARNESS_ROOT, Org
from .records import Ledger
from .site import TOOL_NAMES, Site


def compat_ok(site: Site) -> tuple[bool, str]:
    compat = json.loads((HARNESS_ROOT / "compat.json").read_text(encoding="utf-8"))
    version = 1
    if version not in compat.get("record_schema_versions", []):
        return False, f"record schema {version} not in compat.json"
    console = (site.config.get("console") or {}).get("revision")
    entry = compat.get("console", {}).get(str(version), {})
    if console and console not in entry.get("revisions", []):
        return False, f"console {console} is not compatible with record schema {version}"
    return True, f"record schema {version}; console {'none installed' if not console else console}"


def session_checks(site: Site) -> list[str]:
    from .runtimes.claude_code import drift
    from .state import unsaved

    out = []
    led = Ledger(site)
    t0 = time.monotonic()
    info = led.build_index()
    out.append(f"index rebuilt: {info['records']} records in {time.monotonic() - t0:.2f}s at {info['path']}")
    ok, why = compat_ok(site)
    out.append(("compat ok: " if ok else "COMPAT FAIL: ") + why)
    from .util import harness_label

    hc = harness_checks(site)
    out.append(f"harness {harness_label(site.harness_root)} at {site.harness_root}" + (f"; HARNESS MISMATCH: {hc[0]}" if hc else ""))
    pending = unsaved(site)
    out.append(f"unsaved state changes: {len(pending)}" if pending else "state saved")
    d = drift(site)
    out.append("adapter drift: " + "; ".join(d) if d else "adapter files match their generation manifest")
    from .projectgen import project_drift

    pd = project_drift(site)
    out.append("project guidance drift: " + "; ".join(pd) if pd else "project guidance matches its generation manifest")
    return out


def harness_checks(site: Site) -> list[str]:
    """D1: config.json harness.root must be the checkout this command runs from."""
    named = (site.config.get("harness") or {}).get("root")
    running = site.harness_root.resolve()
    if named and Path(named).resolve() != running:
        return [f"config.json names harness {named} but this command runs from {running}; "
                f"run `{running}/dureh --site {site.root} adapter sync` (or `bin/dureh adapter sync --harness {running}`)"]
    return []


def tool_coverage(site: Site | None) -> list[str]:
    """Each harness tool must be referenced by the generated runtime instructions (L-02)."""
    if site is None:
        return []
    corpus = ""
    for p in [site.root / "CLAUDE.md", *(site.root / ".claude" / "agents").glob("*.md")]:
        if p.is_file():
            corpus += p.read_text(encoding="utf-8")
    import re
    missing = [t for t in TOOL_NAMES if not re.search(rf"dureh`? {re.escape(t)}\b|`{re.escape(t)}`|dureh {re.escape(t)}\b", corpus)]
    return [f"tool `{t}` is not referenced by any generated instruction" for t in missing]


MAX_FAILED_TESTS, FAILED_TAIL_LINES, FAILED_LINE_CHARS = 10, 5, 300
_SEP = "=" * 70


def unit_failures(stderr: str) -> list[dict]:
    """Release 71 (UR-20261016-02): each failing or erroring test of a unittest run, `{test, kind, tail}`: the test id
    (module.Class.method), FAIL or ERROR, and the last FAILED_TAIL_LINES lines of its report (each at most
    FAILED_LINE_CHARS characters). Every failure is listed; the caller bounds how many it keeps."""
    import re

    out = []
    for block in stderr.split(_SEP)[1:]:
        lines = block.strip("\n").splitlines()
        if not lines:
            continue
        m = re.match(r"^(FAIL|ERROR): (\S+) \(([^)]+)\)", lines[0])
        if not m:
            continue
        kind, method, where = m.groups()
        test = where if where.endswith("." + method) else f"{where}.{method}"
        body = []
        for line in lines[1:]:
            if line.startswith("-" * 70):
                if body:  # the line under the traceback: the run's summary follows
                    break
                continue
            body.append(line)
        body = [ln.rstrip() for ln in body if ln.strip() and set(ln.strip()) - set("^~")]  # no caret markers
        tail = [ln if len(ln) <= FAILED_LINE_CHARS else ln[:FAILED_LINE_CHARS - 3] + "..."
                for ln in body[-FAILED_TAIL_LINES:]]
        out.append({"test": test, "kind": kind, "tail": tail})
    return out


def run_self(site: Site | None, actor: str = "checker", unit: bool = True, record: bool = True) -> dict:
    start = time.monotonic()
    problems: list[str] = []
    counts = {}
    try:
        Org.load(HARNESS_ROOT)
    except Exception as exc:  # noqa: BLE001 - report any org load failure
        problems.append(f"org definition invalid: {exc}")
    if unit:
        env = {k: v for k, v in os.environ.items() if k != "DUREH_OBSERVE"}
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                              cwd=HARNESS_ROOT, capture_output=True, text=True, timeout=1800, env=env)
        tail = proc.stderr.strip().splitlines()[-3:]
        counts["unit"] = {"exit": proc.returncode, "summary": " | ".join(tail)}
        if proc.returncode != 0:
            problems.append("unit tests failed: " + " | ".join(tail))
            failed = unit_failures(proc.stderr)
            kept = failed[:MAX_FAILED_TESTS]
            for f in kept:  # release 71: name each failing test with the end of its report
                problems.append(f"{f['kind']} {f['test']}: " + " | ".join(f["tail"]))
            if len(failed) > len(kept):
                problems.append(f"... and {len(failed) - len(kept)} more failing test(s)")
            counts["unit"]["failures"] = kept
            counts["unit"]["failed_tests"] = len(failed)
    if site is not None:
        from .install import package_warnings, validate_package
        from .ops import config_errors, config_warnings
        from .util import DurehError

        try:  # release 46: the installed package validates on this harness (schema, roles, protected, labels)
            validate_package(site.startup)
        except DurehError as exc:
            problems.append(f"installed package does not validate: {exc}")
        warnings = package_warnings(site.startup) + config_warnings(site)
        if warnings:
            counts["warnings"] = warnings
        problems += config_errors(site)
        problems += harness_checks(site)
        problems += tool_coverage(site)
        from .runtimes.claude_code import drift
        problems += drift(site)
        ok, why = compat_ok(site)
        if not ok:
            problems.append(why)
        try:
            Ledger(site).build_index()
        except Exception as exc:  # noqa: BLE001
            problems.append(f"records failed validation: {exc}")
    result = {"result": "pass" if not problems else "fail", "problems": problems, "counts": counts,
              "seconds": round(time.monotonic() - start, 2)}
    if site is not None and not site.observe and record:
        Ledger(site).append("run", "selftest", actor, None, {"run_type": "selftest", "status": result["result"], **result})
    return result


UNIT_COMMAND = "-m unittest discover -s tests -t ."


def _unit_script(prefix: Path) -> str:
    """The unit suite as one job: the harness checkout this command runs from, its own interpreter; unittest's
    report goes to `<prefix>.unit.out`, its exit code to `<prefix>.unit.exit`."""
    import shlex

    out, code = shlex.quote(f"{prefix}.unit.out"), shlex.quote(f"{prefix}.unit.exit")
    return "\n".join(["#!/bin/bash", "# DUREH verify --self --submit: harness unit suite as one scheduler job (generated)",
                      f"cd {shlex.quote(str(HARNESS_ROOT))} || exit 96",
                      "export PYTHONDONTWRITEBYTECODE=1", "unset DUREH_OBSERVE DUREH_ACTOR DUREH_AGENT_ID",
                      f"{shlex.quote(sys.executable)} {UNIT_COMMAND} > {out} 2>&1", "rc=$?",
                      f'echo "$rc" > {code}', 'echo "unit suite exit $rc"', "exit $rc"]) + "\n"


def submit_self(site: Site, actor: str) -> dict:
    """`verify --self --submit` (release 77, project-dureh/dureh#41): the site checks run here (as `--skip-unit`), the
    unit suite runs as ONE scheduler job with the site's own scheduler config (`verify --wi`'s job options); a
    `run` record (`run_type: selftest`, `status: submitted`) holds the job and the site-check result. `verify --self
    --collect` records the outcome once the job has ended."""
    from . import ops
    from .util import fail, utc_compact
    from .workflow import checks_runner, checks_sbatch

    if site.observe:
        fail("observe mode: `verify --self --submit` records a run; use `verify --self --skip-unit`")
    if checks_runner(site) != "slurm":
        fail("verify --self --submit needs a scheduler (config checks.runner slurm, sbatch on the site's PATH);"
             " run `verify --self` here instead")
    checks = run_self(site, actor, unit=False, record=False)
    logs = ops.wi_log_dir(site, "selftest")
    logs.mkdir(parents=True, exist_ok=True)
    prefix = logs / f"selftest-{utc_compact()}"
    script = Path(f"{prefix}.sh")
    script.write_text(_unit_script(prefix), encoding="utf-8")
    script.chmod(0o755)
    cmd, wall = checks_sbatch(site, f"{ops.job_prefix(site)}-selftest", HARNESS_ROOT, prefix, script)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=site.env())
    except (OSError, subprocess.SubprocessError) as exc:
        fail(f"cannot submit the unit suite as a scheduler job ({exc}); nothing was recorded")
    if proc.returncode != 0:
        fail(f"sbatch failed for the unit suite job: {proc.stderr.strip()[-2000:]}; nothing was recorded")
    job = proc.stdout.strip().split(";")[0]
    run = Ledger(site).append("run", "selftest", actor, None, {
        "run_type": "selftest", "status": "submitted", "job_id": job, "harness": str(HARNESS_ROOT),
        "site_checks": checks["result"], "problems": checks["problems"], "counts": checks["counts"],
        "script": str(script), "prefix": str(prefix), "log_out": f"{prefix}.out", "log_err": f"{prefix}.err",
        "command": cmd, "time": wall,
        "note": f"unit suite submitted as scheduler job {job}; record it with `verify --self --collect`"})
    return {"submitted": True, "run": run["id"], "job_id": job, "site_checks": checks["result"],
            "problems": checks["problems"], "log_out": f"{prefix}.out",
            "wake": ops.wake_command(site, run["id"]),
            "next": f"start the wake command as a Monitor; when it reports the job ended run `verify --self --collect {run['id']}`"}


def _collected(led: Ledger, run_id: str) -> dict | None:
    return next((r for r in led.load("run") if r["payload"].get("collected_from") == run_id), None)


def collect_self(site: Site, actor: str, run_id: str | None = None) -> dict:
    """`verify --self --collect [RUN]` (release 77): record a submitted unit-suite job's outcome as a selftest run
    (`collected_from`), with the failing tests named as `verify --self` names them. Refused while the job runs and
    for a run already collected."""
    from . import ops
    from .util import fail

    led = Ledger(site)
    subs = [r for r in led.load("run") if r["payload"].get("run_type") == "selftest"
            and r["payload"].get("status") == "submitted"]
    if run_id:
        run = next((r for r in subs if r["id"] == run_id), None) or fail(f"{run_id} is not a submitted selftest run")
    else:
        open_ = [r for r in subs if not _collected(led, r["id"])]
        run = open_[-1] if open_ else fail("no submitted selftest run to collect; run `verify --self --submit` first")
    done = _collected(led, run["id"])
    if done:
        fail(f"selftest run {run['id']} was already collected as {done['id']} ({done['payload']['status']})")
    p = run["payload"]
    prefix = str(site.relocate(p["prefix"]))
    exit_file = Path(f"{prefix}.unit.exit")
    state = ops._final_state(led, run["id"])
    raw = ""
    if state is None:
        state, raw = ops.job_state(site, str(p["job_id"]))
        state = (state.split() or ["UNKNOWN"])[0]
    if state not in ops.TERMINAL_STATES and not (state == "UNKNOWN" and exit_file.is_file()):
        fail(f"selftest run {run['id']}: job {p['job_id']} is still {state}; nothing was recorded")
    if not site.observe and ops._final_state(led, run["id"]) is None:
        led.event("run.status", ops._status_actor(led, actor), None,
                  {"run": run["id"], "job_id": str(p["job_id"]), "scheduler_state": state, "raw": raw,
                   "final": True, "collected": True}, [run["id"]])
    problems = list(p.get("problems") or [])
    text = exit_file.read_text().strip() if exit_file.is_file() else ""
    report = Path(f"{prefix}.unit.out")
    stderr = report.read_text(encoding="utf-8", errors="replace") if report.is_file() else ""
    tail = stderr.strip().splitlines()[-3:]
    unit = {"exit": int(text) if text.lstrip("-").isdigit() else "not-run", "summary": " | ".join(tail),
            "job": {"job_id": str(p["job_id"]), "state": state}}
    if unit["exit"] != 0:
        problems.append(f"unit tests failed (job {p['job_id']} {state}): " + (" | ".join(tail) or "no report"))
        failed = unit_failures(stderr)
        kept = failed[:MAX_FAILED_TESTS]
        for f in kept:
            problems.append(f"{f['kind']} {f['test']}: " + " | ".join(f["tail"]))
        if len(failed) > len(kept):
            problems.append(f"... and {len(failed) - len(kept)} more failing test(s)")
        unit.update(failures=kept, failed_tests=len(failed))
    result = {"result": "pass" if not problems else "fail", "problems": problems,
              "counts": {**(p.get("counts") or {}), "unit": unit}, "collected_from": run["id"],
              "log": str(report)}
    if not site.observe:
        led.append("run", "selftest", actor, None, {"run_type": "selftest", "status": result["result"], **result},
                   [run["id"]])
    return result
