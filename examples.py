"""Real worked Don't/Do examples from handbook chapter 29 ("The Do's & Don'ts
Compendium", 21 categories, ~307 paired examples). Lets agents ground each fix
in a concrete, copy-ready example instead of inventing one."""
import re

_PATH = "handbook.md"
_cache = None


def _clean(s):
    return "".join(c if ord(c) < 128 else "-" for c in s)


def _load():
    """Parse chapter 29 into {category_number: (title, body)} once."""
    global _cache
    if _cache is None:
        text = open(_PATH, encoding="utf-8").read()
        start = re.search(r"^#\s+29\b", text, re.MULTILINE)
        end = re.search(r"^#\s+30\b", text, re.MULTILINE)
        seg = text[start.start(): end.start() if end else len(text)]
        heads = list(re.finditer(r"^##\s+(\d+)\.\s+(.*)$", seg, re.MULTILINE))
        cats = {}
        for i, h in enumerate(heads):
            s = h.end()
            e = heads[i + 1].start() if i + 1 < len(heads) else len(seg)
            cats[int(h.group(1))] = (h.group(2).strip(), seg[s:e])
        _cache = cats
    return _cache


def categories():
    return {n: t for n, (t, _) in _load().items()}


def get_examples(category, limit=4):
    """Return real Don't/Do example pairs for a chapter-29 category (number or keyword)."""
    cats = _load()
    num = None
    if isinstance(category, int) or str(category).strip().isdigit():
        num = int(str(category).strip())
    else:
        kw = str(category).lower()
        for n, (title, _) in cats.items():
            if kw in title.lower():
                num = n
                break
    if num not in cats:
        avail = "; ".join(f"{n}. {_clean(t)}" for n, (t, _) in sorted(cats.items()))
        return f"Unknown category '{category}'. Available: {avail}"

    title, body = cats[num]
    chunks = [c for c in re.split(r"(?=\*\*Don't:\*\*)", body) if "**Do:**" in c]
    pairs = [_clean(c).strip().strip(">").strip() for c in chunks[:limit]]
    return f"Real Don't/Do examples - {num}. {_clean(title)}:\n\n" + "\n\n".join(pairs)
