"""Build the lead (orchestrator) agent.

The lead owns the user conversation. Its tools are the three dynamically-
built delegate wrappers (whose input schemas are derived from each
subagent's tools) plus the lead-level direct tools (customer lookup,
initiate_return, ticket creation, csat, escalate).

All destructive actions are at the lead level and gated by
HumanInTheLoopMiddleware so the customer confirms before they run.
"""

from __future__ import annotations

from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import (
    HumanInTheLoopMiddleware,
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
)
from langgraph.checkpoint.memory import InMemorySaver

from app.agents.subagents import build_subagents
from app.middleware.refine import make_refine_query
from app.middleware.validate import make_validate_response
from app.state import SupportState
from app.tools.delegates import build_delegates
from app.tools.orders import initiate_return
from app.tools.tickets import create_support_ticket, request_csat
from app.tools.workflow import escalate_to_human, lookup_customer_by_email

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text().strip()


def build_lead(main_model, nano_model):
    """Compile the lead agent + its specialist subagents + dynamic delegates."""
    orders_sub, catalog_sub, kb_sub = build_subagents(nano_model)
    ask_orders, ask_catalog, ask_kb = build_delegates(orders_sub, catalog_sub, kb_sub)

    refine_query = make_refine_query(nano_model)
    validate_response = make_validate_response(nano_model)
    summariser = SummarizationMiddleware(model=nano_model, max_tokens_before_summary=4000)

    return create_agent(
        model=main_model,
        tools=[
            ask_orders,
            ask_catalog,
            ask_kb,
            lookup_customer_by_email,
            # destructive actions — all gated by HumanInTheLoopMiddleware below
            initiate_return,
            create_support_ticket,
            request_csat,
            escalate_to_human,
        ],
        prompt=_load_prompt("lead"),
        state_schema=SupportState,
        middleware=[
            refine_query,
            validate_response,
            ToolCallLimitMiddleware(tool_name="ask_orders_specialist", run_limit=3),
            ToolCallLimitMiddleware(tool_name="ask_catalog_specialist", run_limit=3),
            ToolCallLimitMiddleware(tool_name="ask_kb_specialist", run_limit=3),
            HumanInTheLoopMiddleware(
                interrupt_on={
                    "initiate_return": True,
                    "create_support_ticket": True,
                    "escalate_to_human": True,
                    "request_csat": True,
                },
            ),
            summariser,
        ],
        checkpointer=InMemorySaver(),
        name="lead",
    )
