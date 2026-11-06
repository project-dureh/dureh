"""The desk on its company's GitHub App (dureh-team#56 step 5, release 84): with config `github.app` set, the harness
mints the App's installation token itself and writes it to `github.token_file` (mode 600), where `github.gh()` already
reads it at every call; the desk then writes as `<app>[bot]`, not as the Director. The session job runs
`dureh github token --every 1800` beside the session (sessions._build), so the token outlives the hour it is valid.

  github.app = {app_id: <int>, installation_id: <int>, key_file: <path to the App's private key, mode 600>}

The key is read by path only (openssl or `cryptography` signs a 10-minute JWT); neither the key nor a token is ever
printed, logged or put on a command line. Without `github.app` nothing changes (the Director's token file stays)."""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from .site import Site
from .util import fail

API = "https://api.github.com"
APP_KEYS = ("app_id", "installation_id", "key_file")
JWT_BACK, JWT_LIFE = 60, 540
DEFAULT_EVERY = 1800


def app_errors(site: Site, raw) -> list[str]:
    """What is wrong with config `github.app` (shape only; the key file's mode when it is named)."""
    if raw is None:
        return []
    if not isinstance(raw, dict):
        return ["config github.app must be an object {app_id, installation_id, key_file}"]
    out = [f"config github.app.{k}: unknown key ({', '.join(APP_KEYS)})" for k in raw if k not in APP_KEYS]
    for k in ("app_id", "installation_id"):
        v = raw.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            out.append(f"config github.app.{k} must be a positive integer, not {v!r}")
    key = raw.get("key_file")
    if not isinstance(key, str) or not key.strip():
        out.append("config github.app.key_file must be the path of the App's private key (mode 600)")
    else:
        from .github import _mode_problem

        problem = _mode_problem(site.expand(key))
        if problem:
            out.append("config github.app.key_file: " + problem.replace("token file", "key file"))
    return out


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _sign(data: bytes, key: Path) -> bytes:
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
    except ImportError:
        proc = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key)], input=data, capture_output=True,
                              timeout=30)
        if proc.returncode:
            fail(f"openssl could not sign with the App key {key}")
        return proc.stdout
    pk = serialization.load_pem_private_key(key.read_bytes(), password=None)
    return pk.sign(data, padding.PKCS1v15(), hashes.SHA256())


def jwt(app_id: int, key: Path, now: float | None = None) -> str:
    now = int(time.time() if now is None else now)
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(json.dumps({"iat": now - JWT_BACK, "exp": now + JWT_LIFE, "iss": app_id},
                              separators=(",", ":")).encode())
    return f"{head}.{body}.{_b64url(_sign(f'{head}.{body}'.encode(), key))}"


def http_json(method: str, path: str, bearer: str):  # replaced in tests
    req = urllib.request.Request(API + path, method=method, headers={
        "Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dureh-harness"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as exc:
        fail(f"{method} {path}: HTTP {exc.code}")
    except urllib.error.URLError as exc:
        fail(f"{method} {path}: {exc.reason}")


def refresh(site: Site) -> dict:
    """Mint one installation token and replace `github.token_file` with it (mode 600, atomic)."""
    raw = site.config.get("github") or {}
    app = raw.get("app")
    if app is None:
        fail("config github.app is not set: the desk uses the token file as placed (docs/github.md)")
    errs = app_errors(site, app)
    if errs:
        fail("; ".join(errs))
    if not raw.get("token_file"):
        fail("config github.token_file is not set: the App token needs a place to go")
    res = http_json("POST", f"/app/installations/{app['installation_id']}/access_tokens",
                    jwt(app["app_id"], Path(site.expand(app["key_file"])))) or {}
    token, expires = res.get("token"), res.get("expires_at")
    if not token or not expires:
        fail("GitHub answered without a token")
    path = Path(site.expand(raw["token_file"]))
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    os.replace(tmp, path)
    return {"token_file": str(path), "expires_at": expires, "installation_id": app["installation_id"]}


def serve(site: Site, every: int = DEFAULT_EVERY, once: bool = False, sleep=time.sleep) -> int:
    """Refresh now and then every `every` seconds; one line per refresh (never the token). A failed refresh is
    reported and retried at the next turn: the last token stays until it expires."""
    while True:
        try:
            out = refresh(site)
            print(f"github token refreshed, valid until {out['expires_at']}", flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad turn must not end the loop
            print(f"github token refresh failed: {exc}", flush=True)
            if once:
                return 1
        if once:
            return 0
        sleep(max(60, every))


# ------------------------------------------------------------ pushes over HTTPS with the App token (release 95)
# dureh-team#56 step 6: with config github.push_transport "app" (and github.app set), `dureh push` reaches a GitHub
# repository of the company's organization (github.org, the account the App is installed on) over HTTPS with the
# installation token instead of whatever the remote URL says (in practice the Director's SSH key). Every other remote
# (another owner such as a personal account, a local path) keeps its URL: those are listed as "still on SSH" by
# `dureh github transport`, a blocker for removing the Director's SSH key (step 8), never silently served with a human
# token. Fail-closed: no fresh token, or a checkout whose own url rewriting wins over ours, refuses the push.
PUSH_TRANSPORTS = ("ssh", "app")
VIA_APP = "app-https"
VIA_REMOTE = "remote as configured"
TOKEN_MAX_AGE = 3000  # s: refreshed every 1800, valid 3600; 10 min left for the push itself
HELPER = Path(__file__).resolve().parent / "git_credential.py"
_GITHUB = re.compile(r"^(?:git@github\.com:|https://github\.com/|ssh://git@github\.com/)([A-Za-z0-9-]+)/")


def push_transport_errors(raw: dict) -> list[str]:
    v = raw.get("push_transport")
    if v is None:
        return []
    if v not in PUSH_TRANSPORTS:
        return [f"config github.push_transport must be \"ssh\" or \"app\" (release 95), not {v!r}"]
    if v == "app" and raw.get("app") is None:
        return ["config github.push_transport is \"app\" but github.app is not set: pushes over the App need the App"]
    return []


def push_transport(site: Site, url: str) -> dict:
    """How `dureh push` reaches `url`: {"via": "app-https", "owner", "https_url", "token_file"} when the App
    carries it, else {"via": "remote as configured", "why"}. Reads config only (no network, no token)."""
    raw = site.config.get("github") or {}
    m = _GITHUB.match(url or "")
    if not m:
        return {"via": VIA_REMOTE, "why": "not a GitHub remote"}
    owner = m.group(1)
    if not raw.get("enabled") or raw.get("app") is None or raw.get("push_transport") != "app":
        what = ("config github.push_transport is not \"app\"" if raw.get("app") is not None and raw.get("enabled")
                else "no company App (config github.app)")
        return {"via": VIA_REMOTE, "owner": owner, "why": what}
    org = str(raw.get("org") or "")
    if owner.lower() != org.lower():
        return {"via": VIA_REMOTE, "owner": owner,
                "why": f"{owner} is not {org}, the organization the company App is installed on"}
    rest = url[m.end():]
    if not rest.endswith(".git"):
        rest += ".git"
    return {"via": VIA_APP, "owner": owner, "https_url": f"https://github.com/{owner}/{rest}",
            "token_file": site.expand(raw["token_file"])}


def push_git_env(site: Site, url: str) -> tuple[dict, dict]:
    """(git environment, transport) for every network git call of one push to `url`. For the App: url rewriting of
    the owner's SSH forms to https://github.com/<owner>/, the helper list reset (no global helper such as
    `gh auth git-credential` is asked) and replaced by git_credential.py, the repository path sent to it, and no
    prompt. The token is checked here (fresh, mode 600) and read only by the helper, at git's request."""
    import shlex
    import sys

    env = site.env()
    t = push_transport(site, url)
    if t["via"] != VIA_APP:
        return env, t
    from .git_credential import token_problem

    problem = token_problem(t["token_file"], TOKEN_MAX_AGE)
    if problem:
        fail(f"refused: config github.push_transport is \"app\" and {problem}; nothing was pushed (no fallback to SSH)")
    owner = t["owner"]
    helper = " ".join(shlex.quote(str(x)) for x in (sys.executable, "-I", HELPER, "--token-file", t["token_file"],
                                                    "--owner", owner, "--max-age", TOKEN_MAX_AGE))
    pairs = [(f"url.https://github.com/{owner}/.insteadOf", f"git@github.com:{owner}/"),
             (f"url.https://github.com/{owner}/.insteadOf", f"ssh://git@github.com/{owner}/"),
             ("credential.helper", ""),
             ("credential.https://github.com.helper", "!" + helper),
             ("credential.https://github.com.useHttpPath", "true"),
             ("credential.interactive", "false")]
    try:
        n = int(env.get("GIT_CONFIG_COUNT") or 0)
    except ValueError:
        n = 0
    for i, (k, v) in enumerate(pairs, start=n):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
    env["GIT_CONFIG_COUNT"] = str(n + len(pairs))
    env["GIT_TERMINAL_PROMPT"] = "0"
    for k in ("GIT_ASKPASS", "SSH_ASKPASS"):
        env.pop(k, None)
    return env, t


def check_resolved(repo_path: Path, remote: str, env: dict, t: dict) -> None:
    """Fail-closed: the remote must resolve to the App's https URL under `env`; a checkout or global `insteadOf` that
    wins over ours (same or longer prefix) would send the push elsewhere under the App's name."""
    if t["via"] != VIA_APP:
        return
    proc = subprocess.run(["git", "ls-remote", "--get-url", remote], cwd=repo_path, capture_output=True, text=True,
                          env=env, timeout=30)
    got = proc.stdout.strip()
    want = t["https_url"]
    if proc.returncode or got.rstrip("/").removesuffix(".git") != want.removesuffix(".git"):
        fail(f"refused: the push over the company App must go to {want}, but git resolves {remote} to {got or '?'}"
             " (a url.<base>.insteadOf in this checkout or the global git config wins); nothing was pushed")


def transports(site: Site) -> dict:
    """`dureh github transport`: every GitHub remote config push names (push.allowed_remotes entries, push.policy
    owners) with the transport `dureh push` uses for it. Config only; no network, no token."""
    pcfg = site.config.get("push") or {}
    rows, seen = [], set()
    allowed = pcfg.get("allowed_remotes") or []
    for e in allowed if isinstance(allowed, list) else []:
        u = e if isinstance(e, str) else (e.get("url") if isinstance(e, dict) else None)
        if not isinstance(u, str) or u in seen:
            continue
        seen.add(u)
        t = push_transport(site, u)
        if t.get("why") == "not a GitHub remote":
            continue
        rows.append({"remote": u, "source": "allowed_remotes", **{k: v for k, v in t.items() if k != "token_file"}})
    pol = pcfg.get("policy") or []
    for r in pol if isinstance(pol, list) else []:
        if not isinstance(r, dict) or r.get("deny") is True:
            continue
        owners = ((r.get("match") or {}).get("owner")) if isinstance(r.get("match"), dict) else None
        names = (r.get("match") or {}).get("name") or ["*"]
        for o in owners if isinstance(owners, list) else []:
            for n in names if isinstance(names, list) else ["*"]:
                u = f"git@github.com:{o}/{n}.git"
                if u in seen:
                    continue
                seen.add(u)
                t = push_transport(site, u)
                rows.append({"remote": u, "source": f"policy {r.get('id')}",
                             **{k: v for k, v in t.items() if k != "token_file"}})
    raw = site.config.get("github") or {}
    return {"push_transport": raw.get("push_transport", "ssh"), "org": raw.get("org"),
            "app": raw.get("app") is not None, "remotes": rows,
            "still_on_ssh": [r["remote"] for r in rows if r["via"] != VIA_APP and r["remote"].startswith(("git@", "ssh://"))]}


def transport_lines(res: dict) -> list[str]:
    head = (f"push transport: {res['push_transport']} (config github.push_transport; company App"
            f" {'set' if res['app'] else 'not set'}, organization {res['org'] or '-'})")
    out = [head]
    for r in res["remotes"]:
        if r["via"] == VIA_APP:
            out.append(f"  app https  {r['remote']} -> {r['https_url']} ({r['source']})")
        else:
            out.append(f"  as is      {r['remote']} ({r['source']}): {r['why']}")
    if res["still_on_ssh"]:
        out.append(f"still on SSH ({len(res['still_on_ssh'])}): " + ", ".join(res["still_on_ssh"])
                   + " -- these keep needing an SSH key that can push there (a blocker for removing the Director's"
                     " SSH key, dureh-team#56 step 8)")
    else:
        out.append("still on SSH: none")
    return out
