"""Delegate tools — the lead's view of each subagent.

Each delegate wraps a subagent.invoke(...) call and returns ONLY the final
string. The subagent's intermediate tool calls and tool results never enter
the lead's message history; that's the context-isolation property.

The kb delegate returns a `Command` that also propagates extracted doc-ids
into the lead's state, so the lead can populate `referenced_doc_ids` in the
support ticket.
"""

from __future__ import annotations

import re
from typing import Annotated

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command

from app.agents.subagents import build_subagents

_orders = None
_catalog = None
_kb = None

_DOC_TAG = re.compile(r"\[([a-zA-Z0-9_\-]+)\]")


def init_delegates(nano_model) -> None:
    """Wire subagent instances into module globals so the @tool wrappers can call them.

    Called once from build_lead. Keeps the subagents importable without
    needing the LLM creds at import time.
    """
    global _orders, _catalog, _kb
    _orders, _catalog, _kb = build_subagents(nano_model)


def _final_text(response) -> str:
    msgs = response.get("messages", []) if isinstance(response, dict) else []
    if not msgs:
        return ""
    last = msgs[-1]
    content = getattr(last, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or block.get("delta") or ""
                if t:
                    parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


@tool
async def ask_orders_specialist(question: str) -> str:
    """Ask the ORDERS expert. Use for ALL order-related questions:
    order lookup, listing a customer's recent orders, status checks,
    return eligibility, initiating a return.

    Provide a SELF-CONTAINED question that includes every known id
    (customer_id, order_id, item_index). The specialist has no memory of
    earlier questions. Returns a concise factual answer, or "MISSING: <field>"
    if a required id is absent.
    """
    if _orders is None:
        return "ERROR: orders specialist not initialised."
    response = await _orders.ainvoke({"messages": [HumanMessage(content=question)]})
    return _final_text(response)


@tool
async def ask_catalog_specialist(question: str) -> str:
    """Ask the CATALOG expert. Use for product search and warranty terms.

    Provide a SELF-CONTAINED question. Include the SKU when known. Returns a
    concise factual answer, or "MISSING: sku" if a SKU is needed but missing.
    """
    if _catalog is None:
        return "ERROR: catalog specialist not initialised."
    response = await _catalog.ainvoke({"messages": [HumanMessage(content=question)]})
    return _final_text(response)


@tool
async def ask_kb_specialist(
    question: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Ask the KNOWLEDGE BASE expert. Use for help-center policies and procedures.

    Returns the relevant article excerpts with [doc_id] tags preserved
    (e.g. [kb-001], [kb-009]). The doc ids will be available for you to
    populate `referenced_doc_ids` when filing a ticket.
    """
    if _kb is None:
        return Command(
            update={"messages": [ToolMessage("ERROR: kb specialist not initialised.", tool_call_id=tool_call_id)]}
        )
    response = await _kb.ainvoke({"messages": [HumanMessage(content=question)]})
    text = _final_text(response)
    doc_ids = sorted(set(_DOC_TAG.findall(text)))
    return Command(
        update={
            "last_retrieved_docs": doc_ids,
            "messages": [ToolMessage(text, tool_call_id=tool_call_id)],
        }
    )
