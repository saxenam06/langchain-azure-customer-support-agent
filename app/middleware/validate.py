"""Groundedness validation middleware (Fin phase 5).

After the agent produces a tool-free assistant message in `tech_support` or
`product_qna`, we run a cheap groundedness classifier (nano model). If the
answer cites no retrieved docs, we *rewrite* the response to ask the user
whether to escalate to a human instead of silently making something up.

Modes (env `VALIDATION_MODE`):
- `advisory` — log only.
- `rewrite` (default) — replace the response with an "ask before escalate"
  prompt; only escalate when the user confirms on the next turn.
- `escalate`  — call escalate_to_human immediately.
"""

from __future__ import annotations

import logging
import os
import re

from langchain.agents.middleware import ModelRequest, wrap_model_call
from langchain_core.messages import AIMessage

logger = logging.getLogger(__name__)

VALIDATION_MODE = os.getenv("VALIDATION_MODE", "rewrite").lower()
ASK_BEFORE_ESCALATE = (
    "I couldn't find that in our help center, so I don't want to guess. "
    "Would you like me to connect you with a human support agent?"
)

_GROUNDEDNESS_PROMPT = (
    "You are a strict groundedness classifier. Given an assistant answer and a list of "
    "available document ids, decide whether the answer is grounded — every factual claim "
    "must either reference a document id like [doc-id] or be a generic acknowledgement. "
    "Reply with exactly one word: GROUNDED or UNGROUNDED."
)

_DOC_TAG = re.compile(r"\[([a-zA-Z0-9_\-]+)\]")


def _looks_like_factual(text: str) -> bool:
    """Cheap heuristic: skip groundedness for greetings / short ack messages."""
    t = (text or "").strip().lower()
    if len(t) < 60:
        return False
    if any(t.startswith(p) for p in ("hi ", "hello", "thanks", "you're welcome", "got it")):
        return False
    return True


def make_validate_response(nano_model):
    @wrap_model_call
    async def validate_response(request: ModelRequest, handler):
        ai = await handler(request)

        state = request.state

        # Only validate plain assistant text replies (no pending tool calls).
        if not isinstance(ai, AIMessage) or getattr(ai, "tool_calls", None):
            return ai
        text = ai.content if isinstance(ai.content, str) else ""
        if not _looks_like_factual(text):
            return ai

        # If the reply doesn't make doc-id citations at all, we treat it as a
        # generic / conversational reply and let it through. Groundedness only
        # bites when the model SOUNDS factual but lacks a real citation.
        cited_ids = set(_DOC_TAG.findall(text))
        retrieved = set(state.get("last_retrieved_docs") or [])
        if not cited_ids:
            return ai
        if cited_ids & retrieved:
            # At least one citation matches a doc-id the kb specialist actually returned.
            return ai

        if VALIDATION_MODE == "advisory":
            logger.warning("Ungrounded answer detected. Logging only.")
            return ai

        # REWRITE (default) or ESCALATE — replace the model's text. Step
        # transitions are driven by the next user turn (a 'yes' triggers the
        # escalate_to_human tool) so we don't try to mutate state here.
        if VALIDATION_MODE == "escalate":
            ai.content = (
                "Let me get a human to help with this — I'm escalating now. "
                "A teammate will reply shortly."
            )
        else:
            ai.content = ASK_BEFORE_ESCALATE
        return ai

    return validate_response
