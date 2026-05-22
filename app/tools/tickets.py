"""Ticket + CSAT tools.

`create_support_ticket` takes a STRUCTURED `ZavaSupportTicket` Pydantic model.
Pydantic validation runs before the tool body, so the lead cannot submit an
incomplete ticket; it gets a validation error as a tool message and has to
gather the missing data before retrying.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from langchain.tools import ToolRuntime, tool

from app.agents.ticket_schema import ZavaSupportTicket
from app.data_loader import get_app_data
from app.state import SupportState


@tool
def create_support_ticket(
    ticket: ZavaSupportTicket,
    runtime: ToolRuntime[None, SupportState],
) -> str:
    """Submit a structured support ticket. ALL fields in ZavaSupportTicket are
    required. Gather every field by delegating to specialists or asking the
    user BEFORE calling this tool — Pydantic will reject the call if any
    required field is missing or malformed and you will have to retry.

    The user will be asked to confirm before the ticket is actually filed
    (HumanInTheLoopMiddleware handles the confirmation).
    """
    data = get_app_data()
    if data is None:
        return "Tickets store unavailable."
    record = {
        "ticket_id": f"T-{uuid.uuid4().hex[:8].upper()}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **ticket.model_dump(),
    }
    data.tickets.append(record)
    return (
        f"Ticket {record['ticket_id']} filed "
        f"(category={ticket.category}, severity={ticket.severity}, "
        f"order_id={ticket.order_id}, product={ticket.product_name})."
    )


@tool
def request_csat(ticket_id: str, runtime: ToolRuntime[None, SupportState]) -> str:
    """Send a CSAT survey link for the given ticket (logged in this demo)."""
    data = get_app_data()
    if data is None:
        return "Tickets store unavailable."
    for t in data.tickets:
        if t["ticket_id"] == ticket_id:
            t["csat_requested"] = True
            return f"CSAT survey queued for {ticket_id}."
    return f"Ticket {ticket_id} not found."
