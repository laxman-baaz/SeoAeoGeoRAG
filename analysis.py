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
