"""Streamlit UI for the multi-agent SEO/GEO/AEO audit.
Run with:  streamlit run streamlit_app.py
"""
import re

import streamlit as st

import analysis
import fixer
import git_ops
import redis_store
from agent import AEO_PROMPT, GEO_PROMPT, SEO_PROMPT, run_full_audit, run_site_audit_fanout
from crawler import crawl_site, scan_page

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

mode = st.radio("Audit mode", ["Single page", "Full site"], horizontal=True)
url = st.text_input("URL", placeholder="https://example.com" if mode == "Full site" else "https://example.com/page")
max_pages = st.number_input("Max pages to crawl", 1, 1000, 100) if mode == "Full site" else None

if st.button("Run Full Audit", type="primary"):
    if not url.strip():
        st.warning("Please enter a URL.")
        st.stop()

    if mode == "Full site":
        with st.spinner(f"Crawling site (up to {max_pages} pages, polite)..."):
            result = crawl_site(url.strip(), max_pages=int(max_pages))
        if "error" in result:
            st.error(result["error"])
            st.stop()
        domain = result["domain"]
        with st.spinner(f"Auditing all {result['pages_crawled']} pages (deterministic checks + one LLM "
                        "pass per page, in parallel)..."):
            out = run_site_audit_fanout(domain)
        st.session_state["last_audit"] = {
            "mode": "site", "url": domain, "domain": domain,
            "stats": {"Pages": result["pages_crawled"]},
            "checklists": analysis.site_summary(domain),
            "sections": out["sections"], "scores": out["scores"],
            "composite": out["composite"], "summary": out["summary"],
            "pages": out.get("pages", {}),
        }
    else:
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
        st.session_state["last_audit"] = {
            "mode": "page", "url": scanned_url, "domain": domain,
            "stats": {"Words": page["word_count"], "Headings": len(page["headings"]),
                      "Schema types": len(page["schema_types"]), "Images no alt": page["images_missing_alt"]},
            "checklists": "\n\n".join([analysis.seo_checklist(domain, scanned_url),
                                       analysis.aeo_checklist(domain, scanned_url),
                                       analysis.geo_checklist(domain, scanned_url)]),
            "sections": out["sections"], "scores": out["scores"],
            "composite": out["composite"], "summary": out["summary"],
        }

# ----------------------------- Audit report (renders from session, persists) -----------------------------
audit = st.session_state.get("last_audit")
if audit:
    label = "Audited site" if audit.get("mode") == "site" else "Audited"
    st.success(f"{label}: {audit['url']}")

    cols = st.columns(len(audit["stats"]) or 1)
    for col, (k, v) in zip(cols, audit["stats"].items()):
        col.metric(k, v)

    scores = audit["scores"]
    sc = st.columns(4)
    for col, (lbl, val) in zip(sc, [("SEO", scores["SEO"]), ("AEO", scores["AEO"]),
                                    ("GEO", scores["GEO"]), ("Composite", audit["composite"])]):
        col.markdown(score_card(lbl, val), unsafe_allow_html=True)

    st.markdown(audit["summary"])

    is_site = audit.get("mode") == "site"
    tab_names = ["SEO report", "AEO report", "GEO report",
                 "Site summary" if is_site else "Checklists"]
    if is_site and audit.get("pages"):
        tab_names.append(f"Per-page ({len(audit['pages'])})")
    tabs = st.tabs(tab_names)
    with tabs[0]:
        st.markdown(with_100(audit["sections"]["SEO"]))
    with tabs[1]:
        st.markdown(with_100(audit["sections"]["AEO"]))
    with tabs[2]:
        st.markdown(with_100(audit["sections"]["GEO"]))
    with tabs[3]:
        st.code(audit["checklists"])
    if is_site and audit.get("pages"):
        with tabs[4]:
            st.caption("One LLM audit per crawled page (deterministic checks back the scores/sections).")
            for url, report in audit["pages"].items():
                with st.expander(url):
                    st.markdown(with_100(report))


# ----------------- Auto-Fix chat (Claude Code, conversational) — only after a report -----------------
if audit:
    st.divider()
    st.header("Auto-Fix → Pull Request (Claude Code)")
    st.caption(f"Findings source: {audit['url']} · chat with Claude — ask, discuss, or tell it to apply fixes.")
    repo_path = st.text_input("Local repo path (a cloned Next.js repo)",
                              value=st.session_state.get("repo_path", ""))
    base = st.text_input("PR base branch", value="master")
    st.session_state.setdefault("fix_chat", [])

    rp = st.session_state.get("repo_path") or repo_path
    repo_ok = bool(rp) and git_ops.is_git_repo(rp)
    changes = repo_ok and git_ops.has_changes(rp)

    if st.session_state.get("chat_session_id") and st.button("🗨️ New conversation"):
        st.session_state["chat_session_id"] = None
        st.session_state["fix_chat"] = []
        st.rerun()

    # --- conversation history ---
    for m in st.session_state["fix_chat"]:
        with st.chat_message(m["role"]):
            st.markdown(m["content"])

    # --- pending changes + PR controls ---
    if changes:
        files = git_ops.changed_files(rp)
        with st.expander(f"📝 Pending changes on `{fixer.FIX_BRANCH}` ({len(files)} file(s))", expanded=True):
            st.code(git_ops.diff(rp) or "(changes present)", language="diff")
        run_build = st.checkbox("Verify with local build before PR", value=False)
        c1, c2 = st.columns(2)
        if c1.button("Approve → open PR", type="primary"):
            if run_build:
                with st.spinner("npm install + next build (slow)..."):
                    ok, log = fixer.verify_build(rp)
                (st.success if ok else st.warning)(
                    "Build passed." if ok else "Build didn't pass locally; proceeding (GitHub CI will build).")
                if not ok:
                    with st.expander("Build log"):
                        st.code(log)
            with st.spinner("Branch → commit → push → PR..."):
                try:
                    st.success(f"PR opened: {fixer.open_pr(rp, audit['url'], base=base)}")
                except Exception as e:
                    st.error(f"PR step failed: {e}")
        if c2.button("Discard changes"):
            git_ops.discard(rp)
            st.rerun()

    # --- chat input (pinned to the bottom) ---
    msg = st.chat_input("Ask about a finding, discuss an approach, or tell Claude to apply a fix "
                        "(e.g. 'why is the GEO score low?' or 'shorten the /erp title').")
    if msg:
        if not (repo_path and git_ops.is_git_repo(repo_path)):
            st.error("Enter a valid local repo path above first.")
            st.stop()
        st.session_state["repo_path"] = repo_path
        first_turn = not st.session_state.get("chat_session_id")
        # Get on the fix branch at the start of a conversation (clean tree) so any edits land there.
        if first_turn and not git_ops.has_changes(repo_path):
            try:
                fixer.prepare_branch(repo_path, base=base)
            except Exception as e:
                st.error(f"Could not prepare branch: {e}")
                st.stop()

        st.session_state["fix_chat"].append({"role": "user", "content": msg})
        with st.chat_message("user"):
            st.markdown(msg)

        with st.chat_message("assistant"):
            final = ""
            with st.status("Claude…", expanded=True) as status:
                for ev in fixer.stream_claude_chat(
                        repo_path, msg,
                        sections=audit["sections"] if first_turn else None,
                        url=audit["url"], mode=audit.get("mode", "page"),
                        session_id=st.session_state.get("chat_session_id")):
                    if ev["type"] == "tool":
                        st.write(ev["text"])
                    elif ev["type"] == "thinking":
                        st.caption(ev["text"][:280])
                    elif ev["type"] == "session":
                        st.session_state["chat_session_id"] = ev["text"]
                    elif ev["type"] == "error":
                        st.error(ev["text"])
                    elif ev["type"] == "result":
                        final = ev["text"]
                status.update(label="done", state="complete")

            reply = final.strip() or "(no reply)"
            changed = git_ops.changed_files(repo_path) if git_ops.has_changes(repo_path) else []
            if changed:
                reply += "\n\n_✏️ Edited: " + ", ".join(changed) + " — review the diff above before opening a PR._"
            st.markdown(reply)

        st.session_state["fix_chat"].append({"role": "assistant", "content": reply})
        st.rerun()
