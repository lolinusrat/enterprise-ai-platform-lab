"""Okapi BM25 over chunks. Identifiers such as PAY-4031 or max_client_conn are kept whole
and also split into their parts, so both "PAY-4031" and "4031" match."""
import math
import re
from collections import Counter

import numpy as np

TOKEN = re.compile(r"[a-z0-9]+(?:[-_.][a-z0-9]+)*")
STOP = frozenset(
    "a an and are as at be by can do does for from get got has have how i if in is it its me my "
    "of on or our should so that the their them there this to us was we what when where which "
    "who will with you your".split()
)


def normalize(token: str) -> str:
    if len(token) > 4 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    out = []
    for tok in TOKEN.findall(text.lower()):
        parts = re.split(r"[-_.]", tok)
        if len(parts) > 1:
            out.append(tok)
        out.extend(normalize(p) for p in parts if p and p not in STOP)
    return out


class BM25:
    def __init__(self, texts: list[str], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tfs = [Counter(tokenize(t)) for t in texts]
        self.lengths = np.array([sum(tf.values()) for tf in self.tfs], dtype=float)
        self.avg_len = float(self.lengths.mean())
        df = Counter(term for tf in self.tfs for term in tf)
        n = len(texts)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, tf in enumerate(self.tfs):
            for term, count in tf.items():
                self.postings.setdefault(term, []).append((i, count))

    def scores(self, query: str) -> np.ndarray:
        scores = np.zeros(len(self.tfs))
        norm = self.k1 * (1 - self.b + self.b * self.lengths / self.avg_len)
        for term in set(tokenize(query)):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, tf in self.postings[term]:
                scores[i] += idf * tf * (self.k1 + 1) / (tf + norm[i])
        return scores

    def search(self, query: str, top_k: int, mask: np.ndarray | None = None) -> list[tuple[int, float]]:
        """Top chunks with a positive score. `mask` restricts the search to permitted chunks."""
        s = self.scores(query)
        if mask is not None:
            s = np.where(mask, s, 0.0)
        idx = [i for i in np.argsort(-s, kind="stable")[:top_k] if s[i] > 0]
        return [(int(i), float(s[i])) for i in idx]
