"""The LangGraph workflow: classify -> (decline | agent <-> tools)."""
from typing import Literal

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from .models import get_model, get_rate_limiter
from .tools import TOOLS

INTENTS = ("order_status", "refund", "policy", "other")

CLASSIFY_PROMPT = ("Classify the customer message for an online store's support desk. "
                   "Reply with exactly one word: order_status, refund, policy or other. "
                   "Use 'other' for anything not about orders, refunds or store policy.")

# v1 is the shipped prompt. v2 is a "token-saving" edit that the eval suite should catch as a regression.
AGENT_PROMPTS = {
    "v1": ("You are a support agent for an online store. Always use the tools to look up orders and "
           "refund policies; never guess order details or policy terms. When computing a refund, "
           "look up the order, then the policy for its category, then use calculate. "
           "Answer in at most three sentences and include the concrete figures you found."),
    "v2": ("You are a support agent for an online store. Be concise to save cost. Answer from your own "
           "knowledge when you can; only call tools if absolutely necessary."),
}

DECLINE = "I can only help with orders, refunds and store policies."


class State(MessagesState):
    intent: str


def build_graph(backend: str = "groq", model: str | None = None, prompt_version: str = "v1"):
    llm = get_model(backend, model)
    agent_llm = llm.bind_tools(TOOLS)
    agent_prompt = AGENT_PROMPTS[prompt_version]
    # Throttle inside the node, before the model call, so the wait counts toward node latency
    # but not toward the model span's latency.
    limiter = get_rate_limiter(backend)

    def throttle():
        if limiter:
            limiter.acquire()

    def classify(state: State):
        question = next(m for m in reversed(state["messages"]) if isinstance(m, HumanMessage))
        throttle()
        reply = llm.invoke([SystemMessage(CLASSIFY_PROMPT), question])
        word = str(reply.content).strip().lower().split()[0].strip(".,'\"") if str(reply.content).strip() else "other"
        return {"intent": word if word in INTENTS else "other"}

    def route(state: State) -> Literal["agent", "decline"]:
        return "decline" if state["intent"] == "other" else "agent"

    def agent(state: State):
        throttle()
        return {"messages": [agent_llm.invoke([SystemMessage(agent_prompt), *state["messages"]])]}

    def decline(state: State):
        return {"messages": [AIMessage(DECLINE)]}

    g = StateGraph(State)
    g.add_node("classify", classify)
    g.add_node("agent", agent)
    g.add_node("tools", ToolNode(TOOLS))
    g.add_node("decline", decline)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route)
    g.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    g.add_edge("tools", "agent")
    g.add_edge("decline", END)
    return g.compile(name="support_agent")
