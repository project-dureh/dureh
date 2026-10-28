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
