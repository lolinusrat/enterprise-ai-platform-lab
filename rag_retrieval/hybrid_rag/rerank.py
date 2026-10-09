"""Cross-encoder reranking: scores (query, chunk) pairs jointly instead of comparing two embeddings."""
import os

from fastembed.rerank.cross_encoder import TextCrossEncoder

RERANK_MODEL = os.environ.get("RERANK_MODEL", "Xenova/ms-marco-MiniLM-L-6-v2")


class Reranker:
    def __init__(self, model: str = RERANK_MODEL):
        self.name = model
        self.model = TextCrossEncoder(model)

    def score(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self.model.rerank(query, texts)] if texts else []
