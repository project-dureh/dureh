"""A git credential helper for company pushes over HTTPS with the company App's installation token (dureh-team#56
step 6, release 95). `dureh push` names it in the git environment it builds (github_app.push_git_env) and git runs it
as

  python3 -I <harness>/dureh_core/git_credential.py --token-file <path> --owner <org> --max-age <s> get

with the request on stdin (git credential protocol: key=value lines, a blank line ends it). It answers only `get` for
`protocol=https`, `host=github.com` and a `path` whose owner is `--owner`, and only while the token file is a regular
file of mode 600, not empty, and younger than `--max-age` seconds (`dureh github token` rewrites it every 30 min; an
installation token is valid for 1 h). Then it writes `username=x-access-token` and `password=<token>` to stdout, which
only git reads. Anything else answers `quit=1` (git stops: no other helper, no prompt, so no human credential is
used) and one line on stderr that never carries the token. `store` and `erase` are ignored.

Standard library only and no package imports: it runs as a script under `python3 -I`, so neither the harness tree
nor the user's site-packages are on its path."""
import argparse
import os
import stat
import sys
import time

USERNAME = "x-access-token"


def _refuse(why: str) -> int:
    sys.stdout.write("quit=1\n")
    sys.stdout.flush()
    sys.stderr.write(f"dureh git-credential: refused: {why} (nothing was sent to GitHub)\n")
    return 0


def read_request(stream) -> dict:
    req = {}
    for line in stream:
        line = line.rstrip("\r\n")
        if not line:
            break
        key, sep, value = line.partition("=")
        if sep:
            req[key] = value
    return req


def token_problem(path: str, max_age: float, now: float | None = None) -> str | None:
    """Why the token file cannot be used (never its content), or None."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return f"the App token file {path} does not exist (is `dureh github token --every 1800` running?)"
    except OSError as exc:
        return f"cannot stat the App token file {path}: {exc.strerror}"
    if not stat.S_ISREG(st.st_mode):
        return f"the App token file {path} is not a regular file"
    if stat.S_IMODE(st.st_mode) & 0o077:
        return f"the App token file {path} has mode {stat.S_IMODE(st.st_mode):o}; it must be 600"
    age = (time.time() if now is None else now) - st.st_mtime
    if age > max_age:
        return (f"the App token file {path} was written {int(age // 60)} min ago (limit {int(max_age // 60)} min; the"
                " token may have expired): is `dureh github token --every 1800` running? `dureh github token --once`"
                " refreshes it")
    return None


def answer(req: dict, token_file: str, owner: str, max_age: float, out=sys.stdout) -> int:
    if req.get("protocol") != "https" or req.get("host", "").lower() != "github.com":
        return _refuse(f"this helper answers https://github.com only, not {req.get('protocol')}://{req.get('host')}")
    path = req.get("path", "").lstrip("/")
    if not path:
        return _refuse("git sent no repository path (credential.useHttpPath is off): the owner cannot be checked")
    if path.split("/", 1)[0].lower() != owner.lower():
        return _refuse(f"the repository {path} is not of {owner}, the organization the company App is installed on")
    problem = token_problem(token_file, max_age)
    if problem:
        return _refuse(problem)
    try:
        with open(token_file, encoding="utf-8") as f:
            token = f.read().strip()
    except OSError as exc:
        return _refuse(f"cannot read the App token file {token_file}: {exc.strerror}")
    if not token or any(c.isspace() for c in token):
        return _refuse(f"the App token file {token_file} is empty or holds more than one token")
    out.write(f"username={USERNAME}\npassword={token}\n")
    out.flush()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="git_credential.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--token-file", required=True)
    p.add_argument("--owner", required=True)
    p.add_argument("--max-age", type=float, default=3000)
    p.add_argument("operation")
    a = p.parse_args(argv)
    req = read_request(sys.stdin)
    if a.operation != "get":
        return 0  # store / erase: the token file is the only store
    return answer(req, a.token_file, a.owner, a.max_age)


if __name__ == "__main__":
    sys.exit(main())
