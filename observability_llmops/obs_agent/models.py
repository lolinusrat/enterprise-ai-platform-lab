"""Model backends: Groq (default), local Ollama, or a deterministic scripted model for tests and CI."""
import functools
import json
import os
import re

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

@functools.cache
def get_rate_limiter(backend: str):
    """Process-wide client-side throttle for Groq's on-demand tier (30 requests/min). Override with GROQ_RPM."""
    if backend != "groq":
        return None
    from langchain_core.rate_limiters import InMemoryRateLimiter
    rpm = float(os.environ.get("GROQ_RPM", "28"))
    return InMemoryRateLimiter(requests_per_second=rpm / 60, check_every_n_seconds=0.05, max_bucket_size=1)


DEFAULT_MODELS = {"groq": "openai/gpt-oss-20b", "ollama": "qwen2.5-coder:14b-instruct", "scripted": "scripted-v1"}
MODEL_CHOICES = {"groq": ["openai/gpt-oss-20b", "openai/gpt-oss-120b"], "ollama": [DEFAULT_MODELS["ollama"]],
                 "scripted": ["scripted-v1"]}


def get_model(backend: str = "groq", model: str | None = None) -> BaseChatModel:
    model = model or DEFAULT_MODELS[backend]
    if backend == "scripted":
        return ScriptedChatModel(model_name=model)
    from langchain_openai import ChatOpenAI
    if backend == "groq":
        return ChatOpenAI(model=model, base_url="https://api.groq.com/openai/v1",
                          api_key=os.environ["GROQ_API_KEY"], temperature=0, max_retries=6)
    if backend == "ollama":
        base = os.environ.get("OLLAMA_API_BASE", "http://localhost:11434")
        return ChatOpenAI(model=model, base_url=f"{base}/v1", api_key="ollama", temperature=0)
    raise ValueError(f"unknown backend {backend!r}")


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


class ScriptedChatModel(BaseChatModel):
    """Rule-based stand-in for an LLM. Emits tool calls and usage_metadata like a real provider,
    so the graph, the spans and the token accounting can be tested without network access."""

    model_name: str = "scripted-v1"

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[t.name for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, tools=None, **kwargs):
        system = next((m.content for m in messages if isinstance(m, SystemMessage)), "")
        if "Classify" in system:
            msg = AIMessage(content=self._classify(messages[-1].content))
        else:
            msg = self._agent_step(messages, system)
        prompt_text = "".join(str(m.content) for m in messages)
        out = _tokens(str(msg.content) + json.dumps(msg.tool_calls))
        msg.usage_metadata = {"input_tokens": _tokens(prompt_text), "output_tokens": out,
                              "total_tokens": _tokens(prompt_text) + out}
        msg.response_metadata = {"model_name": self.model_name,
                                 "finish_reason": "tool_calls" if msg.tool_calls else "stop"}
        return ChatResult(generations=[ChatGeneration(message=msg)])

    @staticmethod
    def _classify(text: str) -> str:
        t = text.lower()
        if re.search(r"refund|return|money back|get back", t):
            return "refund" if re.search(r"\b[a-z]\d{4}\b", t) or "bought" in t or "ordered" in t else "policy"
        if re.search(r"order|status|shipped|deliver|where is", t):
            return "order_status"
        if "policy" in t:
            return "policy"
        return "other"

    def _agent_step(self, messages, system):
        question = next(m.content for m in messages if m.type == "human")
        results = [m for m in messages if isinstance(m, ToolMessage)]
        lazy = "only call tools if absolutely necessary" in system
        if not results and not lazy:
            calls = [{"name": "lookup_order", "args": {"order_id": oid.upper()}}
                     for oid in re.findall(r"\b[A-Za-z]\d{4}\b", question)]
            calls += [{"name": "get_refund_policy", "args": {"category": c}}
                      for c in ("electronics", "apparel", "perishable") if c in question.lower()]
            if calls:
                return AIMessage(content="", tool_calls=[{**c, "id": f"call_{i}"} for i, c in enumerate(calls)])
        # second round: look up the policy for the category of any order fetched
        fetched = {m.name: m.content for m in results}
        if "lookup_order" in fetched and "get_refund_policy" not in fetched and "refund" in question.lower():
            category = json.loads(fetched["lookup_order"]).get("category")
            if category:
                return AIMessage(content="", tool_calls=[{"name": "get_refund_policy",
                                                          "args": {"category": category}, "id": "call_p"}])
        facts = " ".join(m.content for m in results) or "I don't have that information."
        return AIMessage(content=f"Here is what I found: {facts}")
