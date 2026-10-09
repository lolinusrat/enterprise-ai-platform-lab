"""Tools the support agent can call. Backed by fixed in-memory data so evals are reproducible."""
import ast
import json
import operator

from langchain_core.tools import tool

ORDERS = {
    "A1001": {"status": "shipped", "carrier": "UPS", "eta": "2026-10-12", "item": "wireless headphones",
              "category": "electronics", "price_usd": 120.00, "opened": True},
    "A1002": {"status": "delivered", "delivered_on": "2026-09-20", "item": "rain jacket",
              "category": "apparel", "price_usd": 80.00, "opened": False},
    "A1003": {"status": "processing", "item": "running shoes", "category": "apparel", "price_usd": 45.00},
    "A1004": {"status": "delivered", "delivered_on": "2026-10-01", "item": "fruit box",
              "category": "perishable", "price_usd": 30.00},
}

POLICIES = {
    "electronics": "Returns within 30 days of delivery. Opened items carry a 15% restocking fee.",
    "apparel": "Returns within 60 days of delivery for a full refund, tags attached.",
    "perishable": "Perishable goods are non-refundable.",
}


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by id (for example A1001). Returns status, item, category, price and shipping details."""
    order = ORDERS.get(order_id.strip().upper())
    if order is None:
        return json.dumps({"error": f"order {order_id} not found"})
    return json.dumps({"order_id": order_id.upper(), **order})


@tool
def get_refund_policy(category: str) -> str:
    """Get the refund and return policy for a product category: electronics, apparel or perishable."""
    policy = POLICIES.get(category.strip().lower())
    return policy or f"No policy on file for category '{category}'."


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.USub: operator.neg}


def _eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("unsupported expression")


@tool
def calculate(expression: str) -> str:
    """Evaluate an arithmetic expression such as '120 * 0.85'. Supports + - * / and parentheses."""
    try:
        return str(round(_eval(ast.parse(expression, mode="eval").body), 2))
    except (ValueError, SyntaxError, ZeroDivisionError) as e:
        return f"error: {e}"


TOOLS = [lookup_order, get_refund_policy, calculate]
