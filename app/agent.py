"""Thin facade: build the lead agent.

Models are created once at startup. The lead's middleware stack and the worker
subagents are wired in `app/agents/lead.py`.
"""

from __future__ import annotations

import logging
import os

from azure.identity.aio import DefaultAzureCredential, get_bearer_token_provider
from langchain_openai import ChatOpenAI

from app.agents.lead import build_lead
from app.data_loader import AppData

logger = logging.getLogger(__name__)


def _aoai_v1_endpoint() -> str:
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    if not endpoint.endswith("/openai/v1"):
        endpoint = f"{endpoint}/openai/v1"
    return endpoint


def build_models() -> tuple[ChatOpenAI, ChatOpenAI, DefaultAzureCredential]:
    """Create the main + nano models. Main drives the lead; nano drives every subagent
    and the refine/validate/summarise middleware utilities."""
    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
    base_url = _aoai_v1_endpoint()

    main = ChatOpenAI(
        model=os.getenv("AZURE_OPENAI_MAIN_DEPLOYMENT", "gpt-5.4-mini"),
        base_url=base_url,
        api_key=token_provider,
        streaming=True,
        use_responses_api=True,
    )
    nano = ChatOpenAI(
        model=os.getenv("AZURE_OPENAI_NANO_DEPLOYMENT", "gpt-5-nano"),
        base_url=base_url,
        api_key=token_provider,
        streaming=False,
        use_responses_api=True,
        # Tag every nano call so the streamer can drop it from the user-facing bubble.
        tags=["nano-utility"],
    )
    return main, nano, credential


def build_agent(main_model: ChatOpenAI, nano_model: ChatOpenAI):
    """Compile the lead (orchestrator) agent + initialise specialist subagents."""
    return build_lead(main_model, nano_model)


__all__ = ["build_agent", "build_models", "AppData"]
