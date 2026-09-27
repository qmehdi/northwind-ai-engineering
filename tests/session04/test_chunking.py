import pytest

from nw.policy.chunking import (
    chunk_corpus,
    chunk_document,
    load_corpus,
    parse_document,
    split_sections,
    window,
)
from nw.policy.redact import redact

pytestmark = pytest.mark.session04


def test_redaction_is_stable_within_a_text():
    r = redact(
        "Email billing@northwind.example or billing@northwind.example, account NW-10007, card 4111 1111 1111 1111."
    )
    assert "[EMAIL_1]" in r.text and r.text.count("[EMAIL_1]") == 2
    assert "[ACCOUNT_1]" in r.text and "[CARD_1]" in r.text
    assert "NW-10007" not in r.text and "4111" not in r.text
    assert r.count == 3


def test_sections_follow_headings():
    doc = parse_document(
        open(__file__).read()
        and __import__("tests.session04.conftest", fromlist=["DOCS"]).DOCS["sla-2025"]
    )
    sections = split_sections(doc.body)
    assert [h for h, _ in sections] == ["Uptime commitments", "Response times", "Service credits"]
    assert "99.95" in sections[0][1]


def test_chunks_carry_metadata_and_supersession(corpus_dir):
    chunks = chunk_corpus(corpus_dir)
    by_doc = {}
    for c in chunks:
        by_doc.setdefault(c.doc_id, []).append(c)
    assert set(by_doc) == {"sla-2025", "sla-2023", "refunds", "escalation-internal"}
    old = by_doc["sla-2023"][0]
    assert old.superseded_by == "sla-2025" and not old.current
    assert by_doc["sla-2025"][0].current and by_doc["sla-2025"][0].effective == "2025-03-01"
    assert by_doc["escalation-internal"][0].audience == "internal"
    assert all(c.text.startswith(c.title) for c in chunks)


def test_sensitive_fields_never_reach_a_chunk(corpus_dir):
    chunks = chunk_corpus(corpus_dir)
    joined = " ".join(c.text for c in chunks)
    assert (
        "billing@northwind.example" not in joined
        and "NW-10007" not in joined
        and "555 0100" not in joined
    )
    assert sum(c.redactions for c in chunks) >= 3


def test_long_sections_are_windowed_with_overlap():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
    pieces = window(text, max_tokens=200, overlap_tokens=80)
    assert len(pieces) > 1
    assert all(len(p) // 4 <= 260 for p in pieces)
    assert any(
        pieces[i].split("\n\n")[0] == pieces[i - 1].split("\n\n")[-1] for i in range(1, len(pieces))
    )


def test_corpus_links_supersession_both_ways(corpus_dir):
    docs = {d.doc_id: d for d in load_corpus(corpus_dir)}
    assert docs["sla-2025"].supersedes == "sla-2023"
    assert docs["sla-2023"].superseded_by == "sla-2025"


def test_dates_and_percentages_survive_redaction():
    r = redact(
        "Effective 2025-03-01, uptime 99.95 percent, credits within 30 days, call +1 555 0100 2200."
    )
    assert "2025-03-01" in r.text and "99.95" in r.text and "30 days" in r.text
    assert "[PHONE_1]" in r.text and r.count == 1


def test_chunk_ids_are_unique_and_independent_of_the_window(corpus_dir):
    """Step 8 changes the window size; gold chunk ids must survive for every section
    that is not split, so the id depends on the section, not on document order."""
    default = chunk_corpus(corpus_dir)
    ids = [c.id for c in default]
    assert len(ids) == len(set(ids))
    assert set(ids) == {c.id for c in chunk_corpus(corpus_dir, max_tokens=1000)}
    long_section = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(6))
    doc = parse_document(
        "---\ntitle: Long\ndoc_id: long\naudience: customer\neffective: 2025-01-01\n---\n"
        "# Long\n\n## Short\n\nOne line.\n\n## Wide\n\n" + long_section + "\n"
    )
    whole = {c.section: c.id for c in chunk_document(doc)}
    split = chunk_document(doc, max_tokens=60, overlap_tokens=10)
    assert len(split) > 2 and len({c.id for c in split}) == len(split)
    assert [c.id for c in split if c.section == "Short"] == [whole["Short"]]
    assert [c.id for c in split if c.section == "Wide"][0] == whole["Wide"]
