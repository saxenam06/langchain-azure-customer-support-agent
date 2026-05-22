"""Build the lead (orchestrator) agent.

The lead is a single create_agent that holds the user-facing conversation.
Its tools are the three delegate wrappers (one per specialist domain) plus
a handful of direct tools (customer lookup, ticket creation, csat, escalate).

Middleware stack:
  refine_query                  — input cleanup (nano)
  validate_response             — post-call groundedness on every reply
  ToolCallLimitMiddleware x 3   — bound each delegate to <=3 calls per turn
  HumanInTheLoopMiddleware      — confirm before destructive actions
  SummarizationMiddleware       — condense long histories
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

from app.middleware.refine import make_refine_query
from app.middleware.validate import make_validate_response
from app.state import SupportState
from app.tools.delegates import (
    ask_catalog_specialist,
    ask_kb_specialist,
    ask_orders_specialist,
    init_delegates,
)
from app.tools.tickets import create_support_ticket, request_csat
from app.tools.workflow import escalate_to_human, lookup_customer_by_email

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.txt").read_text().strip()


def build_lead(main_model, nano_model):
    """Compile the lead agent + initialise the subagents it delegates to."""
    init_delegates(nano_model)

    refine_query = make_refine_query(nano_model)
    validate_response = make_validate_response(nano_model)
    summariser = SummarizationMiddleware(model=nano_model, max_tokens_before_summary=4000)

    return create_agent(
        model=main_model,
        tools=[
            ask_orders_specialist,
            ask_catalog_specialist,
            ask_kb_specialist,
            lookup_customer_by_email,
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
