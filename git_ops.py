"""Git + GitHub PR helpers for the auto-fix flow. Thin wrappers over `git` and
the `gh` CLI, all scoped to a repo directory. Raises RuntimeError with the
command output on failure so callers can surface it."""
import re
import subprocess

import requests


def _run(args, cwd, secret=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        out = f"`{' '.join(args)}` failed:\n{p.stdout}\n{p.stderr}".strip()
        if secret:
            out = out.replace(secret, "***")
        raise RuntimeError(out)
    return p.stdout.strip()


def parse_remote(repo):
    """Return (owner, name) from origin's URL (https or ssh form)."""
    url = _run(["git", "remote", "get-url", "origin"], repo)
    m = re.search(r"github\.com[:/]([^/]+)/(.+?)(?:\.git)?$", url)
    if not m:
        raise RuntimeError(f"Could not parse GitHub owner/repo from origin: {url}")
    return m.group(1), m.group(2)


def push_with_token(repo, branch, token):
    """Push `branch` to origin using a token, without persisting it in the remote config."""
    owner, name = parse_remote(repo)
    auth_url = f"https://x-access-token:{token}@github.com/{owner}/{name}.git"
    return _run(["git", "push", auth_url, f"{branch}:{branch}"], repo, secret=token)


def create_pr_api(repo, base, head, title, body, token):
    """Open a PR via the GitHub REST API. Returns the PR html_url."""
    owner, name = parse_remote(repo)
    r = requests.post(
        f"https://api.github.com/repos/{owner}/{name}/pulls",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "head": head, "base": base, "body": body},
        timeout=30,
    )
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub PR API {r.status_code}: {r.text}")
    return r.json()["html_url"]


def is_git_repo(repo):
    try:
        _run(["git", "rev-parse", "--is-inside-work-tree"], repo)
        return True
    except (RuntimeError, FileNotFoundError):
        return False


def default_base_branch(repo):
    """Best guess at the PR base: origin's HEAD, else main/master if present."""
    try:
        ref = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], repo)
        return ref.rsplit("/", 1)[-1]
    except RuntimeError:
        for b in ("main", "master"):
            try:
                _run(["git", "rev-parse", "--verify", b], repo)
                return b
            except RuntimeError:
                continue
    return "main"


def has_changes(repo):
    """True if the working tree has uncommitted changes."""
    return bool(_run(["git", "status", "--porcelain"], repo))


def changed_files(repo):
    """Modified + untracked file paths in the working tree."""
    out = _run(["git", "status", "--porcelain"], repo)
    files = []
    for line in out.splitlines():
        if line.strip():
            parts = line.split(maxsplit=1)  # ['XY', 'path'] — drops the status column
            if len(parts) == 2:
                files.append(parts[1].strip())
    return files


def diff(repo):
    """Unified diff of tracked changes in the working tree."""
    return _run(["git", "diff"], repo)


def discard(repo):
    """Revert all uncommitted changes to tracked files."""
    _run(["git", "checkout", "--", "."], repo)


def create_branch(repo, name):
    _run(["git", "checkout", "-b", name], repo)
    return name


def _fetch(repo):
    try:
        _run(["git", "fetch", "origin"], repo)
    except RuntimeError:
        pass


def _remote_branch_exists(repo, branch):
    return bool(_run(["git", "ls-remote", "--heads", "origin", branch], repo).strip())


def _local_branch_exists(repo, branch):
    try:
        _run(["git", "rev-parse", "--verify", branch], repo)
        return True
    except RuntimeError:
        return False


def prepare_branch(repo, branch, base):
    """Check out the persistent fix branch so commits accumulate. Fetches the branch EXPLICITLY
    (works on shallow/limited clones where `git fetch origin` won't create origin/<branch>),
    falling back to a local branch, then to a new branch off base. Requires a clean tree."""
    # 1. Reuse the remote fix branch if it exists.
    try:
        _run(["git", "fetch", "origin", branch], repo)
        _run(["git", "checkout", "-B", branch, "FETCH_HEAD"], repo)
        return branch
    except RuntimeError:
        pass
    # 2. Reuse a local branch if present.
    if _local_branch_exists(repo, branch):
        _run(["git", "checkout", branch], repo)
        return branch
    # 3. Brand new branch, based on the base branch.
    try:
        _run(["git", "fetch", "origin", base], repo)
        _run(["git", "checkout", "-B", branch, "FETCH_HEAD"], repo)
    except RuntimeError:
        _run(["git", "checkout", "-B", branch, base], repo)
    return branch


def existing_pr_url(repo, branch, token):
    """Return the URL of an already-open PR for this branch, or None."""
    owner, name = parse_remote(repo)
    r = requests.get(
        f"https://api.github.com/repos/{owner}/{name}/pulls",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        params={"head": f"{owner}:{branch}", "state": "open"}, timeout=30,
    )
    if r.status_code < 300 and r.json():
        return r.json()[0]["html_url"]
    return None


def commit_all(repo, message):
    _run(["git", "add", "-A"], repo)
    return _run(["git", "commit", "-m", message], repo)


def push(repo, branch):
    return _run(["git", "push", "-u", "origin", branch], repo)


def open_pr(repo, base, head, title, body):
    """Create a PR via gh CLI. Returns the PR URL."""
    return _run([
        "gh", "pr", "create",
        "--base", base, "--head", head,
        "--title", title, "--body", body,
    ], repo)


def gh_authenticated():
    try:
        _run(["gh", "auth", "status"], ".")
        return True
    except (RuntimeError, FileNotFoundError):
        return False
