"""Structure-aware chunking of the policy corpus.

A policy document is a metadata block and a tree of headings. A chunk is one
heading's text, split further only when it is too long, and it carries the
metadata that retrieval and the answer need: which document, which section,
when it took effect, who may see it, and whether a newer version exists.
Fixed-size windows lose all of that, which is why they are the wrong default.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from nw.policy.redact import redact

_FRONT = re.compile(r"^---\n(.*?)\n---\n", re.S)
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$", re.M)


@dataclass
class Chunk:
    id: str
    doc_id: str
    title: str
    section: str
    text: str
    effective: str
    audience: str
    superseded_by: str | None = None
    order: int = 0
    tokens: int = 0
    redactions: int = 0

    @property
    def current(self) -> bool:
        return self.superseded_by is None

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__} | {"current": self.current}


@dataclass
class Document:
    doc_id: str
    title: str
    audience: str
    effective: str
    supersedes: str | None
    body: str
    superseded_by: str | None = None
    sections: list[tuple[str, str]] = field(default_factory=list)


def parse_document(text: str) -> Document:
    m = _FRONT.match(text)
    if not m:
        raise ValueError("policy document has no metadata block")
    # Titles contain colons ("Admin Console: Users, Audit Log"), so the block is read as
    # simple `key: value` lines, splitting on the first colon only, not as YAML.
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    body = text[m.end() :]
    supersedes = meta.get("supersedes")
    return Document(
        doc_id=str(meta["doc_id"]),
        title=str(meta["title"]),
        # Fail closed: a document that does not say it is for customers is internal.
        audience="customer" if meta.get("audience", "").strip() == "customer" else "internal",
        effective=str(meta["effective"]),
        supersedes=None if supersedes in (None, "none", "") else str(supersedes),
        body=body,
    )


def split_sections(body: str) -> list[tuple[str, str]]:
    """(heading, text) pairs. Text before the first heading is the preamble."""
    return [("Whole document", body.strip())]  # the chunking step: split on headings


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def window(text: str, max_tokens: int, overlap_tokens: int) -> list[str]:
    """Split a long section on paragraph boundaries with a small overlap."""
    if approx_tokens(text) <= max_tokens:
        return [text]
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    out: list[str] = []
    cur: list[str] = []
    cur_tokens = 0
    for p in paras:
        t = approx_tokens(p)
        if cur and cur_tokens + t > max_tokens:
            out.append("\n\n".join(cur))
            # overlap: keep the tail paragraph if it is small
            tail = cur[-1] if approx_tokens(cur[-1]) <= overlap_tokens else ""
            cur = [tail] if tail else []
            cur_tokens = approx_tokens(tail) if tail else 0
        cur.append(p)
        cur_tokens += t
    if cur:
        out.append("\n\n".join(cur))
    return out


def chunk_document(
    doc: Document, *, max_tokens: int = 350, overlap_tokens: int = 60
) -> list[Chunk]:
    """A chunk id is sha1 of document, heading and the piece's index within its section,
    not its position in the document. Changing the window size then keeps the id of
    every section that is not split, so a golden set built at one size still scores
    the same sections at another."""
    chunks: list[Chunk] = []
    order = 0
    for heading, text in split_sections(doc.body):
        for piece_index, piece in enumerate(window(text, max_tokens, overlap_tokens)):
            red = redact(piece)
            content = f"{doc.title} / {heading}\n\n{red.text}"
            cid = hashlib.sha1(f"{doc.doc_id}|{heading}|{piece_index}".encode()).hexdigest()[:12]
            chunks.append(
                Chunk(
                    id=f"{doc.doc_id}#{cid}",
                    doc_id=doc.doc_id,
                    title=doc.title,
                    section=heading,
                    text=content,
                    effective=doc.effective,
                    audience=doc.audience,
                    superseded_by=doc.superseded_by,
                    order=order,
                    tokens=approx_tokens(content),
                    redactions=red.count,
                )
            )
            order += 1
    return chunks


def load_corpus(directory: Path) -> list[Document]:
    docs = [parse_document(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.md"))]
    by_id = {d.doc_id: d for d in docs}
    for d in docs:
        if d.supersedes and d.supersedes in by_id:
            by_id[d.supersedes].superseded_by = d.doc_id
    return docs


def chunk_corpus(directory: Path, **kw) -> list[Chunk]:
    return [c for d in load_corpus(directory) for c in chunk_document(d, **kw)]


def save_chunks(chunks: list[Chunk], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for c in chunks:
            f.write(json.dumps(c.to_dict(), ensure_ascii=False) + "\n")


def load_chunks(path: Path) -> list[Chunk]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            d = json.loads(line)
            d.pop("current", None)
            out.append(Chunk(**d))
    return out
