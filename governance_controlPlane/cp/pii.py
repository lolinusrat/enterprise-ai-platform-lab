"""Pattern-based PII detection and redaction for the model gateway.

A production gateway would use a proper detector (Presidio, a cloud DLP API); these
patterns cover the demo's Australian banking examples and are deliberately simple.
"""
import re

PATTERNS = [
    # BSB (ddd-ddd) followed by an account number; checked before card numbers
    ("account number", re.compile(r"\b\d{3}-\d{3}\s?\d{6,10}\b")),
    ("card number", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("email", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")),
    ("phone", re.compile(r"(?:\+61\s?4|\b04)\d{2}\s?\d{3}\s?\d{3}\b")),
    ("person name", re.compile(r"\b(?:Mr|Mrs|Ms|Dr|I'm|I am|my name is)\.?\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)")),
]


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for d in reversed(digits):
        n = int(d)
        if alt:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
        alt = not alt
    return total % 10 == 0


def find(text: str) -> list[tuple[str, int, int]]:
    """Return non-overlapping (kind, start, end) spans, in text order."""
    spans: list[tuple[str, int, int]] = []
    for kind, rx in PATTERNS:
        for m in rx.finditer(text):
            g = 1 if rx.groups else 0
            s, e = m.span(g)
            if kind == "card number" and not _luhn(re.sub(r"\D", "", m.group(0))):
                continue
            if any(s < pe and e > ps for _, ps, pe in spans):
                continue
            spans.append((kind, s, e))
    return sorted(spans, key=lambda x: x[1])


def redact(text: str, spans: list[tuple[str, int, int]]) -> str:
    out, last = [], 0
    for kind, s, e in spans:
        out.append(text[last:s])
        out.append(f"[{kind.upper().replace(' ', '_')}]")
        last = e
    out.append(text[last:])
    return "".join(out)
