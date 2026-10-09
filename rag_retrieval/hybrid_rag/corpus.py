"""Load the markdown corpus (frontmatter carries the ACL) and split it into paragraph chunks."""
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "data" / "docs"
MIN_CHUNK_CHARS = 120


@dataclass(frozen=True)
class Doc:
    id: str
    title: str
    department: str
    classification: str
    groups: tuple[str, ...]
    updated: str
    body: str


@dataclass(frozen=True)
class Chunk:
    id: str
    doc_id: str
    position: int
    text: str
    # ACL is copied onto every chunk so the indexes can filter without a join.
    classification: str = field(compare=False)
    groups: tuple[str, ...] = field(compare=False)

    def indexed_text(self, title: str) -> str:
        return f"{title}\n{self.text}"


def parse_doc(path: Path) -> Doc:
    raw = path.read_text()
    _, header, body = raw.split("---\n", 2)
    meta = dict(line.split(": ", 1) for line in header.strip().splitlines())
    return Doc(
        id=path.stem,
        title=meta["title"],
        department=meta["department"],
        classification=meta["classification"],
        groups=tuple(g.strip() for g in meta["groups"].split(",")),
        updated=meta["updated"],
        body=body.strip(),
    )


def chunk_doc(doc: Doc) -> list[Chunk]:
    """One chunk per paragraph; short paragraphs are merged into the next one."""
    chunks, buf = [], ""
    for para in (p.strip() for p in doc.body.split("\n\n") if p.strip()):
        buf = f"{buf}\n\n{para}" if buf else para
        if len(buf) >= MIN_CHUNK_CHARS:
            chunks.append(buf)
            buf = ""
    if buf:
        if chunks and len(buf) < MIN_CHUNK_CHARS:
            chunks[-1] = f"{chunks[-1]}\n\n{buf}"
        else:
            chunks.append(buf)
    return [
        Chunk(f"{doc.id}#{i}", doc.id, i, text, doc.classification, doc.groups)
        for i, text in enumerate(chunks)
    ]


def load_corpus(path: Path = DOCS) -> tuple[dict[str, Doc], list[Chunk]]:
    docs = {d.id: d for d in (parse_doc(p) for p in sorted(path.glob("*.md")))}
    chunks = [c for d in docs.values() for c in chunk_doc(d)]
    return docs, chunks
