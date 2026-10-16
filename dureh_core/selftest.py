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


def run_self(site: Site | None, actor: str = "checker", unit: bool = True) -> dict:
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
    if site is not None and not site.observe:
        Ledger(site).append("run", "selftest", actor, None, {"run_type": "selftest", "status": result["result"], **result})
    return result
