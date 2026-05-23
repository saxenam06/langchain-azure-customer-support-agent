"""Starlette ASGI entrypoint with lifespan-managed agent + data loading.

Routes:
    GET  /                — static UI
    GET  /api/health      — readiness probe
    GET  /api/customers   — mock customer list (UI dropdown)
    POST /api/chat        — NDJSON streaming chat
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Load .env before importing modules that read env at import time.
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app.agent import build_agent, build_models
from app.data_loader import load_all
from app.streaming import event, iter_message_events

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

ENVIRONMENT = os.getenv("ENVIRONMENT", "production")
STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: Starlette):
    logger.info("Starting up (env=%s)…", ENVIRONMENT)
    app_data = await load_all()
    main_model, nano_model, credential = build_models()
    agent = build_agent(main_model, nano_model)

    app.state.app_data = app_data
    app.state.agent = agent
    app.state.ready = True
    logger.info("✅ Lead agent ready (%d KB articles)", len(app_data.kb_articles))

    try:
        yield
    finally:
        try:
            await credential.close()
        except Exception:
            pass


# ---- Routes ----------------------------------------------------------------
async def index(request):
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


async def health(request):
    ready = getattr(request.app.state, "ready", False)
    return JSONResponse(
        {
            "status": "healthy" if ready else "starting",
            "ready": ready,
            "environment": ENVIRONMENT,
        },
        status_code=200 if ready else 503,
    )


async def customers(request):
    """Return a small list for the mock-customer dropdown in the UI."""
    data = getattr(request.app.state, "app_data", None)
    if data is None:
        return JSONResponse([], status_code=503)
    sample = [
        {"customer_id": c["customer_id"], "name": c.get("customer_name"), "email": c.get("email")}
        for c in data.customers[:5]
    ]
    return JSONResponse(sample)


async def chat(request):
    state = request.app.state
    if not getattr(state, "ready", False):
        return JSONResponse({"error": "Agent is not ready yet."}, status_code=503)

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON body."}, status_code=400)

    message = body.get("message")
    if not message:
        return JSONResponse({"error": "message is required"}, status_code=400)

    history = body.get("history") or []
    customer_id = body.get("customer_id")
    thread_id = body.get("thread_id") or str(uuid.uuid4())

    history_msgs = [{"role": m["role"], "content": m["content"]} for m in history if m.get("role")]
    history_msgs.append({"role": "user", "content": message})

    initial_state: dict = {"messages": history_msgs}
    if customer_id is not None:
        initial_state["customer_id"] = int(customer_id)

    config = {
        "configurable": {"thread_id": thread_id},
    }

    async def generate():
        # Always emit the thread id on the first event.
        yield event({"thread_id": thread_id})

        full_text: list[str] = []
        emitted_doc_ids: set[str] = set()
        # Per-message dedupe: LangGraph can emit both streaming token deltas
        # AND a final aggregated chunk with the full cumulative text for the
        # same message id. Track per-msg-id text we've already streamed.
        text_by_msg: dict[str, str] = {}

        try:
            async for chunk in state.agent.astream(initial_state, config, stream_mode="messages"):
                # stream_mode="messages" yields (AIMessageChunk, metadata) tuples.
                if isinstance(chunk, tuple) and len(chunk) >= 1:
                    msg = chunk[0]
                    metadata = chunk[1] if len(chunk) > 1 else {}
                else:
                    msg = chunk
                    metadata = {}

                # Drop chunks from internal nano-utility LLM calls (refine /
                # validate). They show up in the stream because LangGraph
                # streams every model call in the graph; we don't want them
                # in the user's chat bubble. (Tools/handoffs/citations from
                # the main model are still emitted below.)
                tags = metadata.get("tags", []) if isinstance(metadata, dict) else []
                if "nano-utility" in tags:
                    continue

                # Supervisor architecture has no step machine — we no longer
                # emit {"step": ...} events. The debug drawer can derive
                # "which specialist" from the {"tool": ...} events below
                # (delegate tool names: ask_orders_specialist, etc.).

                for ev in iter_message_events(msg):
                    if ev["kind"] == "text":
                        msg_id = getattr(msg, "id", None) or "_anon_"
                        chunk_text = ev["text"]
                        already = text_by_msg.get(msg_id, "")
                        # Case A: chunk is a *delta* — append it.
                        new_emit = chunk_text
                        # Case B: chunk is *cumulative* and starts with what
                        # we've already emitted — only emit the suffix.
                        if already and chunk_text.startswith(already):
                            new_emit = chunk_text[len(already):]
                        # Case C: chunk repeats text we've already emitted in
                        # full (final aggregated chunk) — skip.
                        elif already and chunk_text == already:
                            continue
                        # Case D: model produced an exact duplicate paragraph
                        # (very rare) — also skip.
                        elif already.endswith(chunk_text):
                            continue
                        if not new_emit:
                            continue
                        text_by_msg[msg_id] = already + new_emit
                        full_text.append(new_emit)
                        yield event({"chunk": new_emit})
                    elif ev["kind"] == "tool":
                        yield event({"tool": ev["tool"]})
                    elif ev["kind"] == "citations":
                        data = state.app_data
                        for doc_id in ev["doc_ids"]:
                            if doc_id in emitted_doc_ids:
                                continue
                            emitted_doc_ids.add(doc_id)
                            article = next(
                                (a for a in data.kb_articles
                                 if str(a.get("article_id") or a.get("id")) == doc_id),
                                None,
                            )
                            yield event({
                                "citation": {
                                    "doc_id": doc_id,
                                    "title": (article or {}).get("title", doc_id),
                                }
                            })
        except Exception as exc:
            logger.exception("Error during agent stream")
            yield event({"error": f"agent stream failed: {exc}"})

        yield event({
            "done": True,
            "message": "".join(full_text),
            "thread_id": thread_id,
        })

    return StreamingResponse(generate(), media_type="application/x-ndjson")


# ---- App -------------------------------------------------------------------
routes = [
    Route("/", index, methods=["GET"]),
    Route("/api/health", health, methods=["GET"]),
    Route("/api/customers", customers, methods=["GET"]),
    Route("/api/chat", chat, methods=["POST"]),
    Mount("/static", app=StaticFiles(directory=str(STATIC_DIR)), name="static"),
]

app = Starlette(debug=False, routes=routes, lifespan=lifespan)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
