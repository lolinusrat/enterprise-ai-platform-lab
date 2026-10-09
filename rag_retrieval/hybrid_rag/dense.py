"""Dense retrieval: bge-small embeddings (ONNX via fastembed), exact cosine search over a matrix.
Embeddings are cached on disk, keyed by model and corpus content."""
import hashlib
from pathlib import Path

import numpy as np
from fastembed import TextEmbedding

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
CACHE = Path(__file__).resolve().parent.parent / ".cache"


class DenseIndex:
    def __init__(self, texts: list[str], model: str = EMBED_MODEL):
        self.model = TextEmbedding(model)
        key = hashlib.sha256((model + "\x00".join(texts)).encode()).hexdigest()[:16]
        path = CACHE / f"emb_{key}.npy"
        if path.exists():
            self.matrix = np.load(path)
        else:
            self.matrix = self._normed(np.array(list(self.model.embed(texts))))
            CACHE.mkdir(exist_ok=True)
            np.save(path, self.matrix)

    @staticmethod
    def _normed(m: np.ndarray) -> np.ndarray:
        return m / np.linalg.norm(m, axis=-1, keepdims=True)

    def embed_query(self, query: str) -> np.ndarray:
        return self._normed(np.array(list(self.model.embed([QUERY_PREFIX + query]))[0]))

    def search(self, query: str, top_k: int, mask: np.ndarray | None = None,
               qvec: np.ndarray | None = None) -> list[tuple[int, float]]:
        """Exact search. With `mask`, unpermitted chunks are excluded before ranking (pre-filtering),
        which is what a vector DB's filtered search does; an ANN index needs filter-aware traversal
        to give the same guarantee."""
        sims = self.matrix @ (self.embed_query(query) if qvec is None else qvec)
        if mask is not None:
            sims = np.where(mask, sims, -np.inf)
        idx = np.argsort(-sims, kind="stable")[:top_k]
        return [(int(i), float(sims[i])) for i in idx if np.isfinite(sims[i])]
