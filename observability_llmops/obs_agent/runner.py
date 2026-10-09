"""Run one question through the instrumented graph and return the answer with its trace and usage."""
from langchain_core.messages import HumanMessage

from .telemetry import OTelCallbackHandler, collector, setup_tracing
from .usage import summarize


def run_traced(graph, question: str, attributes: dict | None = None, timeout_s: float = 120) -> dict:
    setup_tracing()
    handler = OTelCallbackHandler(attributes=attributes)
    error = None
    state = {}
    try:
        state = graph.invoke({"messages": [HumanMessage(question)]},
                             config={"callbacks": [handler], "recursion_limit": 12})
    except Exception as e:  # recorded on the root span; surfaced to the caller as a failed run
        error = f"{type(e).__name__}: {e}"
    spans = collector.pop(handler.last_trace_id) if handler.last_trace_id else []
    messages = state.get("messages", [])
    return {
        "question": question,
        "answer": str(messages[-1].content) if messages else "",
        "intent": state.get("intent"),
        "error": error,
        "trace_id": handler.last_trace_id,
        "spans": spans,
        "usage": summarize(spans),
    }
