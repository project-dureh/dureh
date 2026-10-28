"""DUREH command line. Every subcommand is a harness tool with one job."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .util import DurehError, dumps, fail


RESERVED_EVENT_PREFIXES = ("workday.", "day.", "session.", "hours.", "slack.", "github.", "harness.", "member.", "office-hours.")  # harness.switched: session start only


def _percent(value: str) -> float:
    try:
        return float(value.strip().rstrip("%"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number; use the week's percentage from Claude Code "
                                         "/usage, for example --weekly-percent 16") from None


def _site(args):
    from .site import find_site

    return find_site(getattr(args, "site", None))


def _actor(args) -> str:
    env = os.environ.get("DUREH_ACTOR")
    given = getattr(args, "actor", None)
    if env and given and env != given:
        _identity_refused(args, env, f"--actor {given} conflicts with the harness-assigned identity {env}")
    if not env and os.environ.get("DUREH_AGENT_ID"):
        # project-dureh/dureh#35: a process an agent started (a script, an absolute or relative path the hook did not
        # rewrite) carries the agent id but no identity; it is that agent, never the desk
        env = _agent_role(args, os.environ["DUREH_AGENT_ID"])
        if given and env != given:
            _identity_refused(args, env, f"--actor {given} conflicts with the identity of agent"
                                         f" {os.environ['DUREH_AGENT_ID']} ({env})")
    return env or given or "desk"


def _identity_refused(args, role: str | None, reason: str) -> None:
    """UR-20261026-02: a refusal on the identity path leaves a `guard.denied` event (tool `cli`), as the hook's do."""
    from .records import Ledger

    try:
        site = _site(args)
        if not site.observe:
            known = role if role and role in site.org.roles else "desk"
            Ledger(site).event("guard.denied", known, None, {
                "tool": "cli", "role": role or "unknown", "agent_id": os.environ.get("DUREH_AGENT_ID"),
                "target": " ".join(["dureh", *sys.argv[1:]])[:4000], "reason": f"DUREH: {reason}"[:500]})
    except DurehError:
        pass
    fail(reason)


def _agent_role(args, agent_id: str) -> str:
    """The role an agent id was started as (its `agent.started` record); refuse when it has none."""
    from .records import Ledger

    try:
        led = Ledger(_site(args))
        roles = [ev["actor"] for ev in led.events() if ev["type"] == "agent.started"
                 and ev["payload"].get("agent_id") == agent_id]
    except DurehError:
        roles = []
    if not roles or roles[-1] == "desk":
        _identity_refused(args, None, f"this process belongs to agent {agent_id}, which has no recorded role; run the"
                                      " harness as `bin/dureh <command>` written out in your own command so the hook"
                                      " sets your identity")
    return roles[-1]


def _print(value) -> None:
    if isinstance(value, str):
        print(value)
    else:
        print(dumps(value), end="")


# ------------------------------------------------------------------ handlers
def cmd_install(a):
    from .install import install

    r = install(a.startup, a.target_site or a.site, a.preset, a.persona, a.interactive, run_unit=not a.skip_unit)
    print(f"Installed startup package {r['package']} into {r['site']}")
    print("Created fresh site state; no package state or recovery pointer was imported.")
    print(f"Bootstrap work items (awaiting Gate 1): {', '.join(r['work_items']) or 'none'}")
    print(f"Self-test: {r['selftest']['result']} {r['selftest']['problems'] or ''}".rstrip())
    for w in (r["selftest"].get("counts") or {}).get("warnings") or []:
        print(f"warning: {w}")
    print(f"State: {r['state'].get('backend')} revision {r['state'].get('revision')}")


def cmd_adapter_sync(a):
    from .runtimes import claude_code

    site = _site(a)
    if a.harness:
        # D1: switch the site to another checkout by running that checkout's own dureh, which then
        # regenerates bin/dureh and the hooks to exec itself and re-pins deployment.json to its revision.
        other = Path(a.harness).resolve()
        exe = other / "dureh"
        if not (other / "dureh_core").is_dir() or not os.access(exe, os.X_OK):
            fail(f"{other} is not a harness checkout (no executable dureh with dureh_core/)")
        if other != site.harness_root.resolve():
            os.execv(str(exe), [str(exe), "--site", str(site.root), "adapter", "sync",
                                *(["--package-rev", a.package_rev] if a.package_rev else [])])
    old_root = (site.config.get("harness") or {}).get("root")
    stored_site_root = site.stored_root
    from .install import apply_config_defaults, sync_config_errors, sync_package

    # release 65: the config is checked as the package being installed (--package-rev R, or the source's HEAD) will
    # leave it, against that package's files, before anything changes; refused, the installed package stays
    synced = sync_package(site, a.package_rev, check=lambda manifest, pkg: sync_config_errors(site, manifest, pkg))
    if synced.get("skipped"):
        installed = site.package_manifest() if (site.startup / "startup.yaml").is_file() else {}
        refused = sync_config_errors(site, installed, site.startup)
        if refused:
            fail("; ".join(refused))
        print(f"package: {synced['skipped']}")
    else:
        print(f"package {synced['id']}: {synced['from'] or 'unpinned'} -> {synced['to']}"
              + (f" ({len(synced['changed'])} file(s) changed; package.synced recorded)" if synced["changed"] else " (unchanged)"))
    for w in synced.get("warnings") or []:
        print(f"warning: {w}")
    added = apply_config_defaults(site)  # UP-0069: package config_defaults the site's config.json lacks (added once)
    if added:
        print(f"config.json: added from the package's config_defaults: {', '.join(added)}")
    m = claude_code.generate(site)
    print(f"generated {len(m['files'])} files for claude-code")
    from .projectgen import generate_projects

    for r in generate_projects(site):
        state = r.get("error") or ("updated and saved" if r["changed"] else "unchanged")
        skipped = f"; left untouched (not generated by dureh): {', '.join(r['skipped'])}" if r.get("skipped") else ""
        print(f"project {r['project']}: {state}{skipped}")
    from .install import refresh_deployment

    dep = refresh_deployment(site)
    print(f"deployment pins: harness {dep['harness']['rev'][:12]} package {str(dep['package']['rev'])[:12]}")
    from .util import harness_label

    switched = f" (was {old_root})" if old_root != str(site.harness_root) else ""
    print(f"harness: {harness_label(site.harness_root)} at {site.harness_root}{switched}")
    if stored_site_root != str(site.root):
        print(f"site root: {site.root} (config site.root said {stored_site_root}; refreshed"
              + (", site.moved recorded: paths recorded under the old root resolve here" if stored_site_root else "")
              + "). Next: `worktree repair --all` (dry-run first), then `verify --self`")


def cmd_hook(a):
    from .runtimes import claude_code

    raw = sys.stdin.read()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        data = {}
    try:
        # UR-20261026-02: a guard check answers within its budget (a refusal past it), never by timing out
        out = claude_code.handle_within(_site(a), data, a.timeout)
    except Exception as exc:  # noqa: BLE001 - a hook must fail closed on PreToolUse
        if data.get("hook_event_name") == "PreToolUse":
            out = claude_code._deny(f"harness error, failing closed: {exc}")
        else:
            out = {"systemMessage": f"DUREH hook error: {exc}"}
    if out:
        print(json.dumps(out, ensure_ascii=False))


def cmd_wi_new(a):
    from .records import wi_label
    from .workflow import wi_new

    r = wi_new(_site(a), _actor(a), a.title, a.goal, a.role, a.target, a.acceptance, a.lane, a.bundle, a.value,
               a.check, a.output, a.constraint, a.parent, None, a.academic, fix_for=a.fix_for,
               checks_runner_choice=a.checks_runner, requirements=a.requirements, label=a.label)
    print(f"{wi_label(r['id'], r['payload'])} created (proposed; needs Gate 1): {r['payload']['title']}")


def cmd_wi_approve(a):
    from .workflow import wi_approve

    from .records import approver

    ev = wi_approve(_site(a), _actor(a), a.wi, a.memo, approver(a.by, what="`wi approve`"))
    print(f"{a.wi} approved (Gate 1) by {a.by}: {ev['id']}")


def cmd_wi_amend(a):
    from .workflow import wi_amend

    fields = {"acceptance": a.acceptance, "checks": a.check, "constraints": a.constraint, "outputs": a.output,
              "checks_runner": a.checks_runner, "requirements": a.requirements}
    _print(wi_amend(_site(a), _actor(a), a.wi, a.memo, {k: v for k, v in fields.items() if v is not None},
                    a.rebase, add_constraints=a.add_constraint, reopen=a.reopen))


def cmd_wi_show(a):
    from .records import Ledger, wi_label

    led = Ledger(_site(a))
    out = []
    for wi in a.wi:
        rec = led.get("work-item", wi)
        evs = led.events(wi)
        if a.summary:
            out.append({"wi": wi, "label": wi_label(wi, rec["payload"]), "title": rec["payload"]["title"],
                        "role": rec["payload"]["role"],
                        "status": led.wi_status(wi)["status"],
                        "last_events": [f"{e['ts'][11:19]} {e['type']} {e['actor']}" for e in evs[-5:]]})
        else:
            out.append({"work_item": rec, "status": led.wi_status(wi),
                        "events": [{"ts": e["ts"], "type": e["type"], "actor": e["actor"], "payload": e["payload"]}
                                   for e in evs]})
    _print(out[0] if len(out) == 1 else out)


def cmd_wi_list(a):
    from .records import Ledger

    for w in Ledger(_site(a)).work_items():
        if a.status and w["status"] != a.status:
            continue
        print(f"{w['id']}  {str(w['status']):10s} {w['role']:15s} {w['target']:20s} {w['title']}")


def cmd_wi_stage(a):
    from .workflow import wi_stage

    ev = wi_stage(_site(a), _actor(a), a.wi, a.summary)
    print(f"{a.wi} staged at {ev['payload'].get('head') or 'no worktree'}")


def cmd_wi_promote(a):
    from .workflow import wi_promote

    ev = wi_promote(_site(a), _actor(a), a.wi, a.memo, a.public_summary)
    print(f"{a.wi} promoted: merge {ev['payload'].get('merge')}; compensation: {ev['payload']['compensation']}")
    env = ev["payload"].get("env")
    if env:  # release 81: the target main tree's environment after the merge
        print(f"main env: {env['env']}" + (f" (dependency files changed: {', '.join(env['deps_changed'])})"
                                            if env.get("deps_changed") else "")
              + (f"; {env['error']}; next: {env['next']}" if env["env"] == "error" else "")
              + (f"; {env['hint']}" if env.get("hint") else ""))


def cmd_dispatch_brief(a):
    from .workflow import dispatch_brief
    from .workday import gate

    site = _site(a)
    why = gate(site, a.wi, checking=bool(a.role) and site.org.has(a.role, "verify"))
    if why:
        fail(why)
    r = dispatch_brief(site, a.wi, _actor(a), a.role)
    env = r.get("worktree_env") or {}
    if env.get("env") in ("shared", "rebuilt"):
        how = (f"shares {env.get('shared')}" if env["env"] == "shared"
               else f"built with `{env.get('command')}` in {env.get('seconds')} s")
        print(f"# worktree env: {env.get('venv')} {how}")
    if r.get("runtime") not in (None, "claude-code"):
        print(f"# {r['subagent_type']} runs on the {r['runtime']} runtime: run `dureh member run --wi {a.wi}` (the harness"
              f" submits it; do not spawn it). The fallback as a Claude subagent: `dureh member run --wi {a.wi} --fallback"
              f" --reason ...`, then spawn subagent_type={r['subagent_type']} model={r['model']} with everything below")
    else:
        print(f"# spawn subagent_type={r['subagent_type']} model={r['model']} kind={r['kind']};"
              " pass everything below as the prompt (pass model= to the Agent tool; the hook refuses another model)")
    print(r["brief"], end="")


def cmd_dispatch_stop(a):
    from .workflow import dispatch_stop

    dispatch_stop(_site(a), _actor(a), a.wi, a.reason)
    print(f"{a.wi} stopped: {a.reason}. Stop the runtime agent too; its worktree is kept for inspection.")


def cmd_verify(a):
    site = None
    if a.self and (a.submit or a.collect is not None):  # release 77 (#41): the unit suite as a scheduler job
        from .selftest import collect_self, submit_self

        if a.skip_unit or a.local or (a.submit and a.collect is not None):
            fail("verify --self takes one of --submit, --collect [RUN] (not with --skip-unit or --local)")
        if a.submit:
            _print(submit_self(_site(a), _actor(a)))
            return
        r = collect_self(_site(a), _actor(a), a.collect or None)
        _print(r)
        if r["result"] != "pass":
            raise SystemExit(1)
        return
    if a.self:
        from .selftest import run_self

        try:
            site = _site(a)
        except DurehError:
            site = None
        r = run_self(site, _actor(a) if site else "checker", unit=not a.skip_unit)
        _print(r)
        if r["result"] != "pass":
            raise SystemExit(1)
        return
    if a.exam:
        from .exams import score

        _print(score(_site(a), _actor(a), a.exam, a.answers, a.task))
        return
    if not a.wi:
        fail("verify needs --self, --wi, or --exam")
    from .workflow import verify_collect, verify_submit, verify_wi

    if a.submit and (a.collect is not None or a.local):
        fail("--submit cannot be combined with --collect or --local")
    if a.collect is not None and a.local:
        fail("--collect cannot be combined with --local")
    if a.submit:
        r = verify_submit(_site(a), _actor(a), a.wi)
        if r["submitted"]:
            from .ops import desk_wake

            wake = desk_wake(_site(a), _actor(a), r["run"])  # release 77 (#38)
            _print({k: r[k] for k in ("submitted", "run", "job_id", "head", "log_out", "superseded", "superseded_note",
                                      "next") if k in r} | ({"wake": wake} if wake else {}))
            return
    elif a.collect is not None:
        r = verify_collect(_site(a), _actor(a), a.wi, a.collect or None)
        if r.get("superseded"):  # release 61: the stale run was voided; nothing was verified
            _print({k: r[k] for k in ("superseded", "event", "job_id", "old_head", "new_head", "job_state", "cancelled",
                                      "note", "next")})
            return
    else:
        r = verify_wi(_site(a), _actor(a), a.wi, local=a.local)
    # a bundle recount (UR-20261008-01) has no runner and reports its children instead of changed paths
    out = {k: r[k] for k in ("result", "note", "changed", "violations", "missing_outputs", "seconds", "runner",
                              "runner_source", "children", "run", "collected_from") if r.get(k) is not None}
    if r.get("job"):
        out["job"] = {k: r["job"][k] for k in ("job_id", "state", "log_out", "waited_seconds") if k in r["job"]}
    _print(out | {"checks": [{"command": c["command"], "exit": c["exit"], "seconds": c["seconds"]}
                             for c in r.get("checks") or []]})
    if r["result"] != "pass":
        raise SystemExit(1)


def cmd_ledger_add(a):
    from .records import Ledger

    led = Ledger(_site(a))
    actor = _actor(a)
    if a.kind == "finding":
        rec = led.append("finding", "finding", actor, a.wi, {"summary": a.summary, "evidence_level": a.evidence_level,
                                                             "tags": a.tag or [], "sources": a.source or []})
        led.event("finding.created", actor, a.wi, {"finding": rec["id"], "tags": a.tag or []}, [rec["id"]])
    elif a.kind == "event":
        try:
            payload = json.loads(a.payload or "{}")
        except json.JSONDecodeError as exc:
            fail(f"--payload must be valid JSON: {exc}")
        if (a.type or "").startswith(RESERVED_EVENT_PREFIXES):
            fail(f"{a.type} events are written only by `dureh day`, `dureh site workday` and `dureh site session start`")
        rec = led.event(a.type or fail("--type is required for events"), actor, a.wi, payload, a.ref)
    elif a.kind == "decision":
        payload = {"subject": a.summary or fail("--summary is required"), "disposition": a.disposition,
                   "by": a.by, "memo": a.memo or ""}
        if a.question or a.option or a.chosen or a.fact:
            from .records import parse_fact, typed_block

            payload["typed"] = typed_block(a.question or "", a.option or [], a.chosen or "", a.confidence, a.action,
                                           dict(parse_fact(f) for f in a.fact or []), a.by, actor)
        rec = led.append("decision", a.type or "record", actor, a.wi, payload, a.ref)
    else:
        fail(f"use the dedicated tool for {a.kind} records")
    _print(rec)


def cmd_ledger_query(a):
    from .records import Ledger

    _print(Ledger(_site(a)).sql(a.sql))


def cmd_ledger_index(a):
    from .records import Ledger

    _print(Ledger(_site(a)).build_index())


def cmd_event_add(a):
    """Phase 1 compatibility form of `ledger add event`."""
    from .records import Ledger

    try:
        payload = json.loads(a.payload)
    except json.JSONDecodeError as exc:
        fail(f"--payload must be valid JSON: {exc}")
    if not a.kind.strip() or not a.wi.strip() or not _actor(a).strip():
        fail("--kind, --wi, and --actor must be nonempty")
    if a.kind.startswith(RESERVED_EVENT_PREFIXES):
        fail(f"{a.kind} events are written only by `dureh day`, `dureh site workday`, `dureh site session start`, `dureh slack` and `dureh github`")
    _print(Ledger(_site(a)).event(a.kind, _actor(a), a.wi, payload))


def cmd_event_query(a):
    from .records import Ledger

    _print(Ledger(_site(a)).events(a.wi))


def cmd_status(a):
    from .ops import render_status, status

    st = status(_site(a))
    _print(st if a.json else render_status(st))


def cmd_slurm(a):
    from . import ops

    site = _site(a)
    if a.slurm_command == "submit":
        if a.via is not None:
            if a.module or a.rev:
                raise DurehError("--module with --via is phase 2 (not yet): the module's own jobs run its live checkout; "
                                 "drop --module/--rev or submit a plain script with --script --module")
            for flag, value in (("--script", a.script), ("--each", a.each), ("--max-parallel", a.max_parallel),
                                ("--sbatch-arg", a.sbatch_arg), ("--resubmit-failed", a.resubmit_failed),
                                ("--single", a.single or None), ("--reason", a.reason)):
                if value not in (None, []):
                    raise DurehError(f"{flag} does not go with --via: the module command renders and submits the job itself")
            r = ops.slurm_submit_via(site, _actor(a), a.wi, a.via, record=a.record, after=a.after, dry_run=a.dry_run,
                                     via_module=a.via_module)
        elif a.record is not None or a.via_module is not None:
            raise DurehError("--record and --via-module go with --via")
        elif a.resubmit_failed:
            if a.each:
                raise DurehError("--resubmit-failed takes the items from the run; drop --each")
            r = ops.slurm_resubmit_failed(site, _actor(a), a.resubmit_failed, a.script, a.dry_run, a.sbatch_arg,
                                          a.module, a.rev, a.after, a.max_parallel, wi=a.wi)
        else:
            r = ops.slurm_submit(site, _actor(a), a.wi, a.script, a.dry_run, a.sbatch_arg, module=a.module,
                                 rev=a.rev, each=" ".join(a.each) if a.each else None, after=a.after,
                                 max_parallel=a.max_parallel, single=a.single, reason=a.reason)
        wake = None if a.dry_run else ops.desk_wake(site, _actor(a), r.get("id"))
        _print({**r, "wake": wake} if wake else r)  # release 77 (#38): the desk starts it as a Monitor
    elif a.slurm_command == "status":
        _print(ops.slurm_status(site, _actor(a), a.run))
    elif a.slurm_command == "wait" and a.notify:
        print(ops.wait_notify(site, _actor(a), a.run, a.timeout, a.interval), flush=True)
    elif a.slurm_command == "wait":
        _print(ops.slurm_wait(site, _actor(a), a.run, 3600 if a.timeout is None else a.timeout, a.interval))
    elif a.slurm_command == "watch":
        ops.slurm_watch(site, _actor(a), a.interval, a.once)
    else:
        _print(ops.slurm_cancel(site, _actor(a), a.run))


def cmd_path_refs(a):
    from .ops import path_refs

    r = path_refs(_site(a), a.path)
    print(f"{len(r['hits'])} reference(s) to {', '.join(r['needles'])}" + (" (truncated)" if r["truncated"] else ""))
    for h in r["hits"]:
        print(h)


def cmd_purge(a):
    from .workflow import purge, purge_cache, purge_cache_approve, purge_paths

    if bool(a.wi) == bool(a.project):
        fail("purge takes --wi <WI> or --project <name> --cache <cache> (not both)")
    if a.project:  # release 77 (#40): files in a project cache, with a bound Director approval
        if not a.cache or not a.path:
            fail("purge --project needs --cache <name> and at least one --path")
        if a.approve:
            if a.approval or a.dry_run:
                fail("--approve records the approval; it does not go with --approval or --dry-run")
            from .records import approver

            _print(purge_cache_approve(_site(a), _actor(a), a.project, a.cache, a.path, a.memo or "",
                                       approver(a.by, what="`purge --approve`")))
        else:
            _print(purge_cache(_site(a), _actor(a), a.project, a.cache, a.path, a.approval, a.dry_run))
        return
    for flag, v in (("--cache", a.cache), ("--approval", a.approval), ("--approve", a.approve or None),
                    ("--memo", a.memo), ("--by", a.by)):
        if v is not None:
            fail(f"{flag} goes with --project")
    if a.path:
        _print(purge_paths(_site(a), _actor(a), a.wi, a.path, a.dry_run))
    else:
        _print(purge(_site(a), _actor(a), a.wi, a.dry_run))


def cmd_push(a):
    from . import ops

    site = _site(a)
    if a.approve:
        from .records import approver

        _print(ops.push_approve(site, _actor(a), a.repo, a.remote, a.ref, a.memo or "",
                                approver(a.by, what="`push --approve`")))
    else:
        _print(ops.push(site, _actor(a), a.repo, a.remote, a.ref, a.approval, a.dry_run))


def cmd_usage(a):
    from .ops import usage

    _print(usage(_site(a)))


def cmd_site(a):
    from . import sitelinks

    site = _site(a)
    if a.site_command == "link-module":
        _print(sitelinks.link_module(site, _actor(a), a.id, a.source, a.rev, a.branch, a.relink, a.force))
    elif a.site_command == "new-module":
        _print(sitelinks.new_module(site, _actor(a), a.id, a.package, a.source, a.python, a.depends or [],
                                    a.env_rebuild))
    elif a.site_command == "retire-module":
        out = sitelinks.retire_module(site, _actor(a), a.id, a.dry_run, a.force)
        _print(out)
        if out.get("summary"):
            print(out["summary"])
    elif a.site_command == "new-project":
        _print(sitelinks.new_project(site, _actor(a), a.id, a.purpose))
    else:
        _print(sitelinks.link_project(site, _actor(a), a.name, a.source))


def cmd_site_workday(a):
    from . import workday

    site = _site(a)
    rest = a.rest or []
    if a.action in ("on", "off"):
        _print(workday.switch(site, a.action == "on", a.memo or ""))
    elif a.action == "set":
        if len(rest) != 2 or rest[1] not in ("on", "off"):
            fail(f"usage: dureh site workday set <{'|'.join(workday.DEVICES)}> on|off --memo \"...\"")
        _print(workday.set_device(site, rest[0], rest[1] == "on", a.memo or ""))
    elif a.action == "black-company":
        if rest not in (["on"], ["off"]):
            fail("usage: dureh site workday black-company on|off --memo \"...\"")
        _print(workday.black_company(site, rest == ["on"], a.memo or ""))
    elif a.action == "calibrate":
        if a.weekly_percent is None:
            fail("calibrate needs --weekly-percent (the current week's percentage from Claude Code /usage)")
        _print(workday.calibrate(site, a.weekly_percent))
    else:
        _print(workday.status(site))


def cmd_site_cluster(a):
    from . import cluster

    site = _site(a)
    if a.action == "show":
        _print(cluster.profile(site))
    elif a.write:
        _print(cluster.write(site, _actor(a), cluster.detect(site)))
    else:
        _print(cluster.detect(site))


def sessions_purposes() -> tuple:
    from .sessions.base import PURPOSES

    return PURPOSES


def cmd_site_session(a):
    from . import sessions

    site = _site(a)
    if a.action == "plan":
        print(sessions.plan(site, a.purpose, prompt=a.prompt, no_prompt=a.no_prompt), end="")
        if not a.no_apply_landed:
            from . import upstream
            from .util import harness_label

            pending = upstream.pending_landed_checkout(site)
            order = upstream.release_order(site.pinned_harness_root, pending["checkout"]) if pending else None
            if pending and not order["newer"]:
                print(f"will skip landed {pending['id']}: {order['reason']}; the site keeps its harness")
            elif pending:
                print(f"will switch harness to {harness_label(pending['checkout'])} at {pending['checkout']} "
                      f"(landed {pending['id']}) before submitting; the script above still names the current harness")
    elif a.action == "start":
        rec = sessions.start(site, _actor(a), a.purpose, force=a.force, memo=a.memo, prompt=a.prompt, no_prompt=a.no_prompt,
                             apply_landed=not a.no_apply_landed)
        p = rec["payload"]
        line = ""
        if p.get("harness_switch_skipped"):
            sk = p["harness_switch_skipped"]
            line = f"skipped landed {sk['landed']}: {sk['reason']}; recorded {sk['record']} (harness.switch-skipped). "
        if p.get("harness_switch"):
            hs = p["harness_switch"]
            line = (f"switched harness {hs['from_label']} -> {hs['to_label']} (landed {hs['landed']}); recorded "
                    f"{hs['record']} ({sessions.SWITCHED}). ")
        if (rec.get("office_hours") or {}).get("job_id"):
            print(f"office hours: job {rec['office_hours']['job_id']} submitted (wall {rec['office_hours']['wall']})")
        elif (rec.get("office_hours") or {}).get("error"):
            print(f"note: office hours did not start: {rec['office_hours']['error']}")
        line += (f"Submitted session job {p['job_id']} at {p['submitted_at']} (wall time {p['wall_time']}, purpose "
                 f"{p['purpose']}, " + (f"first prompt {p['prompt']!r}" if p.get("prompt") else "no first prompt")
                 + f"); recorded {rec['id']} (session.started).")
        if p.get("restart"):
            line += (f" Restart: today's job {p['previous']['job_id']} ({p['previous']['record']}) is gone from squeue;"
                     f" the working day still runs from {p['day_start']}.")
        elif p.get("force"):
            line += (f" Forced second start ({p['force']['memo']}): {p['force']['why']};"
                     f" the working day still runs from {p['day_start']}.")
        elif p.get("previous"):
            line += (f" Second session today after the {p['previous']['purpose']} session (job {p['previous']['job_id']},"
                     f" {p['previous']['record']}); the working day still runs from {p['day_start']}.")
        else:
            line += " The working day runs from now."
        print(line)
        for n in p.get("notes") or []:
            print(f"note: {n}")
    elif a.action == "release":
        out = sessions.release(site, _actor(a), cancel_now=a.cancel_now, must_run=True)
        if (out.get("office_hours") or {}).get("event"):
            print(f"office hours stopped ({out['office_hours']['event']}, job {out['office_hours']['job_id']})")
        if a.json:
            _print(out)
        print(sessions.render_release(out))
    else:
        st = sessions.status(site)
        _print(st if a.json else sessions.render_status(st))


def cmd_member(a):
    from . import members

    site, c = _site(a), a.member_command
    if c == "run":
        r = members.run(site, _actor(a), a.wi, fallback=a.fallback, reason=a.reason)
        if a.json:
            _print(r)
        elif r.get("fallback"):
            print(f"fallback recorded ({r['event']}): {r['next']}")
        else:
            from .ops import desk_wake

            wake = desk_wake(site, _actor(a), r.get("run"))  # release 77 (#38): the desk dispatched it itself
            print(f"member run {r['run']}: {r['role']} on {r['runtime']} ({r['model']}) submitted as job {r['job_id']}"
                  f" (wall {r['wall']}); recorded {r['dispatch']}. Next: {r['next']}"
                  + (f"\nwake: {wake}  (start it as a Monitor now; one line when the job ends)" if wake else ""))
    elif c == "exec":
        r = members.execute(site, a.run)
        print(f"member run {a.run}: {r['outcome']}" + (f" ({r['reason']})" if r.get("reason") else ""))
    elif c == "status":
        st = members.status(site, a.wi)
        _print(st if a.json else members.render_status(st))
    elif c == "collect":
        r = members.collect(site, _actor(a), a.wi, a.run, stage=a.stage, summary=a.summary, discard=a.discard,
                            reason=a.reason, rework=a.rework, unchanged=a.unchanged)
        _print(r)
    elif c == "canary":
        from .members import codex

        r = codex.canary(site, _actor(a), members.settings(site))
        if a.json:
            _print(r)
        else:
            print(f"codex canary on {r['host']} ({r['version']}): {r['result'].upper()} ({r['event']})")
            for row in r["rows"]:
                print(f"  {'ok  ' if row['ok'] else 'FAIL'} {row['case']}: exit {row.get('exit')}; {row.get('evidence')}")
        if r["result"] != "pass":
            fail("the codex canary did not pass on this host; codex members will not run here")


def cmd_site_office_hours(a):
    from . import office_hours

    site = _site(a)
    if a.action == "plan":
        print(office_hours.plan(site)["text"], end="")
    elif a.action == "start":
        rec = office_hours.start(site, _actor(a))
        p = rec["payload"]
        print(f"office hours: job {p['job_id']} submitted (wall {p['wall']}); recorded {rec['id']} ({office_hours.STARTED})."
              " The server file appears when the models are ready: `dureh site office-hours status`")
    elif a.action == "stop":
        rec = office_hours.stop(site, _actor(a), a.reason or "stopped by the desk", must_run=True)
        print(f"office hours: job {rec['payload']['job_id']} stopped; recorded {rec['id']} ({office_hours.STOPPED})")
    else:
        st = office_hours.status(site)
        _print(st if a.json else office_hours.render_status(st))


def _early_reason(a) -> str:
    """`--early --reason R` (release 74, #33); a value given to --early itself (the old `--early "<words>"`) is the
    reason when --reason is absent. --reason or --approval without --early is refused."""
    if a.early is None:
        if a.reason or a.approval:
            raise DurehError("--reason and --approval go with --early (ending before the day's end)")
        return ""
    reason = a.reason or (a.early if isinstance(a.early, str) else "")
    if not reason.strip():
        raise DurehError("--early needs --reason \"<why>\" (and, before the day's end, --approval <id>)")
    return reason


def cmd_day(a):
    from . import workday

    site, actor, c = _site(a), _actor(a), a.day_command
    if c == "status":
        st = workday.status(site)
        if a.json:
            _print(st)
        else:
            from .records import Ledger, titled_text

            text = workday.render(st)
            text = titled_text(text, Ledger(site).labels()) if text else text
            _print(text if st.get("enabled") else "\n".join(filter(None, [text, "Workday: off (the Director switches it on)"])))
    elif c == "plan":
        text = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
        if a.addendum:
            if a.director_action:
                raise DurehError("--director-action belongs to the day plan, not to an addendum")
            _print(workday.plan(site, actor, a.goal or "", text, a.wi or [], addendum=True))
        else:
            if not a.goal:
                raise DurehError("day plan: --goal is required (except with --addendum)")
            _print(workday.plan(site, actor, a.goal, text, a.wi or [], a.director_action or []))
    elif c == "approve":
        from .records import approver

        _print(workday.approve(site, actor, a.memo, by=approver(a.by, workday.APPROVERS, "`day approve`"),
                               addendum=a.addendum))
    elif c == "action":
        _print(workday.action_done(site, actor, a.which, a.ref))
    elif c == "decide":
        typed = None
        if a.option or a.question or a.fact:
            from .records import parse_fact, typed_block

            typed = typed_block(a.question or a.title, a.option or [], a.choice, a.confidence, a.action,
                                dict(parse_fact(f) for f in a.fact or []), a.by or "", actor)
        fail_closed = bool(a.fail_closed)
        if a.ask:
            from . import github

            fail_closed = fail_closed or github.ask_fail_closed(site, a.ask)
        _print(workday.decide(site, actor, a.title, a.choice, a.why, a.rev or [], typed=typed, by=a.by or actor,
                              fail_closed=fail_closed, action=a.action, ask=a.ask))
    elif c == "end":
        _print(workday.end(site, actor, a.done, a.problems or "", a.overtime or [], _early_reason(a), a.approval))
    elif c == "overtime":
        text = Path(a.plan).read_text(encoding="utf-8") if a.plan else None
        _print(workday.overtime(site, actor, a.items, a.memo, plan_text=text, goal=a.goal))
    elif c == "overtime-plan":
        _print(workday.overtime_plan(site, actor, Path(a.file).read_text(encoding="utf-8"), a.goal, a.usage))
    elif c == "close":
        from . import sessions

        rec = workday.close(site, actor, a.note or "", cancel_now=a.cancel_now, early=_early_reason(a),
                            approval=a.approval)
        _print(rec)
        print(sessions.render_release(rec["release"]))
    elif c == "urgent":
        _print(workday.urgent(site, actor, a.wi, a.memo))


def cmd_upstream(a):
    from . import upstream

    site = _site(a)
    actor = _actor(a)
    if a.upstream_command == "draft":
        from .records import parse_fact

        _print(upstream.draft(site, actor, a.finding, a.type, a.severity,
                              dict(parse_fact(f) for f in a.fact or []), a.why))
    elif a.upstream_command == "list":
        rows = upstream.list_requests(site)
        if a.json:
            _print(rows)
        else:
            for r in rows:
                print(f"{r['id']}\t{r['type']}\t{r['severity']}\t{r['status']}")
    elif a.upstream_command == "message":
        sys.stdout.write(upstream.message(site))
    elif a.upstream_command == "send":
        _print(upstream.send(site, actor, a.kind, a.body_file))
    elif a.upstream_command == "inbox":
        if a.watch:
            if a.record:
                fail("--watch records nothing; run `upstream inbox --record` for each line it prints")
            upstream.watch_inbox(site, actor, a.interval, emit=lambda line: print(line, flush=True))
            return
        msgs = upstream.inbox(site, actor, a.record)
        if not msgs:
            print("no new messages from upstream")
        for m in msgs:
            print(f"== {m['file']} kind={m['kind']} refs={m['refs']}\n{m['body']}\n")
        if msgs and a.record:
            print(f"recorded {len(msgs)} message(s) as received")
    elif a.upstream_command == "status":
        _print(upstream.status(site, actor, a.ur, a.to, a.rev))
    else:
        _print(upstream.export(site, actor, a.ur, a.out))


def cmd_slack(a):
    from . import slack

    site, c = _site(a), a.slack_command
    if not slack.enabled(site):
        print(slack.OFF_LINE)
        return
    if c == "ask":
        text = Path(a.file).read_text(encoding="utf-8") if a.file else None
        _print(slack.ask(site, _actor(a), a.kind, text, a.no_consult, a.wait, a.until))
    elif c == "opinions":
        if a.watch:
            slack.watch_opinions(site, _actor(a), a.days, a.interval, emit=lambda line: print(line, flush=True))
            return
        res = slack.opinions(site, _actor(a), a.days)
        for item in res["items"]:
            print(json.dumps(item, ensure_ascii=False))
        print(f"opinions: {len(res['items'])} new, {res['ignored']} non-trusted ignored, {res['unasked']} agent "
              f"unrequested ignored, {res['threads']} thread(s)", file=sys.stderr)
    elif c in ("ask-agent", "answer", "consult"):
        text = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
        if c == "ask-agent":
            _print(slack.ask_agent(site, _actor(a), a.agent, text))
        elif c == "answer":
            _print(slack.answer(site, _actor(a), a.id, text))
        else:
            res = slack.consult(site, _actor(a), text, a.wait)
            print(res["text"] if res["answered"] else f"no answer within {a.wait:g} s", flush=True)
            print(json.dumps({k: v for k, v in res.items() if k != "text"}, ensure_ascii=False), file=sys.stderr)
    elif c == "check":
        res = slack.check(site, _actor(a))
        for dec in res["decisions"]:
            print(slack.decision_line(dec))
        for w in res["waiting"]:
            extra = f"; ignored reactions (not the Director): {', '.join(w['ignored'])}" if w["ignored"] else ""
            if w.get("until"):
                extra = f" (until {slack._hhmm(w['until'])}, then approved by the Director's rule of 2026-10-06)" + extra
            print(f"slack approval {w['ts']} ({w['kind']}): waiting for the Director{extra}")
        if not res["decisions"] and not res["waiting"]:
            print("no open Slack approval requests")
    elif c == "watch":
        slack.watch(site, _actor(a), a.interval, emit=lambda line: print(line, flush=True))
    else:
        text = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
        _print(slack.post(site, _actor(a), text, a.dry_run))


def cmd_github(a):
    from . import github

    site, c = _site(a), a.github_command
    if not github.enabled(site):
        print(github.OFF_LINE)
        return
    if c == "status":
        res = github.status(site)
        if a.json:
            _print(res)
        else:
            print("\n".join(github.status_lines(res)))
    elif c == "watch":
        github.watch(site, _actor(a), a.interval, emit=lambda line: print(line, flush=True), polls=1 if a.once else None)
    elif c == "sweep":
        _print(github.sweep(site, _actor(a), a.dry_run))
    elif a.item_command == "add":
        _print(github.item_add(site, _actor(a), a.wi, a.status, a.project, a.layer, a.units, a.repo, a.url,
                               a.priority, a.start, a.target))
    elif a.item_command == "set":
        _print(github.item_set(site, _actor(a), a.wi, a.url, a.status, a.project, a.layer, a.units, a.decision,
                               a.priority, a.start, a.target, a.on_hold))
    elif a.item_command == "ask":
        body = Path(a.body_file).read_text(encoding="utf-8")
        _print(github.item_ask(site, _actor(a), a.title, body, a.options, a.layer, a.wi, a.project, a.repo, a.units,
                               fail_closed=a.fail_closed, decide=a.decide, recommend=a.recommend,
                               otherwise=a.otherwise, on_hold=a.on_hold))
    elif a.item_command == "close":
        _print(github.item_close(site, _actor(a), a.wi, a.url, a.comment))
    else:
        text = Path(a.file).read_text(encoding="utf-8") if a.file else sys.stdin.read()
        _print(github.comment(site, _actor(a), text, a.wi, a.url))


def cmd_report(a):
    from . import slack

    site = _site(a)
    text = Path(a.file).read_text(encoding="utf-8") if a.file else ("" if sys.stdin.isatty() else sys.stdin.read())
    res = slack.report(site, _actor(a), a.kind, text, a.wait)
    _print(res)
    if res["session"]:
        print(slack.SESSION_LINE)


def cmd_authorize_write(a):
    from .paths import write_decision

    site = _site(a)
    site.org.role(a.role)
    ok, why = write_decision(site, a.role, site.root / a.path if not os.path.isabs(a.path) else Path(a.path))
    if not ok:
        fail(f"unauthorized write denied: role {a.role!r} cannot write {a.path!r}: {why}")
    print(f"authorized: {a.role} -> {a.path} ({why})")


def cmd_worktree(a):
    from .workflow import env_repair, prune_stale_venvs, worktree_repair, worktree_repair_all

    if a.prune_stale:
        if a.wi or not (a.all or a.target) or (a.all and a.target):
            fail("--prune-stale goes with --all (every root) or --target <target> (one main tree), not --wi")
        if a.envs_only or a.no_envs or a.force:
            fail("--prune-stale takes none of --envs-only, --no-envs and --force")
        r = prune_stale_venvs(_site(a), _actor(a), target=a.target, remove=a.remove and not a.dry_run)
        if a.json:
            _print(r)
            return
        for line in prune_stale_lines(r):
            print(line)
        return
    if a.remove:
        fail("--remove goes with --prune-stale")
    if a.all:
        if a.wi or a.target:
            fail("--all covers every worktree and environment of the site; it takes neither --wi nor --target")
        if a.envs_only and a.no_envs:
            fail("give at most one of --envs-only and --no-envs")
        r = worktree_repair_all(_site(a), _actor(a), dry_run=a.dry_run, envs=not a.no_envs, envs_only=a.envs_only,
                                force=a.force)
        if a.json:
            _print(r)
            return
        for line in repair_all_lines(r):
            print(line)
        return
    if a.envs_only or a.no_envs:
        fail("--envs-only and --no-envs go with --all")
    if bool(a.wi) == bool(a.target):
        fail("give exactly one of --wi (a WI worktree), --target (a target's main tree, environment only) or --all")
    if a.wi:
        r = worktree_repair(_site(a), _actor(a), a.wi, dry_run=a.dry_run, force=a.force)
    else:
        r = env_repair(_site(a), _actor(a), a.target, force=a.force, dry_run=a.dry_run)
    _print(r)


def repair_all_lines(r: dict) -> list[str]:
    """One table-like line per item of `worktree repair --all` and a summary line (a dry run says "would")."""
    dry = r["summary"]["dry_run"]
    would = {"repaired": "would repair", "pruned": "would prune", "rebuilt": "would rebuild"} if dry else {}
    out = []
    for row in r["items"]:
        state = would.get(row["state"], row["state"])
        note = row.get("error") or row.get("reason") or row.get("detail") or ""
        if row.get("env"):
            note = f"env {would.get(row['env'], row['env'])}" + (f": {row['env_reason']}" if row.get("env_reason") else "")
        if row.get("stale") and row["state"] != "error":
            note = f"{note} (named {row['stale']})" if note else f"named {row['stale']}"
        if row.get("stale_lock") and row["state"] != "error":
            note = f"{note} ({row['stale_lock']})" if note else row["stale_lock"]
        out.append(f"{row['kind']:<9} {row['name']:<40} {(row.get('target') or '-'):<18} {state:<14} {note}".rstrip())
    w, v = r["summary"]["worktrees"], r["summary"]["venvs"]
    fmt = lambda c: ", ".join(f"{n} {would.get(k, k)}" for k, n in c.items())
    out.append(f"summary: worktrees {fmt(w)}; environments {fmt(v)}" if r["summary"]["envs"]
               else f"summary: worktrees {fmt(w)} (environments not checked: --no-envs)")
    if dry:
        out.append("dry run: nothing was changed or recorded")
    return out


def _size(n: int) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def prune_stale_lines(r: dict) -> list[str]:
    """One line per `.venv.stale-*` copy of `worktree repair --prune-stale` and a summary line."""
    out = []
    for row in r["items"]:
        state = {"listed": "would remove"}.get(row["state"], row["state"])
        note = row.get("error") or _size(row["size"])
        out.append(f"{row['kind']:<10} {row['name']:<50} {(row.get('target') or '-'):<18} {state:<13} {note}".rstrip())
    s = r["summary"]
    if not r["items"]:
        out.append("summary: no .venv.stale-* copies found")
    elif s["remove"]:
        out.append(f"summary: {s['removed']} of {s['items']} stale copies removed, {_size(s['size'])}"
                   + (f"; {s['errors']} error(s)" if s["errors"] else ""))
    else:
        out.append(f"summary: {s['items']} stale copies, {_size(s['size'])}; nothing removed (add --remove)")
    return out


def cmd_protected(a):
    from .protect import listing, render_listing

    out = listing(_site(a))
    _print(out if a.json else render_listing(out))


def cmd_artifact_write(a):
    from .paths import write_decision
    from .util import reject_symlinks, safe_relative, write_create_only

    site = _site(a)
    site.org.role(a.role)
    rel = safe_relative(a.path)
    ok, why = write_decision(site, a.role, site.root / rel.as_posix())
    if not ok:
        fail(f"unauthorized write denied: role {a.role!r} cannot write {rel.as_posix()!r}: {why}")
    target = reject_symlinks(site.root, rel, include_leaf=False)
    source = Path(a.input).expanduser()
    if source.is_symlink() or not source.is_file():
        fail(f"required input file is missing or is a symlink: {source}")
    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    reject_symlinks(site.root, rel, include_leaf=False)
    write_create_only(target, data, 0o644)
    print(f"wrote {len(data)} bytes: {rel.as_posix()}")


# ------------------------------------------------------------------- parser
def _early_args(x, what: str) -> None:
    """`day end` / `day close`: --early --approval <id> --reason R (release 74, #33)."""
    x.add_argument("--early", nargs="?", const=True, default=None, metavar="WORDS",
                   help=f"{what} before the day's planned end (needs --approval and --reason; `day status` shows the end "
                        "in local time and UTC)" + (", or past it with plan items still open (--reason alone)"
                                                   if what == "end" else ""))
    x.add_argument("--approval", help="with --early: the recorded approval (a Director/DUREH `approve` decision id, or "
                                      "a board Decision = Approve 1: its github.decision.changed event id, issue URL or "
                                      "org/repo#N)")
    x.add_argument("--reason", help="with --early: why the day ends early (the Director's words)")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dureh", description="DUREH harness tools: wi, dispatch, verify, ledger, event, status, "
                                "slurm, path-refs, purge, push, usage, site, day, upstream, slack, github, report, "
                                "authorize-write, worktree, protected, artifact, member (plus install, adapter, hook).")
    p.add_argument("--site", help="installed site directory (or DUREH_SITE)")
    p.add_argument("--observe", action="store_true",
                   help="read-only: allow only status, wi show/list, ledger query, upstream list/message/inbox (without "
                        "--record; --watch included), path-refs, usage, verify --self, day status, site session status, "
                        "protected list, member status, github status; never record anything (or DUREH_OBSERVE=1)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(parent, name, func, help_):
        sp = parent.add_parser(name, help=help_)
        sp.set_defaults(func=func)
        return sp

    x = add(sub, "install", cmd_install, "install a startup package into a new, empty site")
    x.add_argument("--startup", required=True)
    x.add_argument("--site", dest="target_site")
    x.add_argument("--preset", help="JSON preset with site settings and persona answers")
    x.add_argument("--persona", help="JSON file with persona answers")
    x.add_argument("--interactive", action="store_true")
    x.add_argument("--skip-unit", action="store_true", help="skip harness unit tests in the install self-test")

    ad = sub.add_parser("adapter", help="runtime adapter output").add_subparsers(dest="adapter_command", required=True)
    x = add(ad, "sync", cmd_adapter_sync, "re-copy the package from its source at a committed revision (config "
            "startup.source; refused with uncommitted changes), validate it, and regenerate runtime files")
    x.add_argument("--harness", help="switch this site to that harness checkout (a release under releases/) and sync from it")
    x.add_argument("--package-rev", metavar="REV", help="the package source revision to install (default: its committed HEAD)")

    h = add(sub, "hook", cmd_hook, "runtime hook entry point (reads the event JSON on stdin)")
    h.add_argument("runtime", choices=["claude-code"])
    h.add_argument("--timeout", type=float, help="the runtime's timeout for this hook in seconds (the generated "
                   "settings pass it); a PreToolUse check answers within 80%% of it, refusing past that")

    wi = sub.add_parser("wi", help="work items").add_subparsers(dest="wi_command", required=True)
    x = add(wi, "new", cmd_wi_new, "create a work item (desk)")
    x.add_argument("--title", required=True)
    x.add_argument("--label", help="short title (at most 40 characters) shown with the WI id to the Director;"
                                   " default: the title, cut to 40 characters")
    x.add_argument("--goal", required=True)
    x.add_argument("--role", required=True)
    x.add_argument("--target", required=True, help="harness | package | site | modules/<name> | projects/<name>")
    x.add_argument("--acceptance", action="append", required=True)
    x.add_argument("--lane", default="work", choices=["work", "reorg", "desk"])
    x.add_argument("--bundle")
    x.add_argument("--parent", help="bundle work item owned by a manager")
    x.add_argument("--fix-for", help="the work item whose problem this one fixes (after the end time, a fix for a day "
                                     "plan item goes on under the finish-plan rule)")
    x.add_argument("--value")
    x.add_argument("--check", action="append", help="command the checker runs in the WI worktree")
    x.add_argument("--output", action="append", help="declared output path that must exist")
    x.add_argument("--constraint", action="append")
    x.add_argument("--academic", action="store_true")
    x.add_argument("--checks-runner", choices=["local", "site"],
                   help="local: run this item's checks in the verify process (each must match config checks.local_allow);"
                        " site (default): config checks.runner")
    x.add_argument("--requirements", metavar="FILE",
                   help="a requirements file inside the site, pinned by sha256; the brief names it (read first) instead"
                        " of carrying its text, so only that line counts toward the brief limit")
    x.add_argument("--actor")
    x = add(wi, "approve", cmd_wi_approve, "record the Gate 1 approval (desk); --by names the approver")
    x.add_argument("--wi", required=True)
    x.add_argument("--memo", required=True)
    x.add_argument("--by", help="who approved: Director, DUREH or desk (required, no default; release 78)")
    x.add_argument("--actor")
    x = add(wi, "amend", cmd_wi_amend, "change acceptance/checks/constraints/outputs or move the base (desk; Director's words)")
    x.add_argument("--wi", required=True)
    x.add_argument("--memo", required=True)
    x.add_argument("--acceptance", action="append", help="replaces the whole list (repeat)")
    x.add_argument("--check", action="append", help="replaces the whole list (repeat)")
    x.add_argument("--constraint", action="append", help="replaces the whole list (repeat)")
    x.add_argument("--add-constraint", action="append",
                   help="appends to the constraints (repeat); the only amendment allowed on a staged item")
    x.add_argument("--output", action="append", help="replaces the whole list (repeat)")
    x.add_argument("--rebase", nargs="?", const="", default=None,
                   help="move the base to REV (default: the target's current HEAD); the next brief tells the worker to rebase")
    x.add_argument("--checks-runner", choices=["local", "site"],
                   help="per-item checks runner, as for wi new (local needs every check in checks.local_allow)")
    x.add_argument("--requirements", metavar="FILE",
                   help="pin (or re-pin after a change) the item's requirements file by sha256, as for wi new")
    x.add_argument("--reopen", action="store_true",
                   help="re-open a verified, unpromoted item: the verification is void and it goes back to approved"
                        " for a new dispatch (no dispatch stop); other changes are optional")
    x.add_argument("--actor")
    x = add(wi, "show", cmd_wi_show, "show work items with their events (repeat --wi; --summary for status and last events)")
    x.add_argument("--wi", required=True, action="append")
    x.add_argument("--summary", action="store_true")
    x = add(wi, "list", cmd_wi_list, "list work items with projected status")
    x.add_argument("--status")
    x = add(wi, "stage", cmd_wi_stage, "mark committed work ready for the checker")
    x.add_argument("--wi", required=True)
    x.add_argument("--summary", required=True)
    x.add_argument("--actor")
    x = add(wi, "promote", cmd_wi_promote, "Gate 2: merge a verified work item (desk); a merge conflict refuses before the merge")
    x.add_argument("--wi", required=True)
    x.add_argument("--memo", required=True)
    x.add_argument("--public-summary", help="release 82, a public module only (config modules[].public): the merge"
                   " commit message `<type>: <short English summary>` instead of the WI's label")
    x.add_argument("--actor")

    d = sub.add_parser("dispatch", help="dispatch contract").add_subparsers(dest="dispatch_command", required=True)
    x = add(d, "brief", cmd_dispatch_brief, "prepare a recorded brief for spawning one agent")
    x.add_argument("--wi", required=True)
    x.add_argument("--role", help="checker, to verify a staged item")
    x.add_argument("--actor")
    x = add(d, "stop", cmd_dispatch_stop, "record a stop for a dispatched work item")
    x.add_argument("--wi", required=True)
    x.add_argument("--reason", required=True)
    x.add_argument("--actor")

    mb = sub.add_parser("member", help="member runtimes (release 64): run a role whose runtime is not claude-code "
                        "(codex, local) as one scheduler job").add_subparsers(dest="member_command", required=True)
    x = add(mb, "run", cmd_member, "after `dispatch brief`: record the dispatch and submit ONE job that runs the member;"
            " returns at once (end your turn). --fallback records the role's fallback instead (then spawn the role as a"
            " Claude subagent with the same brief)")
    x.add_argument("--wi", required=True)
    x.add_argument("--fallback", action="store_true", help="run this brief as a Claude subagent instead (needs --reason)")
    x.add_argument("--reason", help="--fallback: why")
    x.add_argument("--json", action="store_true")
    x.add_argument("--actor")
    x = add(mb, "exec", cmd_member, "inside the job `member run` submitted, and only there: run the member and record it")
    x.add_argument("--run", required=True)
    x = add(mb, "status", cmd_member, "read-only: the item's member runs (job, run status, outcome, collection)")
    x.add_argument("--wi", required=True)
    x.add_argument("--json", action="store_true")
    x = add(mb, "collect", cmd_member, "record an ended member run: a checker's verdict becomes the verification (bound to"
            " the verified head; a codex FAIL is held, the item stays staged until --rework or `member run --fallback`);"
            " a worker's changes stay for review (--stage --summary commits and stages them, --stage --unchanged"
            " --reason re-stages an unchanged head, --discard --reason restores them)")
    x.add_argument("--wi", required=True)
    x.add_argument("--run", help="a member run id (default: the item's latest)")
    x.add_argument("--stage", action="store_true", help="local worker: commit its changes and stage the item")
    x.add_argument("--summary", help="--stage: the stage summary")
    x.add_argument("--unchanged", action="store_true",
                   help="with --stage: re-stage the same, already staged head for re-verification (needs --reason)")
    x.add_argument("--discard", action="store_true", help="local worker: restore its changes")
    x.add_argument("--rework", action="store_true",
                   help="a held codex FAIL: choose rework (records rework.requested from the held verification)")
    x.add_argument("--reason", help="--discard, --stage --unchanged, --rework: why")
    x.add_argument("--actor")
    x = add(mb, "canary", cmd_member, "prove on this host that the codex member profile refuses reads outside the"
            " evidence bundle and every write (codex sandbox, no model); recorded as member.canary")
    x.add_argument("--runtime", required=True, choices=["codex"])
    x.add_argument("--json", action="store_true")
    x.add_argument("--actor")

    x = add(sub, "verify", cmd_verify, "self-test, per-WI verification (declared checks as one scheduler job), or exam scoring")
    x.add_argument("--self", action="store_true")
    x.add_argument("--skip-unit", action="store_true")
    x.add_argument("--wi")
    x.add_argument("--local", action="store_true",
                   help="with --wi: run the declared checks in this process instead of a scheduler job "
                        "(escape hatch; the default is config checks.runner: slurm where sbatch exists, else local)")
    x.add_argument("--submit", action="store_true",
                   help="with --wi: submit the declared-checks job and return at once with a verify run id (checker;"
                        " end the turn, then --collect when `slurm watch` reports the job ended); with --self (release"
                        " 77): check the site here and submit the harness unit suite as one scheduler job")
    x.add_argument("--collect", nargs="?", const="", default=None, metavar="RUN",
                   help="with --wi: record the verification of a finished submitted verify job (default: the latest);"
                        " with --self: record the unit-suite job's outcome")
    x.add_argument("--exam", help="role whose exam to score")
    x.add_argument("--task")
    x.add_argument("--answers")
    x.add_argument("--actor")

    led = sub.add_parser("ledger", help="records").add_subparsers(dest="ledger_command", required=True)
    x = add(led, "add", cmd_ledger_add, "append a record (finding, event, decision)")
    x.add_argument("kind", choices=["finding", "event", "decision"])
    x.add_argument("--wi")
    x.add_argument("--type")
    x.add_argument("--summary")
    x.add_argument("--evidence-level", default="proposed", choices=["measured", "reviewed", "inferred", "proposed"])
    x.add_argument("--tag", action="append")
    x.add_argument("--source", action="append")
    x.add_argument("--payload")
    x.add_argument("--ref", action="append")
    x.add_argument("--disposition", default="record", choices=["approve", "revise", "defer", "reject", "record"])
    x.add_argument("--by", default="desk")
    x.add_argument("--memo")
    x.add_argument("--question", help="typed decision: the one question answered (with --option, --chosen; "
                                      "`--type triage` for UR triage)")
    x.add_argument("--option", action="append", help="typed decision: one of the fixed options (repeat, two or more)")
    x.add_argument("--chosen", help="typed decision: the option chosen")
    x.add_argument("--confidence", default="medium", choices=["high", "medium", "low"],
                   help="typed decision: low requires --action escalated or provisional")
    x.add_argument("--action", default="acted", choices=["acted", "escalated", "provisional"],
                   help="typed decision: escalated names the Director in --by; provisional is listed in the end report")
    x.add_argument("--fact", action="append", help="typed decision: name=yes|no the choice rests on (repeatable)")
    x.add_argument("--actor")
    x = add(led, "query", cmd_ledger_query, "read-only SQL over the rebuildable index")
    x.add_argument("sql")
    add(led, "index", cmd_ledger_index, "rebuild the node-local index from records")

    ev = sub.add_parser("event", help="Phase 1 event interface").add_subparsers(dest="event_command", required=True)
    x = add(ev, "add", cmd_event_add, "append one immutable event")
    x.add_argument("--kind", required=True)
    x.add_argument("--wi", required=True)
    x.add_argument("--actor", required=True)
    x.add_argument("--payload", required=True)
    x = add(ev, "query", cmd_event_query, "list events, optionally for one WI")
    x.add_argument("--wi")

    x = add(sub, "status", cmd_status, "work items, lanes, pending gates, index freshness")
    x.add_argument("--json", action="store_true")

    sl = sub.add_parser("slurm", help="scheduler jobs").add_subparsers(dest="slurm_command", required=True)
    x = add(sl, "submit", cmd_slurm, "submit and record a job for a dispatched WI (one job per subject: --each; "
                                      "a module's own submit command: --via)")
    x.add_argument("--wi", required=True)
    x.add_argument("--script", help="job script (required, except with --resubmit-failed: default the run's script)")
    x.add_argument("--via", metavar="COMMAND",
                   help="run this module submit command (one quoted string) in the WI's job root with the site environment "
                        "and ingest the submission record it writes (docs/slurm.md \"via\"); the record path comes from "
                        "--record, else config modules[<m>].submit.record_arg, else the last stdout line "
                        "`submission-record: <path>`. Not with --script/--each/--module (--module + --via is phase 2)")
    x.add_argument("--record", metavar="PATH", help="with --via: the submission record the command writes (relative to "
                                                    "the job root); passed to the module when record_arg is configured")
    x.add_argument("--via-module", metavar="MODULE",
                   help="with --via: the config modules[] entry whose `submit` options the harness may add (default: "
                        "the entry whose cli/id is the command's first word; needed for wrapped commands such as `uv run`)")
    x.add_argument("--each", nargs="+", metavar="ITEMS",
                   help="one array task per item (all at once, no cap): `a,b`, `a b` or @file (one per line, # comments; "
                        "inside the site); an item's level comes from the package's labels.item_grammar (else plain), one "
                        "level per array; the job gets the item, level, index and the grammar's named fields under config "
                        "slurm.each_env names, default DUREH_ITER, DUREH_ITER_BY, DUREH_ITER_INDEX, DUREH_ITER_<FIELD>")
    x.add_argument("--single", action="store_true",
                   help="one job without --each when the loop rule (config slurm.each_level) is on (needs --reason)")
    x.add_argument("--reason", help="why one job (--single)")
    x.add_argument("--after", action="append", metavar="RUN",
                   help="start only after these runs completed (afterok on their job; an array waits for all tasks); "
                        "comma list or repeated")
    x.add_argument("--resubmit-failed", metavar="RUN",
                   help="a new array over the items of an --each run whose task did not complete; for a --via run the "
                        "module command runs again with its configured items_arg @<failed items file>")
    x.add_argument("--max-parallel", type=int, metavar="N",
                   help="with --each: at most N tasks at once (default: none; Slurm schedules)")
    x.add_argument("--sbatch-arg", action="append")
    x.add_argument("--module", help="run against a read-only `git archive` tree of modules/<m> at a pinned revision "
                                    "(exports the module's root_env and rev_env from config, else DUREH_MODULE_ROOT and DUREH_MODULE_REV)")
    x.add_argument("--rev", help="with --module: revision to run (default: the WI project's manifest pin "
                                 "provenance.tool_versions.<m>); a different revision is recorded as rev_override")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--actor")
    for name in ("status", "cancel"):
        x = add(sl, name, cmd_slurm, f"{name} a recorded job")
        x.add_argument("--run", required=True)
        x.add_argument("--actor")
    x = add(sl, "wait", cmd_slurm, "block until a recorded job ends (records its final state); instead of polling loops")
    x.add_argument("--run", required=True)
    x.add_argument("--timeout", type=int, default=None, help="seconds (default 3600; with --notify: no limit)")
    x.add_argument("--interval", type=int, default=60, help="seconds between checks (default 60)")
    x.add_argument("--notify", action="store_true",
                   help="release 77: wait until the job ends and print ONE line (final state, run, job, WI, next step);"
                        " the desk starts it as a Monitor right after submitting a job itself")
    x.add_argument("--actor")
    x = add(sl, "watch", cmd_slurm, "desk only: print one line per tracked job as it ends (run, job, WI, role, agent, "
                                    "state, next step) and record its final state; run it as a Monitor, re-dispatch "
                                    "the owning WI when a line arrives")
    x.add_argument("--interval", type=int, default=300, help="seconds between scheduler checks (default 300; one sacct call per check)")
    x.add_argument("--once", action="store_true", help="one pass, then exit")
    x.add_argument("--actor")

    x = add(sub, "path-refs", cmd_path_refs, "find references to a path before moving or deleting it")
    x.add_argument("path")
    x = add(sub, "purge", cmd_purge, "remove a finished WI's worktree, scratch outputs and its own jobs' scheduler logs; "
                                     "with --path, only untracked or ignored paths in a dispatched or staged WI's "
                                     "worktree (release 72)")
    x.add_argument("--wi")
    x.add_argument("--path", action="append", help="a path relative to the WI's worktree to remove (repeatable): "
                                                   "untracked or ignored only, never a tracked file or a path that "
                                                   "resolves outside the worktree; the WI must be dispatched or staged."
                                                   " With --project: a file in projects/<name>/cache/<cache>/")
    x.add_argument("--project", help="release 77: delete files from projects/<name>/cache/<cache>/ (desk only; never a"
                                     " file pinned in config.json or the project manifest; needs --approval)")
    x.add_argument("--cache", help="with --project: the cache directory name, e.g. containers")
    x.add_argument("--approval", help="with --project: the Director's purge-approval decision id (bound to the files"
                                      " and their sha256); required, no default")
    x.add_argument("--approve", action="store_true", help="with --project: record the Director's approval for these"
                                                          " exact files (needs --memo), like `push --approve`")
    x.add_argument("--memo", help="with --approve: where the Director approved (board item, message)")
    x.add_argument("--by", help="with --approve: who approved, Director, DUREH or desk (required, no default; "
                                "release 78)")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--actor")
    x = add(sub, "push", cmd_push, "push an approved revision to an allowlisted remote; without --approval only a "
                                   "fast-forward of an existing branch under config push.standing_ff (release 58) or the remote's "
                                   "push.allowed_remotes rule (release 82: branches, standing_ff, new_branches, tags)")
    x.add_argument("--repo", required=True)
    x.add_argument("--remote", required=True)
    x.add_argument("--ref", required=True)
    x.add_argument("--approval", help="the Director's push-approval decision id; omitted, the push needs config "
                                      "push.standing_ff and must fast-forward the destination branch")
    x.add_argument("--approve", action="store_true", help="record the Director's approval bound to this revision")
    x.add_argument("--memo")
    x.add_argument("--by", help="with --approve: who approved, Director, DUREH or desk (required, no default; "
                                "release 78)")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--actor")
    add(sub, "usage", cmd_usage, "tokens per WI and per promoted WI from recorded agent runs")

    st = sub.add_parser("site", help="Director-side site setup").add_subparsers(dest="site_command", required=True)
    x = add(st, "link-module", cmd_site, "clone a module repository at a pinned revision into modules/<id>; with "
            "--relink change a linked module's revision (and branch) in place")
    x.add_argument("--id", required=True)
    x.add_argument("--source", help="the repository to clone (required unless --relink)")
    x.add_argument("--rev", required=True)
    x.add_argument("--branch", help="release 65: the local branch, created at --rev and tracking origin/<branch> "
                   "(recorded in the module entry; default main)")
    x.add_argument("--relink", action="store_true", help="release 65: move a linked module to --rev [--branch] in place;"
                   " refuses a dirty checkout or unpushed local commits")
    x.add_argument("--force", action="store_true", help="with --relink, for a push-off module only: relink despite a "
                   "dirty checkout or unpushed local commits")
    x.add_argument("--actor")
    x = add(st, "new-module", cmd_site, "create a NEW module repository from the standard skeleton and register it")
    x.add_argument("--id", required=True)
    x.add_argument("--package", required=True, help="Python package name (src/<package>)")
    x.add_argument("--source", help="create the repository here (outside the site) and link it; default modules/<id>")
    x.add_argument("--python", help="python pin (default: an existing module's .python-version, else 3.11)")
    x.add_argument("--depends", action="append", help="site module it imports; pinned by git revision (repeat)")
    x.add_argument("--env-rebuild", default="uv sync --frozen --all-groups --all-extras")
    x.add_argument("--actor")
    x = add(st, "retire-module", cmd_site, "retire a site module (release 55): refuse live work, a dirty or unpushed "
            "checkout and structural references (--force), remove final WIs' worktrees and branches, the config entry "
            "and modules/<id>, then record site.module-retired with the re-link command (release 57: after the removal; "
            "a failure mid-way records site.module-retire-failed) and regenerate the adapter files")
    x.add_argument("--id", required=True)
    x.add_argument("--dry-run", action="store_true", help="list every step with its paths and the refusal it would hit; change nothing")
    x.add_argument("--force", action="store_true", help="retire despite structural references (never a dirty or unpushed checkout)")
    x.add_argument("--actor")
    x = add(st, "link-project", cmd_site, "DataLad-clone a project into projects/<name> without fetching data")
    x.add_argument("--name", required=True)
    x.add_argument("--source", required=True)
    x.add_argument("--actor")
    x = add(st, "new-project", cmd_site, "create a records project projects/<id> (git: research-notes/, reports/, "
            "runs/; no data, no DataLad) where the package allows it (startup.yaml `projects: {records: true}`); "
            "a plain directory already there is adopted")
    x.add_argument("--id", required=True)
    x.add_argument("--purpose")
    x.add_argument("--actor")

    x = add(st, "workday", cmd_site_workday, "Director's switches: workday mode on|off, `set <device> on|off` "
                                              "(hours, weekday, usage, session), `black-company on|off`, calibrate, status")
    x.add_argument("action", choices=["on", "off", "set", "black-company", "calibrate", "status"])
    x.add_argument("rest", nargs="*", help="set: <device> on|off; black-company: on|off")
    x.add_argument("--memo", help="the Director's reason (on/off, set, black-company)")
    x.add_argument("--weekly-percent", type=_percent, help="calibrate: current week's usage percent shown by Claude Code /usage")

    x = add(st, "cluster", cmd_site_cluster, "cluster profile: detect from sinfo/sacctmgr, or show config.json `cluster`")
    x.add_argument("action", choices=["detect", "show"])
    x.add_argument("--write", action="store_true", help="detect: save into config.json `cluster` (desk/Director)")
    x.add_argument("--actor")
    x = add(st, "office-hours", cmd_site_office_hours, "release 64: the model server job for the local member runtime"
            " (config office_hours): plan (print the script), start, stop, status")
    x.add_argument("action", choices=["plan", "start", "stop", "status"])
    x.add_argument("--reason", help="stop: why")
    x.add_argument("--json", action="store_true", help="status: machine-readable")
    x.add_argument("--actor")
    x = add(st, "session", cmd_site_session, "the day's session job: plan (print the sbatch script, submit nothing), "
                                               "start (submit it and record session.started), status (read-only), "
                                               "release (end the recorded running job by close marker; --cancel-now scancels)")
    x.add_argument("action", choices=["plan", "start", "status", "release"])
    x.add_argument("--cancel-now", action="store_true",
                   help="release: scancel the recorded session job at once instead of writing the close marker "
                        "(the fallback when a marker was ignored)")
    x.add_argument("--purpose", default="desk", choices=list(sessions_purposes()),
                   help="desk: the day's session (default); overtime: a new session for approved overtime")
    x.add_argument("--force", action="store_true", help="start: a second session today although today's job is still "
                                                        "in squeue; needs --memo, recorded in session.started `force`")
    x.add_argument("--memo", help="start --force: why a second session starts today")
    g = x.add_mutually_exclusive_group()
    g.add_argument("--prompt", help="plan/start: the session's first prompt, replacing the default (desk: none; "
                                    "overtime: \"<desk name>, 추가근무 출근\" from state/persona; config session.prompts.<purpose>)")
    g.add_argument("--no-prompt", action="store_true", help="plan/start: no first prompt this time")
    x.add_argument("--no-apply-landed", action="store_true",
                   help="start: do not switch the site to a pending landed release checkout (the newest `landed` mailbox "
                        "message) before submitting; plan: do not mention it")
    x.add_argument("--json", action="store_true", help="status/release: machine-readable")
    x.add_argument("--actor")

    dy = sub.add_parser("day", help="workday mode: day plan, approval, decisions, end report, overtime"
                        ).add_subparsers(dest="day_command", required=True)
    x = add(dy, "status", cmd_day, "phase of the day and today's Claude usage against the daily share")
    x.add_argument("--json", action="store_true")
    x = add(dy, "plan", cmd_day, "record the day plan for the Director (text from --file or stdin); with --addendum, "
                                 "an addendum to today's approved, open plan")
    x.add_argument("--goal", help="the day's goal (required for a day plan; optional with --addendum)")
    x.add_argument("--addendum", action="store_true",
                   help="release 73: record an addendum to today's approved and open day plan (`day.planned` with "
                        "addendum: true; the day does not restart); once approved with `day approve --addendum` its "
                        "WIs count into the day like the plan's own")
    x.add_argument("--file")
    x.add_argument("--wi", nargs="*", help="the plan's work items (default: every WI id the plan text names); after "
                                           "the end time they and their fixes go on until finished (finish-plan rule)")
    x.add_argument("--director-action", action="append", metavar="TEXT",
                   help="an action only the Director can take today (a dataset link, a push approval, a config "
                        "change; repeatable, release 58): recorded as director_actions and posted by `slack ask "
                        "--kind day-plan` under 대표님 조치 필요 so the Director handles them in one go at approval")
    x = add(dy, "approve", cmd_day, "record the approval of the plan (the Director's, or DUREH's under approval layer 2); "
                                    "starts the clock (workday mode on)")
    x.add_argument("--memo", required=True, help="the approver's words")
    x.add_argument("--by", help="who approved: Director (layer 3) or DUREH (approval layer 2, CHARTER; valid with "
                                "workday mode on or off); required, no default (release 78)")
    x.add_argument("--addendum", action="store_true", help="release 73: approve the day's latest addendum "
                                                           "(`day plan --addendum`); the day's clock is unchanged")
    x = add(dy, "action", cmd_day, "#32: `day action done <n|text> [--ref R]`: mark one of today's Director actions "
                                   "done; `dureh status` stops listing it and the end report names it as done")
    x.add_argument("verb", choices=["done"])
    x.add_argument("which", help="the action's number in the plan (from 1) or a unique part of its text")
    x.add_argument("--ref", help="what shows it done (a commit, an issue URL, a record id)")
    x = add(dy, "decide", cmd_day, "record a decision point (own proposal followed) and commit state as a rollback point")
    x.add_argument("--title", required=True)
    x.add_argument("--choice", required=True)
    x.add_argument("--why", required=True)
    x.add_argument("--rev", action="append", help="commit(s) made for this decision (repeatable)")
    x.add_argument("--option", action="append", help="typed decision (release 52): the fixed options --choice was "
                                                      "picked from (repeat, two or more); also writes a `day-choice` "
                                                      "decision record referencing the event")
    x.add_argument("--question", help="typed decision: the question answered (default: --title)")
    x.add_argument("--confidence", default="medium", choices=["high", "medium", "low"])
    x.add_argument("--action", default="acted", choices=["acted", "escalated", "provisional"])
    x.add_argument("--fact", action="append", help="typed decision: name=yes|no the choice rests on (repeatable)")
    x.add_argument("--by", help="typed decision: who decides; required (not the desk) with --action escalated")
    x.add_argument("--fail-closed", action="store_true",
                   help="release 78: the decision answers a fail-closed kind: --action provisional is refused and an "
                        "acted choice needs the approver in --by")
    x.add_argument("--ask", metavar="URL", help="release 78: the board ask this decision answers; an ask recorded with "
                                                "`github item ask --fail-closed` makes the decision fail-closed")
    x = add(dy, "end", cmd_day, "record the end report (done, problems, proposed overtime WIs); after the end time "
                                "only when the plan is finished or a stop hit (usage share, data at risk, wall time)")
    x.add_argument("--done", required=True)
    x.add_argument("--problems")
    x.add_argument("--overtime", nargs="*", help="WIs proposed for overtime (fixes and normalization tests only)")
    _early_args(x, "end")
    x = add(dy, "overtime", cmd_day, "record the overtime items the Director accepted (after the end report, or while "
                                     "on-duty as soon as the Director approves: it takes effect at once)")
    x.add_argument("--items", nargs="+", required=True)
    x.add_argument("--memo", required=True)
    x.add_argument("--plan", help="file with the overtime portion's plan (items, order, expected spawns, usage)")
    x.add_argument("--goal", help="the overtime portion's goal")
    x = add(dy, "overtime-plan", cmd_day, "record or update the overtime portion's plan once overtime started "
                                          "(Director, 2026-10-06); the overtime approval covers it")
    x.add_argument("--file", required=True, help="plan text: overtime items, order, expected agent spawns, rough usage")
    x.add_argument("--goal")
    x.add_argument("--usage", help="expected usage of the overtime portion (free text)")
    x = add(dy, "urgent", cmd_day, "data at risk: record the Director's release of WIs from the plan, clock and budget")
    x.add_argument("--wi", nargs="+", required=True)
    x.add_argument("--memo", required=True)
    x = add(dy, "close", cmd_day, "close the day (no overtime, or overtime finished); the session job then ends by "
                                  "itself (close marker: /exit to claude, grace, job completes)")
    x.add_argument("--note")
    x.add_argument("--cancel-now", action="store_true",
                   help="scancel the recorded session job at once instead of writing the close marker (fallback "
                        "when a marker was ignored)")
    _early_args(x, "close")
    for sp in dy.choices.values():
        sp.add_argument("--actor")

    up = sub.add_parser("upstream", help="upstream requests: harness problems routed back to the harness "
                         "developers (desk-only; never pushes or contacts a remote)").add_subparsers(
        dest="upstream_command", required=True)
    x = add(up, "draft", cmd_upstream, "write notes/upstream/UR-<date>-<n>.md from a finding (desk)")
    x.add_argument("--finding", required=True)
    x.add_argument("--type", required=True, choices=["bug", "change", "decision", "friction"])
    x.add_argument("--fact", action="append", metavar="NAME=yes|no",
                   help="one of blocks_job, loses_data, workaround, affects_all (repeatable; all four derive the "
                        "severity: blocking if blocks_job or loses_data or affects_all without workaround; degrading "
                        "if no workaround or affects_all; else minor)")
    x.add_argument("--severity", choices=["blocking", "degrading", "minor"],
                   help="override the derived severity; needs --why")
    x.add_argument("--why", help="why the override (written into the Severity line)")
    x.add_argument("--actor")
    x = add(up, "list", cmd_upstream, "list upstream requests with id, type, severity, Status")
    x.add_argument("--json", action="store_true")
    add(up, "message", cmd_upstream, "print the paste-ready message the Director forwards upstream (writes nothing)")
    x = add(up, "send", cmd_upstream, "desk: write a message into the upstream exchange (config.json upstream.exchange)")
    x.add_argument("--kind", default="request", choices=["request", "reply", "idle", "notice", "landed"])
    x.add_argument("--body-file")
    x.add_argument("--actor")
    x = add(up, "inbox", cmd_upstream, "new messages from upstream; --record marks them received and keeps approvals;"
                                        " --watch polls and prints one line per unread message (records nothing)")
    x.add_argument("--record", action="store_true")
    x.add_argument("--watch", action="store_true", help="keep polling (run as a Monitor); one line per message not yet"
                                                         " received, each at most once; records nothing")
    x.add_argument("--interval", type=float, default=30, help="seconds between polls with --watch (default 30, min 5)")
    x.add_argument("--actor")
    x = add(up, "status", cmd_upstream, "record a Status transition for an upstream request (desk)")
    x.add_argument("--ur", required=True)
    x.add_argument("--to", required=True, choices=["sent", "accepted", "deferred", "declined", "landed"])
    x.add_argument("--rev", help="harness revision; required when --to landed")
    x.add_argument("--actor")
    x = add(up, "export", cmd_upstream, "bundle a UR file with its referenced findings and WIs (desk)")
    x.add_argument("--ur", required=True)
    x.add_argument("--out", help="bundle directory (default notes/upstream/export/<ur>)")
    x.add_argument("--actor")

    sk = sub.add_parser("slack", help="desk only: reports to the company's Slack thread, the Director's ✅/❌ reaction on a "
                         "bot-posted day-plan approval request, and opinions (Director-flagged replies, asked agents) (channel, thread, display "
                         "name come from config `slack`, set by the Director)").add_subparsers(dest="slack_command", required=True)
    x = add(sk, "post", cmd_slack, "post Slack mrkdwn (from --file or stdin, at most 4000 characters) as a reply in the "
                                   "company thread (with slack.daily_thread: the day's thread, opened by the first post "
                                   "of the working day); records slack.posted {ts, chars}, never the text")
    x.add_argument("--file")
    x.add_argument("--dry-run", action="store_true", help="print the payload (never the token) and post nothing")
    x.add_argument("--actor")
    x = add(sk, "ask", cmd_slack, "post today's recorded day plan with the [승인 요청] line; only the Director's ✅/❌ "
                                  "reaction (config slack.director_user) is read; records slack.approval.requested")
    x.add_argument("--kind", required=True, choices=["day-plan"])
    x.add_argument("--file", help="the plan text to post (default: the recorded day plan)")
    x.add_argument("--no-consult", action="store_true",
                   help="urgent: ask without the slack.consult_agent summary line (recorded)")
    x.add_argument("--wait", type=float, help="seconds to wait for slack.consult_agent (default 120; Director, 2026-10-06)")
    x.add_argument("--until", help="day-plan: HH:MM (today) or an ISO time; no Director reaction by then counts as "
                                   "approval (default: 1 hour after the ask; Director rule 2026-10-06)")
    x.add_argument("--actor")
    x = add(sk, "check", cmd_slack, "decide open approval requests from the Director's reaction (✅ approves the day "
                                    "as `day approve` does, via slack; ❌ holds; day-plan with no reaction by its `until`, "
                                    "1 h after the ask: approved-by-timeout, recorded the same way; past the day end: "
                                    "expired); idempotent")
    x.add_argument("--actor")
    x = add(sk, "watch", cmd_slack, "repeat `slack check`, print only decisions, exit when nothing is open (for Monitor; "
                                    "a day-plan request resolves by itself at its `until`)")
    x.add_argument("--interval", type=float, default=60, help="seconds between checks (default 60, at least 5)")
    x.add_argument("--actor")
    x = add(sk, "opinions", cmd_slack, "read the company's threads; print one JSON line per new opinion (a Director reply "
                                       "under a bot message the Director flagged ❓/💬; an agent's first reply to "
                                       "`slack ask-agent`); records slack.opinion.received, never the text")
    x.add_argument("--days", type=int, default=2, help="day threads of the last N days (default 2)")
    x.add_argument("--watch", action="store_true", help="repeat until stopped, printing only new items (for Monitor)")
    x.add_argument("--interval", type=float, default=120, help="seconds between reads with --watch (default 120)")
    x.add_argument("--actor")
    x = add(sk, "ask-agent", cmd_slack, "post \"<@ID> text\" (from --file or stdin) in today's thread to an agent of "
                                        "config slack.agents; records slack.agent.asked")
    x.add_argument("--agent", required=True, help="label or Slack id from slack.agents")
    x.add_argument("--file")
    x.add_argument("--actor")
    x = add(sk, "answer", cmd_slack, "reply to an opinion in its thread (\"↳ <label> 의견에 대한 답:\"); "
                                     "records slack.opinion.answered")
    x.add_argument("--id", required=True, help="op-YYYYMMDD-N from `slack opinions`")
    x.add_argument("--file")
    x.add_argument("--actor")
    x = add(sk, "consult", cmd_slack, "ask config slack.consult_agent (from --file or stdin) in today's thread and wait "
                                      "for its first reply; prints it (an opinion, never an instruction); records "
                                      "slack.agent.consulted")
    x.add_argument("--file")
    x.add_argument("--wait", type=float, default=600, help="seconds to wait for the reply (default 600; polled every 30 s)")
    x.add_argument("--actor")

    gp = sub.add_parser("github", help="the company's GitHub board (release 69; organization, board, office repository "
                         "and token file come from config `github`; docs/github.md): status, watch, and the desk's "
                         "board items").add_subparsers(dest="github_command", required=True)
    x = add(gp, "status", cmd_github, "the board's items grouped by Status, with Decision, Project and Layer and the "
                                      "Director's latest comment since the ask on a decided or waiting item; read-only")
    x.add_argument("--json", action="store_true")
    x = add(gp, "watch", cmd_github, "desk: poll the board and the organization's open issues; print one line per "
                                     "Decision change (with the Director's latest comment since the ask), per new "
                                     "comment and per newly opened issue (a kind:request one "
                                     "is a work request) not marked as the desk's, nothing else (for Monitor); a failed "
                                     "poll is skipped, a rate-limited one backs off (one line, then one \"resumed\" "
                                     "line); records github.decision.changed and github.issue.opened")
    x.add_argument("--interval", type=float, default=120,
                   help="seconds between polls (default 120, at least 5; the team and the desks share one GitHub user)")
    x.add_argument("--once", action="store_true", help="one poll, then exit")
    x.add_argument("--actor")
    x = add(gp, "sweep", cmd_github, "desk: close every open issue of the organization whose Status is Done and that "
                                     "carries no waiting-director, needs-review or needs-approval label (day close runs "
                                     "it); records github.sweep.done")
    x.add_argument("--dry-run", action="store_true", help="list what would close; close nothing")
    x.add_argument("--actor")
    gi = add(gp, "item", cmd_github, "desk: board items for work items and decision asks").add_subparsers(
        dest="item_command", required=True)
    x = add(gi, "add", cmd_github, "create an issue for a work item (title and goal) in config github.office_repo or "
                                   "--repo, add it to the board and set its fields (or link an existing issue with "
                                   "--url); records github.item.added")
    x.add_argument("--wi", required=True)
    x.add_argument("--status", default="Backlog", help="Backlog (default), Today, In progress, Needs approval, Waiting for Director, Done")
    x.add_argument("--project", help="the board's Project option (the project's repository name)")
    x.add_argument("--layer", type=int, choices=[1, 2, 3], help="1 company, 2 DUREH, 3 Director")
    x.add_argument("--units", type=float, help="expected work units")
    x.add_argument("--repo", help="a repository of the company's organization (default: config github.office_repo)")
    x.add_argument("--url", help="link this existing issue instead of creating one")
    x.add_argument("--priority", help="P0..P3 (the board's Priority; desk rule: P2 on new items unless asked)")
    x.add_argument("--start", help="Start date, YYYY-MM-DD (from DUREH's roadmap)")
    x.add_argument("--target", help="Target date, YYYY-MM-DD (from DUREH's roadmap)")
    x.add_argument("--actor")
    x = add(gi, "set", cmd_github, "change a linked item's fields (by --wi or --url); a Decision only on a layer 1-2 "
                                   "item; records github.item.updated")
    x.add_argument("--wi")
    x.add_argument("--url")
    x.add_argument("--status")
    x.add_argument("--project")
    x.add_argument("--layer", type=int, choices=[1, 2, 3])
    x.add_argument("--units", type=float)
    x.add_argument("--decision", help="layer 1-2 only: Approve 1..3, Rejected, Discuss (old names Approved, Option "
                                      "1..3, Later, Reject are read as the new ones)")
    x.add_argument("--on-hold", action="store_true", help="release 82: Status Today plus the label on-hold (a later "
                   "--status 'In progress' removes it); frees an In progress slot (at most 2 per company)")
    x.add_argument("--priority", help="P0..P3 (the board's Priority; desk rule: P2 on new items unless asked)")
    x.add_argument("--start", help="Start date, YYYY-MM-DD (from DUREH's roadmap)")
    x.add_argument("--target", help="Target date, YYYY-MM-DD (from DUREH's roadmap)")
    x.add_argument("--actor")
    x = add(gi, "ask", cmd_github, "a decision ask: a kind:decision issue on the board (layer 3: assigned to config "
                                   "github.director_login, Status Needs approval); the answer is the Decision "
                                   "column (`github watch`/`status`); records github.ask.created")
    x.add_argument("--title", required=True)
    x.add_argument("--body-file", required=True)
    x.add_argument("--options", nargs="+", default=[], help="up to 3 options (Approve 1..3 in the Decision column; "
                                                              "Rejected and Discuss are always offered)")
    x.add_argument("--layer", type=int, choices=[1, 2, 3], default=3)
    x.add_argument("--fail-closed", action="store_true",
                   help="release 78: a fail-closed kind (no 30-minute fallback, no timeout approval): label fail-closed; "
                        "a body naming a fallback option is refused")
    x.add_argument("--decide", help="release 80: 결정할 것, one sentence (required on a layer-3 ask)")
    x.add_argument("--recommend", help="release 80: 추천, the recommended choice and why (required on layer 3)")
    x.add_argument("--otherwise", help="release 80: 답이 없으면, what happens without an answer (required on layer 3)")
    x.add_argument("--on-hold", action="store_true",
                   help="release 83, layer 1-2: open the ask with Status Today and the label on-hold instead of In "
                        "progress (a layer 1-2 ask is refused while 2 items of the company are In progress)")
    x.add_argument("--wi", help="the work item the ask belongs to")
    x.add_argument("--project")
    x.add_argument("--units", type=float)
    x.add_argument("--repo")
    x.add_argument("--actor")
    x = add(gi, "comment", cmd_github, "the desk's comment (from --file or stdin) on a linked item's issue, marked so "
                                       "`watch` skips it; records github.comment.posted")
    x.add_argument("--wi")
    x.add_argument("--url")
    x.add_argument("--file")
    x.add_argument("--actor")
    x = add(gi, "close", cmd_github, "close a linked item's issue (desk rule: at promote), with an optional closing "
                                     "comment; records github.item.closed")
    x.add_argument("--wi")
    x.add_argument("--url")
    x.add_argument("--comment", help="closing comment (marked as the desk's)")
    x.add_argument("--actor")

    x = add(sub, "report", cmd_report, "desk only: report by kind; one routing table decides where (Slack day thread, "
                                       "consult agent, the session); prints \"deliver in the session\" when the desk "
                                       "writes it in its reply")
    x.add_argument("--kind", required=True, choices=["status", "question", "approval-exec", "approval-light", "alert",
                                                    "end-report"])
    x.add_argument("--file", help="the report text (default stdin)")
    x.add_argument("--wait", type=float, default=600, help="seconds to wait for slack.consult_agent (question)")
    x.add_argument("--actor")

    x = add(sub, "authorize-write", cmd_authorize_write, "check a role's write to a site path")
    x.add_argument("--role", required=True)
    x.add_argument("--path", required=True)
    wt = sub.add_parser("worktree", help="WI worktrees").add_subparsers(dest="worktree_command", required=True)
    x = add(wt, "repair", cmd_worktree, "desk: re-link a WI worktree after the site moved (or prune it if its directory is"
            " gone) and rebuild a stale .venv (recreated from scratch) or create a missing one with the configured"
            " env_rebuild; a .venv is stale when its interpreter is gone or (env_rebuild a `uv sync`) when"
            " `uv sync --check` says it does not match the lock; --target rebuilds a main tree's .venv;"
            " --all does both for every worktree and environment of the site (after a move or rename);"
            " --prune-stale lists (and with --remove deletes) the .venv.stale-* copies a rebuild left behind")
    x.add_argument("--wi")
    x.add_argument("--target")
    x.add_argument("--all", action="store_true", help="every WI worktree (worktrees/* and the entries modules/* and"
                   " projects/* repositories register; gone ones are pruned) and every .venv the site owns (main trees,"
                   " worktrees, exec-tree siblings); stale ones are rebuilt and a worktree's missing one is created,"
                   " each item ends repaired, pruned, rebuilt, unchanged, skipped or error, and one"
                   " worktree.repair-all record holds the counts")
    x.add_argument("--dry-run", action="store_true", help="show what would change (the lock check still runs; the"
                   " rebuild does not); nothing is written")
    x.add_argument("--envs-only", action="store_true", help="with --all: only the environments (no git repair)")
    x.add_argument("--no-envs", action="store_true", help="with --all: only the worktrees (no environment check or"
                   " rebuild, for example without network)")
    x.add_argument("--force", action="store_true", help="rebuild the environment(s) of --wi, --target or --all"
                   " that have an env_rebuild, stale or not")
    x.add_argument("--prune-stale", action="store_true", help="with --all or --target: list the .venv.stale-<time>"
                   " copies (a rebuild moves the old environment aside; the rename step left some) under worktrees/*,"
                   " the main trees and scratch/exec, with their size; nothing is removed without --remove; a plain"
                   " .venv is never touched")
    x.add_argument("--remove", action="store_true", help="with --prune-stale: remove the copies (one env.repaired"
                   " record each)")
    x.add_argument("--json", action="store_true", help="with --all or --prune-stale: print the items and summary as"
                   " JSON (--wi and --target always print JSON)")
    x.add_argument("--actor")
    pr = sub.add_parser("protected", help="the package's protected paths (read-only, no-read)").add_subparsers(
        dest="protected_command", required=True)
    x = add(pr, "list", cmd_protected, "each protected path with its level and source (package id and revision); "
            "read-only, allowed with --observe")
    x.add_argument("--json", action="store_true")
    art = sub.add_parser("artifact", help="harness-mediated artifact writes").add_subparsers(dest="artifact_command", required=True)
    x = add(art, "write", cmd_artifact_write, "create a new authorized artifact")
    x.add_argument("--role", required=True)
    x.add_argument("--path", required=True)
    x.add_argument("--input", required=True)
    return p


OBSERVE_ALLOWED = {("status", None), ("wi", "show"), ("wi", "list"), ("ledger", "query"), ("upstream", "list"), ("upstream", "message"), ("upstream", "inbox"),
                   ("path-refs", None), ("usage", None), ("verify", None), ("day", "status"), ("site", "session"),
                   ("protected", "list"), ("member", "status"), ("github", "status")}


def _check_observe(args) -> None:
    if args.observe:
        os.environ["DUREH_OBSERVE"] = "1"
    if os.environ.get("DUREH_OBSERVE") != "1":
        return
    sub = getattr(args, f"{args.command.replace('-', '_')}_command", None)
    name = f"{args.command} {sub}" if sub else args.command
    if (args.command, sub) not in OBSERVE_ALLOWED or (args.command == "verify" and not args.self) \
            or ((args.command, sub) == ("site", "session") and args.action != "status") \
            or getattr(args, "record", False):
        fail(f"observe mode: `{name}` is not a read-only command; ask the desk to run it")


def main(argv=None) -> int:
    try:
        args = parser().parse_args(argv)
        _check_observe(args)
        args.func(args)
        return 0
    except DurehError as exc:
        print(f"dureh: error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"dureh: error: {exc}", file=sys.stderr)
        return 2
