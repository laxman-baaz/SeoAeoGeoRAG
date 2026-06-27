"""Single-page deep scanner. Fetches one page and extracts EVERYTHING relevant
to an SEO/GEO/AEO audit (full content, full heading outline, all schema, all
meta tags, plus site-level robots.txt and sitemap.xml). All of it is stored in
Redis (see redis_store) for the ReAct agent to reason over."""
import json
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import redis_store

USER_AGENT = "SEOAuditBot/1.0"
HEADERS = {"User-Agent": f"Mozilla/5.0 (compatible; {USER_AGENT})"}


def _meta(soup, name=None, prop=None):
    if name:
        tag = soup.find("meta", attrs={"name": name})
    else:
        tag = soup.find("meta", attrs={"property": prop})
    return tag.get("content", "").strip() if tag and tag.get("content") else ""


def _fetch_text(base, path):
    """Return the body of base+path if it exists (HTTP 200), else None."""
    try:
        r = requests.get(urljoin(base, path), headers=HEADERS, timeout=10)
        return r.text if r.status_code == 200 else None
    except Exception:
        return None


def _collect_sitemap(base):
    """Return (urls, present), expanding one level of sitemap-index nesting."""
    urls, present = [], False
    text = _fetch_text(base, "/sitemap.xml")
    if text is not None:
        present = True
        for loc in re.findall(r"<loc>\s*(.*?)\s*</loc>", text):
            if loc.endswith(".xml"):
                sub = _fetch_text(base, loc)
                if sub:
                    urls += re.findall(r"<loc>\s*(.*?)\s*</loc>", sub)
            else:
                urls.append(loc)
    return urls, present


def _extract_deep(url, soup, resp):
    """Exhaustive per-page signal extraction. Nothing is truncated."""
    domain = urlparse(url).netloc

    title = soup.title.string.strip() if soup.title and soup.title.string else ""

    canonical_tag = soup.find("link", rel="canonical")
    canonical = canonical_tag.get("href", "").strip() if canonical_tag else ""

    html_tag = soup.find("html")
    lang = html_tag.get("lang", "").strip() if html_tag else ""

    charset_tag = soup.find("meta", charset=True)
    charset = charset_tag.get("charset", "").strip() if charset_tag else ""

    # Full heading outline (h1-h6), in document order per level.
    outline = []
    for lvl in range(1, 7):
        for h in soup.find_all(f"h{lvl}"):
            txt = h.get_text(strip=True)
            if txt:
                outline.append({"level": lvl, "text": txt})
    h1_count = sum(1 for h in outline if h["level"] == 1)
    question_headings = sum(1 for h in outline if h["text"].rstrip().endswith("?"))

    # Full body text from paragraphs and list items.
    blocks = [el.get_text(" ", strip=True) for el in soup.find_all(["p", "li"])]
    content = "\n".join(b for b in blocks if b)
    word_count = len(content.split())

    # All JSON-LD schema objects (full), plus their @types.
    schema_objects, schema_types, sameas = [], [], []
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if not isinstance(item, dict):
                continue
            schema_objects.append(item)
            t = item.get("@type")
            if t:
                schema_types.extend(t if isinstance(t, list) else [t])
            sa = item.get("sameAs")
            if sa:
                sameas.extend(sa if isinstance(sa, list) else [sa])

    # Images and alt coverage.
    images = soup.find_all("img")
    missing_alt = [img.get("src", "") for img in images if not img.get("alt", "").strip()]

    # Link breakdown (internal vs external).
    internal = external = 0
    for a in soup.find_all("a", href=True):
        p = urlparse(urljoin(url, a["href"]))
        if p.netloc == domain:
            internal += 1
        elif p.netloc:
            external += 1

    # Content structure (AEO) + readability.
    list_count = len(soup.find_all(["ul", "ol"]))
    table_count = len(soup.find_all("table"))
    paragraph_count = len(soup.find_all("p"))
    sentences = [s for s in re.split(r"[.!?]+", content) if s.strip()]
    avg_sentence_words = round(word_count / len(sentences), 1) if sentences else 0

    # Entity / freshness signals (GEO) from schema.
    date_published = date_modified = publisher = ""
    for o in schema_objects:
        date_published = date_published or o.get("datePublished", "")
        date_modified = date_modified or o.get("dateModified", "")
        if not publisher and o.get("publisher"):
            pub = o["publisher"]
            publisher = pub.get("name", "") if isinstance(pub, dict) else str(pub)

    # Open Graph + Twitter card + hreflang.
    og = {p["property"]: p.get("content", "") for p in soup.find_all("meta", property=re.compile(r"^og:"))}
    twitter = {m["name"]: m.get("content", "") for m in soup.find_all("meta", attrs={"name": re.compile(r"^twitter:")})}
    hreflang = [l.get("hreflang") for l in soup.find_all("link", rel="alternate") if l.get("hreflang")]

    meta_author = _meta(soup, name="author")
    has_author = bool(meta_author) or any(
        ("author" in o or o.get("@type") == "Person") for o in schema_objects
    )

    return {
        "url": url,
        "status_code": resp.status_code,
        "https": urlparse(url).scheme == "https",
        "content_type": resp.headers.get("Content-Type", ""),
        "x_robots_tag": resp.headers.get("X-Robots-Tag", ""),
        "page_bytes": len(resp.content),
        # meta / head
        "title": title,
        "title_length": len(title),
        "meta_description": _meta(soup, name="description"),
        "meta_description_length": len(_meta(soup, name="description")),
        "meta_robots": _meta(soup, name="robots"),
        "meta_keywords": _meta(soup, name="keywords"),
        "meta_author": meta_author,
        "canonical": canonical,
        "lang": lang,
        "charset": charset,
        "viewport": _meta(soup, name="viewport"),
        "og": og,
        "twitter": twitter,
        "hreflang": hreflang,
        # structure / content (FULL)
        "headings": outline,
        "h1_count": h1_count,
        "question_headings": question_headings,
        "content": content,
        "word_count": word_count,
        "paragraph_count": paragraph_count,
        "list_count": list_count,
        "table_count": table_count,
        "avg_sentence_words": avg_sentence_words,
        # schema (FULL)
        "schema_types": sorted(set(schema_types)),
        "schema_objects": schema_objects,
        "sameas_count": len(sameas),
        "sameas": sameas[:25],
        "has_author": has_author,
        "publisher": publisher,
        "date_published": date_published,
        "date_modified": date_modified,
        # media / links
        "images_total": len(images),
        "images_missing_alt": len(missing_alt),
        "images_missing_alt_srcs": missing_alt,
        "links_internal": internal,
        "links_external": external,
    }


def scan_page(url):
    """Deep-scan a single page into Redis. Returns {domain, url} or {error}."""
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return {"error": "Invalid URL. Include http:// or https:// (e.g. https://example.com)"}

    domain = parsed.netloc
    base = f"{parsed.scheme}://{domain}"

    try:
        redis_store.reset(domain)
    except Exception as e:
        return {"error": f"Redis not available ({e}). Check .env."}

    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except Exception as e:
        return {"error": f"Failed to fetch {url}: {e}"}

    if "text/html" not in resp.headers.get("Content-Type", ""):
        return {"error": f"Not an HTML page (Content-Type: {resp.headers.get('Content-Type', '?')})"}

    soup = BeautifulSoup(resp.text, "html.parser")
    page = _extract_deep(url, soup, resp)
    redis_store.save_page(domain, url, page)

    robots = _fetch_text(base, "/robots.txt")
    sitemap_urls, sitemap_present = _collect_sitemap(base)
    redis_store.set_meta(domain, {
        "scanned_url": url,
        "domain": domain,
        "robots_txt_present": robots is not None,
        "robots_txt": (robots or "")[:4000],
        "sitemap_present": sitemap_present,
        "sitemap_url_count": len(sitemap_urls),
    })

    return {"domain": domain, "url": url}
