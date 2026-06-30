"""Deterministic, dimension-specific checklists over the deep-scanned page in
Redis. Each specialist agent starts from its own checklist (exact pass/fail
facts) before reasoning."""
import redis_store


def _fmt(title, url, checks):
    passed = sum(1 for ok, _, _ in checks if ok)
    lines = [f"{title} for {url}", f"Passed {passed}/{len(checks)} checks", ""]
    for ok, name, detail in checks:
        lines.append(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))
    return "\n".join(lines)


def seo_checklist(domain, url):
    p = redis_store.get_page(domain, url)
    if not p:
        return f"No scan data for {url}"
    m = redis_store.get_meta(domain)
    checks = [
        (p["https"], "HTTPS", ""),
        (p["status_code"] == 200, "HTTP 200", f"status {p['status_code']}"),
        (p["title_length"] > 0, "Title present", f"{p['title_length']} chars"),
        (30 <= p["title_length"] <= 60, "Title length 30-60", f"{p['title_length']} chars"),
        (p["meta_description_length"] > 0, "Meta description present", f"{p['meta_description_length']} chars"),
        (120 <= p["meta_description_length"] <= 160, "Meta description 120-160", f"{p['meta_description_length']} chars"),
        (p["h1_count"] == 1, "Exactly one H1", f"{p['h1_count']} H1(s)"),
        (bool(p["canonical"]), "Canonical tag", p["canonical"]),
        (bool(p["lang"]), "Lang attribute", p["lang"]),
        (bool(p["viewport"]), "Viewport (mobile)", ""),
        ("noindex" not in p["meta_robots"].lower(), "Not noindex (meta robots)", p["meta_robots"] or "(none)"),
        ("noindex" not in p["x_robots_tag"].lower(), "Not noindex (X-Robots-Tag)", p["x_robots_tag"] or "(none)"),
        (p["images_missing_alt"] == 0, "All images have alt", f"{p['images_missing_alt']}/{p['images_total']} missing"),
        (bool(m.get("robots_txt_present")), "robots.txt present", ""),
        (bool(m.get("sitemap_present")), "sitemap.xml present", f"{m.get('sitemap_url_count', 0)} URLs"),
        (p["links_internal"] > 0, "Internal links present", f"{p['links_internal']} internal"),
        (len(p["headings"]) >= 2, "Heading structure", f"{len(p['headings'])} headings"),
    ]
    return _fmt("SEO checklist", url, checks)


def aeo_checklist(domain, url):
    p = redis_store.get_page(domain, url)
    if not p:
        return f"No scan data for {url}"
    avg = p.get("avg_sentence_words", 0)
    checks = [
        (any(t in ("FAQPage", "QAPage") for t in p["schema_types"]), "FAQ/QA schema", ", ".join(p["schema_types"]) or "(none)"),
        (p["question_headings"] > 0, "Question-style headings", f"{p['question_headings']} found"),
        (p.get("list_count", 0) + p.get("table_count", 0) > 0, "Lists/tables (scannable answers)", f"{p.get('list_count', 0)} lists, {p.get('table_count', 0)} tables"),
        (0 < avg <= 25, "Readable sentences (<=25 avg words)", f"{avg} avg words/sentence"),
        (p["word_count"] >= 300, "Content depth (>=300 words)", f"{p['word_count']} words"),
        (len(p["headings"]) >= 3, "Rich heading structure", f"{len(p['headings'])} headings"),
        (p["meta_description_length"] > 0, "Meta description (answer preview)", f"{p['meta_description_length']} chars"),
        (bool(p["og"]), "Open Graph (rich preview)", ", ".join(sorted(p["og"])) or "(none)"),
    ]
    return _fmt("AEO checklist", url, checks)


def geo_checklist(domain, url):
    p = redis_store.get_page(domain, url)
    if not p:
        return f"No scan data for {url}"
    checks = [
        (bool(p["schema_types"]), "Structured data (JSON-LD)", ", ".join(p["schema_types"]) or "(none)"),
        ("Organization" in p["schema_types"], "Organization schema", ""),
        (p["has_author"], "Author / Person signal", ""),
        (p["sameas_count"] > 0, "sameAs entity links", f"{p['sameas_count']} found"),
        (bool(p.get("publisher")), "Publisher entity", p.get("publisher", "") or "(none)"),
        (bool(p.get("date_published")), "Date published", str(p.get("date_published", "")) or "(none)"),
        (bool(p.get("date_modified")), "Date modified (freshness)", str(p.get("date_modified", "")) or "(none)"),
        (p["links_external"] > 0, "External citations/links", f"{p['links_external']} external"),
        (p["word_count"] >= 300, "Citable depth (>=300 words)", f"{p['word_count']} words"),
    ]
    return _fmt("GEO checklist", url, checks)


# ----------------------------- Site-level aggregates (full-site mode) -----------------------------
# Predicate per issue over a single page's signals; counted across all crawled pages.
SITE_ISSUES = {
    "missing_meta_description": lambda p: p["meta_description_length"] == 0,
    "short_meta_description": lambda p: 0 < p["meta_description_length"] < 120,
    "missing_title": lambda p: p["title_length"] == 0,
    "title_too_long": lambda p: p["title_length"] > 60,
    "missing_h1": lambda p: p["h1_count"] == 0,
    "multiple_h1": lambda p: p["h1_count"] > 1,
    "missing_canonical": lambda p: not p["canonical"],
    "noindex": lambda p: "noindex" in (p["meta_robots"] + " " + p["x_robots_tag"]).lower(),
    "missing_schema": lambda p: not p["schema_types"],
    "missing_open_graph": lambda p: not p["og"],
    "images_missing_alt": lambda p: p["images_missing_alt"] > 0,
    "thin_content": lambda p: p["word_count"] < 300,
    "no_question_headings": lambda p: p.get("question_headings", 0) == 0,
    "no_faq_schema": lambda p: not any(t in ("FAQPage", "QAPage") for t in p["schema_types"]),
    "no_organization_schema": lambda p: "Organization" not in p["schema_types"],
    "no_author_signal": lambda p: not p["has_author"],
}


def site_summary(domain):
    pages = list(redis_store.iter_pages(domain))
    n = len(pages)
    meta = redis_store.get_meta(domain)
    lines = [
        f"Site: {domain}",
        f"Pages crawled: {n}",
        f"robots.txt: {'found' if meta.get('robots_txt_present') else 'MISSING'}",
        f"sitemap.xml: {'found' if meta.get('sitemap_present') else 'MISSING'} ({meta.get('sitemap_url_count', 0)} URLs)",
        "",
        "Issue counts (affected pages / total):",
    ]
    for key, test in SITE_ISSUES.items():
        lines.append(f"  {key}: {sum(1 for p in pages if test(p))}/{n}")

    titles = {}
    for p in pages:
        t = p["title"].strip().lower()
        if t:
            titles.setdefault(t, []).append(p["url"])
    dupes = sum(1 for urls in titles.values() if len(urls) > 1)
    lines.append(f"  duplicate_titles: {dupes} title(s) shared across multiple pages")
    return "\n".join(lines)


def pages_with_issue(domain, issue, limit=50):
    test = SITE_ISSUES.get(issue)
    if not test:
        return f"Unknown issue '{issue}'. Valid issues: {', '.join(SITE_ISSUES)}"
    hits = [p["url"] for p in redis_store.iter_pages(domain) if test(p)]
    shown = hits[:limit]
    body = "\n".join(shown) if shown else "(none)"
    if len(hits) > len(shown):
        body += f"\n... and {len(hits) - len(shown)} more"
    return f"{len(hits)} page(s) with {issue}:\n{body}"


# ----------------------------- Fan-out helpers (deterministic backbone) -----------------------------
# Which site issues belong to which dimension (for deterministic scoring + breakdowns).
_DIM_ISSUES = {
    "SEO": ["missing_meta_description", "short_meta_description", "missing_title", "title_too_long",
            "missing_h1", "multiple_h1", "missing_canonical", "noindex", "missing_open_graph",
            "images_missing_alt", "thin_content"],
    "AEO": ["no_question_headings", "no_faq_schema"],
    "GEO": ["missing_schema", "no_organization_schema", "no_author_signal"],
}


def dimension_scores(domain):
    """Deterministic 0-100 per dimension = % of (page x check) that pass. Same input -> same score."""
    pages = list(redis_store.iter_pages(domain))
    n = len(pages) or 1
    scores = {}
    for dim, issues in _DIM_ISSUES.items():
        total = len(issues) * n
        failed = sum(sum(1 for p in pages if SITE_ISSUES[iss](p)) for iss in issues)
        scores[dim] = round(100 * (1 - failed / total)) if total else None
    return scores


# Human-readable title, why it matters, and a concrete fix example per issue.
ISSUE_INFO = {
    "missing_meta_description": ("Missing meta description", "No SERP snippet text — lower click-through.",
        'Add a 120-160 char description with a keyword + CTA, e.g. `description: "Custom ERP for enterprises — strategy, build & support. Book a call."`'),
    "short_meta_description": ("Meta description too short", "Under 120 chars wastes SERP space.",
        "Expand the description to 120-160 chars including a keyword and a benefit/CTA."),
    "missing_title": ("Missing title tag", "No clickable SERP headline.",
        "Add a 50-60 char title, primary keyword first, brand last."),
    "title_too_long": ("Title too long (>60 chars)", "Truncates in the SERP.",
        "Trim to 50-60 chars, e.g. `Custom ERP Development for Enterprises | Baaz`."),
    "missing_h1": ("Missing H1", "No clear page topic for crawlers.", "Add one descriptive `<h1>`."),
    "multiple_h1": ("Multiple H1s", "Dilutes the page's main topic.",
        "Keep one `<h1>`; demote the rest to `<h2>`/`<h3>`."),
    "missing_canonical": ("Missing canonical", "Risks duplicate-content dilution.",
        'Add a self-referencing canonical:\n```html\n<link rel="canonical" href="https://baaz.pro/your-path" />\n```'),
    "noindex": ("Set to noindex", "Excluded from search results.",
        "Remove the noindex directive if the page should rank."),
    "missing_open_graph": ("Missing Open Graph tags", "Poor link previews on social/AI surfaces.",
        "Add `og:title`, `og:description`, and `og:image`."),
    "images_missing_alt": ("Images missing alt text", "Hurts accessibility + image SEO.",
        "Add descriptive `alt` text to each image."),
    "thin_content": ("Thin content (<300 words)", "Too little to rank or be cited.",
        "Expand with substantive, specific content (aim for 600+ words on key pages)."),
    "no_question_headings": ("No question-style headings", "Answer engines lift answers under question headings.",
        'Add H2s phrased as real questions ("How do I …?") with a self-contained 40-60 word answer beneath each.'),
    "no_faq_schema": ("No FAQ/Q&A schema", "Misses FAQ rich results + AI answer extraction.",
        'Add FAQPage JSON-LD:\n```json\n{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"…?","acceptedAnswer":{"@type":"Answer","text":"…"}}]}\n```'),
    "missing_schema": ("No structured data (JSON-LD)", "LLMs rely on schema to understand the page.",
        "Add relevant JSON-LD (Article / WebPage / Organization)."),
    "no_organization_schema": ("No Organization schema", "Your brand isn't a recognized entity to LLMs.",
        'Add Organization JSON-LD with a `sameAs` array (best in the root layout so it covers all pages):\n```json\n{"@context":"https://schema.org","@type":"Organization","name":"Baaz","url":"https://baaz.pro","sameAs":["https://www.linkedin.com/company/baaz"]}\n```'),
    "no_author_signal": ("No author / Person signal", "Weakens E-E-A-T and citation trust.",
        'Add an author (`<meta name="author" …>` or an `author`/`Person` in the page schema).'),
}


def issue_breakdown(domain, dim, limit_urls=8):
    """Clean, deterministic per-dimension report: each issue -> title, why, fix example, affected pages."""
    pages = list(redis_store.iter_pages(domain))
    blocks = []
    for iss in _DIM_ISSUES[dim]:
        hits = [p["url"] for p in pages if SITE_ISSUES[iss](p)]
        if not hits:
            continue
        title, why, fix = ISSUE_INFO.get(iss, (iss, "", ""))
        shown = hits[:limit_urls]
        more = f"\n- …and {len(hits) - len(shown)} more" if len(hits) > len(shown) else ""
        blocks.append(
            f"#### {title} · {len(hits)} page(s)\n"
            f"*{why}*\n\n"
            f"**Fix:** {fix}\n\n"
            f"**Affected pages:**\n" + "\n".join(f"- {u}" for u in shown) + more
        )
    return "\n\n".join(blocks) if blocks else f"✅ No {dim} issues found across the crawled pages."
