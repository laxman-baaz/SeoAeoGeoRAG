"""Streamlit UI for the multi-agent SEO/GEO/AEO audit.
Run with:  streamlit run streamlit_app.py
"""
import re

import streamlit as st

import analysis
import fixer
import git_ops
import redis_store
from agent import AEO_PROMPT, GEO_PROMPT, SEO_PROMPT, run_full_audit
from crawler import scan_page

st.set_page_config(page_title="SEO + GEO + AEO Multi-Agent Audit", page_icon="🔍", layout="wide")

st.title("AI SEO + GEO + AEO Audit — Multi-Agent")
st.caption("One specialist ReAct agent per dimension. Deeper and slower, but thorough.")


def score_card(label, val):
    """A score with a big number + smaller '/100', left-aligned to match the st.metric row above."""
    shown = "—" if val is None else val
    return (
        "<div style='text-align:left'>"
        f"<div style='font-size:0.8rem;color:#9aa0a6'>{label}</div>"
        "<div style='display:flex;align-items:baseline;gap:4px'>"
        f"<span style='font-size:2.25rem;font-weight:600;line-height:1.2'>{shown}</span>"
        "<span style='font-size:1rem;color:#9aa0a6'>/100</span>"
        "</div></div>"
    )


def with_100(report):
    """Append '/100' to the agent's final 'SCORE: NN' line so it reads NN/100 (idempotent)."""
    return re.sub(r"(?i)(score\s*[:=]?\s*)(\d{1,3})\b(?!\s*/\s*100)", r"\1\2/100", report or "")


DEFAULTS = {"seo": SEO_PROMPT, "aeo": AEO_PROMPT, "geo": GEO_PROMPT}

with st.sidebar:
    st.header("Agent instructions")
    st.caption("Edit each specialist agent's prompt. Edits persist across runs.")
    for key in DEFAULTS:
        st.session_state.setdefault(f"{key}_prompt", DEFAULTS[key])
    reflect = st.checkbox("Reflection pass (slower, higher quality)", value=True,
                          help="After each agent's draft, a reviewer critiques it and the agent revises.")
    if st.button("Reset all to default"):
        for key in DEFAULTS:
            st.session_state[f"{key}_prompt"] = DEFAULTS[key]
    tab_seo, tab_aeo, tab_geo = st.tabs(["SEO", "AEO", "GEO"])
    with tab_seo:
        st.text_area("SEO agent prompt", key="seo_prompt", height=340)
    with tab_aeo:
        st.text_area("AEO agent prompt", key="aeo_prompt", height=340)
    with tab_geo:
        st.text_area("GEO agent prompt", key="geo_prompt", height=340)

url = st.text_input("Page URL", placeholder="https://example.com/page")

if st.button("Run Full Audit", type="primary"):
    if not url.strip():
        st.warning("Please enter a URL.")
        st.stop()

    with st.spinner("Scanning page (content, schema, meta, robots.txt, sitemap)..."):
        result = scan_page(url.strip())
    if "error" in result:
        st.error(result["error"])
        st.stop()

    domain, scanned_url = result["domain"], result["url"]
    page = redis_store.get_page(domain, scanned_url)

    with st.spinner("Running SEO, AEO & GEO agents in parallel..."):
        out = run_full_audit(
            domain, scanned_url,
            st.session_state["seo_prompt"], st.session_state["aeo_prompt"],
            st.session_state["geo_prompt"], reflect,
        )

    # Persist the whole audit so the report (and the Auto-Fix section) survive reruns.
    st.session_state["last_audit"] = {
        "url": scanned_url, "domain": domain,
        "stats": {"Words": page["word_count"], "Headings": len(page["headings"]),
                  "Schema types": len(page["schema_types"]), "Images no alt": page["images_missing_alt"]},
        "sections": out["sections"], "scores": out["scores"],
        "composite": out["composite"], "summary": out["summary"],
    }

# ----------------------------- Audit report (renders from session, persists) -----------------------------
audit = st.session_state.get("last_audit")
if audit:
    st.success(f"Audited {audit['url']}")

    cols = st.columns(4)
    for col, (label, val) in zip(cols, audit["stats"].items()):
        col.metric(label, val)

    scores = audit["scores"]
    sc = st.columns(4)
    for col, (label, val) in zip(sc, [("SEO", scores["SEO"]), ("AEO", scores["AEO"]),
                                      ("GEO", scores["GEO"]), ("Composite", audit["composite"])]):
        col.markdown(score_card(label, val), unsafe_allow_html=True)

    st.markdown(audit["summary"])

    t_seo, t_aeo, t_geo, t_check = st.tabs(["SEO report", "AEO report", "GEO report", "Checklists"])
    with t_seo:
        st.markdown(with_100(audit["sections"]["SEO"]))
    with t_aeo:
        st.markdown(with_100(audit["sections"]["AEO"]))
    with t_geo:
        st.markdown(with_100(audit["sections"]["GEO"]))
    with t_check:
        st.code(analysis.seo_checklist(audit["domain"], audit["url"]))
        st.code(analysis.aeo_checklist(audit["domain"], audit["url"]))
        st.code(analysis.geo_checklist(audit["domain"], audit["url"]))


# ----------------------------- Auto-Fix (Claude Code → PR) — only after a report exists -----------------
if audit:
    st.divider()
    st.header("Auto-Fix → Pull Request (Claude Code)")
    st.caption(f"Findings source: {audit['url']}")
    repo_path = st.text_input("Local repo path (a cloned Next.js repo)",
                              value=st.session_state.get("repo_path", ""))
    base = st.text_input("PR base branch", value="master")

    if st.button("Run Claude fix"):
        try:
            if not git_ops.is_git_repo(repo_path):
                st.error("That path is not a git repository.")
            elif git_ops.has_changes(repo_path):
                st.warning("The repo has uncommitted changes. Discard or commit them first so the "
                           "diff shows only Claude's edits.")
            else:
                with st.spinner(f"Preparing '{fixer.FIX_BRANCH}' branch..."):
                    fixer.prepare_branch(repo_path, base=base)
                with st.spinner("Claude Code is editing the repo (this can take a minute)..."):
                    res = fixer.run_claude_fix(repo_path, audit["sections"], audit["url"])
                st.session_state["repo_path"] = repo_path
                st.session_state["claude_ran"] = True
                st.session_state["claude_out"] = res["output"]
        except Exception as e:
            st.error(f"Claude fix failed: {e}")

    if st.session_state.get("claude_ran"):
        rp = st.session_state["repo_path"]
        out = st.session_state.get("claude_out") or ""

        # Surface the per-dimension COVERAGE report so you can verify SEO + AEO + GEO were all handled.
        parts = re.split(r"##+\s*COVERAGE", out, maxsplit=1, flags=re.I)
        if len(parts) == 2:
            st.markdown("#### Coverage (SEO / AEO / GEO)")
            st.markdown(parts[1].strip()[:1500])
        with st.expander("Full Claude output"):
            st.text(out[-4000:])

        diff = git_ops.diff(rp)
        if not diff.strip() and not git_ops.has_changes(rp):
            st.info("Claude made no changes.")
        else:
            st.subheader("Proposed changes — review before opening the PR")
            st.code(diff or "(new files added; see changed files)", language="diff")
            st.caption("Changed: " + ", ".join(git_ops.changed_files(rp)))

            run_build = st.checkbox(
                "Verify with local build before PR", value=False,
                help="Runs npm install + next build (advisory). Many repos won't build locally without "
                     "their env/setup; a failed build still lets you proceed — GitHub CI builds the PR.")

            c1, c2 = st.columns(2)
            if c1.button("Approve → open PR", type="primary"):
                if run_build:
                    with st.spinner("npm install + next build (slow)..."):
                        ok, log = fixer.verify_build(rp)
                    if ok:
                        st.success("Build passed.")
                    else:
                        st.warning("Local build did not pass (often a local env/deps issue). Proceeding; "
                                   "GitHub CI will build the PR.")
                        with st.expander("Build log"):
                            st.code(log)
                with st.spinner("Branch → commit → push → PR..."):
                    try:
                        pr_url = fixer.open_pr(rp, audit["url"], base=base)
                        st.success(f"PR opened: {pr_url}")
                        st.session_state["claude_ran"] = False
                    except Exception as e:
                        st.error(f"PR step failed: {e}")
            if c2.button("Discard changes"):
                git_ops.discard(rp)
                st.session_state["claude_ran"] = False
                st.rerun()
