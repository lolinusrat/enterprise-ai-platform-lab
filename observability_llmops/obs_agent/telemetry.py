"""OpenTelemetry instrumentation for LangGraph via a LangChain callback handler.

Span hierarchy per request (attributes follow the OTel GenAI semantic conventions):

    invoke_agent support_agent          gen_ai.operation.name=invoke_agent
      node classify                     langgraph.node, langgraph.step
        chat openai/gpt-oss-20b         gen_ai.usage.input_tokens / output_tokens, cost
      node agent
        chat openai/gpt-oss-20b
      node tools
        execute_tool lookup_order       gen_ai.tool.name, gen_ai.tool.call.id

Message content is not recorded unless OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true.
"""
import json
import os
import threading
from pathlib import Path
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter, SpanExportResult
from opentelemetry.trace import SpanKind, Status, StatusCode

from .usage import cost_usd

CAPTURE_CONTENT = os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "").lower() == "true"
MAX_CONTENT = 2000


def span_to_dict(span) -> dict:
    ctx = span.get_span_context()
    return {
        "name": span.name,
        "trace_id": format(ctx.trace_id, "032x"),
        "span_id": format(ctx.span_id, "016x"),
        "parent_id": format(span.parent.span_id, "016x") if span.parent else None,
        "start_ns": span.start_time,
        "end_ns": span.end_time,
        "duration_ms": round((span.end_time - span.start_time) / 1e6, 2),
        "status": span.status.status_code.name,
        "attributes": dict(span.attributes or {}),
        "events": [{"name": e.name, "attributes": dict(e.attributes or {})} for e in span.events],
    }


class JsonlSpanExporter(SpanExporter):
    """Appends one JSON object per finished span to a local file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def export(self, spans):
        with self._lock, self.path.open("a") as f:
            for s in spans:
                f.write(json.dumps(span_to_dict(s), default=str) + "\n")
        return SpanExportResult.SUCCESS

    def shutdown(self):
        pass


class TraceCollector(SpanProcessor):
    """Keeps finished spans in memory grouped by trace id, so a caller can take exactly its own trace."""

    def __init__(self):
        self._traces: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def on_end(self, span):
        d = span_to_dict(span)
        with self._lock:
            self._traces.setdefault(d["trace_id"], []).append(d)

    def pop(self, trace_id: str) -> list[dict]:
        with self._lock:
            spans = self._traces.pop(trace_id, [])
        return sorted(spans, key=lambda s: s["start_ns"])


_provider: TracerProvider | None = None
_setup_lock = threading.Lock()
collector = TraceCollector()


def setup_tracing(service_name: str = "support-agent", jsonl_path: str | None = "traces/spans.jsonl") -> TracerProvider:
    """Configure the global tracer provider once: in-memory (for evals/UI), JSONL file,
    and OTLP/HTTP when OTEL_EXPORTER_OTLP_ENDPOINT is set (Jaeger, Phoenix, LangSmith, ...)."""
    global _provider
    with _setup_lock:
        if _provider is None:
            _provider = _build_provider(service_name, jsonl_path)
            trace.set_tracer_provider(_provider)
    return _provider


def _build_provider(service_name, jsonl_path) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(collector)
    if jsonl_path:
        provider.add_span_processor(SimpleSpanProcessor(JsonlSpanExporter(jsonl_path)))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


def _clip(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text[:MAX_CONTENT]


class OTelCallbackHandler(BaseCallbackHandler):
    """Turns LangChain/LangGraph callback events into OTel spans.

    Only the graph root, graph nodes, chat model calls and tool calls get spans. Internal runnables
    (routers, channel writes, sequences) are skipped; their children attach to the nearest traced ancestor."""

    def __init__(self, tracer: trace.Tracer | None = None, attributes: dict | None = None):
        self.tracer = tracer or trace.get_tracer("obs_agent.langgraph")
        self.root_attributes = attributes or {}
        self._spans: dict[UUID, trace.Span] = {}
        self._alias: dict[UUID, UUID | None] = {}  # untraced run -> nearest traced ancestor
        self._node: dict[UUID, str] = {}
        self._lock = threading.Lock()
        self.last_trace_id: str | None = None

    # -- helpers -------------------------------------------------------------------------------
    def _parent(self, parent_run_id):
        while parent_run_id is not None and parent_run_id not in self._spans:
            parent_run_id = self._alias.get(parent_run_id)
        return parent_run_id

    def _start(self, run_id, parent_run_id, name, kind, attrs):
        with self._lock:
            parent = self._parent(parent_run_id)
            # A graph run always starts its own trace, even inside an instrumented web request.
            ctx = trace.set_span_in_context(self._spans[parent]) if parent is not None else Context()
            span = self.tracer.start_span(name, context=ctx, kind=kind, attributes=attrs)
            self._spans[run_id] = span
            if parent is None:
                self.last_trace_id = format(span.get_span_context().trace_id, "032x")
            if parent is not None and parent in self._node:
                self._node[run_id] = self._node[parent]
            return span

    def _end(self, run_id, error: BaseException | None = None):
        with self._lock:
            span = self._spans.pop(run_id, None)
            self._alias.pop(run_id, None)
            self._node.pop(run_id, None)
        if span is None:
            return None
        if error is not None:
            span.record_exception(error)
            span.set_status(Status(StatusCode.ERROR, str(error)[:200]))
        span.end()
        return span

    # -- graph and nodes ---------------------------------------------------------------------
    def on_chain_start(self, serialized, inputs, *, run_id, parent_run_id=None, tags=None, metadata=None, **kw):
        metadata = metadata or {}
        name = kw.get("name") or (serialized or {}).get("name", "chain")
        node = metadata.get("langgraph_node")
        if parent_run_id is None:
            attrs = {"gen_ai.operation.name": "invoke_agent", "gen_ai.agent.name": name,
                     **{k: v for k, v in self.root_attributes.items() if v is not None}}
            if CAPTURE_CONTENT:
                attrs["gen_ai.input.messages"] = _clip(inputs)
            self._start(run_id, None, f"invoke_agent {name}", SpanKind.INTERNAL, attrs)
        elif node and name == node:
            self._start(run_id, parent_run_id, f"node {node}", SpanKind.INTERNAL,
                        {"langgraph.node": node, "langgraph.step": metadata.get("langgraph_step", -1)})
            self._node[run_id] = node
        else:
            self._alias[run_id] = parent_run_id

    def on_chain_end(self, outputs, *, run_id, **kw):
        span = self._spans.get(run_id)
        if span is not None and span.attributes.get("gen_ai.operation.name") == "invoke_agent":
            if isinstance(outputs, dict) and outputs.get("intent"):
                span.set_attribute("support.intent", outputs["intent"])
            if CAPTURE_CONTENT and isinstance(outputs, dict) and outputs.get("messages"):
                span.set_attribute("gen_ai.output.messages", _clip(outputs["messages"][-1].content))
        self._end(run_id)

    def on_chain_error(self, error, *, run_id, **kw):
        self._end(run_id, error)

    # -- model calls ---------------------------------------------------------------------------
    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, metadata=None, **kw):
        metadata = metadata or {}
        params = kw.get("invocation_params") or {}
        model = metadata.get("ls_model_name") or params.get("model") or params.get("model_name") or "unknown"
        attrs = {"gen_ai.operation.name": "chat",
                 "gen_ai.provider.name": metadata.get("ls_provider", "unknown"),
                 "gen_ai.request.model": model,
                 "langgraph.node": metadata.get("langgraph_node", "?")}
        if metadata.get("ls_temperature") is not None:
            attrs["gen_ai.request.temperature"] = metadata["ls_temperature"]
        if params.get("tools"):
            attrs["gen_ai.request.tool_count"] = len(params["tools"])
        if CAPTURE_CONTENT:
            attrs["gen_ai.input.messages"] = _clip([{"role": m.type, "content": m.content} for m in messages[0]])
        self._start(run_id, parent_run_id, f"chat {model}", SpanKind.CLIENT, attrs)

    def on_llm_end(self, response, *, run_id, **kw):
        span = self._spans.get(run_id)
        if span is not None:
            gen = response.generations[0][0]
            msg = getattr(gen, "message", None)
            usage = getattr(msg, "usage_metadata", None) or {}
            meta = getattr(msg, "response_metadata", None) or {}
            model = span.attributes.get("gen_ai.request.model", "unknown")
            i, o = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
            span.set_attributes({
                "gen_ai.response.model": meta.get("model_name", model),
                "gen_ai.response.finish_reasons": [meta.get("finish_reason") or "unknown"],
                "gen_ai.usage.input_tokens": i,
                "gen_ai.usage.output_tokens": o,
                "gen_ai.usage.reasoning_tokens": (usage.get("output_token_details") or {}).get("reasoning", 0) or 0,
                "gen_ai.usage.cache_read_tokens": (usage.get("input_token_details") or {}).get("cache_read", 0) or 0,
                "gen_ai.usage.cost_usd": round(cost_usd(model, i, o), 10),
            })
            if msg is not None and msg.tool_calls:
                span.set_attribute("gen_ai.response.tool_calls", [t["name"] for t in msg.tool_calls])
            if CAPTURE_CONTENT and msg is not None:
                span.set_attribute("gen_ai.output.messages", _clip(msg.content))
        self._end(run_id)

    def on_llm_error(self, error, *, run_id, **kw):
        self._end(run_id, error)

    # -- tools ---------------------------------------------------------------------------------
    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, inputs=None, **kw):
        name = kw.get("name") or (serialized or {}).get("name", "tool")
        attrs = {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": name, "gen_ai.tool.type": "function"}
        tool_call_id = (kw.get("metadata") or {}).get("tool_call_id") or kw.get("tool_call_id")
        if tool_call_id:
            attrs["gen_ai.tool.call.id"] = tool_call_id
        if CAPTURE_CONTENT:
            attrs["gen_ai.tool.call.arguments"] = _clip(inputs if inputs is not None else input_str)
        self._start(run_id, parent_run_id, f"execute_tool {name}", SpanKind.INTERNAL, attrs)

    def on_tool_end(self, output, *, run_id, **kw):
        span = self._spans.get(run_id)
        if span is not None:
            content = getattr(output, "content", output)
            if "not found" in str(content) or str(content).startswith("error"):
                span.set_attribute("gen_ai.tool.result.error", True)
            if CAPTURE_CONTENT:
                span.set_attribute("gen_ai.tool.call.result", _clip(content))
        self._end(run_id)

    def on_tool_error(self, error, *, run_id, **kw):
        self._end(run_id, error)
