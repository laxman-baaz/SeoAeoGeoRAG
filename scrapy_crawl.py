"""Standalone Scrapy crawler, run as a SUBPROCESS (so its Twisted reactor never collides with
Streamlit's long-running process). Crawls one site, reuses crawler._extract_deep for per-page
signals, and stores everything in Redis — the same shape the rest of the app expects.

Usage:  python scrapy_crawl.py <start_url> <max_pages>
The parent (crawler.crawl_site) resets the domain in Redis first, then reads the results back."""
import sys
from urllib.parse import urlparse

import scrapy
from bs4 import BeautifulSoup
from scrapy.crawler import CrawlerProcess
from scrapy.linkextractors import LinkExtractor

import crawler
import redis_store


class _Headers:
    """Case-insensitive .get over Scrapy's bytes headers, so crawler._extract_deep works unchanged."""
    def __init__(self, headers):
        self._h = {k.decode().lower(): b", ".join(v).decode("latin-1") for k, v in headers.items()}

    def get(self, key, default=""):
        return self._h.get(key.lower(), default)


class _Resp:
    """Minimal requests-like response shim around a Scrapy response for _extract_deep."""
    def __init__(self, response):
        self.status_code = response.status
        self.content = response.body
        self.headers = _Headers(response.headers)


class SiteSpider(scrapy.Spider):
    name = "site"

    def __init__(self, seeds, domain, **kw):
        super().__init__(**kw)
        self.start_urls = seeds
        self.domain = domain
        self.allowed_domains = [domain]
        self._links = LinkExtractor(allow_domains=[domain])

    def parse(self, response):
        ctype = response.headers.get(b"Content-Type", b"").decode("latin-1")
        if "text/html" not in ctype:
            return
        soup = BeautifulSoup(response.text, "html.parser")
        url = crawler._norm(response.url)
        page = crawler._extract_deep(url, soup, _Resp(response))
        page["content"] = page["content"][:2000]          # trim for full-site memory/prompt size
        page["schema_objects"] = page["schema_objects"][:3]
        redis_store.save_page(self.domain, url, page)
        for link in self._links.extract_links(response):
            yield response.follow(link.url, callback=self.parse)


def main(start_url, max_pages):
    parsed = urlparse(start_url)
    domain = parsed.netloc
    base = f"{parsed.scheme}://{domain}"

    robots = crawler._fetch_text(base, "/robots.txt")
    sitemap_urls, sitemap_present = crawler._collect_sitemap(base)
    seeds = [crawler._norm(start_url)]
    for u in sitemap_urls:
        u = crawler._norm(u)
        if urlparse(u).netloc == domain:
            seeds.append(u)

    process = CrawlerProcess(settings={
        "ROBOTSTXT_OBEY": True,
        "CONCURRENT_REQUESTS": 8,
        "AUTOTHROTTLE_ENABLED": True,
        "AUTOTHROTTLE_START_DELAY": 0.3,
        "AUTOTHROTTLE_MAX_DELAY": 5.0,
        "CLOSESPIDER_PAGECOUNT": max_pages,
        "DEPTH_LIMIT": 0,
        "RETRY_ENABLED": True,
        "LOG_LEVEL": "ERROR",
        "USER_AGENT": crawler.HEADERS["User-Agent"],
        "TELNETCONSOLE_ENABLED": False,
    })
    process.crawl(SiteSpider, seeds=list(dict.fromkeys(seeds)), domain=domain)
    process.start()  # blocks until the crawl finishes

    redis_store.set_meta(domain, {
        "start_url": start_url, "domain": domain,
        "robots_txt_present": robots is not None,
        "robots_txt": (robots or "")[:4000],
        "sitemap_present": sitemap_present,
        "sitemap_url_count": len(sitemap_urls),
        "pages_crawled": len(redis_store.page_urls(domain)),
    })


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
