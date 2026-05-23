"""Delegate tools — the lead's view of each specialist subagent.

Each delegate is built dynamically at startup. Its input schema (the Pydantic
Query class) and docstring are derived from the subagent's actual tools, so
adding a tool to a subagent automatically updates the lead's view — no prose
edits required.

Pattern:
  1. `build_delegates(orders_sub, catalog_sub, kb_sub)` is called from
     `app/agents/lead.py` once subagents are constructed.
  2. For each subagent we build a `Query` Pydantic model whose fields are
     `goal` + the union of every non-injected arg across the subagent's tools.
  3. We wrap that with `@tool(args_schema=Query)`; the @tool body unpacks the
     query, formats a natural-language message, and invokes the subagent.

The kb delegate additionally returns a `Command` that propagates extracted
doc-ids into the lead's state so the `validate_response` middleware can
ground-check the lead's reply.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from langchain.tools import InjectedToolCallId, tool
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command

from app.agents.auto_query import (
    build_delegate_docstring,
    build_query_model,
    format_query_message,
)

_DOC_TAG = re.compile(r"\[([a-zA-Z0-9_\-]+)\]")


def _final_text(response: Any) -> str:
    """Extract the final assistant text from a subagent invoke response."""
    msgs = response.get("messages", []) if isinstance(response, dict) else []
    if not msgs:
        return ""
    last = msgs[-1]
    content = getattr(last, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or block.get("delta") or ""
                if t:
                    parts.append(t)
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return ""


_ORDERS_PURPOSE = (
    "Delegate to the orders worker. It reasons over Zava order records (orders, "
    "items, statuses, return-eligibility windows) using its own toolset. The "
    "worker is READ-ONLY — destructive operations (initiate_return) are at the "
    "lead level, gated by user confirmation."
)

_CATALOG_PURPOSE = (
    "Delegate to the catalog worker. It searches the Zava product catalog and "
    "looks up warranty terms by SKU/category using its own toolset."
)

_KB_PURPOSE = (
    "Delegate to the knowledge-base worker. It searches Zava's help-center "
    "articles. Replies preserve [kb-XXX] citation tags which the lead can "
    "use to populate `referenced_doc_ids` on a ticket."
)


def build_delegates(orders_sub, catalog_sub, kb_sub):
    """Return [ask_orders_specialist, ask_catalog_specialist, ask_kb_specialist].

    Called once at startup after subagents are constructed. Each delegate's
    input schema and docstring are computed from its subagent's tools.
    """

    # --- Orders delegate ---
    OrdersQuery = build_query_model("Orders", orders_sub)

    @tool("ask_orders_specialist", args_schema=OrdersQuery)
    async def ask_orders_specialist(**query) -> str:  # noqa: D401
        """Auto-generated; replaced below."""
        msg = format_query_message(query)
        response = await orders_sub.ainvoke({"messages": [HumanMessage(content=msg)]})
        return _final_text(response)

    ask_orders_specialist.description = build_delegate_docstring(_ORDERS_PURPOSE, orders_sub)

    # --- Catalog delegate ---
    CatalogQuery = build_query_model("Catalog", catalog_sub)

    @tool("ask_catalog_specialist", args_schema=CatalogQuery)
    async def ask_catalog_specialist(**query) -> str:  # noqa: D401
        """Auto-generated; replaced below."""
        msg = format_query_message(query)
        response = await catalog_sub.ainvoke({"messages": [HumanMessage(content=msg)]})
        return _final_text(response)

    ask_catalog_specialist.description = build_delegate_docstring(_CATALOG_PURPOSE, catalog_sub)

    # --- KB delegate (returns Command to update lead state with retrieved doc ids) ---
    KbQuery = build_query_model("Kb", kb_sub)

    @tool("ask_kb_specialist", args_schema=KbQuery)
    async def ask_kb_specialist(
        tool_call_id: Annotated[str, InjectedToolCallId],
        **query,
    ) -> Command:  # noqa: D401
        """Auto-generated; replaced below."""
        msg = format_query_message(query)
        response = await kb_sub.ainvoke({"messages": [HumanMessage(content=msg)]})
        text = _final_text(response)
        doc_ids = sorted(set(_DOC_TAG.findall(text)))
        return Command(
            update={
                "last_retrieved_docs": doc_ids,
                "messages": [ToolMessage(text, tool_call_id=tool_call_id)],
            }
        )

    ask_kb_specialist.description = build_delegate_docstring(_KB_PURPOSE, kb_sub)

    return ask_orders_specialist, ask_catalog_specialist, ask_kb_specialist
