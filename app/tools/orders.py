"""Order, return-eligibility, and initiate-return tools."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.data_loader import get_app_data
from app.state import SupportState

RETURN_WINDOW_DAYS = 30


def _data(runtime: ToolRuntime):
    return get_app_data()


@tool
def lookup_order(
    order_id: int,
    runtime: ToolRuntime[None, SupportState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Look up a Zava order by id. Returns header + line items + status."""
    data = _data(runtime)
    if data is None:
        return Command(
            update={"messages": [ToolMessage("Orders not loaded.", tool_call_id=tool_call_id)]}
        )
    order = data.orders_by_id.get(order_id)
    if not order:
        return Command(
            update={"messages": [ToolMessage(f"No order found with id {order_id}.", tool_call_id=tool_call_id)]}
        )
    # Enrich each line item with its 0-based item_index and a product name so
    # the model can pass the correct value to check_return_eligibility /
    # initiate_return AND tell the customer what they actually bought.
    enriched = dict(order)
    enriched_items = []
    for i, item in enumerate(order.get("items", [])):
        product = data.products_by_id.get(item.get("product_id")) or {}
        enriched_items.append({
            "item_index": i,
            "product_name": product.get("product_name", f"Product #{item.get('product_id')}"),
            "sku": product.get("sku"),
            **item,
        })
    enriched["items"] = enriched_items
    return Command(
        update={
            "order_id": order_id,
            "messages": [ToolMessage(json.dumps(enriched, default=str), tool_call_id=tool_call_id)],
        }
    )


@tool
def list_my_orders(
    customer_id: int,
    runtime: ToolRuntime[None, SupportState],
    tool_call_id: Annotated[str, InjectedToolCallId],
    limit: int = 5,
) -> Command:
    """List the most recent orders for the given customer_id.

    The caller (the orders subagent in the supervisor design) MUST pass
    customer_id explicitly — there is no shared parent state to read from.
    """
    data = _data(runtime)
    if data is None:
        return Command(
            update={"messages": [ToolMessage("Orders not loaded.", tool_call_id=tool_call_id)]}
        )
    if customer_id is None:
        return Command(
            update={"messages": [ToolMessage(
                "MISSING: customer_id",
                tool_call_id=tool_call_id,
            )]}
        )
    matching = [o for o in data.orders if o.get("customer_id") == customer_id]
    matching.sort(key=lambda o: o.get("order_date", ""), reverse=True)
    matching = matching[:limit]
    if not matching:
        return Command(
            update={"messages": [ToolMessage(
                f"No orders on file for customer {customer_id}.",
                tool_call_id=tool_call_id,
            )]}
        )
    summary_lines = []
    for o in matching:
        items = o.get("items", []) or []
        names = []
        for item in items:
            product = data.products_by_id.get(item.get("product_id")) or {}
            name = product.get("product_name") or f"Product #{item.get('product_id')}"
            qty = item.get("quantity", 1)
            names.append(f"{name} x{qty}" if qty != 1 else name)
        items_summary = ", ".join(names[:3]) + (f" (+{len(names)-3} more)" if len(names) > 3 else "")
        summary_lines.append(
            f"#{o['order_id']} — {o.get('order_date', '?')[:10]} — "
            f"${o.get('total_amount', 0):.2f} — {o.get('status', '?')} — {items_summary}"
        )
    return Command(
        update={
            "messages": [ToolMessage(
                "Recent orders:\n" + "\n".join(summary_lines),
                tool_call_id=tool_call_id,
            )]
        }
    )


@tool
def get_order_status(
    order_id: int,
    runtime: ToolRuntime[None, SupportState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> str:
    """Return the shipping status string for an order. Cheaper than lookup_order when only status is needed."""
    data = _data(runtime)
    if data is None:
        return "Orders not loaded."
    order = data.orders_by_id.get(order_id)
    if not order:
        return f"No order found with id {order_id}."
    return f"Order {order_id}: status={order.get('status', 'unknown')}, " \
           f"placed={order.get('order_date', '?')}, total=${order.get('total_amount', 0):.2f}"


@tool
def check_return_eligibility(
    order_id: int,
    item_index: int,
    runtime: ToolRuntime[None, SupportState],
) -> str:
    """Check whether a specific line item from an order is still eligible to return.

    `item_index` is the 0-based position of the line item in the order's `items` array
    (the same `item_index` returned by `lookup_order`). It is NOT the product_id.
    """
    data = _data(runtime)
    if data is None:
        return "Orders not loaded."
    order = data.orders_by_id.get(order_id)
    if not order:
        return f"No order found with id {order_id}."

    items = order.get("items", [])
    if not (0 <= item_index < len(items)):
        return f"item_index {item_index} is out of range for order {order_id} (has {len(items)} items)."

    try:
        order_dt = datetime.fromisoformat(order["order_date"].replace("Z", "+00:00"))
        if order_dt.tzinfo is None:
            order_dt = order_dt.replace(tzinfo=timezone.utc)
    except Exception:
        return "Could not parse order date."

    age_days = (datetime.now(timezone.utc) - order_dt).days
    if age_days > RETURN_WINDOW_DAYS:
        return f"Not eligible: ordered {age_days} days ago (window is {RETURN_WINDOW_DAYS} days)."
    return f"Eligible: {RETURN_WINDOW_DAYS - age_days} days remaining in the return window."


@tool
def initiate_return(
    order_id: int,
    item_index: int,
    reason: str,
    runtime: ToolRuntime[None, SupportState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Create a return (RMA) record. Mutates app data — only call after explicit customer confirmation.

    `item_index` is the 0-based position of the line item in the order's `items`
    array (same value as returned by `lookup_order`). NOT the product_id.
    """
    data = _data(runtime)
    if data is None:
        return Command(
            update={"messages": [ToolMessage("Orders not loaded.", tool_call_id=tool_call_id)]}
        )
    order = data.orders_by_id.get(order_id)
    if not order:
        return Command(
            update={"messages": [ToolMessage(f"No order found with id {order_id}.", tool_call_id=tool_call_id)]}
        )
    items = order.get("items", [])
    if not (0 <= item_index < len(items)):
        return Command(
            update={"messages": [ToolMessage(f"item_index {item_index} not in order.", tool_call_id=tool_call_id)]}
        )
    item = items[item_index]
    refund = round(item.get("unit_price", 0) * item.get("quantity", 1), 2)
    rma = {
        "return_id": f"RMA-{uuid.uuid4().hex[:8].upper()}",
        "order_id": order_id,
        "item_index": item_index,
        "reason": reason,
        "status": "requested",
        "refund_amount": refund,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    data.returns.append(rma)
    return Command(
        update={
            "messages": [
                ToolMessage(
                    f"Return created: {rma['return_id']} (estimated refund ${refund:.2f}). "
                    f"Customer should ship back within 14 days.",
                    tool_call_id=tool_call_id,
                )
            ]
        }
    )
