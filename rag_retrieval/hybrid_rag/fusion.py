"""Reciprocal rank fusion: combines rankings without having to calibrate BM25 and cosine scores."""

RRF_K = 60


def rrf(rankings: dict[str, list[int]], k: int = RRF_K, weights: dict[str, float] | None = None) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for name, ranking in rankings.items():
        w = (weights or {}).get(name, 1.0)
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + w / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
