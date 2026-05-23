"""Tool exports.

Delegate tools are built dynamically at startup (see `app/tools/delegates.py`),
so they are NOT module-level attributes. The lead's tool list is composed in
`app/agents/lead.py`.

The exports here are direct tools the lead uses (workflow + tickets + the
build-delegates factory).
"""

from .delegates import build_delegates
from .tickets import create_support_ticket, request_csat
from .workflow import escalate_to_human, lookup_customer_by_email

__all__ = [
    "build_delegates",
    "create_support_ticket",
    "request_csat",
    "escalate_to_human",
    "lookup_customer_by_email",
]
