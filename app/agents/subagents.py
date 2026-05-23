"""Worker subagents — one per domain. Each is read-only.

Subagents reason over their tools to satisfy a high-level goal from the lead.
Mutations (initiate_return, create_support_ticket, etc.) live at the lead
level so HumanInTheLoopMiddleware can gate them.

Prompts are intentionally minimal: identity + voice + the `MISSING:` failure
convention. Capabilities and arg requirements live in the tool schemas the
lead sees via the auto-derived delegate (see app/agents/auto_query.py).
"""

from __future__ import annotations

from pathlib import Path

from langchain.agents import create_agent

from app.tools.catalog import check_warranty, semantic_search_products
from app.tools.kb import search_help_center
from app.tools.orders import (
    check_return_eligibility,
    get_order_status,
    list_my_orders,
    lookup_order,
)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text().strip()


def build_subagents(nano_model):
    """Build the three READ-ONLY worker subagents."""
    orders_subagent = create_agent(
        model=nano_model,
        tools=[
            lookup_order,
            list_my_orders,
            get_order_status,
            check_return_eligibility,
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
