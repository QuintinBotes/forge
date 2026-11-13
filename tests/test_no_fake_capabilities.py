"""Production code must not fabricate results (ADR-0007).

``example.invalid`` was the URL of a PR that never existed, returned by the
Temporal stand-in on production paths. It may appear in tests, never in shipped
code.

The retrieval fakes get the same treatment (ADR-0018): the hash embedder
(``DeterministicEmbeddingClient``) and the fixture reranker
(``FixtureRerankerClient``) may be *constructed* only by tests. Production
wiring builds a configured embedder/reranker or runs honestly without one
(BM25-only, ``rerank: "off"``) — it never dresses keyword/hash results up as
semantic/cross-encoded ones.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = "example.invalid"


def _shipped_python_files() -> list[Path]:
    files: list[Path] = []
    for top in ("apps", "packages"):
        for path in (ROOT / top).rglob("*.py"):
            parts = set(path.relative_to(ROOT).parts)
            if parts & {"tests", "node_modules", ".venv", "__pycache__"}:
                continue
            files.append(path)
    return files


def test_no_shipped_code_mentions_a_fabricated_pr_url() -> None:
    files = _shipped_python_files()
    assert files, "scanned no files; the repository layout changed"
    offenders = [
        str(p.relative_to(ROOT)) for p in files if FORBIDDEN in p.read_text(errors="ignore")
    ]
    assert offenders == []


# Construction sites of the retrieval fakes (ADR-0018). The defining modules
# are exempt (they define the classes; the reranker factory no longer builds
# the fixture at all — ``FORGE_RERANK_PROVIDER=fixture`` is refused, tests
# construct ``FixtureRerankerClient()`` directly), as are the two eval
# harness modules whose deterministic-client path is a documented wiring
# check. The rest of ``packages/evaluation`` — sweval, benchmark, golden and
# mint — is imported by the API and worker, so it is production code and
# stays guarded; a whole-tree exemption would hide fakes there.
_FAKE_CONSTRUCTIONS = ("DeterministicEmbeddingClient(", "FixtureRerankerClient(")
_FAKE_EXEMPT_FILES = {
    Path("packages/knowledge-core/forge_knowledge/embeddings.py"),
    Path("packages/knowledge-core/forge_knowledge/reranker.py"),
    Path("packages/evaluation/forge_eval/retrieval_eval.py"),
    Path("packages/evaluation/forge_eval/corpus_eval.py"),
}


def _fake_construction_offenders() -> list[str]:
    offenders: list[str] = []
    for path in _shipped_python_files():
        relative = path.relative_to(ROOT)
        if relative in _FAKE_EXEMPT_FILES:
            continue
        source = path.read_text(errors="ignore")
        if any(construction in source for construction in _FAKE_CONSTRUCTIONS):
            offenders.append(str(relative))
    return offenders


def test_no_shipped_code_constructs_the_retrieval_fakes() -> None:
    files = _shipped_python_files()
    assert files, "scanned no files; the repository layout changed"
    # The exempt defining modules must exist, so the exemption cannot silently
    # rot into a blanket pass if the layout moves.
    for exempt in _FAKE_EXEMPT_FILES:
        assert (ROOT / exempt).is_file(), f"exempt module missing: {exempt}"
    assert _fake_construction_offenders() == []
