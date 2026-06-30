# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A CLI + Streamlit tool that audits a site for SEO/AEO/GEO. Two modes: **single page** (deep-scan one URL) or **full site** (crawl all pages, audit site-wide aggregates). A deterministic scanner/crawler extracts every audit-relevant signal into Redis; then **three specialist ReAct agents** (SEO, AEO, GEO) reason over it (plus a local RAG handbook) and a synthesis step yields a composite score + executive summary. The split is the core idea: the scanner extracts facts exactly, the LLMs only reason.

## Commands

```bash
pip install -r requirements.txt

# Build the vector DB first — required before audits. Reads handbook.md,
# chunks it (~1359 chunks), embeds it, persists a Chroma store to ./db/
python vector_store.py

# Run an audit — pick one:
streamlit run streamlit_app.py   # web UI (URL box + rendered report)
python app.py                    # CLI (prompts for a single page URL)
```

Both entry points call the same `scan_page` → `run_audit` pipeline; `streamlit_app.py` also shows the deterministic checklist and quick metrics.

Requires a **Redis** server reachable via the `REDIS_*` vars in `.env` (Redis Cloud free tier). Without it, scans fail with a clear error.

There are no tests, linters, or build steps in this repo.

This targets **LangChain 1.x**, where integrations live in standalone packages: `langchain_text_splitters`, `langchain_chroma`, `langchain_huggingface`, `langchain_groq`. The agent is built with `create_agent` from `langchain.agents` (NOT the deprecated `langgraph.prebuilt.create_react_agent`; signature is `create_agent(model, tools, system_prompt=...)`). Do not use the old `langchain.text_splitter` / `langchain_community.vectorstores` import paths — they fail on v1. `langchain-chroma` auto-persists (no `db.persist()`).

Console output is plain ASCII on purpose — the Windows `cp1252` terminal crashes on emoji in `print()`.

## Architecture

`app.py` / `streamlit_app.py` orchestrate two stages: **deep scan (no LLM)** → **multi-agent audit (LLM)**. Scope is **single-page, in depth** (deliberately, to stay within the Redis free tier). The split is intentional: the scanner extracts facts deterministically; the agents only reason. The audit uses **three specialist ReAct agents** (SEO, AEO, GEO), each with its own focused toolset, plus a synthesis step.

0. **Full-site mode (fan-out)** — `crawler.py → crawl_site(start_url, max_pages=100, delay=0.3)` crawls every page into Redis. It runs **Scrapy** (async/concurrent, robots-aware, autothrottled) via a **subprocess** (`scrapy_crawl.py`) so Twisted's reactor never collides with the host process; the spider reuses `_extract_deep` through a small response shim, and falls back to `_crawl_requests` (the simple BFS) if Scrapy yields nothing. `CLOSESPIDER_PAGECOUNT` caps pages approximately (in-flight concurrent requests can overshoot slightly). The audit is `agent.run_site_audit_fanout(domain)`: a **hybrid** —
   - **Deterministic backbone (complete + reproducible)**: `analysis.dimension_scores(domain)` gives 0-100 per dimension from per-page checklist pass-rates (same input → same score, no LLM noise); `analysis.issue_breakdown(domain, dim)` lists EVERY affected page per issue (`_DIM_ISSUES` maps issues→dimensions). These are the score cards + the SEO/AEO/GEO tabs, so coverage is complete and stable across runs.
   - **LLM fan-out (depth)**: `agent.audit_one_page(domain, url)` = ONE LLM call per page (facts injected, no ReAct/tools), fanned out in a `ThreadPoolExecutor` (concurrency 8). Shown in the "Per-page" tab.
   - This fixed the old `run_site_audit` problem (3 agents over aggregates surfaced only ~5 varying issues). `site_summary`/`pages_with_issue`/`site_tools`/`run_site_audit`/`*_SITE_PROMPT` remain in the codebase but the UI now uses the fan-out. Single-page mode below is unchanged.

1. **`crawler.py` → `scan_page(url)`** (single-page mode) — deep-scans ONE page, no truncation: full body text, full h1–h6 outline, all JSON-LD objects, all meta/OG/Twitter/hreflang tags, canonical, lang, charset, viewport, HTTPS, status, `X-Robots-Tag`, image alt coverage, internal/external link counts; plus AEO signals (list/table/paragraph counts, avg sentence length, question-heading count) and GEO signals (author, `sameAs` list, publisher, datePublished/dateModified). Also fetches site-level **robots.txt** + **sitemap.xml**. Stores it all in Redis. Returns `{domain, url}` or `{"error": ...}`.

2. **`redis_store.py`** — the scan's memory (Redis Cloud via discrete `REDIS_HOST/PORT/USERNAME/PASSWORD` vars, falling back to `REDIS_URL`). Per-domain keys (`audit:{domain}:*`): `page:{url}` (full JSON signals), `pages` (set), `meta` (hash). `reset(domain)` wipes a domain before each scan. The `frontier`/`seen`/`visited` helpers are unused (left for a future multi-page mode).

3. **`analysis.py`** — three deterministic checklists: `seo_checklist`, `aeo_checklist`, `geo_checklist`, each a pass/fail map with a passed/total tally. Each agent starts from its own checklist.

4. **`tools.py`** — per-dimension tool factories: `seo_tools` (checklist, technical_signals, meta_tags, heading_structure, links_and_images), `aeo_tools` (checklist, question_coverage, answer_structure, content, headings), `geo_tools` (checklist, structured_data, entity_signals, ai_crawler_access, content). All include `search_handbook` (Chroma RAG) and `get_examples` (real Don't/Do pairs from handbook ch29 via `examples.py`, 21 categories / ~307 pairs). Shared retriever is cached in `_retr()` and **built at factory time on the main thread** (see gotcha). Content tools slice to `CONTENT_SLICE=6000` chars for the LLM though full text stays in Redis.

5. **`agent.py`** — the multi-agent layer. The LLM is `ChatOpenAI(MODEL)` via `_llm()` (`MODEL = "gpt-4o-mini"`; bump to `gpt-4o` for higher quality). `run_seo_agent`/`run_aeo_agent`/`run_geo_agent` each build a `create_agent` (from `langchain.agents`) with that dimension's tools + prompt (`SEO_PROMPT`/`AEO_PROMPT`/`GEO_PROMPT`, editable from the UI). Each runner takes `reflect=True`: after the ReAct draft, `_run` does a **reflect + RAG-augment + revise** pass — an LLM critic (`REVIEW_PROMPT`) finds gaps, **`rag.reference_material(dim, draft)`** retrieves handbook passages (semantically matched to the draft's issues) + real worked examples, and the agent revises (`REVISE_MSG`) using both the critique and that reference material. These are **plain LLM calls (no tools)** grounded in `_evidence()` (the deterministic checklist + key metrics) so they can't trigger model tool-format errors and can't hallucinate page facts; the block is wrapped in try/except and degrades to the draft. Each report ends with a score line parsed by `extract_score` (tolerant of `SCORE: 65`, `**SCORE:** 65`, `Score: 65/100`). `run_full_audit(..., reflect=True)` runs all three (each isolated in try/except so one failure keeps the others), computes the composite (mean), and calls `synthesize` for an executive summary + cross-cutting top-5. Returns `{sections, scores, composite, summary}`.

6. **`vector_store.py` → `create_vector_db()`** — one-time setup. Loads **`handbook.md`** (the real ~1.4MB handbook text), markdown-aware split into 1500-char chunks (200 overlap) → ~1359 chunks, embeds with local HuggingFace `sentence-transformers` (`EMBEDDING_MODEL`, imported by `tools.py`), persists to `./db/`. The agent prompts (`SEO_PROMPT`/`AEO_PROMPT`/`GEO_PROMPT`) are comprehensive: each names the handbook chapters to query, the exact signals to assess, a **worked few-shot example** (the quality bar — every finding must match its Signal/Evidence+tool/Impact/Fix-with-before→after structure), **real embedded Don't/Do examples** from the handbook plus an instruction to call `get_examples(category)` for more, and a **scoring rubric**.

## Auto-fix → PR (fixer.py / git_ops.py)

Turns an audit into a pull request against a **Next.js (App Router)** repo you control. **Claude Code (the `claude` CLI, headless) does the coding**; deterministic Python does git/PR; you review the diff.

- **`fixer.py`** — `prepare_branch(repo, base)` switches onto the **single persistent fix branch `FIX_BRANCH = "seo-autofix"`** (reusing `origin/seo-autofix` if it exists so commits accumulate) — called BEFORE editing while the tree is clean, so switching never conflicts. `run_claude_fix(repo_path, sections, url)` runs `claude -p` (prompt via **stdin**, `--permission-mode acceptEdits`) so Claude edits source directly (edit-only; no git). `verify_build` = `npm install --legacy-peer-deps` + `npx next build` (advisory). `open_pr` commits → `push_with_token` → **reuses the open PR** (`existing_pr_url`) if one exists, else `create_pr_api` — so repeated runs = ONE branch, ONE PR that accumulates commits. (Branch name is flat, not `autoFix`, because `autoFix/seo-*` branches make `autoFix` a ref-namespace dir that a plain `autoFix` branch collides with.)
- **`git_ops.py`** — git + GitHub REST helpers; `has_changes`/`changed_files`/`diff`/`discard`, and `push_with_token`/`create_pr_api` using `GITHUB_TOKEN` (no `gh` CLI; token scrubbed from errors).
- **Auto-fix chat (conversational)** — `fixer.stream_claude_chat(repo, message, sections, url, mode, session_id)` runs `claude -p --output-format stream-json --verbose --permission-mode acceptEdits`, yielding live events (`session`/`tool`/`thinking`/`result`). It's a **persistent Claude Code session**: the first call seeds the audit + `CHAT_SYSTEM` via `--append-system-prompt` and captures the `session_id`; later calls pass `--resume <session_id>` so the codebase + conversation context persist. `CHAT_SYSTEM` makes it answer/discuss by default and **only edit files when the user clearly asks** (so "hi" gets a reply, not edits). All subprocess streams use `encoding="utf-8", errors="replace"` (Claude emits UTF-8; Windows cp1252 would crash). (`stream_claude_fix`/`run_claude_fix` + `CLAUDE_FIX_PROMPT*` remain as the older one-shot variants.)
- **Streamlit "Auto-Fix → PR (Claude Code)" section** — a **bottom-pinned chat** (`st.chat_input`) after an audit. `chat_session_id` in session_state keeps the conversation; "🗨️ New conversation" resets it. Tool calls stream live into `st.status`; Claude's final text is the chat reply. `prepare_branch` runs at conversation start (clean tree); any edits accumulate on `seo-autofix`; pending changes show a **git diff** (approval gate) → **Approve → open PR** (build optional) or **Discard**.

To run it: audit a URL **served by that repo** (e.g. a `baaz.pro` route), set `GITHUB_TOKEN` in `.env`, give the local clone path, run the fix, review the diff, PR. Design rules to preserve: Claude gets **edit-only** permission (no bash/git) so it can't run commands; git/PR stays in our deterministic code; the **diff is the human gate** before commit; `claude`/`npm` are invoked so Windows resolves `.exe`/`.cmd` (claude via `shutil.which` + stdin; npm via `shell=True`). The audit agents still use OpenAI (`gpt-4o-mini`); only the fixer uses Claude.

## Important gotchas

- **The handbook lives in `handbook.md`, not `handbook.html`**: `handbook.html` is a client-rendered app whose text is **base64-embedded** inside a `<script>` (decoded at runtime via `atob`). Static parsing of the HTML yields ~0 usable text — an earlier ingest produced a single garbage chunk ("Binary file ... matches"). `handbook.md` was decoded out of that base64 blob and is the real RAG source. Rebuild with `python vector_store.py` after editing it.
- **ChromaDB must be created on the main thread**: LangGraph runs tools in worker threads, and ChromaDB's Rust bindings crash if the `PersistentClient` is first created there (`'RustBindingsAPI' object has no attribute 'bindings'`). `tools._handbook_tool()` therefore calls `_retr()` at factory-build time (main thread), not lazily inside the tool. Don't move client creation into a tool body.
- **Reflection must stay tool-less and grounded**: the revise step is deliberately a plain `llm.invoke` (no agent/tools). Letting a long revise turn call tools triggered malformed tool calls on Groq Llama (`tool_use_failed` 400); doing it tool-less but *without* evidence caused hallucinated metrics (fake HTTPS/page-speed/word-count). The fix is both: no tools **and** pass `_evidence()` with an explicit "never invent a metric not in this data" instruction. Keep both properties if you touch `_run`.
- **Redis is required**: set `REDIS_HOST`/`REDIS_PORT`/`REDIS_USERNAME`/`REDIS_PASSWORD` in `.env` (discrete vars avoid URL-encoding breakage when the password has special chars like `@`). No DB → `scan_page` returns an error.
- **Embedding model must match across build and query**: `tools.py` imports `EMBEDDING_MODEL` from `vector_store.py`. Changing it means deleting `./db/` and re-running `python vector_store.py`, or retrieval returns garbage.
- **Keys**: the LLM uses OpenAI (`OPENAI_API_KEY`); Redis needs the `REDIS_*` vars. Embeddings run locally via `sentence-transformers` (no API key; model downloads on first run). To switch back to Groq, change `_llm()`/`MODEL` in `agent.py` to `ChatGroq`/a Groq model (it has tight free-tier caps: 12k tokens/min, 100k/day).
- Each `scan_page` call does `redis_store.reset(domain)` first — a re-scan wipes prior data rather than merging.
- Agents run **in parallel** by default (`run_full_audit(..., parallel=True)`, `ThreadPoolExecutor`) — wall-clock ≈ the slowest single agent (~70s with reflection vs ~3 min sequential). `_prewarm()` builds the Chroma + Redis clients on the **main thread** first so the worker threads don't race to create the (thread-sensitive) Chroma client. Pass `parallel=False` to force sequential.
