"""Retrieval augmentation for the audit agents.

After an agent writes its first draft, we use that draft (the issues it found) as the query to
retrieve the most relevant handbook passages AND real worked Don't/Do examples, formatted as
'reference material'. The agent then revises into a richer, example-grounded final report.

Reads use the shared Chroma retriever built on the main thread (tools._retr), so this is safe to
call from the parallel agent threads (only client *creation* is thread-sensitive, not reads)."""
import examples
import tools

# Chapter-29 example categories most relevant to each dimension.
_DIM_EXAMPLE_CATS = {
    "SEO": [1, 3, 7, 8],     # titles/meta, headings & structure, technical, schema
    "AEO": [15, 17, 16],     # featured snippets, FAQ/Q&A, voice
    "GEO": [18, 20, 19],     # citability rewrites, entity authority, AI crawler access
}


def _dedup(chunks, limit):
    seen, out = set(), []
    for c in chunks:
        key = c[:120]
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def reference_material(dim, draft, max_chunks=5, examples_per_cat=2):
    """Build a reference block for the revise step: handbook passages semantically matched to the
    draft's issues + worked examples for this dimension's categories."""
    retriever = tools._retr()
    # Query the handbook with the draft's content (its issues) and a dimension cue, then dedup.
    chunks = []
    for query in (draft[:1500], f"{dim} best practices and fixes"):
        try:
            chunks += [d.page_content[:600] for d in retriever.invoke(query)]
        except Exception:
            pass
    chunks = _dedup(chunks, max_chunks)

    blocks = []
    if chunks:
        blocks.append("HANDBOOK GUIDANCE:\n" + "\n\n".join(chunks))
    ex = [examples.get_examples(cat, limit=examples_per_cat) for cat in _DIM_EXAMPLE_CATS.get(dim, [])]
    if ex:
        blocks.append("WORKED EXAMPLES:\n" + "\n\n".join(ex))
    return "\n\n".join(blocks) if blocks else "(no reference material found)"
