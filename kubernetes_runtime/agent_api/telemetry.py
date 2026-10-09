"""Telemetry: OpenTelemetry traces (OTLP to the collector), Prometheus metrics, JSON logs.

Every span carries the pod, node and namespace as resource attributes (from the downward API),
so a trace in Jaeger shows which replica served the request.
"""
import json
import logging
import os
import sys

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_ON, Decision, ParentBased, Sampler, SamplingResult
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Gauge, Histogram

from .config import settings

REQUESTS = Counter("agent_http_requests_total", "HTTP requests", ["route", "status"])
LATENCY = Histogram("agent_http_request_seconds", "HTTP request latency", ["route"],
                    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
INFLIGHT = Gauge("agent_inflight_requests", "Requests being processed")
TOOL_CALLS = Counter("agent_tool_calls_total", "Tool executions", ["tool"])
LLM_TOKENS = Counter("agent_llm_tokens_total", "Model tokens", ["backend", "direction"])
READY = Gauge("agent_ready", "1 when the readiness probe passes")


NOISE = {"/livez", "/readyz", "/startupz", "/metrics"}


class DropProbes(Sampler):
    """Kubelet probes and Prometheus scrapes hit every pod every few seconds; tracing them would
    bury real requests. FastAPI opens the server span (named by method, with `url.path` set)
    before routing, so the decision is made on the path attribute."""

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None, links=None, trace_state=None):
        if (attributes or {}).get("url.path") in NOISE:
            return SamplingResult(Decision.DROP)
        return ALWAYS_ON.should_sample(parent_context, trace_id, name, kind, attributes, links, trace_state)

    def get_description(self) -> str:
        return "DropProbes"


def setup_tracing() -> None:
    resource = Resource.create({
        "service.name": settings.service_name,
        "service.version": settings.version,
        "k8s.pod.name": settings.pod_name,
        "k8s.node.name": settings.node_name,
        "k8s.namespace.name": settings.namespace,
    })
    provider = TracerProvider(resource=resource, sampler=ParentBased(root=DropProbes()))
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def shutdown_tracing() -> None:
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        provider.shutdown()  # flush pending spans before the pod exits


tracer = trace.get_tracer("agent_api")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span().get_span_context()
        doc = {"level": record.levelname.lower(), "msg": record.getMessage(), "logger": record.name,
               "pod": settings.pod_name}
        if span.is_valid:
            doc["trace_id"] = format(span.trace_id, "032x")
        doc.update(getattr(record, "fields", {}))
        return json.dumps(doc)


def setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    log = logging.getLogger("agent_api")
    log.handlers[:] = [handler]
    log.setLevel(logging.INFO)
    log.propagate = False
    return log
