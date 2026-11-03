"""`dureh day handshake` (release 89 candidate): the desk's start-of-day check, before any work.

The Director, 2026-11-02: operations are checked every morning before work, "like a handshake" (that day a site's
dispatch stopped all morning on a model bootstrap waiver that had expired unseen). One line per check, each `ok`,
`warn`, `FAIL` or `skip` (not checked here); the exit code is 2 when a check FAILs, 1 when one warns, else 0.

Read-only: it records nothing, contacts no network service (the board's Decisions come from the GitHub watch's cached
view, when there is one) and runs with `--observe`. A check that cannot be answered is `skip` or `warn`, never `ok`.
"""
from __future__ import annotations

import re
from datetime import date

from .site import Site
from .util import DurehError

OK, WARN, FAIL, SKIP = "ok", "warn", "FAIL", "skip"
SESSION_WARN_SECONDS = 3600  # the session job's wall time: under an hour left is a warning (the desk's stop rule)


def _line(name: str, level: str, text: str) -> dict:
    return {"check": name, "level": level, "text": text}


def _safe(name: str, fn, *args) -> list[dict]:
    """A check that raises is a warning that names the error, never a silent ok (fail-safe)."""
    try:
        return fn(*args)
    except (DurehError, OSError, ValueError, KeyError, TypeError) as exc:
        return [_line(name, WARN, f"not checked: {exc}")]


# ------------------------------------------------------------------ checks
def _dispatch(site: Site) -> list[dict]:
    """Per model tier the site maps or a dispatchable role uses: can `dispatch brief` run on it today."""
    from .workflow import _qualified

    models = site.config.get("models") or {}
    tiers = list(site.model_tiers())
    users: dict[str, list[str]] = {}
    try:
        for r in site.org.roles.values():
            if r.kind != "desk":
                for t in [r.tier, *r.tier_options]:
                    users.setdefault(t, []).append(r.name)
    except DurehError as exc:
        return [_line("dispatch", FAIL, f"the organization does not load: {exc}")]
    tiers += [t for t in users if t not in tiers]
    if not tiers:
        return [_line("dispatch", WARN, "no model tier is configured (config models)")]
    out = []
    for t in tiers:
        entry = models.get(t)
        name = f"dispatch {t}"
        if not isinstance(entry, dict) or not entry.get("model"):
            out.append(_line(name, FAIL, f"no model mapping for tier {t} (roles {', '.join(sorted(users.get(t, [])))}):"
                                         " dispatch refuses it; ask DUREH"))
            continue
        try:
            ok, why = _qualified(entry, site, t)
        except ValueError:
            ok, why = False, f"waiver_expires {entry.get('waiver_expires')!r} is not a date"
        if not ok:  # release 90: with models.fallback_tiers the tier runs on a higher one (warn), else FAIL
            from .qualification import fallback_for

            fb = fallback_for(site, t)
            if fb is not None:
                out.append(_line(name, WARN, f"{entry['model']}: {why}; dispatch falls back to tier {fb[0]}"
                                             f" ({fb[1]['model']}: {fb[2]}), costs more; run the exam"))
                continue
        out.append(_line(name, OK if ok else FAIL, f"{entry['model']}: {why}"))
    return out


def _qualifications(site: Site, today: date | None) -> list[dict]:
    """Release 90: `run-*` qualifications that do not hold (FAIL) or end within 7 days (warn)."""
    from .qualification import warnings

    lines = warnings(site, today=today)
    if not lines:
        return [_line("qualifications", OK, "no exam qualification failing or ending within 7 days")]
    return [_line("qualifications", WARN if "ends within" in w else FAIL, w) for w in lines]


def _newer_models(site: Site) -> list[dict]:
    """Phase 2 (newest-model rule): a tier whose model family has a newer version in `models.candidates`."""
    from .qualification import newer_models

    if not isinstance((site.config.get("models") or {}).get("candidates"), list):
        return [_line("newer models", SKIP, "models.candidates is not set: newer model versions are not checked")]
    rows = newer_models(site)
    if not rows:
        return [_line("newer models", OK, "no tier's model family has a newer version in models.candidates")]
    return [_line("newer models", WARN, f"newer model available for tier {r['tier']}: {r['newer']} (incumbent"
                                        f" {r['incumbent']}); run the candidate exam this week (`dureh exam run --tier"
                                        f" {r['tier']} --model {r['newer']}`), then ask for the switch with the scores")
            for r in rows]


def _waivers(site: Site, today: date | None) -> list[dict]:
    from .ops import model_waiver_warnings

    lines = model_waiver_warnings(site, today=today)
    if not lines:
        return [_line("waivers", OK, "no bootstrap waiver expired or ending within 7 days")]
    return [_line("waivers", FAIL if w.startswith("model bootstrap waiver expired") else WARN, w) for w in lines]


def _harness(site: Site) -> list[dict]:
    """The release checkout the site runs, against the newest `landed` release the mailbox names (all a site can
    know about newer releases; without an exchange the newest is not known here)."""
    from . import upstream
    from .util import harness_label

    label = harness_label(site.harness_root)
    if not (site.config.get("upstream") or {}).get("exchange"):
        return [_line("harness", SKIP, f"runs {label}; the newest release is not known here (no upstream.exchange)")]
    pending = upstream.pending_landed_checkout(site)
    if pending:
        order = upstream.release_order(site.pinned_harness_root, pending["checkout"])
        if order["newer"]:
            return [_line("harness", WARN, f"runs {label}; landed {pending['id']} names {order['to']} at"
                                           f" {pending['checkout']}: the next `site session start` switches to it, or"
                                           " `adapter sync --harness <checkout>` in a merge window")]
    return [_line("harness", OK, f"runs {label}; no newer landed release in the mailbox")]


_LEFT = re.compile(r"^(?:(\d+)-)?(?:(\d+):)?(\d+):(\d+)$")


def _seconds(left: str) -> int | None:
    """squeue %L (`D-HH:MM:SS`, `HH:MM:SS`, `MM:SS`) in seconds; None for UNLIMITED, NOT_SET or anything else."""
    m = _LEFT.match((left or "").strip())
    if not m:
        return None
    d, h, mi, s = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + s


def _session(st: dict) -> list[dict]:
    s, j = st.get("session"), st.get("job") or {}
    out = []
    if not s:
        out.append(_line("session", WARN, "no session job started by dureh is recorded (`site session status`)"))
    elif j.get("queued"):
        left = _seconds(j.get("time_left", ""))
        level = WARN if j.get("state") != "RUNNING" or (left is not None and left < SESSION_WARN_SECONDS) else OK
        out.append(_line("session", level, f"job {s['job_id']} {j.get('state')}, time left {j.get('time_left')}"
                                           f" (wall time {s.get('wall_time') or '?'})"))
    elif j.get("queued") is False:
        out.append(_line("session", WARN, f"job {s['job_id']} is not in squeue ({'ended ' + j['ended'] if j.get('ended') else 'finished or cancelled'});"
                                          " this session is not the recorded job: tell DUREH"))
    else:
        out.append(_line("session", SKIP, f"job {s['job_id']}: squeue unavailable ({j.get('error') or 'no job id'})"))
    for u in st.get("unrecorded") or []:
        out.append(_line("session", WARN, f"job {u['job_id']} ({u['name']}, {u['state']}) runs in this site without a"
                                          " session.started record: `day close` cannot end it; tell DUREH"))
    return out


def _day(st: dict, today: date) -> list[dict]:
    d = st.get("day") or {}
    phase = d.get("phase") or "?"
    text = f"{phase}" + (f" ({d['reason']})" if d.get("reason") else "") + ("" if st.get("workday") else "; workday mode off")
    if d.get("plan"):
        text += f"; plan {d['plan']} of {d.get('date') or '?'}" + (f", approved {d['approval']}" if d.get("approval") else "")
    stale = d.get("plan") and d.get("date") and d["date"] < today.isoformat() and phase not in ("off-duty",)
    if stale:
        return [_line("day", WARN, text + ": an earlier day is still open; close it (`day end`/`day close`) or tell"
                                          " DUREH before planning today")]
    return [_line("day", WARN if phase == "past-end" else OK, text)]


def _director(site: Site, st: dict) -> list[dict]:
    """Board asks and Director steps the desk recorded and has not closed, with their Decision from the GitHub
    watch's cached view (no network); today's open Director actions from the day plan."""
    from . import github
    from .records import Ledger

    out = []
    actions = (st.get("day") or {}).get("director_actions") or []
    if actions:
        out.append(_line("director actions", OK, f"{len(actions)} open in today's plan: " + "; ".join(actions)[:300]))
    if not github.enabled(site):
        return out + [_line("asks", SKIP, "github off: board asks not checked")]
    asks, closed = {}, set()
    for e in Ledger(site).events():
        p = e.get("payload") or {}
        if e["type"] in (github.ASKED, github.DIRECTOR_STEP) and p.get("url"):
            asks[p["url"]] = p
        elif e["type"] == github.CLOSED and p.get("url"):
            closed.add(p["url"])
        elif e["type"] == github.SWEPT:
            closed.update(p.get("closed") or [])
    open_asks = [p for u, p in asks.items() if u not in closed]
    if not open_asks:
        return out + [_line("asks", OK, "no open board ask or Director step recorded")]
    try:
        cache = github._load_state(site, github.config(site))
    except DurehError:
        cache = None
    ref = lambda p: f"{str(p.get('repo') or '').split('/')[-1]}#{p.get('number')}"  # noqa: E731
    if cache is None:
        return out + [_line("asks", WARN, f"{len(open_asks)} open ({', '.join(ref(p) for p in open_asks)}); Decisions not"
                                          " checked (no cached board view): read them with `github status`")]
    decisions = cache.get("decisions") or {}
    decided = [f"{ref(p)} -> {decisions[p['item_id']]}" for p in open_asks if decisions.get(p.get("item_id"))]
    waiting = [ref(p) for p in open_asks if not decisions.get(p.get("item_id"))]
    seen = f" (board view of {cache.get('polled') or '?'})"
    if waiting:
        out.append(_line("asks", OK, f"{len(waiting)} wait for the Director: {', '.join(waiting)}{seen}"))
    if decided:
        out.append(_line("asks", WARN, f"decided, still open: {', '.join(decided)}{seen}: act on them and report on the item"))
    return out


def _verify_runs(site: Site) -> list[dict]:
    """Release 91: a staged item whose pending verify job was submitted on an older head, or before `wi amend`
    changed its declared checks. Its result would be voided at `--collect`; the desk resubmits first."""
    from .records import Ledger
    from .workflow import changed_paths, pending_verify

    led = Ledger(site)
    events = led.events()
    payloads = led.work_item_payloads(events)
    stale = []
    for wi, st in sorted(led._project(events).items()):
        if st["status"] != "staged":
            continue
        run = pending_verify(led, wi)
        if not run:
            continue
        p, why = run["payload"], []
        try:
            _, head = changed_paths(site, wi)
        except DurehError:
            head = None
        if head and head != p.get("head"):
            why.append(f"head moved {str(p.get('head'))[:12]} -> {head[:12]}")
        if list(p.get("checks") or []) != list((payloads.get(wi) or {}).get("checks") or []):
            why.append("declared checks amended since")
        if why:
            stale.append(f"{wi} run {run['id']} (job {p.get('job_id')}): {', '.join(why)}")
    if not stale:
        return [_line("verify runs", OK, "no pending verify run on a moved head or amended checks")]
    return [_line("verify runs", WARN, "; ".join(stale) + "; its result would be voided at --collect:"
                  " run `dureh verify --wi <wi> --submit` (supersedes it)")]


def _upstream(site: Site) -> list[dict]:
    from .upstream import collaboration

    c = collaboration(site)
    out = [_line("blocked WIs", WARN if c["blocked_wis"] else OK,
                 ("blocked on upstream: " + ", ".join(c["blocked_wis"]) + f" (open requests {', '.join(c['open'])})")
                 if c["blocked_wis"] else "none blocked on an upstream request")]
    if not c.get("exchange"):
        out.append(_line("mailbox", SKIP, "no upstream.exchange: no mailbox"))
    else:
        n = c.get("new_from_upstream") or 0
        out.append(_line("mailbox", WARN if n else OK, f"{n} unread message{'s' if n != 1 else ''} from upstream"
                                                       + (": `upstream inbox --record`" if n else "")
                                                       + f"; merge window {c['merge_window']}"))
    return out


def _config(site: Site) -> list[dict]:
    from .ops import config_errors

    errs = config_errors(site)
    if not errs:
        return [_line("config", OK, "no config errors")]
    return [_line("config", FAIL, e) for e in errs]


# ------------------------------------------------------------------ run
def handshake(site: Site, today: date | None = None) -> dict:
    """Every check, in order; `exit` 2 on any FAIL, 1 on any warn, else 0."""
    from . import sessions

    today = today or date.today()
    try:
        st = sessions.status(site)
        ses = _session(st) + _day(st, today)
    except (DurehError, OSError) as exc:
        st, ses = {}, [_line("session", WARN, f"not checked: {exc}")]
    checks = (_safe("dispatch", _dispatch, site) + _safe("waivers", _waivers, site, today)
              + _safe("qualifications", _qualifications, site, today)
              + _safe("newer models", _newer_models, site)
              + _safe("harness", _harness, site) + ses + _safe("verify runs", _verify_runs, site) + _safe("asks", _director, site, st)
              + _safe("upstream", _upstream, site) + _safe("config", _config, site))
    levels = {c["level"] for c in checks}
    code = 2 if FAIL in levels else 1 if WARN in levels else 0
    return {"site": str(site.root), "date": today.isoformat(), "checks": checks, "exit": code,
            "result": {2: "FAIL", 1: "warn", 0: "ok"}[code]}


def render(res: dict) -> str:
    lines = [f"{c['level']:<4} {c['check']}: {c['text']}" for c in res["checks"]]
    n = {lv: sum(1 for c in res["checks"] if c["level"] == lv) for lv in (FAIL, WARN, OK, SKIP)}
    lines.append(f"handshake {res['result']}: {n[FAIL]} FAIL, {n[WARN]} warn, {n[OK]} ok, {n[SKIP]} skip"
                 f" ({res['site']}, {res['date']})")
    return "\n".join(lines)
