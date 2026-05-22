"""Workflow tools surviving in the supervisor design: customer lookup + escalate.

The handoffs-era tools (`set_intent`, `back_to_triage`) are gone — the lead is
a single agent that delegates via specialist subagents instead of swapping
step prompts.
"""

from __future__ import annotations

from typing import Annotated

from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from app.data_loader import get_app_data
from app.state import SupportState


@tool
def lookup_customer_by_email(
    email: str,
    runtime: ToolRuntime[None, SupportState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Resolve a customer by email. Updates state with customer_id + customer_email."""
    data = get_app_data()
    if data is None:
        return Command(
            update={"messages": [ToolMessage("Customer database not loaded.", tool_call_id=tool_call_id)]}
        )
    customer = data.customers_by_email.get(email.strip().lower())
    if not customer:
        return Command(
            update={"messages": [ToolMessage(f"No customer found with email {email}.", tool_call_id=tool_call_id)]}
        )
    return Command(
        update={
            "customer_id": customer["customer_id"],
            "customer_email": customer["email"],
            "messages": [
                ToolMessage(
                    f"Found customer {customer.get('customer_name', '?')} (id={customer['customer_id']}).",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )


@tool
def escalate_to_human(
    reason: str,
    runtime: ToolRuntime[None, SupportState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Hand off to a human agent. The customer is asked to confirm before this fires
    (HumanInTheLoopMiddleware handles the confirmation)."""
    return Command(
        update={
            "messages": [
                ToolMessage(
                    f"Escalated to a human agent. Reason: {reason}. "
                    "A teammate will reply by email shortly.",
                    tool_call_id=tool_call_id,
                )
            ],
        }
    )
