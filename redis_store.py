"""Redis-backed memory for crawls. Holds the URL frontier, visited set,
per-page signals, and run metadata so a full-site crawl is not RAM-bound
and is resumable. Connection comes from REDIS_URL in .env (Redis Cloud)."""
import json
import os

import redis
from dotenv import load_dotenv

load_dotenv()

_client = None


def get_client():
    global _client
    if _client is None:
        # Prefer discrete host/port/password vars (no URL-encoding needed for
        # passwords with special characters); fall back to a full REDIS_URL.
        host = os.getenv("REDIS_HOST")
        url = os.getenv("REDIS_URL")
        if host:
            _client = redis.Redis(
                host=host,
                port=int(os.getenv("REDIS_PORT", "6379")),
                username=os.getenv("REDIS_USERNAME", "default"),
                password=os.getenv("REDIS_PASSWORD"),
                decode_responses=True,
            )
        elif url:
            _client = redis.from_url(url, decode_responses=True)
        else:
            raise RuntimeError(
                "Set REDIS_HOST/REDIS_PORT/REDIS_PASSWORD (or REDIS_URL) in .env. "
                "Free database: https://redis.io/try-free/"
            )
    return _client


def _k(domain, suffix):
    return f"audit:{domain}:{suffix}"


def reset(domain):
    """Delete all keys for a domain so each crawl starts clean."""
    r = get_client()
    keys = r.keys(_k(domain, "*"))
    if keys:
        r.delete(*keys)


# --- frontier / visited ---
def enqueue(domain, url):
    """Add a URL to the frontier once (deduped via the 'seen' set)."""
    r = get_client()
    if r.sadd(_k(domain, "seen"), url) == 0:
        return False
    r.rpush(_k(domain, "frontier"), url)
    return True


def next_url(domain):
    return get_client().lpop(_k(domain, "frontier"))


def mark_visited(domain, url):
    get_client().sadd(_k(domain, "visited"), url)


def visited_count(domain):
    return get_client().scard(_k(domain, "visited"))


# --- page data ---
def save_page(domain, url, data):
    r = get_client()
    r.set(_k(domain, f"page:{url}"), json.dumps(data))
    r.sadd(_k(domain, "pages"), url)


def get_page(domain, url):
    raw = get_client().get(_k(domain, f"page:{url}"))
    return json.loads(raw) if raw else None


def page_urls(domain):
    return sorted(get_client().smembers(_k(domain, "pages")))


def iter_pages(domain):
    for url in page_urls(domain):
        page = get_page(domain, url)
        if page:
            yield page


# --- run metadata ---
def set_meta(domain, mapping):
    get_client().hset(
        _k(domain, "meta"), mapping={k: json.dumps(v) for k, v in mapping.items()}
    )


def get_meta(domain):
    raw = get_client().hgetall(_k(domain, "meta"))
    return {k: json.loads(v) for k, v in raw.items()}
