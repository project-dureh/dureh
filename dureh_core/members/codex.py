"""The `codex` member runtime (release 64): a read-only verifier.

`codex exec` runs in the run's evidence bundle (`-C <evidence dir>`) under a named Codex permission profile passed
with `-c` overrides: filesystem reads limited to the bundle, the platform essentials (`:minimal`) and what the
sandbox helper itself needs to start, no write entry (so nothing is writable), network off. User config, exec rules and
project docs are not loaded, web search is off, the session is not persisted. The generic `--sandbox read-only` flag
is NOT used (it overrode the profile and widened reads in an earlier canary). `member canary --runtime codex` applies
the same profile to plain commands (`codex sandbox`, no model) and proves on this host that a read outside the bundle
and any write are refused; a member run refuses to start until that canary passed on its host.
"""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from ..records import Ledger
from ..site import Site
from ..util import DurehError, dumps, git, write_replace

VERDICT_RE = re.compile(r"^\s*VERDICT:\s*(PASS|FAIL)\b", re.I | re.M)
LOGIN_RE = re.compile(r"not logged in|log ?in required|please log ?in|unauthori[sz]ed|\b401\b|authenticat", re.I)
QUOTA_RE = re.compile(r"quota|rate.?limit|usage limit|\b429\b|insufficient|too many requests", re.I)
EVIDENCE_FILE_CAP = 200_000  # bytes of one untracked changed file copied into the bundle
PREAMBLE = """You are the verify role `{role}` reviewing work item {wi}, read-only, on the codex runtime of the DUREH harness.
The harness has already run the item's declared checks and gathered the evidence in the current directory:
  brief.md       the brief prepared for the checker (its instructions to run `dureh verify` are for a Claude
                 checker and are already done: the results are in checks.json)
  acceptance.md  the item's goal, acceptance criteria, constraints, declared outputs and checks
  diff.patch     the change under review (base..head), changed.txt the changed paths
  checks.json    the declared checks: command, exit code, seconds, last output lines
  files/         copies of changed files git does not track yet
Read them with read-only shell commands (cat, sed, grep, ls). Write nothing and run nothing else: the sandbox refuses
it and a refusal is not a finding. Judge each acceptance criterion against this evidence only.
Answer with one line per finding (criterion, evidence with file:line where you can, met / not met), then end with
exactly one last line: `VERDICT: PASS` (every criterion met and every check passed) or `VERDICT: FAIL`.
"""


def find_bin(site: Site, cfg: dict) -> str | None:
    name = str(cfg["codex"]["bin"])
    return name if Path(name).is_absolute() and Path(name).is_file() else shutil.which(name, path=site.env().get("PATH"))


def version(binary: str, env: dict) -> str:
    try:
        proc = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=60, env=env,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unknown ({exc})"
    lines = [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip() and not ln.startswith("WARNING")]
    return lines[0].strip() if lines else "unknown"


def sandbox_read_paths(site: Site, cfg: dict, binary: str) -> list[str]:
    """What the Codex sandbox helper must read to start inside a narrow profile (measured with codex-cli 0.162 on a
    Linux cluster node: under `:minimal` alone bubblewrap cannot exec `codex-linux-sandbox`): the helper links under
    `$CODEX_HOME/tmp/arg0` and the directory of the native binary. Config `members.codex.sandbox_read` replaces this
    detection."""
    import os

    given = cfg["codex"].get("sandbox_read")
    if given is not None:
        return [site.expand(str(x)) for x in given]
    out = []
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if (home / "tmp" / "arg0").is_dir():
        out.append(str(home / "tmp" / "arg0"))
    real = Path(binary).resolve()
    out.append(str(real.parent))
    if real.suffix == ".js":  # an npm install: the native binary lives in a platform package
        for d in sorted(real.parent.parent.glob("node_modules/*/codex-linux-*/vendor/*/bin")):
            out.append(str(d))
    return out


def permission_overrides(name: str, readable: list[str]) -> list[str]:
    """The named permission profile as `-c` overrides (shared by exec and the canary): `:minimal` and the readable
    paths read, no write entry, network off. No deny entry: with codex 0.162 a denied parent wins over a readable
    child, and paths outside the profile are not mounted at all."""
    fs = {":minimal": "read"}
    for p in readable:
        fs[str(p)] = "read"
    toml = "{" + ",".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in fs.items()) + "}"
    return ["-c", f'default_permissions="{name}"', "-c", f"permissions.{name}.filesystem={toml}",
            "-c", f"permissions.{name}.network={{enabled=false}}"]


def exec_argv(binary: str, evidence: Path, cfg: dict, readable: list[str]) -> list[str]:
    c = cfg["codex"]
    argv = [binary, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check", "--json",
            "-C", str(evidence), "-c", f"model_reasoning_effort={json.dumps(str(c.get('reasoning_effort') or 'medium'))}",
            *permission_overrides(str(c["permission_profile"]), readable),
            "-c", "allow_login_shell=false", "-c", "include_permissions_instructions=false",
            "-c", "include_environment_context=false", "-c", "project_doc_max_bytes=0",
            "-c", 'web_search="disabled"']
    if c.get("model"):
        argv += ["-m", str(c["model"])]
    return argv


def parse_events(lines: list[str]) -> dict:
    """`codex exec --json` lines -> {output (the last agent message), usage (turn.completed), tool_log, errors}."""
    final, usage, log, errors, thread = "", {}, [], [], None
    for line in lines:
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        t = ev.get("type")
        if t == "thread.started":
            thread = ev.get("thread_id")
        elif t == "item.completed":
            item = ev.get("item") or {}
            if item.get("type") == "agent_message":
                final = item.get("text") or ""
            elif item.get("type"):
                log.append({"tool": item.get("type"), "target": item.get("command") or item.get("query"),
                            "status": item.get("status"), "exit": item.get("exit_code")})
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            for k, v in u.items():
                if isinstance(v, int):
                    usage[k] = usage.get(k, 0) + v
        elif t in ("error", "turn.failed"):
            msg = ev.get("message") or (ev.get("error") or {}).get("message") or json.dumps(ev)[:500]
            errors.append(str(msg))
    return {"output": final, "usage": usage, "tool_log": log, "errors": errors, "thread": thread}


def verdict(text: str) -> str | None:
    found = VERDICT_RE.findall(text or "")
    return found[-1].upper() if found else None


def failure_reason(exit_code, stderr: str, errors: list[str]) -> str | None:
    """Why a codex run did not produce a review (NOT_RUN): login, quota, or the exit code with its last words."""
    text = "\n".join(errors) + "\n" + (stderr or "")
    if LOGIN_RE.search(text):
        return f"codex login failure: {_tail(text)}"
    if QUOTA_RE.search(text):
        return f"codex quota or rate limit: {_tail(text)}"
    if exit_code not in (0, None):
        return f"codex exited {exit_code}: {_tail(text)}"
    return None


def _tail(text: str, n: int = 300) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip() and not ln.startswith("WARNING: failed to clean")]
    return " | ".join(lines)[-n:] or "no message"


def usage_row(parsed: dict, model: str, effort: str) -> dict:
    u = parsed.get("usage") or {}
    return {"coverage": "runtime" if u else "none", "runtime": "codex", "unit": "tokens", "model_requested": model,
            "reasoning_effort": effort, "input_tokens": u.get("input_tokens", 0),
            "cached_input_tokens": u.get("cached_input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
            "source": "codex --json turn.completed"}


# ------------------------------------------------------------------ canary
CANARY_ALLOW, CANARY_DENY = "DUREH-CANARY-ALLOW", "DUREH-CANARY-DENY"


def passed_canary(led: Ledger, host: str | None = None, ver: str | None = None) -> dict | None:
    """The newest passing `member.canary` for codex (on `host`, with codex `ver`, when given)."""
    for e in reversed(led.events()):
        p = e["payload"]
        if e["type"] != "member.canary" or p.get("runtime") != "codex":
            continue
        if host is not None and p.get("host") != host:
            continue
        if ver is not None and p.get("version") != ver:
            continue
        return e if p.get("result") == "pass" else None
    return None


def canary(site: Site, actor: str, cfg: dict, runner=subprocess.run) -> dict:
    """Prove on this host that the member profile refuses a read outside the bundle and any write. A throwaway
    directory holds `evidence/allowed.txt` and `outside/secret.txt`; `codex sandbox` (no model, no quota) runs plain
    commands under the exact profile a member run uses. Records `member.canary` {runtime, host, version, result,
    rows}."""
    from . import minimal_env

    binary = find_bin(site, cfg)
    host = socket.gethostname()
    if not binary:
        rows = [{"case": "codex present", "ok": False, "evidence": f"{cfg['codex']['bin']!r} not on the site's PATH"}]
        return _record_canary(site, actor, host, "absent", rows, cfg)
    env = _env(site, cfg, binary, minimal_env)
    ver = version(binary, env)
    tmp = Path(tempfile.mkdtemp(prefix="dureh-canary-"))
    try:
        ev, out = tmp / "evidence", tmp / "outside"
        ev.mkdir()
        out.mkdir()
        (ev / "allowed.txt").write_text(CANARY_ALLOW + "\n")
        (out / "secret.txt").write_text(CANARY_DENY + "\n")
        readable = [str(ev)] + sandbox_read_paths(site, cfg, binary)
        perm = permission_overrides(str(cfg["codex"]["permission_profile"]), readable)
        cases = [("read inside the bundle", ["/bin/cat", "allowed.txt"], True),
                 ("read outside (relative)", ["/bin/cat", "../outside/secret.txt"], False),
                 ("read outside (absolute via ..)", ["/bin/cat", f"{ev}/../outside/secret.txt"], False),
                 ("write inside the bundle", ["/bin/sh", "-c", "echo x > written.txt"], False),
                 ("write outside the bundle", ["/bin/sh", "-c", f"echo x > {out}/written.txt"], False)]
        rows = []
        for label, cmd, allowed in cases:
            argv = [binary, "sandbox", "-P", str(cfg["codex"]["permission_profile"]), *perm[2:], "-C", str(ev), "--", *cmd]
            try:
                proc = runner(argv, capture_output=True, text=True, timeout=120, env=env, stdin=subprocess.DEVNULL,
                              cwd=str(ev))
                rc, text = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
            except (OSError, subprocess.SubprocessError) as exc:
                rc, text = None, str(exc)
            leaked = CANARY_DENY in text
            if allowed:
                ok = rc == 0 and CANARY_ALLOW in text
            else:
                ok = rc not in (0, None) and not leaked
            rows.append({"case": label, "ok": ok, "exit": rc, "evidence": _tail(text, 200)})
        for label, f in (("no file written inside", ev / "written.txt"), ("no file written outside", out / "written.txt")):
            rows.append({"case": label, "ok": not f.exists(), "evidence": "absent" if not f.exists() else "exists"})
        return _record_canary(site, actor, host, ver, rows, cfg, readable)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _record_canary(site: Site, actor: str, host: str, ver: str, rows: list[dict], cfg: dict,
                   readable: list[str] | None = None) -> dict:
    result = "pass" if rows and all(r["ok"] for r in rows) else "fail"
    payload = {"runtime": "codex", "host": host, "version": ver, "result": result, "rows": rows,
               "profile": cfg["codex"]["permission_profile"], "sandbox_read": readable or []}
    ev = Ledger(site).event("member.canary", actor, None, payload)
    return {**payload, "event": ev["id"]}


def _env(site: Site, cfg: dict, binary: str, minimal_env) -> dict:
    """The minimal environment plus the directories codex needs (its own, and `node` for an npm install)."""
    import os

    extra = [os.path.dirname(binary)]
    node = shutil.which("node", path=site.env().get("PATH"))
    if node:
        extra.append(os.path.dirname(node))
    return minimal_env(extra, list(cfg["codex"].get("env_pass") or []))


# ------------------------------------------------------------------ run
def _evidence(site: Site, ctx: dict, results: list[dict], paths: list[str], head: str | None) -> Path:
    from ..workflow import _base_of

    wi, rec = ctx["wi"], ctx["rec"]
    ev = ctx["rdir"] / "evidence"
    ev.mkdir(parents=True, exist_ok=True)
    p = rec["payload"]
    (ev / "brief.md").write_text(ctx["brief"], encoding="utf-8")
    lines = [f"# {wi}: {p['title']}", "", f"Goal: {p['goal']}", f"Target: {p['target']}", f"Head: {head}", "",
             "## Acceptance", *[f"- {a}" for a in p["acceptance"]]]
    for key, title in (("constraints", "Constraints"), ("outputs", "Declared outputs"), ("checks", "Declared checks")):
        if p.get(key):
            lines += ["", f"## {title}", *[f"- {x}" for x in p[key]]]
    (ev / "acceptance.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (ev / "checks.json").write_text(dumps(results), encoding="utf-8")
    (ev / "changed.txt").write_text("\n".join(paths) + "\n", encoding="utf-8")
    wt = site.worktrees / wi
    diff = ""
    if wt.is_dir() and head:
        base = _base_of(site, wi)
        diff = git(["diff", f"{base}..{head}"], wt, check=False) if base else ""
        tracked = set(git(["ls-files"], wt, check=False).splitlines())
        for name in paths:
            src = wt / name
            if name not in tracked and src.is_file() and not src.is_symlink() and src.stat().st_size <= EVIDENCE_FILE_CAP:
                dst = ev / "files" / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
    (ev / "diff.patch").write_text(diff, encoding="utf-8")
    return ev


def execute(ctx: dict, runner=subprocess.run) -> dict:
    """The job side of a codex checker: canary on this host (run now when this host and codex version have none),
    the declared checks (in this job), the evidence bundle, then `codex exec` under the profile with the wait limit.
    Returns {outcome, reason, verdict, findings, checks, head, changed, usage, ...}."""
    from . import minimal_env
    from .. import workflow as wf

    site, led, cfg, role, wi = ctx["site"], ctx["led"], ctx["cfg"], ctx["role"], ctx["wi"]
    c = cfg["codex"]
    binary = find_bin(site, cfg)
    if not binary:
        return {"outcome": "not_run", "reason": f"codex command {c['bin']!r} not found on this node"}
    env = _env(site, cfg, binary, minimal_env)
    host, ver = socket.gethostname(), version(binary, env)
    if not passed_canary(led, host, ver):
        can = canary(site, role.name, cfg)
        if can["result"] != "pass":
            return {"outcome": "not_run", "reason": f"the codex canary failed on {host} ({can['event']}); the runtime"
                                                    " refuses to run"}
    try:
        vctx = wf._verify_context(site, role.name, wi)
    except DurehError as exc:
        return {"outcome": "not_run", "reason": f"cannot verify now: {exc}"}
    ctx["rec"] = vctx["rec"]
    results = []
    if vctx["checks"]:
        wf._check_venv(wi, vctx["cwd"])
        plan = wf._check_env_present(site, wi, vctx["cwd"], vctx["troot"])
        results = wf._run_checks_local(site, wi, vctx["cwd"], vctx["troot"], vctx["checks"], int(c["checks_timeout"]),
                                       plan)
    paths, head = wf.changed_paths(site, wi)
    evidence = _evidence(site, ctx, results, paths, head)
    readable = [str(evidence)] + sandbox_read_paths(site, cfg, binary)
    argv = exec_argv(binary, evidence, cfg, readable)
    prompt = PREAMBLE.format(role=role.name, wi=wi)
    d = ctx["rdir"] / "dispatch"
    write_replace(d / "codex-argv.json", dumps(argv))
    t0 = time.monotonic()
    base = {"checks": results, "head": head, "changed": paths, "canary": {"host": host, "version": ver},
            "model": c.get("model") or "default"}
    effort = str(c.get("reasoning_effort") or "medium")
    try:
        proc = runner(argv + ["--", prompt], capture_output=True, text=True, timeout=float(c["wait_seconds"]),
                      env=env, cwd=str(evidence), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {**base, "outcome": "not_run", "reason": f"codex hung past its wait limit ({c['wait_seconds']} s)",
                "usage": usage_row({}, base["model"], effort)}
    except OSError as exc:
        return {**base, "outcome": "not_run", "reason": f"codex did not start: {exc}"}
    seconds = round(time.monotonic() - t0, 1)
    (d / "codex-events.jsonl").write_text(proc.stdout or "", encoding="utf-8")
    (d / "codex-stderr.txt").write_text(proc.stderr or "", encoding="utf-8")
    parsed = parse_events((proc.stdout or "").splitlines())
    v = verdict(parsed["output"])
    out = {**base, "review_seconds": seconds, "exit": proc.returncode, "usage": usage_row(parsed, base["model"], effort),
           "tool_calls": len(parsed["tool_log"]), "findings": parsed["output"][-4000:], "verdict": v}
    why = failure_reason(proc.returncode, proc.stderr, parsed["errors"])
    if why and not v:
        return {**out, "outcome": "not_run", "reason": why}
    if not v:
        return {**out, "outcome": "not_run", "reason": "the reviewer gave no `VERDICT: PASS|FAIL` line"}
    return {**out, "outcome": "completed", "reason": None}

