"""Auto-fix engine. Claude Code (the `claude` CLI in headless mode) does the
coding: given the audit findings, it edits the Next.js repo's source directly.
We keep the deterministic, controlled parts:
    run_claude_fix  -> Claude edits the working tree (edit permission only, no git)
    verify_build    -> optional `next build` (advisory)
    open_pr         -> branch -> commit -> push (token) -> PR via GitHub API

The git diff Claude produces is shown for human approval before open_pr runs.
"""
import os
import shutil
import subprocess

from dotenv import load_dotenv

import git_ops

load_dotenv()

# Single persistent branch that accumulates fix commits. Note: a plain "autoFix" collides
# with the old "autoFix/seo-*" branches in git's ref namespace, so use a flat name.
FIX_BRANCH = "seo-autofix"

CLAUDE_FIX_PROMPT = """You are fixing SEO, AEO and GEO issues in THIS Next.js (App Router) repository.
The audit below was run on the page: {url}

Address ALL THREE dimensions, not just SEO. Map {url} to its route folder under app/ (home = app/layout.js
/ app/page.js) and edit the right file(s):
- SEO: title (~50-60 chars), meta description (~120-160 with a CTA), canonical, headings, Open Graph.
- AEO: question-style headings and front-loaded answers; FAQ/QAPage JSON-LD for real questions. You MAY
  edit the page's visible JSX (headings/sections) to add a concise answer or a question heading.
- GEO: Organization JSON-LD with a sameAs array, author/Person signals, Article/WebPage schema, freshness.

Rules:
- Make MINIMAL, correct edits that match the existing code style and stay valid TS/JS.
- If a signal is ALREADY present and adequate, leave it (do not duplicate) — but say so in COVERAGE.
- Do NOT invent unverifiable facts (specific metrics, awards, review counts, dates). If a fix needs a
  real value you don't have, skip it and note it in COVERAGE as needing input.
- Do NOT run git or shell commands and do NOT install anything. Only edit source files.

At the END of your reply, output a section exactly like this so the user can verify coverage:

## COVERAGE
- SEO: <what you changed, or "already adequate", or "needs input: ...">
- AEO: <what you changed, or "already adequate", or "needs input: ...">
- GEO: <what you changed, or "already adequate", or "needs input: ...">

AUDIT REPORTS:
{reports}
"""

CLAUDE_FIX_PROMPT_SITE = """You are fixing SEO, AEO and GEO issues across an ENTIRE Next.js (App Router) website.
The audit below covers the WHOLE site {url} — many pages were crawled, and the issues span MULTIPLE routes.

CRITICAL: the reports name SPECIFIC affected page URLs (e.g. /case-studies/..., /work1, /terms, /privacy,
/erp/...). Do NOT just fix the home page. For EVERY affected route mentioned in the findings:
- Map its URL path to the file under app/ (e.g. /case-studies/x -> app/case-studies/x/{{layout,page}}.js;
  /work1 -> app/work1/{{layout,page}}.js; home -> app/layout.js / app/page.js).
- Open that route's file and fix ITS issues across all three dimensions.

Fix types: SEO (title ~50-60 chars, meta description ~120-160 + CTA, single H1, canonical, Open Graph,
image alt), AEO (question-style headings + front-loaded answers, FAQ/QAPage JSON-LD), GEO (Organization
JSON-LD + sameAs, author/Person signals, Article/WebPage schema, freshness).

Rules:
- Work through the affected routes systematically; fix as many as you can this run.
- Make MINIMAL, correct edits matching existing code style; valid TS/JS.
- If a signal is already adequate on a route, leave it. Do NOT invent unverifiable facts — skip and note it.
- Do NOT run git or shell commands and do NOT install anything. Only edit source files.

At the END, output a '## COVERAGE' section: per dimension (SEO/AEO/GEO), list the ROUTES/FILES you changed
(and any routes skipped + why).

AUDIT REPORTS:
{reports}
"""


def run_claude_fix(repo_path, sections, url, mode="page", timeout=900):
    """Run Claude Code headless to edit the repo. `mode='site'` fixes across all affected routes;
    `mode='page'` fixes the single audited route. Returns {ok, output}."""
    claude = shutil.which("claude") or shutil.which("claude.exe")
    if not claude:
        raise RuntimeError("claude CLI not found on PATH.")
    reports = "\n\n".join(f"### {dim}\n{txt}" for dim, txt in sections.items())
    template = CLAUDE_FIX_PROMPT_SITE if mode == "site" else CLAUDE_FIX_PROMPT
    prompt = template.format(url=url, reports=reports)
    p = subprocess.run(
        [claude, "-p", "--permission-mode", "acceptEdits"],
        cwd=repo_path, input=prompt, capture_output=True, text=True, timeout=timeout,
    )
    return {"ok": p.returncode == 0, "output": (p.stdout or "") + (p.stderr or "")}


def verify_build(repo_path, timeout=900):
    """Run npm install + next build. Returns (ok, log_tail). Advisory only."""
    if not (shutil.which("npm") or shutil.which("npm.cmd")):
        return False, "npm not found on PATH."

    def _npm(cmd):
        return subprocess.run(cmd, cwd=repo_path, capture_output=True, text=True,
                              timeout=timeout, shell=True)

    try:
        inst = _npm("npm install --legacy-peer-deps")
        if inst.returncode != 0:
            return False, ("npm install failed:\n" + (inst.stdout + inst.stderr)[-2000:])
        build = _npm("npx --no-install next build")
        return build.returncode == 0, (build.stdout + build.stderr)[-2000:]
    except subprocess.TimeoutExpired:
        return False, f"Build timed out after {timeout}s."


def prepare_branch(repo_path, base="master", branch=FIX_BRANCH):
    """Switch onto the persistent fix branch BEFORE Claude edits (clean tree => no conflicts),
    reusing origin's branch so commits accumulate across runs."""
    return git_ops.prepare_branch(repo_path, branch, base)


def open_pr(repo_path, url, base="master", branch=FIX_BRANCH, token=None):
    """Commit the (already-edited, on-branch) tree, push, and open the PR — or reuse the existing
    open PR so repeated runs accumulate commits on ONE branch / ONE PR. Returns the PR URL."""
    token = token or os.getenv("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN not set in .env.")
    if not git_ops.has_changes(repo_path):
        raise RuntimeError("No changes in the working tree — nothing to PR. Run the Claude fix first.")

    files = git_ops.changed_files(repo_path)
    git_ops.commit_all(repo_path, f"autofix: SEO/GEO/AEO improvements for {url}")
    git_ops.push_with_token(repo_path, branch, token)

    existing = git_ops.existing_pr_url(repo_path, branch, token)
    if existing:
        return existing  # commits pushed to the open PR; no duplicate created
    body = (f"Automated SEO/GEO/AEO fixes (Claude Code).\n\n"
            "### Files changed in this run\n" + "\n".join(f"- {f}" for f in files) +
            "\n\n_Generated by the audit auto-fixer; review before merging._")
    return git_ops.create_pr_api(repo_path, base, branch,
                                 "AutoFix: SEO/GEO/AEO improvements", body, token)
