"""The `local` member runtime (release 64): a worker on the site's office-hours model server.

The client is a streaming `/api/chat` tool loop against the server `dureh site office-hours start` runs (its address
comes from `logs/office-hours/server.json`). The model gets four tools, the names its profile teaches:

    list_files(path)            names under a directory of the WI worktree (no .git)
    read_file(path)             a text file inside the WI worktree (the package's no-read paths refused)
    write_file(path, content)   replaces one file of the brief's exact write allowlist, at most `write_cap` times each
    run_tests()                 the item's declared checks, run by the harness in this job, exit codes and output tails

No shell, no git: the member never commits; its changes stay in the worktree for the dispatcher's review
(`member collect --stage`). Stall handling: a quiet stream (no bytes for `quiet_seconds`) earns a grace window only
while the model is still loaded (`/api/ps`), at most `grace_windows` times; a wall clock bounds the run; streamed text
that repeats one short unit is stopped; a harness error (connection refused, broken stream) is retried once. Usage
comes from the server's counters (prompt/eval counts and durations).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

from ..util import dumps, write_replace

TOOL_SPECS = {
    "list_files": {"description": "List file names under a directory of your work root (recursive, at most 200).",
                   "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    "read_file": {"description": "Read one UTF-8 text file of your work root.",
                  "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
    "write_file": {"description": "Replace the whole content of one file on your write allowlist.",
                   "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                                   "content": {"type": "string"}},
                                  "required": ["path", "content"]}},
    "run_tests": {"description": "Run the item's declared checks; returns each exit code and its last output lines.",
                  "parameters": {"type": "object", "properties": {}}},
}
COUNTERS = ("prompt_eval_count", "eval_count", "load_duration", "prompt_eval_duration", "eval_duration",
            "total_duration")
READ_CAP = 60_000
LIST_CAP = 200
OUTPUT_TAIL = 4000
PREAMBLE = """You are the worker `{role}` on work item {wi}, on the local runtime of the DUREH harness.
Your work root is the item's worktree; every path you pass is relative to it.
Tools: list_files(path), read_file(path), write_file(path, content) replaces the whole file, run_tests() runs the
item's declared checks. You may write only these files: {files}. Each may be written at most {cap} times.
You have no shell and no git. Do not commit: the harness keeps your changes for review.
When you are done, answer without a tool call: the files you changed, the last test results, open assumptions.
The brief follows.

"""


def repetition(text: str, tail_chars: int = 600, max_period: int = 80, min_repeats: int = 6) -> bool:
    """The trailing `tail_chars` of the text are mostly one short unit repeated back to back."""
    tail = text[-tail_chars:]
    for period in range(1, max_period + 1):
        if period * min_repeats > len(tail):
            break
        unit, repeats, pos = tail[-period:], 0, len(tail)
        while pos - period >= 0 and tail[pos - period:pos] == unit:
            repeats += 1
            pos -= period
        if repeats >= min_repeats and period * repeats >= len(tail) * 0.6 and len(tail) >= min(tail_chars, 60):
            return True
    return False


def parse_profile(path: Path) -> dict:
    """`PARAMETER <name> <value>` lines of a model profile -> options (numbers converted, repeated names listed)."""
    out: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        words = line.strip().split(None, 2)
        if len(words) == 3 and words[0].upper() == "PARAMETER":
            name, raw = words[1], words[2].strip().strip('"')
            try:
                value = int(raw)
            except ValueError:
                try:
                    value = float(raw)
                except ValueError:
                    value = raw
            if name in out:
                out[name] = (out[name] if isinstance(out[name], list) else [out[name]]) + [value]
            else:
                out[name] = value
    return out


# ------------------------------------------------------------------ HTTP
def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))  # the server is local: never a proxy


def get_json(base: str, path: str, timeout: float = 10) -> dict:
    with _opener().open(urllib.request.Request(base.rstrip("/") + path), timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _names(data: dict) -> set[str]:
    out = set()
    for m in data.get("models") or []:
        for k in ("model", "name"):
            if m.get(k):
                out.add(m[k])
                out.add(m[k].split(":")[0] if m[k].endswith(":latest") else m[k])
    return out


def ready(base: str, model: str) -> str | None:
    """None when the server answers and serves the model; else why not."""
    try:
        data = get_json(base, "/api/tags")
    except (OSError, ValueError) as exc:
        return f"the office-hours server at {base} does not answer: {exc}"
    return None if model in _names(data) else f"the office-hours server at {base} does not serve {model}"


def alive(base: str, model: str) -> bool:
    """The model is still loaded (`/api/ps`): a quiet stream earns a grace window only then."""
    try:
        return model in _names(get_json(base, "/api/ps"))
    except (OSError, ValueError):
        return False


def chat_stream(base: str, payload: dict, quiet: float, deadline: float):
    """POST /api/chat (stream). Yields each JSON line, or None when nothing arrived for `quiet` seconds (the
    connection stays open). A reader thread does the blocking reads (bounded by the deadline), so a quiet spell never
    breaks the connection: a socket read that times out cannot be resumed, a queue wait can. Raises the reader's
    error (connection refused or reset, a broken line) for the caller's harness-error handling."""
    import queue
    import threading

    data = json.dumps(dict(payload, stream=True)).encode("utf-8")
    req = urllib.request.Request(base.rstrip("/") + "/api/chat", data=data, headers={"Content-Type": "application/json"})
    lines: queue.Queue = queue.Queue()
    stop = threading.Event()
    holder: dict = {}

    def reader():
        try:
            resp = _opener().open(req, timeout=max(1.0, deadline - time.monotonic()))
            holder["resp"] = resp
            for raw in resp:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    lines.put(("line", line))
            lines.put(("eof", None))
        except Exception as exc:  # noqa: BLE001 - handed to the consumer, which decides
            lines.put(("error", exc))

    threading.Thread(target=reader, daemon=True).start()
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                yield None
                return
            try:
                kind, value = lines.get(timeout=min(quiet, left))
            except queue.Empty:
                yield None
                continue
            if kind == "eof":
                return
            if kind == "error":
                raise value if isinstance(value, (OSError, ValueError)) else OSError(str(value))
            yield json.loads(value)
    finally:
        stop.set()
        resp = holder.get("resp")
        if resp is not None:
            try:
                resp.close()
            except OSError:
                pass


# ------------------------------------------------------------------ tools
class Tools:
    """The member's tool handler: the enforcement point for its file access."""

    def __init__(self, site, root: Path, allow: list[str], cap: int, checks: list[str], env: dict, timeout: float):
        self.site, self.root = site, Path(root).resolve()
        self.allow = {str((self.root / a).resolve()): a for a in allow}
        self.cap, self.checks, self.env, self.timeout = cap, list(checks), env, timeout
        self.writes: dict[str, int] = {}
        self.log: list[dict] = []
        self.tests: list[list[dict]] = []

    def _path(self, arg) -> Path | None:
        if not isinstance(arg, str) or not arg or "\x00" in arg:
            return None
        p = (self.root / arg).resolve()
        if p != self.root and self.root not in p.parents:
            return None
        if ".git" in p.relative_to(self.root).parts:
            return None
        return p

    def _refused_read(self, p: Path) -> str | None:
        from ..paths import classify
        from ..protect import read_refusal

        t = classify(self.site, p)
        return read_refusal(self.site, p, t.logical if t.zone in ("worktree", "target") else None)

    def call(self, name: str, args) -> str:
        args = args if isinstance(args, dict) else {}
        entry = {"tool": name, "path": args.get("path")}
        fn = getattr(self, f"_{name}", None) if name in TOOL_SPECS else None
        if fn is None:
            out, verdict = f"ERROR: no tool {name!r}; use {', '.join(TOOL_SPECS)}", "unknown"
        else:
            try:
                out, verdict = fn(args)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                out, verdict = f"ERROR: {type(exc).__name__}: {exc}", "error"
        entry["result"] = verdict
        self.log.append(entry)
        return out

    def _list_files(self, args):
        p = self._path(args.get("path") or ".")
        if p is None or not p.is_dir():
            return "ERROR: not a directory inside your work root", "denied"
        names = []
        for dirpath, dirs, files in os.walk(p, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__", ".venv"))
            for f in sorted(files):
                names.append(os.path.relpath(os.path.join(dirpath, f), self.root))
                if len(names) >= LIST_CAP:
                    return "\n".join(names) + "\n(truncated)", "allowed"
        return "\n".join(names) or "(empty)", "allowed"

    def _read_file(self, args):
        p = self._path(args.get("path"))
        if p is None or not p.is_file():
            return "ERROR: not a file inside your work root", "denied"
        why = self._refused_read(p)
        if why:
            return f"ERROR: {why}", "denied"
        return p.read_text(encoding="utf-8", errors="replace")[:READ_CAP], "allowed"

    def _write_file(self, args):
        if not isinstance(args.get("content"), str):
            return "ERROR: write_file needs path and content (strings)", "invalid"
        raw = args.get("path")
        p = self._path(raw)
        if p is None or str(p) not in self.allow or (isinstance(raw, str) and (self.root / raw).is_symlink()):
            return f"ERROR: not on your write allowlist ({', '.join(self.allow.values())})", "denied"
        n = self.writes.get(str(p), 0)
        if n >= self.cap:
            return f"ERROR: write limit for this file reached ({self.cap})", "denied"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        self.writes[str(p)] = n + 1
        return f"wrote {len(args['content'])} characters to {self.allow[str(p)]}", "allowed"

    def _run_tests(self, args):
        if not self.checks:
            return "the item declares no checks", "allowed"
        results, parts = [], []
        for cmd in self.checks:
            t0 = time.monotonic()
            try:
                proc = subprocess.run(cmd, shell=True, cwd=self.root, capture_output=True, text=True,
                                      timeout=self.timeout, env=self.env, stdin=subprocess.DEVNULL)
                rc, out = proc.returncode, (proc.stdout + proc.stderr)[-3000:]
            except subprocess.TimeoutExpired:
                rc, out = "timeout", ""
            results.append({"command": cmd, "exit": rc, "seconds": round(time.monotonic() - t0, 2)})
            parts.append(f"$ {cmd}\nexit {rc}\n{out}")
        self.tests.append(results)
        return "\n".join(parts), "allowed"

    def written(self) -> dict:
        return {self.allow[k]: v for k, v in self.writes.items()}


# ------------------------------------------------------------------ the loop
def converse(base: str, model: str, prompt: str, tools: Tools, limits: dict, options: dict, think: bool,
             log_path: Path | None = None, clock=time.monotonic) -> dict:
    """One attempt of the tool loop. Returns {status: ok | stalled | harness_error, reason, output, usage}."""
    wall, quiet = float(limits["wall_seconds"]), float(limits["quiet_seconds"])
    rep = limits.get("repetition") or {}
    started = clock()
    deadline = time.monotonic() + wall
    messages = [{"role": "user", "content": prompt}]
    specs = [{"type": "function", "function": {"name": n, **s}} for n, s in TOOL_SPECS.items()]
    usage = {k: 0 for k in COUNTERS} | {"calls": 0, "thinking_chars": 0, "done_reasons": []}
    status, reason, final = "ok", None, ""

    def note(obj):
        if log_path is not None:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(obj, ensure_ascii=False) + "\n")

    for _ in range(int(limits["max_turns"])):
        payload = {"model": model, "messages": messages, "tools": specs, "think": bool(think), "options": options}
        content, thinking, calls, grace, done = [], 0, [], 0, False
        try:
            stream = chat_stream(base, payload, quiet, deadline)
            while not done:
                try:
                    chunk = next(stream)
                except StopIteration:
                    status, reason = "harness_error", "the stream ended without a final chunk"
                    break
                if chunk is None:
                    if clock() - started >= wall or time.monotonic() >= deadline:
                        status, reason = "stalled", f"wall clock reached ({wall:g} s)"
                        break
                    if grace >= int(limits["grace_windows"]) or not alive(base, model):
                        status, reason = "stalled", (f"no output for {quiet:g} s and the model is not loaded"
                                                     if grace < int(limits["grace_windows"]) else
                                                     f"no output after {grace} grace windows of {quiet:g} s")
                        break
                    grace += 1
                    note({"t": time.time(), "grace": grace})
                    continue
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    thinking += len(msg["thinking"])  # counted, never stored
                piece = msg.get("content") or ""
                if piece:
                    content.append(piece)
                    if repetition("".join(content), int(rep.get("tail_chars", 600)), int(rep.get("max_period", 80)),
                                  int(rep.get("min_repeats", 6))):
                        status, reason = "stalled", "repetition detected in the streamed output"
                        break
                if msg.get("tool_calls"):
                    calls = msg["tool_calls"]
                if chunk.get("done"):
                    for k in COUNTERS:
                        usage[k] += int(chunk.get(k) or 0)
                    usage["done_reasons"].append(str(chunk.get("done_reason") or "unknown"))
                    usage["calls"] += 1
                    done = True
                if clock() - started >= wall:
                    status, reason = "stalled", f"wall clock reached ({wall:g} s)"
                    break
        except (OSError, ValueError) as exc:  # connection refused or reset, a broken JSON line
            status, reason = "harness_error", f"{type(exc).__name__}: {exc}"
        usage["thinking_chars"] += thinking
        if status != "ok":
            break
        text = "".join(content)
        note({"t": time.time(), "assistant": text[-OUTPUT_TAIL:], "tool_calls": calls})
        messages.append({"role": "assistant", "content": text, **({"tool_calls": calls} if calls else {})})
        if not calls:
            final = text
            break
        for call in calls:
            fn = call.get("function") or {}
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            out = tools.call(str(fn.get("name") or ""), args)
            messages.append({"role": "tool", "tool_name": fn.get("name") or "", "content": out})
    else:
        status, reason = "stalled", f"max_turns reached ({limits['max_turns']})"
    return {"status": status, "reason": reason, "output": final[-OUTPUT_TAIL:], "usage": usage}


def execute(ctx: dict, converse_fn=converse) -> dict:
    """The job side of a local worker: the office-hours server and the role's profile, the allowlist from the brief,
    the tool loop (one retry on a harness error). Returns {outcome, reason, output, writes, tests, usage, ...}."""
    from . import minimal_env, write_allowlist
    from .. import office_hours
    from ..paths import target_root
    from ..workflow import _checks_env, checks_env_plan

    site, cfg, role, wi = ctx["site"], ctx["cfg"], ctx["role"], ctx["wi"]
    lim = cfg["local"]
    rec = ctx["led"].get("work-item", wi)
    payload = rec["payload"]
    allow = write_allowlist(site, role, payload)
    if not allow:
        return {"outcome": "not_run", "reason": "the item declares no output inside the role's write scopes"}
    entry = office_hours.profile_for(site, role.profile)
    if entry is None:
        return {"outcome": "not_run", "reason": f"config office_hours.profiles has no profile {role.profile}"}
    profile = site.startup / role.profile
    digest = hashlib.sha256(profile.read_bytes()).hexdigest()
    server = office_hours.server(site)
    if server is None:
        return {"outcome": "not_run", "reason": "no office-hours server is running"}
    served = (server.get("profiles") or {}).get(entry["name"]) or {}
    if served.get("sha256") and served["sha256"] != digest:
        return {"outcome": "not_run", "reason": f"profile {entry['name']} changed since office hours started"
                                                f" ({served['sha256'][:12]} served, {digest[:12]} now); restart office hours"}
    base = f"http://{server['host']}:{server['port']}"
    model = entry["name"]
    why = ready(base, model)
    profile_rec = {"name": model, "file": role.profile, "sha256": digest, "server": base, "server_job": server.get("job")}
    if why:
        return {"outcome": "not_run", "reason": why, "profile": profile_rec}
    wt = site.worktrees / wi
    troot = target_root(site, payload["target"]) or site.root
    plan = checks_env_plan(site, wi, wt, troot)
    env = {**minimal_env(), **{k: v for k, v in _checks_env(site, wi, wt, troot, plan).items()
                               if k in ("PATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "UV_NO_SYNC", "PYTHONPATH",
                                        "DUREH_WORK_ROOT", "DUREH_TARGET_ROOT", "DUREH_WI", "PYTHONDONTWRITEBYTECODE",
                                        "DUREH_SITE")}}
    tools = Tools(site, wt, allow, int(lim["write_cap"]), list(payload.get("checks") or []), env,
                  float(lim["test_timeout"]))
    options = {**parse_profile(profile), **(lim.get("options") or {})}
    prompt = PREAMBLE.format(role=role.name, wi=wi, files=", ".join(allow), cap=int(lim["write_cap"])) + ctx["brief"]
    d = ctx["rdir"] / "dispatch"
    attempts = []
    totals = {k: 0 for k in COUNTERS} | {"calls": 0, "thinking_chars": 0}
    for attempt in range(1 + int(lim["retries"])):
        r = converse_fn(base, model, prompt, tools, lim, options, bool(lim.get("think")),
                        log_path=d / f"stream-{attempt + 1}.jsonl")
        attempts.append({"status": r["status"], "reason": r["reason"]})
        for k in totals:  # every attempt used the server: all count
            totals[k] += int((r.get("usage") or {}).get(k) or 0)
        if r["status"] != "harness_error":
            break
    write_replace(d / "tools.json", dumps({"log": tools.log, "writes": tools.written(), "tests": tools.tests}))
    out = {"profile": profile_rec, "model": model, "attempts": attempts, "output": r["output"],
           "writes": tools.written(), "tests": tools.tests[-1] if tools.tests else None, "tool_calls": len(tools.log),
           "usage": {"coverage": "runtime", "runtime": "local", "unit": "tokens", "model": model,
                     "profile_sha256": digest, "input_tokens": totals["prompt_eval_count"],
                     "output_tokens": totals["eval_count"], **totals,
                     "gpu_seconds": round(totals["total_duration"] / 1e9, 1),
                     "source": "office-hours server counters (prompt_eval_count, eval_count, durations in ns)"}}
    if r["status"] == "ok":
        return {**out, "outcome": "completed", "reason": None}
    return {**out, "outcome": "not_run", "reason": f"{r['status']}: {r['reason']}"}
