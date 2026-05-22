# Architecture walkthrough — agentic design

A walk-through of the repo focused on the agentic parts.

## 1. The big picture

There is **one LangChain v1 agent** (`create_agent(...)`) wired into a Starlette app. The "specialist agents" you see in the UI (triage, returns, tech_support, …) are not separate agents — they're **virtual personas** produced by swapping the system prompt and tool subset on each LLM call. State lives in a LangGraph checkpointer keyed by `thread_id`.

Three layers stack together:

```
HTTP (Starlette) → LangGraph agent loop → Azure OpenAI Responses API
                       ↑
                   4 middlewares + 14 tools + in-memory data
```

The whole thing is assembled once at startup in `app/main.py:42` (`lifespan`):

```
load_all()                # JSON files → AppData (with NumPy KB embeddings)
build_models()            # main = gpt-5.4-mini, nano = gpt-5-nano
build_agent(main, nano)   # create_agent + middleware list + InMemorySaver
```

## 2. The agent itself (`app/agent.py:76`)

```python
create_agent(
    model=main_model,
    tools=ALL_TOOLS,              # all 14 — always registered
    state_schema=SupportState,    # adds current_step, intent, customer_id, ...
    middleware=[refine_query, apply_step_config, validate_response, summariser],
    checkpointer=InMemorySaver(),
)
```

Two key choices:
- **Two model tiers** — `main_model` drives user-facing answers; `nano_model` runs the cheap reliability middlewares. Nano calls are tagged `"nano-utility"` (`app/agent.py:61`) so the UI streamer can drop them.
- **All 14 tools are registered up-front.** The "handoff" is achieved by *hiding* tools from the LLM, not by re-creating the agent.

## 3. State + the handoff trick

`SupportState` (`app/state.py:34`) extends `AgentState` with workflow fields. The only one that drives routing is `current_step`. It can be one of six values: `triage` | `order_lookup` | `returns` | `tech_support` | `product_qna` | `resolution`.

`apply_step_config` (`app/middleware/steps.py:88`) is the heart of the architecture. On every model call it:

1. Reads `state["current_step"]` (defaults to `"triage"`).
2. Strips any existing system messages and prepends the prompt from `app/prompts/<step>.txt`.
3. Filters `request.tools` to only the names allowed for that step (see `STEP_CONFIG` at `app/middleware/steps.py:31`).

So one turn might show the LLM the **triage** prompt + 4 tools; the next turn it sees the **returns** prompt + 5 different tools. From the LLM's perspective, it just became a different specialist.

**Who changes `current_step`?** The LLM never sets it directly — it calls a tool, and three tools return `Command(update={"current_step": ...})` (`app/tools/workflow.py:34`, `:80`, `:95`):

| Tool | Caller step | Resulting step |
|---|---|---|
| `set_intent("return_or_refund")` | triage | `returns` |
| `set_intent("order_status")` | triage | `order_lookup` |
| `back_to_triage()` | any specialist | `resolution` |
| `escalate_to_human(reason)` | any | `resolution` |

LangGraph applies that update, checkpoints it, and on the *next* turn `apply_step_config` reads the new value. That's the whole state machine.

## 4. The four middlewares (`@wrap_model_call`)

Each is an async onion layer that takes `(request, handler)` and calls `await handler(request)`. They run in the order registered in `build_agent`:

| # | Name | Phase | What it mutates |
|---|---|---|---|
| 1 | `refine_query` (`app/middleware/refine.py:30`) | pre-call | Rewrites the last `HumanMessage.content` in place if it's >40 chars and not a one-word confirm. Uses nano. |
| 2 | `apply_step_config` (`app/middleware/steps.py:88`) | pre-call | Swaps system prompt + filters `request.tools`. No LLM. |
| 3 | `validate_response` (`app/middleware/validate.py:54`) | **post-call** | Runs *after* the LLM. Only active in `tech_support` / `product_qna`. If the answer doesn't cite a doc id from `state["last_retrieved_docs"]`, rewrites it to the "ask before escalate" template. |
| 4 | `SummarizationMiddleware` | pre-call | Built-in. Condenses old messages when history > 4000 tokens. |

`validate_response` is the only post-call one — it sees the `AIMessage` returned by the inner handler and can overwrite `ai.content` before it goes back out (`app/middleware/validate.py:88`). Useful detail: in `rewrite` mode it doesn't escalate immediately, it just asks "want a human?" and lets the next user turn trigger the actual `escalate_to_human` tool.

## 5. Tools = state-aware functions returning `Command`

Tools come in two flavors:

**Plain return value** (`app/tools/orders.py:127` `get_order_status`, `app/tools/catalog.py:86` `check_warranty`, etc.) — just return a string. LangGraph turns it into a `ToolMessage`.

**`Command` return** (`app/tools/workflow.py`, `app/tools/orders.py:25` `lookup_order`, `app/tools/kb.py:18`) — explicitly return:

```python
return Command(update={
    "current_step": next_step,
    "customer_id": ...,
    "order_id": ...,
    "last_retrieved_docs": [...],
    "messages": [ToolMessage(..., tool_call_id=tool_call_id)],
})
```

This is how a tool both **emits its tool result** and **mutates conversation state** atomically. `search_help_center` (`app/tools/kb.py:52`) is the cleanest example — it stores the retrieved `doc_ids` into state, which `validate_response` then reads to decide if the next AI message is grounded.

Tool signatures use `ToolRuntime[None, SupportState]` and `InjectedToolCallId` (a typed annotation) — LangGraph injects those at execution time so the tool can both read state and acknowledge its own `tool_call_id`.

The `_INTENT_TO_STEP` map (`app/tools/workflow.py:15`) is the routing table from `set_intent` arguments to next step.

## 6. One full turn, end-to-end

User types "where is my drill?" with `customer_id=42`:

1. **`POST /api/chat`** (`app/main.py:92`) — builds `initial_state = {"messages": [...], "customer_id": 42}` and calls `agent.astream(initial_state, {"configurable": {"thread_id": ...}}, stream_mode="messages")`.
2. **Middleware chain (pre-call)**: `refine_query` rewrites it → `apply_step_config` injects `triage.txt` prompt + 4 triage tools → `summariser` is a no-op (history short).
3. **LLM (main model)** decides to call `set_intent("order_status")`.
4. **Tool node** executes `set_intent` → returns `Command(update={"current_step": "order_lookup", "intent": "order_status", "messages": [ToolMessage("Routed to order_lookup.")]})`.
5. **LangGraph applies the update, checkpoints state.**
6. **Loop continues:** middleware chain runs again, this time `apply_step_config` sees `current_step=order_lookup` and swaps to that prompt + tool set. LLM calls `list_my_orders()` → tool reads `state["customer_id"]` → returns recent orders.
7. **LLM produces final text** "Your most recent order #1234 shipped on…". `validate_response` skips it (not in a RAG step).
8. **Streaming** (`app/main.py:121`): the `generate()` coroutine reads `(AIMessageChunk, metadata)` tuples from `astream`, drops anything tagged `nano-utility`, dedupes against `text_by_msg`, and emits NDJSON lines (`{"chunk": "..."}`, `{"tool": {...}}`, `{"step": "order_lookup"}`, `{"citation": {...}}`).
9. **Browser** consumes the NDJSON, paints token-by-token, and updates the debug drawer.

## 7. Streaming & UI

`stream_mode="messages"` makes LangGraph yield `(message, metadata)` tuples for every node, including internal nano calls. `app/main.py:148` filters those by the `"nano-utility"` tag. `iter_message_events` (`app/streaming.py:40`) classifies each chunk into `text`/`tool`/`citations`. The dedupe logic at `app/main.py:172` exists because LangGraph emits both streaming deltas AND a final aggregated chunk per message id — the test for cumulative vs delta avoids painting the answer twice.

Citations are surfaced by regex-matching `[doc-id]` tags inside `ToolMessage` content (`app/streaming.py:53`); `validate_response` then cross-references those against `state["last_retrieved_docs"]`.

## 8. Data layer

`app/data_loader.py:27` defines `AppData` — JSON files (`customers`, `orders`, `products`, `warranty_terms`, `kb_articles`) loaded once into in-memory dicts with lookup indexes (`customers_by_email`, `orders_by_id`, etc.). KB articles get a pre-computed NumPy embedding matrix; `search_help_center` does plain cosine similarity (`app/tools/kb.py:37`). No vector DB, no Postgres.

Tools access this via a module-level singleton `get_app_data()` (`app/data_loader.py:158`) — pragmatic shortcut because no `context_schema` is configured on the agent.

## Mental model in one paragraph

`create_agent` builds the LangGraph runtime once with all 14 tools and a 4-layer middleware onion. Every model call passes through the onion: nano cleans the input, `apply_step_config` reads `state["current_step"]` and pretends the agent is a specialist by swapping prompt + visible tools, the main model decides, nano checks groundedness. The LLM never decides "which specialist am I" — it picks a tool, and three special tools return `Command(update={"current_step": ...})` to drive the state machine. LangGraph checkpoints state per `thread_id`, so the next user message picks up in the right specialist. Streaming pipes message chunks straight to the browser as NDJSON, with internal nano output filtered out by tag.

The two key files to read in order: `app/middleware/steps.py` (the handoff trick) and `app/tools/workflow.py` (the state mutators that drive it).
