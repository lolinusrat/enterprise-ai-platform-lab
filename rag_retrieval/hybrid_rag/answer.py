"""Optional grounded answer from a local Ollama model. The model only ever sees chunks that already
passed the permission filter, so it cannot repeat what the reader isn't allowed to read."""
import os

import httpx

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "qwen2.5:7b")
SYSTEM = (
    "You answer questions from employees of Halcyon using only the numbered sources provided. "
    "Cite sources inline like [1]. If the sources do not contain the answer, say you could not find it "
    "in the documents you have access to. Do not guess. Keep the answer under 120 words."
)


def build_prompt(query: str, results: list[dict], max_sources: int = 5) -> str:
    sources = "\n\n".join(
        f"[{i}] {r['title']} ({r['classification']}, updated {r['updated']})\n{r['text']}"
        for i, r in enumerate(results[:max_sources], 1)
    )
    return f"Sources:\n\n{sources or '(no sources)'}\n\nQuestion: {query}"


def answer(query: str, results: list[dict], model: str = ANSWER_MODEL) -> dict:
    resp = httpx.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": model,
            "stream": False,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": build_prompt(query, results)}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    body = resp.json()
    return {
        "model": model,
        "answer": body["message"]["content"],
        "sources": [{"n": i, "doc_id": r["doc_id"], "title": r["title"]} for i, r in enumerate(results[:5], 1)],
        "eval_ms": round(body.get("total_duration", 0) / 1e6),
    }
