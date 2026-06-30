"""Per-dimension tool sets for the specialist agents. Each factory returns the
tools that agent is allowed to call. All tools read the deep-scanned page from
Redis (deterministic facts) or search the handbook (Chroma RAG). The shared
embedding model / retriever is loaded once and reused."""
import json

from langchain_chroma import Chroma
from langchain_core.tools import tool
from langchain_huggingface import HuggingFaceEmbeddings

import analysis
import examples
import redis_store
from vector_store import EMBEDDING_MODEL

# Token budgets. Groq free tier caps requests at 12k tokens/min, and ReAct
# accumulates every tool output into the message history, so keep these tight.
CONTENT_SLICE = 3000       # max body text handed to the LLM per call (full text stays in Redis)
HANDBOOK_K = 3             # handbook chunks returned per search
HANDBOOK_CHUNK_CHARS = 600  # chars kept per handbook chunk

_retriever = None


def _retr():
    global _retriever
    if _retriever is None:
        emb = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)
        _retriever = Chroma(persist_directory="db", embedding_function=emb).as_retriever(
            search_kwargs={"k": HANDBOOK_K}
        )
    return _retriever


def _page(domain, url):
    return redis_store.get_page(domain, url) or {}


def _content_slice(domain, url):
    text = _page(domain, url).get("content", "")
    if len(text) > CONTENT_SLICE:
        return text[:CONTENT_SLICE] + f"\n...[truncated, {len(text)} chars total]"
    return text or "(no body text)"


def _outline(domain, url):
    outline = _page(domain, url).get("headings", [])
    return "\n".join(f"{'  ' * (h['level'] - 1)}H{h['level']}: {h['text']}" for h in outline) or "(no headings)"


def _handbook_tool():
    # Build the Chroma client now, on the calling (main) thread. Creating it lazily
    # inside the tool would run on a LangGraph worker thread, where ChromaDB's Rust
    # bindings fail to initialize.
    retriever = _retr()

    @tool
    def search_handbook(query: str) -> str:
        """Search the SEO/GEO/AEO best-practices handbook for guidance on a specific topic."""
        docs = retriever.invoke(query)
        return "\n\n".join(d.page_content[:HANDBOOK_CHUNK_CHARS] for d in docs)

    return search_handbook


def _examples_tool():
    @tool
    def get_examples(category: str) -> str:
        """Return real worked Don't/Do example pairs from the handbook's compendium for a
        category (number 1-21 or keyword). Categories: 1 title tags & meta, 2 URLs,
        3 headings & structure, 4 keyword usage, 5 internal linking, 6 images, 7 technical
        (robots/canonical/sitemaps), 8 schema JSON-LD, 9 page experience, 10 off-page,
        11 local, 12 e-commerce, 13 international/hreflang, 14 E-E-A-T, 15 featured snippets,
        16 voice, 17 FAQ/Q&A, 18 GEO citability rewrites, 19 AI crawler access, 20 entity
        authority, 21 measurement. Mirror the 'Do:' pattern in your fix."""
        return examples.get_examples(category)

    return get_examples


# ----------------------------- SEO -----------------------------
def seo_tools(domain, url):
    @tool
    def get_seo_checklist() -> str:
        """Deterministic pass/fail of core technical & on-page SEO signals."""
        return analysis.seo_checklist(domain, url)

    @tool
    def get_technical_signals() -> str:
        """HTTP status, HTTPS, robots directives (meta robots, X-Robots-Tag, robots.txt),
        sitemap presence, canonical, lang, charset, viewport, hreflang."""
        p, m = _page(domain, url), redis_store.get_meta(domain)
        return json.dumps({
            "status_code": p.get("status_code"), "https": p.get("https"),
            "x_robots_tag": p.get("x_robots_tag"), "meta_robots": p.get("meta_robots"),
            "canonical": p.get("canonical"), "lang": p.get("lang"), "charset": p.get("charset"),
            "viewport": p.get("viewport"), "hreflang": p.get("hreflang"),
            "robots_txt_present": m.get("robots_txt_present"), "robots_txt": (m.get("robots_txt") or "")[:800],
            "sitemap_present": m.get("sitemap_present"), "sitemap_url_count": m.get("sitemap_url_count"),
        }, indent=2)

    @tool
    def get_meta_tags() -> str:
        """Title and meta description (with lengths), keywords, author, Open Graph and Twitter card tags."""
        p = _page(domain, url)
        return json.dumps({k: p.get(k) for k in (
            "title", "title_length", "meta_description", "meta_description_length",
            "meta_keywords", "meta_author", "og", "twitter",
        )}, indent=2)

    @tool
    def get_heading_structure() -> str:
        """Full h1-h6 outline with H1 count and total heading count."""
        p = _page(domain, url)
        return f"H1 count: {p.get('h1_count')}\nTotal headings: {len(p.get('headings', []))}\n\n{_outline(domain, url)}"

    @tool
    def get_links_and_images() -> str:
        """Internal/external link counts and image alt-text coverage (with sample missing-alt sources)."""
        p = _page(domain, url)
        return json.dumps({
            "links_internal": p.get("links_internal"), "links_external": p.get("links_external"),
            "images_total": p.get("images_total"), "images_missing_alt": p.get("images_missing_alt"),
            "missing_alt_samples": p.get("images_missing_alt_srcs", [])[:10],
        }, indent=2)

    return [get_seo_checklist, get_technical_signals, get_meta_tags,
            get_heading_structure, get_links_and_images, _handbook_tool(), _examples_tool()]


# ----------------------------- AEO -----------------------------
def aeo_tools(domain, url):
    @tool
    def get_aeo_checklist() -> str:
        """Deterministic pass/fail of answer-engine readiness signals."""
        return analysis.aeo_checklist(domain, url)

    @tool
    def get_question_coverage() -> str:
        """Question-style headings and FAQ/QA structured data present on the page."""
        p = _page(domain, url)
        qs = [h["text"] for h in p.get("headings", []) if "?" in h["text"]]
        return json.dumps({
            "question_headings_count": p.get("question_headings"),
            "question_headings": qs,
            "faq_or_qa_schema": [t for t in p.get("schema_types", []) if t in ("FAQPage", "QAPage")],
            "all_schema_types": p.get("schema_types"),
        }, indent=2)

    @tool
    def get_answer_structure() -> str:
        """Scannability/readability metrics: list, table, paragraph counts and average sentence length."""
        p = _page(domain, url)
        return json.dumps({
            "list_count": p.get("list_count"), "table_count": p.get("table_count"),
            "paragraph_count": p.get("paragraph_count"), "avg_sentence_words": p.get("avg_sentence_words"),
            "word_count": p.get("word_count"),
        }, indent=2)

    @tool
    def get_content() -> str:
        """The page body text (sliced) to judge whether it answers questions directly and concisely."""
        return _content_slice(domain, url)

    @tool
    def get_headings() -> str:
        """Full h1-h6 outline of the page."""
        return _outline(domain, url)

    return [get_aeo_checklist, get_question_coverage, get_answer_structure,
            get_content, get_headings, _handbook_tool(), _examples_tool()]


# ----------------------------- GEO -----------------------------
def geo_tools(domain, url):
    @tool
    def get_geo_checklist() -> str:
        """Deterministic pass/fail of generative-engine (LLM citation) readiness signals."""
        return analysis.geo_checklist(domain, url)

    @tool
    def get_structured_data() -> str:
        """All JSON-LD structured-data objects on the page, with their @types."""
        p = _page(domain, url)
        objs = p.get("schema_objects", [])
        head = f"Types: {', '.join(p.get('schema_types', [])) or '(none)'}\n\n"
        if not objs:
            return head + "(no JSON-LD)"
        body = json.dumps(objs, indent=2)
        return head + (body[:2500] + "\n...[truncated]" if len(body) > 2500 else body)

    @tool
    def get_entity_signals() -> str:
        """Author/Organization/publisher signals, sameAs entity links, and freshness dates."""
        p = _page(domain, url)
        return json.dumps({
            "has_author": p.get("has_author"), "meta_author": p.get("meta_author"),
            "organization_schema": "Organization" in p.get("schema_types", []),
            "publisher": p.get("publisher"),
            "sameas_count": p.get("sameas_count"), "sameas": p.get("sameas", []),
            "date_published": p.get("date_published"), "date_modified": p.get("date_modified"),
        }, indent=2)

    @tool
    def get_ai_crawler_access() -> str:
        """robots.txt content and whether common AI/search crawler user-agents are mentioned
        (GPTBot, OAI-SearchBot, PerplexityBot, Google-Extended, ClaudeBot, CCBot, Googlebot, Bingbot)."""
        m = redis_store.get_meta(domain)
        robots = (m.get("robots_txt") or "")
        bots = ["GPTBot", "OAI-SearchBot", "PerplexityBot", "Google-Extended",
                "ClaudeBot", "CCBot", "Googlebot", "Bingbot"]
        return json.dumps({
            "robots_txt_present": m.get("robots_txt_present"),
            "ai_bots_mentioned": {b: (b.lower() in robots.lower()) for b in bots},
            "robots_txt": robots[:800],
        }, indent=2)

    @tool
    def get_content() -> str:
        """The page body text (sliced) to judge citable, factual, authoritative depth."""
        return _content_slice(domain, url)

    return [get_geo_checklist, get_structured_data, get_entity_signals,
            get_ai_crawler_access, get_content, _handbook_tool(), _examples_tool()]


# ----------------------------- Site-level tools (full-site mode) -----------------------------
def site_tools(domain):
    """Shared tools for the site-level agents: crawl-wide aggregates + drill into any page."""

    @tool
    def get_site_summary() -> str:
        """Crawl-wide stats and per-issue counts across ALL crawled pages (start here).
        Lists every issue key you can pass to list_pages_with_issue."""
        return analysis.site_summary(domain)

    @tool
    def list_pages_with_issue(issue: str) -> str:
        """List the URLs affected by an issue key (see get_site_summary for the valid keys)."""
        return analysis.pages_with_issue(domain, issue)

    @tool
    def get_page(url: str) -> str:
        """All extracted SEO/GEO/AEO signals for one specific crawled page URL."""
        p = redis_store.get_page(domain, url)
        if not p:
            return f"No crawl data for {url}"
        return json.dumps({k: v for k, v in p.items() if k != "schema_objects"}, indent=2)[:3000]

    return [get_site_summary, list_pages_with_issue, get_page, _handbook_tool(), _examples_tool()]
