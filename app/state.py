"""SupportState — the lead agent's state schema.

The handoffs-era `current_step` / `intent` fields are gone. The lead is a
single agent with no step machine. We keep a small set of identifying-context
fields that tools can update via `Command(update={...})` and that the lead
can refer to across turns.
"""

from __future__ import annotations

from typing import NotRequired

from langchain.agents import AgentState


class SupportState(AgentState):
    """Conversation state for the lead support agent."""

    # Customer identity (set by lookup_customer_by_email)
    customer_id: NotRequired[int | None]
    customer_email: NotRequired[str | None]

    # Last order discussed — convenience for the lead's reasoning
    order_id: NotRequired[int | None]

    # KB doc-ids retrieved on the current turn (set by ask_kb_specialist),
    # consumed by the validate_response middleware for groundedness checks.
    last_retrieved_docs: NotRequired[list[str]]
