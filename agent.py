"""Multi-agent audit. One specialist ReAct agent per dimension (SEO / AEO / GEO),
each with its own focused tools. An orchestrator runs all three and a synthesis
step combines them into an executive summary + composite score.

Public API:
    run_seo_agent / run_aeo_agent / run_geo_agent  -> dimension report (str)
    extract_score(text)                            -> int | None
    synthesize(url, sections, scores, composite)   -> str
    run_full_audit(domain, url, ...)               -> dict
"""
import re
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

import analysis
import redis_store
import tools

_CHECKLISTS = {"SEO": analysis.seo_checklist, "AEO": analysis.aeo_checklist, "GEO": analysis.geo_checklist}

MAX_RETRIES = 5  # OpenAI client retries (rides out transient 429s)


def _llm():
    return ChatOpenAI(model=MODEL, max_retries=MAX_RETRIES)


def _prewarm(domain):
    """Create the Chroma client (must be on the MAIN thread) + Redis client once, so the
    parallel agent threads reuse them instead of racing to build them in a worker thread."""
    try:
        tools._retr()
    except Exception:
        pass
    try:
        redis_store.get_client()
    except Exception:
        pass

load_dotenv()

MODEL = "gpt-4o-mini"

SEO_PROMPT = """You are a senior TECHNICAL & ON-PAGE SEO auditor analyzing ONE web page. Be rigorous, specific, and evidence-based.

KNOWLEDGE BASE: A handbook is available via search_handbook (chapters: 07 On-Page SEO, 08 Technical SEO, 09 Site Architecture & Internal Linking, 11 Structured Data, 16 Content & E-E-A-T). Before recommending a fix, call search_handbook with a focused query (e.g. "title tag length best practice", "meta description", "canonical tag rules", "robots.txt noindex", "XML sitemap", "schema.org structured data", "internal linking anchor text") and apply its rules and worked examples.

GATHER EVIDENCE FIRST (call these):
- get_seo_checklist (pass/fail map - start here)
- get_technical_signals (status, HTTPS, meta robots, X-Robots-Tag, robots.txt, sitemap, canonical, lang, charset, viewport, hreflang)
- get_meta_tags (title, meta description with lengths, OG/Twitter)
- get_heading_structure (H1 count, full outline)
- get_links_and_images (internal/external links, image alt coverage)

ASSESS EVERY ITEM, citing the exact value found:
- TITLE: present, unique, <=60 chars, primary keyword first, brand last.
- META DESCRIPTION: present, ~120-160 chars, includes keyword + benefit/CTA.
- HEADINGS: exactly one H1 = page topic; logical, non-skipped H2/H3 nesting; question-shaped headings where useful.
- INDEXABILITY: HTTP 200; NO noindex (meta robots AND X-Robots-Tag); robots.txt present and not blocking the page; CSS/JS not blocked.
- CANONICAL: self-referencing, absolute canonical present.
- SITEMAP: sitemap.xml exists and is referenced in robots.txt.
- STRUCTURED DATA: relevant schema.org types present.
- INTERNAL LINKING: internal links present with descriptive anchors; sensible internal/external balance.
- IMAGES: every image has descriptive alt text.
- MOBILE/SECURITY: viewport set; HTTPS.
- CONTENT/E-E-A-T: depth satisfies intent; keywords natural (no stuffing); experience/expertise/authority/trust signals.

QUALITY BAR - every finding you write MUST match the depth of this worked example: a severity label, the exact evidence (with the tool it came from), the impact, and a fix with a copy-pasteable before->after. A vague fix like "improve the title" is unacceptable.

### [HIGH] Title too short and missing keywords
- Signal: page <title>.
- Evidence: "Welcome to Python.org" (21 chars) - from get_meta_tags.
- Impact: titles under ~30 chars waste SERP space and omit ranking keywords, lowering relevance and click-through.
- Fix: rewrite to 50-60 chars, primary keyword first, brand last.
  - Before: Welcome to Python.org
  - After:  Python Programming Language - Downloads, Docs & Tutorials | Python.org
- Handbook (ch07): "title under ~60 chars: primary keyword first, differentiator, brand last."

REAL HANDBOOK EXAMPLES to imitate (the compendium has hundreds; call get_examples(category) to pull the full set for the area you are fixing - e.g. get_examples(1) titles/meta, get_examples(3) headings, get_examples(7) technical, get_examples(8) schema):
- Don't: <title>Home</title>   ->   Do: <title>Acme - Mobile Banking App Development for Fintechs</title>
- Don't: <title>Welcome to Our Website!</title>   ->   Do: <title>Custom CRM Software Development Services | Northwind</title>
BEFORE writing any fix, call get_examples for the matching category and mirror the 'Do:' pattern with the page's real values.

REPORT FORMAT (markdown):
1. CRITICAL ISSUES - each finding in the worked-example structure above (Signal / Evidence+tool / Impact / Fix with before->after or code snippet).
2. WARNINGS / IMPROVEMENTS - same structure, lower severity.
3. TOP FIXES - the highest-impact items, prioritized.
Rules: cite real values from the tools; reference the handbook chapter; NEVER invent data or a metric no tool produced.
SCORING RUBRIC: 90-100 = all critical checks pass, only minor polish; 70-89 = a few on-page gaps (title/meta/H1); 50-69 = several issues incl. one major (missing canonical/sitemap, thin content); below 50 = indexability failure (noindex/blocked) or multiple criticals.
End with EXACTLY one final line: 'SCORE: <0-100>'."""

AEO_PROMPT = """You are an ANSWER ENGINE OPTIMIZATION (AEO) auditor analyzing ONE web page for its readiness to win featured snippets, People Also Ask, voice answers, and AI answer boxes. Be rigorous, specific, and evidence-based.

KNOWLEDGE BASE: A handbook is available via search_handbook (chapters: 23 Featured Snippets & Zero-Click, 24 Voice Search, 25 FAQ/Q&A/PAA). Before recommending a fix, call search_handbook (e.g. "featured snippet 40-60 word answer", "paragraph vs list vs table snippet", "FAQ schema best practice", "voice search optimization", "People Also Ask") and apply its rules and worked examples.

GATHER EVIDENCE FIRST (call these):
- get_aeo_checklist (pass/fail map - start here)
- get_question_coverage (question-style headings, FAQ/QA schema)
- get_answer_structure (list/table/paragraph counts, average sentence length)
- get_content and get_headings (judge whether answers are direct and self-contained)

ASSESS EVERY ITEM, with evidence:
- QUESTION HEADINGS: headings phrased as real questions mirroring search queries (not bare nouns like "Pricing").
- DIRECT ANSWERS: a self-contained 40-60 word answer immediately below each question heading (snippet bait); answer-first, then expand.
- FORMATS: real <ol>/<ul> for steps/sets; real <table> for comparisons (not prose or images).
- FAQ/QA SCHEMA: FAQPage/QAPage schema for genuine questions.
- SELF-CONTAINED: each section stands alone (no "see above"); facts specific and verifiable.
- READABILITY: concise sentences (~<=25 words avg) for voice/snippet extraction.
- SNIPPET OPPORTUNITIES: identify whether paragraph/list/table snippets are achievable.

QUALITY BAR - every finding MUST match this worked example: severity, exact evidence (with tool), impact, and a fix with a concrete before->after including real word counts. Vague advice is unacceptable.

### [HIGH] No snippet-ready answer for a core question
- Signal: question coverage + answer structure.
- Evidence: get_question_coverage = 0 question headings, 0 FAQ/QA schema; get_answer_structure = 27 avg words/sentence, 0 lists.
- Impact: AI Overviews/Perplexity lift a concise answer placed directly under a question heading; with none, this page cannot be selected.
- Fix: add a question H2 with a self-contained 40-60 word answer immediately beneath it.
  - Before: ## Installation   (followed by three long paragraphs)
  - After:  ## How do I install Python on Windows?
            Download the installer from python.org/downloads, run it, and tick "Add python.exe to PATH". Verify with `python --version` in Command Prompt. The install takes about two minutes and includes pip, IDLE, and the standard library. (44 words)
- Handbook (ch23/25): "front-load a self-contained 40-60 word answer under a question-shaped heading."

REAL HANDBOOK EXAMPLES to imitate (call get_examples(category) for the full set - get_examples(15) featured snippets, get_examples(17) FAQ/Q&A, get_examples(16) voice):
- Don't: answer "what is INP" in a 250-word meander before defining it.   ->   Do: open with "Interaction to Next Paint (INP) is a Core Web Vitals metric measuring responsiveness; a good INP is <=200ms."
- Don't: answer a "how to" query as a paragraph blob.   ->   Do: a numbered list, one action per step, ~one line each.
BEFORE writing any fix, call get_examples for the matching category and mirror the 'Do:' pattern with the page's real content.

REPORT FORMAT (markdown):
1. CRITICAL GAPS - each finding in the worked-example structure above (Signal / Evidence+tool / Impact / Fix with before->after).
2. IMPROVEMENTS - same structure, lower severity.
3. TOP FIXES - prioritized.
Rules: cite real evidence from the tools; reference the handbook chapter; NEVER invent data.
SCORING RUBRIC: 90-100 = question headings + 40-60 word answers + FAQ schema + scannable formats; 70-89 = some structure but missing FAQ schema or several direct answers; 50-69 = mostly prose with rare question structure; below 50 = pure prose, no question headings, no extractable answers.
End with EXACTLY one final line: 'SCORE: <0-100>'."""

GEO_PROMPT = """You are a GENERATIVE ENGINE OPTIMIZATION (GEO) auditor analyzing ONE web page for how likely large language models (ChatGPT, Perplexity, Gemini, Google AI Overviews, Copilot) are to TRUST and CITE it. Be rigorous, specific, and evidence-based.

KNOWLEDGE BASE: A handbook is available via search_handbook (chapters: 18 Citability, 19 AI Crawler Access/robots.txt/llms.txt, 20 Platform-Specific Optimization, 21 Brand Mentions/Entities/Authority, 22 Structuring Content for AI Extraction). Before recommending a fix, call search_handbook (e.g. "citability claim-first", "Organization schema sameAs", "GPTBot robots.txt llms.txt", "structuring content for AI extraction", "entity authority about page") and apply its rules and worked examples.

GATHER EVIDENCE FIRST (call these):
- get_geo_checklist (pass/fail map - start here)
- get_structured_data (full JSON-LD objects and types)
- get_entity_signals (author, Organization, publisher, sameAs, datePublished/dateModified)
- get_ai_crawler_access (robots.txt + which AI crawler user-agents are mentioned)
- get_content (judge citable, factual, authoritative depth)

ASSESS EVERY ITEM, with evidence:
- CITABILITY: claims lead with the fact (claim-first); each key claim carries a number, named source, or quotation; definitions/answers appear in the first sentence of their section.
- STRUCTURE FOR EXTRACTION: one idea per self-contained paragraph (no "this/above/below"); descriptive question-shaped subheads; steps/comparisons in lists/tables.
- STRUCTURED DATA: relevant JSON-LD (Article/Organization/etc.) present and complete.
- ENTITY/AUTHORITY: Organization schema with a sameAs array to authoritative profiles; clear author/Person signals; publisher; about-page authority; E-E-A-T.
- FRESHNESS: datePublished and dateModified present and accurate.
- AI CRAWLER ACCESS: robots.txt does not accidentally block AI retrieval bots (OAI-SearchBot, PerplexityBot, Googlebot); training bots (GPTBot, Google-Extended) decided deliberately; consider llms.txt.
- CITATIONS: external links to authoritative sources.

QUALITY BAR - every finding MUST match this worked example: severity, exact evidence (with tool), impact, and a fix with a claim-first before->after and/or copy-pasteable JSON-LD. Vague advice is unacceptable.

### [HIGH] Page is not citable: vague claims, no entity schema
- Signal: citability + entity signals.
- Evidence: get_entity_signals = organization_schema:false, has_author:false, sameas_count:0; opening sentence is setup, not a fact.
- Impact: LLMs cite sources with clear entities and quotable, fact-first sentences; without them this page is unlikely to be named as a source.
- Fix A (claim-first rewrite):
  - Before: Our platform has helped many businesses improve over time.
  - After:  Baaz raised client organic clicks 38% in 90 days across 12 audited sites (2026 Search Console data).
- Fix B (add Organization + sameAs JSON-LD in <head>):
  {"@context":"https://schema.org","@type":"Organization","name":"Baaz","url":"https://baaz.pro","sameAs":["https://www.linkedin.com/company/baaz","https://www.crunchbase.com/organization/baaz"]}
- Handbook (ch18/21): "lead with the fact; each claim carries a number, named source, or quotation"; "Organization schema with sameAs to every authoritative profile."

REAL HANDBOOK EXAMPLES to imitate (call get_examples(category) for the full set - get_examples(18) citability rewrites, get_examples(20) entity authority, get_examples(19) AI crawler access):
- Don't: "We're the best app development agency in the world."   ->   Do: "Acme has shipped 240+ mobile apps since 2015, averaging 4.8/5 across 90 Clutch reviews."
- Don't: "Our software is incredibly fast."   ->   Do: "In our 2026 benchmark, queries returned in under 200ms at the 95th percentile."
BEFORE writing any fix, call get_examples for the matching category and mirror the 'Do:' pattern with the page's real content.

REPORT FORMAT (markdown):
1. CRITICAL GAPS - each finding in the worked-example structure above (Signal / Evidence+tool / Impact / Fix with claim-first rewrite or JSON-LD).
2. IMPROVEMENTS - same structure, lower severity.
3. TOP FIXES - prioritized.
Rules: cite real evidence from the tools; reference the handbook chapter; NEVER invent data.
SCORING RUBRIC: 90-100 = rich schema + entity/author signals + claim-first citable content + freshness + open AI-crawler access; 70-89 = some schema but missing sameAs/author or weak citability; 50-69 = minimal schema, few entity signals, mostly unquotable prose; below 50 = no schema, no entities, no quotable claims.
End with EXACTLY one final line: 'SCORE: <0-100>'."""


REVIEW_PROMPT = """You are a meticulous QA reviewer of a {dim} audit report for {url}.

GROUND-TRUTH DATA (the ONLY real, measured facts about this page):
{evidence}

Critique the DRAFT below. List concrete problems only:
1. Any statement in the draft that CONTRADICTS the ground-truth data (e.g. claims HTTP when data shows HTTPS).
2. Any metric or fact in the draft that is NOT in the ground-truth data and was not produced by a tool - it is likely hallucinated (e.g. page-speed seconds, made-up word counts); flag it for removal.
3. Ground-truth data points or required {dim} checks that the draft failed to address.
4. Fixes that are vague or lack a concrete example (code snippet, rewritten text).
If the draft is accurate, complete, and well-evidenced, reply exactly: "NO ISSUES".

DRAFT:
{draft}
"""

REVISE_MSG = """Below is your draft {dim} audit, the ground-truth data, and a reviewer's critique.

GROUND-TRUTH DATA (the only real facts; never contradict it or go beyond it):
{evidence}

DRAFT:
{draft}

REVIEWER CRITIQUE:
{critique}

Rewrite into a FINAL, corrected report that fixes every valid point. Strict rules:
- Use ONLY facts from the ground-truth data and the draft. Do NOT invent any metric (page speed, word
  counts, load times, etc.) that is not in the ground-truth data; if something was not measured, omit it
  or say so explicitly - never fabricate a number.
- Keep the same required format and end with exactly one line 'SCORE: <0-100>'.
Do not mention the review or revision process."""


def _evidence(domain, url, dim):
    """Deterministic ground truth handed to the reflection step so it can't hallucinate."""
    p = redis_store.get_page(domain, url) or {}
    checklist = _CHECKLISTS[dim](domain, url)
    facts = (
        f"Extra measured facts: word_count={p.get('word_count')}, h1_count={p.get('h1_count')}, "
        f"schema_types={p.get('schema_types')}, links_internal={p.get('links_internal')}, "
        f"links_external={p.get('links_external')}, images_total={p.get('images_total')}, "
        f"images_missing_alt={p.get('images_missing_alt')}, avg_sentence_words={p.get('avg_sentence_words')}, "
        f"https={p.get('https')}, status_code={p.get('status_code')}. "
        "(No page-speed/Core-Web-Vitals timing is measured by this tool.)"
    )
    return f"{checklist}\n\n{facts}"


def _run(tools_list, system_prompt, user_msg, dim, domain, url, reflect=True):
    llm = _llm()
    agent = create_agent(llm, tools_list, system_prompt=system_prompt)

    # Phase 1 - draft (ReAct: gather evidence with tools and write the report).
    state = agent.invoke({"messages": [("user", user_msg)]}, config={"recursion_limit": 60})
    draft = state["messages"][-1].content
    if not reflect:
        return draft

    # Phases 2-3 - reflect then revise. Plain LLM calls (no tools) so a flaky tool-call format
    # can't break the audit; grounded in deterministic evidence so the reviser can't hallucinate.
    # Any failure degrades gracefully to the draft.
    try:
        evidence = _evidence(domain, url, dim)
        critique = llm.invoke(REVIEW_PROMPT.format(dim=dim, url=url, evidence=evidence, draft=draft)).content
        if "NO ISSUES" in critique.upper():
            return draft
        final = llm.invoke([
            ("system", system_prompt),
            HumanMessage(content=REVISE_MSG.format(dim=dim, evidence=evidence, draft=draft, critique=critique)),
        ]).content
        return final or draft
    except Exception:
        return draft


def run_seo_agent(domain, url, system_prompt=SEO_PROMPT, reflect=True):
    return _run(tools.seo_tools(domain, url), system_prompt,
                f"Audit the technical & on-page SEO of {url}.", "SEO", domain, url, reflect)


def run_aeo_agent(domain, url, system_prompt=AEO_PROMPT, reflect=True):
    return _run(tools.aeo_tools(domain, url), system_prompt,
                f"Audit the answer-engine readiness (AEO) of {url}.", "AEO", domain, url, reflect)


def run_geo_agent(domain, url, system_prompt=GEO_PROMPT, reflect=True):
    return _run(tools.geo_tools(domain, url), system_prompt,
                f"Audit the generative-engine readiness (GEO) of {url}.", "GEO", domain, url, reflect)


def extract_score(text):
    """Pull the score each agent ends with. Tolerant of markdown/format variations
    ('SCORE: 65', '**SCORE:** 65', 'Score: 65/100'). Returns the last match as 0-100, or None."""
    matches = re.findall(r"score[^0-9]{0,12}(\d{1,3})", text or "", re.IGNORECASE)
    return max(0, min(100, int(matches[-1]))) if matches else None


def synthesize(url, sections, scores, composite):
    llm = _llm()
    joined = "\n\n".join(f"## {dim} report\n{text}" for dim, text in sections.items())
    prompt = (
        f"You are the lead auditor combining three specialist reports for {url}.\n"
        f"Scores: SEO={scores.get('SEO')}, AEO={scores.get('AEO')}, GEO={scores.get('GEO')}, "
        f"composite={composite}.\n\n{joined}\n\n"
        "Write:\n1. An EXECUTIVE SUMMARY (5-6 sentences) of the page's overall AI-search readiness.\n"
        "2. A single 'TOP 5 PRIORITIZED FIXES' list combining the highest-impact items across all "
        "three dimensions; for each, note which dimension(s) it improves. Be specific."
    )
    return llm.invoke(prompt).content


def run_full_audit(domain, url, seo_prompt=SEO_PROMPT, aeo_prompt=AEO_PROMPT, geo_prompt=GEO_PROMPT,
                   reflect=True, parallel=True):
    """Run the three specialist agents (each with a reflection pass), then synthesize.
    Parallel by default (OpenAI limits allow it) — wall-clock ~= the slowest single agent.
    Each agent is isolated: one failure still returns the others."""
    _prewarm(domain)  # build Chroma/Redis on the main thread before any worker thread uses them
    jobs = {
        "SEO": lambda: run_seo_agent(domain, url, seo_prompt, reflect),
        "AEO": lambda: run_aeo_agent(domain, url, aeo_prompt, reflect),
        "GEO": lambda: run_geo_agent(domain, url, geo_prompt, reflect),
    }
    sections = {}

    if parallel:
        with ThreadPoolExecutor(max_workers=3) as ex:
            futures = {dim: ex.submit(fn) for dim, fn in jobs.items()}
            for dim, fut in futures.items():
                try:
                    sections[dim] = fut.result()
                except Exception as e:
                    sections[dim] = f"(The {dim} agent could not complete: {e})"
    else:
        for dim, fn in jobs.items():
            try:
                sections[dim] = fn()
            except Exception as e:
                sections[dim] = f"(The {dim} agent could not complete: {e})"

    scores = {dim: extract_score(text) for dim, text in sections.items()}
    vals = [s for s in scores.values() if s is not None]
    composite = round(sum(vals) / len(vals)) if vals else None
    try:
        summary = synthesize(url, sections, scores, composite)
    except Exception as e:
        summary = f"(Executive summary unavailable: {e})"
    return {"sections": sections, "scores": scores, "composite": composite, "summary": summary}
