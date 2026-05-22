"""Worker subagents — one per domain. Each is built once at startup.

Pattern is the documented LangChain v1 subagent-as-tool pattern. Each subagent
runs in its own subgraph with its own messages list; only the final string
returned by the delegate wrapper bubbles up to the lead.

The lead must include any required ids (customer_id, order_id, sku, item_index)
in the question string — subagents do NOT inherit lead state, so tools like
list_my_orders take customer_id as an explicit argument.
"""

from __future__ import annotations

from pathlib import Path

from langchain.agents import create_agent

from app.tools.catalog import check_warranty, semantic_search_products
from app.tools.kb import search_help_center
from app.tools.orders import (
    check_return_eligibility,
    get_order_status,
    initiate_return,
    list_my_orders,
    lookup_order,
)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text().strip()


def build_subagents(nano_model):
    """Build the three worker subagents.

    nano_model is reused for all three — they handle one focused question each,
    so the cheap model is enough. Use a stronger model if eval shows misses.
    """
    orders_subagent = create_agent(
        model=nano_model,
        tools=[
            lookup_order,
            list_my_orders,
            get_order_status,
            check_return_eligibility,
            initiate_return,
        ],
        prompt=_load_prompt("orders_specialist"),
        name="orders_expert",
    )

    catalog_subagent = create_agent(
        model=nano_model,
        tools=[semantic_search_products, check_warranty],
        prompt=_load_prompt("catalog_specialist"),
        name="catalog_expert",
    )

    kb_subagent = create_agent(
        model=nano_model,
        tools=[search_help_center],
        prompt=_load_prompt("kb_specialist"),
        name="kb_expert",
    )

    return orders_subagent, catalog_subagent, kb_subagent
