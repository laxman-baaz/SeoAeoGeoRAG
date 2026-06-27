# SeoAeoGeoRAG

An AI-powered **SEO + AEO + GEO audit** tool that deep-scans a web page, audits it with three
specialist ReAct agents grounded in a RAG handbook, and can **auto-fix** the issues in a Next.js repo
via Claude Code → opening a pull request.

## How it works

1. **Scan** (`crawler.py`, no LLM) — fetches one page + robots.txt/sitemap, extracts every audit-relevant
   signal into **Redis**.
2. **Audit** (`agent.py`) — three specialist **ReAct agents** (SEO, AEO, GEO) run in parallel, each with
   focused tools, a reflection pass, and a RAG handbook (`search_handbook`) + real worked examples
   (`get_examples`). A synthesis step produces a composite score + executive summary.
3. **Auto-fix → PR** (`fixer.py`) — **Claude Code** (headless `claude -p`) edits the repo's source from the
   findings; deterministic git code opens a PR on a single `seo-autofix` branch.

See **CLAUDE.md** for full architecture and gotchas.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill in OPENAI_API_KEY, REDIS_*, GITHUB_TOKEN
python vector_store.py        # builds the Chroma handbook index from handbook.md
streamlit run streamlit_app.py
```

Requirements: an OpenAI key, a Redis instance, and (for auto-fix) the `claude` CLI installed +
a GitHub token. `handbook.md` is the RAG knowledge source.
