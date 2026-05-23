# Orchestrator-worker refactor plan

A plan to transition this repo from its current **handoffs** architecture to an **orchestrator-worker (supervisor)** architecture, with an evaluation framework to compare the two. The end goal is a reusable framework that can be ported to a CodeBeamer-backed (or any other multi-source) customer-support problem.

> **Tooling note (updated).** The pattern in this plan — subagents wrapped as tools so the lead delegates with full context isolation — is the **officially documented LangChain v1 / LangGraph pattern**. See [docs.langchain.com/oss/python/langgraph/use-subgraphs](https://docs.langchain.com/oss/python/langgraph/use-subgraphs). Everywhere this plan previously implied "write a custom wrapper", we now use library primitives. Specifically:
>
> - `create_agent` (LangChain v1) for both the lead and each subagent.
> - The documented `ask_X_expert` subagent-as-tool pattern for delegation.
> - Built-in middleware: `ToolCallLimitMiddleware`, `HumanInTheLoopMiddleware`, `SummarizationMiddleware`.
> - Optional: the [`langgraph-supervisor`](https://github.com/langchain-ai/langgraph-supervisor-py) package's `create_supervisor` + `create_forward_message_tool` if we want even less hand-rolled code. See §10 for the trade-off.
> - Optional: [Deep Agents](https://docs.langchain.com/oss/python/langgraph/overview) if we end up needing planning + filesystem + context management as a packaged stack.

## 1. Why change

### What the current architecture does well

The current code (`app/middleware/steps.py` + `app/tools/workflow.py`) implements the **handoffs** pattern:

- One `create_agent` instance.
- `apply_step_config` middleware swaps system prompt + filters the visible tool list per turn based on `state["current_step"]`.
- Specialists (`triage`, `order_lookup`, `returns`, `tech_support`, `product_qna`, `resolution`) take turns owning the user-facing conversation.
- State transitions happen when a tool returns `Command(update={"current_step": ...})`.

This is cheap, fast, and natural for support flows where one user message maps cleanly to one specialist domain.

### Where handoffs breaks down

The flows we want to test next have a different shape:

1. **Multi-source synthesis.** One user goal requires facts from multiple backends (e.g., CodeBeamer + logs + CRM). Handoffs would mean transferring the user across specialists, which feels disjointed.
2. **Slot-filling for tickets.** Required ticket fields come from a mix of (a) the user, (b) lookup tools, (c) inference. A single agent must hold the slot list across all of them.
3. **Disambiguation loops.** An identifier (`EH_EC_PWR`, or an integer `7`, or "the cordless one") could mean several things. The agent has to *plan* a lookup, integrate the result, then ask the user.
4. **Context isolation.** Raw tool output (full JSON, parsing details, API noise) should not pollute the lead's reasoning context. Handoffs has all tool chatter visible in one shared message list.
5. **Single voice.** The user should feel they are talking to one agent, even when 3 backends are being queried.

These properties define the **orchestrator-worker (supervisor)** pattern.

### When to pick which

| Trigger | Right pattern |
|---|---|
| Visible UX phases, each a self-contained domain | **Handoffs** |
| One goal, cross-source synthesis, planning loop | **Supervisor** |
| Pure slot-fill against one source + the user | **Single agent** (no multi-agent needed) |

The Zava dataset in this repo can be re-purposed to exercise the second category, so we build supervisor on top of the existing tools and compare.

## 2. Target architecture

```
   Middleware stack on the lead (see §5):
     refine_query → validate_response → ToolCallLimit (x3)
     → HumanInTheLoop → SummarizationMiddleware → [LLM call]

                ┌──────────────────────────────────────────────────────────┐
                │           Lead agent (single voice)                      │
                │  - holds user conversation                               │
                │  - tracks ZavaSupportTicket slots (see §11)              │
                │  - decides: delegate vs ask user vs answer               │
                │                                                          │
                │  Tools visible to lead:                                  │
                │    ask_orders_specialist(question) -> str                │
                │    ask_catalog_specialist(question) -> str               │
                │    ask_kb_specialist(question) -> str                    │
                │    lookup_customer_by_email(email) -> str                │
                │    create_support_ticket(ticket: ZavaSupportTicket)      │
                │    request_csat(ticket_id) -> str                        │
                │    escalate_to_human(reason) -> str                      │
                └─────┬────────────┬─────────────────┬─────────────────────┘
                      │            │                 │
       (own subgraph) │            │                 │
                      ▼            ▼                 ▼
        ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
        │ Orders subagent  │  │ Catalog subagent │  │ KB subagent      │
        │ tools:           │  │ tools:           │  │ tools:           │
        │  lookup_order    │  │  semantic_search │  │  search_help_    │
        │  list_my_orders  │  │      _products   │  │      center      │
        │  get_order_status│  │  check_warranty  │  │                  │
        │  check_return_   │  │                  │  │                  │
        │      eligibility │  │                  │  │                  │
        │  initiate_return │  │                  │  │                  │
        └──────────────────┘  └──────────────────┘  └──────────────────┘
```

Key properties:

- **Lead is the only thing the user talks to.** All `chunk` events streamed to the browser come from the lead.
- **Each subagent runs in its own subgraph** with its own `messages` list, scoped to one question.
- **Only a string bubbles back** to the lead via the delegate tool's return value. Subagent tool calls and intermediate results are invisible to the lead.
- **`create_support_ticket` takes a structured Pydantic model.** Pydantic validation enforces field completeness at the framework level (see §11).
- **No `current_step`, no `set_intent`, no `back_to_triage`.** The state machine goes away.

## 3. File-by-file changes

### Files to delete

- `app/middleware/steps.py` — no more step-based prompt/tool filtering.
- `app/prompts/triage.txt`, `order_lookup.txt`, `returns.txt`, `tech_support.txt`, `product_qna.txt`, `resolution.txt` — replaced by one lead prompt + 3 subagent prompts.

### Files to modify

- `app/state.py` — drop `current_step`, `intent`. Keep `customer_id`, `customer_email`, `order_id`, `last_retrieved_docs`, `awaiting_escalation_confirmation`. Add `known_facts: NotRequired[dict[str, str]]` if we want explicit slot tracking.
- `app/tools/workflow.py` — drop `set_intent`, `back_to_triage`. Keep `lookup_customer_by_email`, `escalate_to_human`.
- `app/tools/tickets.py` — `create_support_ticket` now takes `ticket: ZavaSupportTicket` instead of `summary, category` (see §11).
- `app/middleware/validate.py` — drop the `step in {"tech_support", "product_qna"}` gate; the lead handles every reply now, so groundedness should apply globally.
- `app/tools/__init__.py` — re-export only the tools that survive (delegate wrappers + workflow + ticket tools).
- `app/middleware/__init__.py` — drop the `apply_step_config` export.
- `app/agent.py` → split into:
  - `app/agents/subagents.py` — three `create_agent` calls + their prompts.
  - `app/agents/lead.py` — the lead `create_agent` call + its prompt.
  - `app/agent.py` — thin facade that builds and returns the lead.
- `app/main.py` — replace the single `agent` with the lead; add `?mode=handoffs|supervisor` query parameter so the existing UI can hit either implementation during evaluation.

### Files to add

- `app/agents/subagents.py` — three `create_agent` instances (orders / catalog / kb), each ~10 lines using the documented pattern (§4).
- `app/agents/lead.py` — one `create_agent` for the lead with the middleware stack from §5.
- `app/agents/ticket_schema.py` — `ZavaSupportTicket` Pydantic model (§11). Later, `IncidentTicket` for the CodeBeamer port.
- `app/tools/delegates.py` — three `@tool` wrappers, each ~5 lines using the documented `ask_X_expert` shape (§4).
- `app/agents/prompts/lead.txt` — the orchestrator prompt including the schema block from §11.
- `app/agents/prompts/orders_specialist.txt`, `catalog_specialist.txt`, `kb_specialist.txt` — short worker prompts.
- `eval/` directory (see §6).

> **Net new code:** ~200 lines including prompts and the Pydantic schema. The pattern is library-supported; we are *configuring* it, not building it.

### Files to keep unchanged

- `app/tools/orders.py`, `catalog.py`, `kb.py` — tool implementations don't change.
- `app/data_loader.py` — data layer is fine.
- `app/streaming.py` — stream events are still emitted the same way; only the source agent differs.
- `data/*` — no schema changes required (overlap of integer IDs across `customer_id` / `order_id` / `product_id` already gives us enough disambiguation cases).

## 4. Sub-agent design

We use the **documented LangChain v1 subagent-as-tool pattern** verbatim. From the docs:

> *"Wrap sub-agent invocations as tools for an outer agent. Ensure the prompt directs the outer agent to delegate questions appropriately."*

Each subagent is a `create_agent` with its own narrow prompt and tool list. Each delegate is a thin `@tool` that calls `subagent.invoke({"messages": [{"role": "user", "content": question}]})` and returns `response["messages"][-1].content`. That's it — same pattern as the official `ask_fruit_expert` / `ask_veggie_expert` example.

### Example: orders subagent (matches the documented pattern)

```python
# app/agents/subagents.py
from langchain.agents import create_agent

ORDERS_SPECIALIST_PROMPT = """
You are the orders worker. Answer the lead's single question and stop.

Tools:
- lookup_order(order_id)
- list_my_orders()
- get_order_status(order_id)
- check_return_eligibility(order_id, item_index)
- initiate_return(order_id, item_index, reason)  — only if the question says to.

Rules:
1. Use the minimum tools needed.
2. Reply with ONE concise factual statement (a sentence, a list, or JSON). No greetings.
3. If the question is missing required IDs, reply with "MISSING:" and the field name.
"""

orders_subagent = create_agent(
    model=nano_model,
    tools=[lookup_order, list_my_orders, get_order_status,
           check_return_eligibility, initiate_return],
    prompt=ORDERS_SPECIALIST_PROMPT,
    name="orders_expert",        # picked up by LangSmith traces
    checkpointer=True,           # inherit the lead's checkpointer
)
```

Same shape for `catalog_subagent` (tools: `semantic_search_products`, `check_warranty`) and `kb_subagent` (tools: `search_help_center`).

> **Why no separate `state_schema`.** The LangChain v1 docs show subagents using the default `AgentState`; the message list is *already* isolated because each `subagent.invoke(...)` call starts a fresh state. We were over-engineering — no custom schema is needed unless a subagent has structured outputs to return.

### The delegate wrapper (documented pattern, verbatim)

```python
# app/tools/delegates.py
from langchain.tools import tool

@tool
async def ask_orders_specialist(question: str) -> str:
    """Ask the orders expert. Use for ALL order-related questions:
    order lookup, listing a customer's orders, status checks,
    return eligibility, initiating a return.
    Provide a self-contained question that includes any known order_id /
    customer_id. Returns a concise factual answer or "MISSING: <field>".
    """
    response = await orders_subagent.ainvoke(
        {"messages": [{"role": "user", "content": question}]},
    )
    return response["messages"][-1].content
```

The lead never sees `lookup_order`'s raw JSON. It sees only the worker's distilled reply.

> **Context isolation is automatic.** Because the subagent runs in its own subgraph with a fresh `messages` list, its internal tool calls / tool results die with the call. Only the string returned by the delegate enters the lead's message history. This is the property we wanted; it comes for free from the documented pattern.

## 5. Lead agent design

### Prompt

```
You are the lead support agent for Zava. The customer talks only to you.

You DO NOT have direct access to systems. To get a fact, delegate to a specialist:
  - ask_orders_specialist(question)    — orders, items, eligibility, returns
  - ask_catalog_specialist(question)   — products, SKUs, warranty terms
  - ask_kb_specialist(question)        — help-center articles, policies

You can also:
  - lookup_customer_by_email(email)    — resolve a customer
  - create_support_ticket(ticket=...)  — STRUCTURED Pydantic model, see §11 schema
  - request_csat(ticket_id)
  - escalate_to_human(reason)

Every turn, before replying or delegating, think:
  1. What is the customer's goal?
  2. What facts do I already have?
  3. What's missing?
       - If a system can know it → DELEGATE to the right specialist.
       - If only the user can know it → ASK the user.
  4. Only when I have enough → reply or act.

Rules:
- Never invent facts. If a specialist returns "MISSING: X", either set X yourself
  (if you know it) or ask the user.
- When delegating, write a self-contained question that includes every known ID.
- Do not show the customer raw tool output, IDs they didn't give you, or internal
  identifiers. Speak in their language.
- create_support_ticket, initiate_return, escalate_to_human, and request_csat are
  gated by HumanInTheLoopMiddleware — the framework will pause and ask the user
  to approve. You don't need to manually orchestrate a "shall I proceed?" turn;
  just call the tool when ready and the middleware does the confirmation.
```

The full schema block + slot-filling protocol that the lead prompt also includes lives in §11. Keep this top-level rules section concise; the schema-specific guidance belongs near the schema.

### Lead state

```python
class LeadState(AgentState):
    customer_id: NotRequired[int | None]
    customer_email: NotRequired[str | None]
    order_id: NotRequired[int | None]            # last referenced
    last_retrieved_docs: NotRequired[list[str]]  # for validate middleware
    known_facts: NotRequired[dict[str, str]]     # optional explicit slot tracker
    awaiting_escalation_confirmation: NotRequired[bool]
```

### Middleware on the lead — use the built-ins

```python
from langchain.agents import create_agent
from langchain.agents.middleware import (
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    HumanInTheLoopMiddleware,
)
from langgraph.checkpoint.memory import MemorySaver

lead = create_agent(
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
    prompt=LEAD_PROMPT,
    state_schema=LeadState,
    middleware=[
        # Custom (already in repo)
        refine_query,            # input cleanup
        validate_response,       # post-call groundedness — broaden scope from tech_support/product_qna to ALL replies

        # Built-in: bound delegation depth so a turn can't spiral
        ToolCallLimitMiddleware(tool_name="ask_orders_specialist",  run_limit=3),
        ToolCallLimitMiddleware(tool_name="ask_catalog_specialist", run_limit=3),
        ToolCallLimitMiddleware(tool_name="ask_kb_specialist",      run_limit=3),

        # Built-in: require explicit user confirmation before destructive actions
        HumanInTheLoopMiddleware(
            tool_names=["initiate_return", "create_support_ticket",
                        "escalate_to_human", "request_csat"],
        ),

        # Built-in: condense old turns past a threshold
        SummarizationMiddleware(model=nano_model, max_tokens_before_summary=4000),
    ],
    checkpointer=MemorySaver(),
    name="lead",
)
```

Notes on what changed vs the current code:

- **Drop `apply_step_config`** — no more steps, so no step-based prompt/tool filtering.
- **Broaden `validate_response`** — it currently runs only in `tech_support` / `product_qna`. In the supervisor design every reply passes through the lead, so the groundedness check should apply globally. Adjust the step check in `app/middleware/validate.py` to `if step is None or step in {...}` (drop the gate entirely once steps are gone).
- **`ToolCallLimitMiddleware` (built-in)** — resolves the "cost ceiling" open question from §9 of the previous draft. Cap each delegate at 3 calls per turn; the lead can't enter a feedback loop.
- **`HumanInTheLoopMiddleware` (built-in)** — resolves the "confirmation gating" open question. The current `returns` prompt has a "wait for yes before initiate_return" instruction we trust the LLM to obey; this middleware enforces it at the framework level for *any* listed tool.
- **`SummarizationMiddleware`** — unchanged.
- **`checkpointer=MemorySaver()` on the lead, `checkpointer=True` on subagents** — subagents inherit the lead's checkpointer automatically (per the docs); we don't pass `MemorySaver()` twice.

## 6. Evaluation framework

Add a first-class `eval/` directory.

```
eval/
  queries.jsonl          # one query per line
  user_simulator.py      # nano persona that answers follow-ups
  run_eval.py            # drives one query through one architecture, logs everything
  score.py               # applies the rubric to a transcript
  reports/               # per-run output (gitignored)
```

### `queries.jsonl` entry shape

```json
{
  "id": "S03",
  "query": "Open a ticket. Something's wrong with my last order.",
  "simulated_user": {
    "persona_facts": {
      "email": "jane.moore7739@example.com",
      "customer_id": 5,
      "latest_order_id": 50,
      "item_to_report": "cordless drill",
      "defect": "grinding noise",
      "severity": "moderate, not safety critical"
    }
  },
  "expected_tool_plan_summary": [
    "lookup_customer_by_email OR ask user for email",
    "ask_orders_specialist (most recent order + items)",
    "ask user to disambiguate item if multiple drills",
    "ask user for defect description and reproduction",
    "ask_kb_specialist (drill troubleshooting / expected behavior)",
    "ask_catalog_specialist (warranty for SKU)",
    "create_support_ticket(ticket=ZavaSupportTicket(...))"
  ],
  "expected_final_action": {
    "tool": "create_support_ticket",
    "ticket_must_have": {
      "order_id": 50,
      "customer_id": 5,
      "category": "warranty_claim",
      "product_name_contains": "drill",
      "observed_symptoms_contains": "grinding",
      "referenced_doc_ids_nonempty": true,
      "warranty_status_in": ["covered", "not_covered", "lifetime"]
    }
  }
}
```

The scorer checks each `ticket_must_have` rule against the actual `ZavaSupportTicket` the lead submitted. This is precise because the ticket is a typed object, not a free-text blob.

### Scoring rubric (each query scored 0–6)

| # | Criterion | What it measures |
|---|---|---|
| 1 | **Disambiguation** | Did the lead resolve ambiguous identifiers (numeric IDs, "the cordless one") correctly and not silently guess? |
| 2 | **Slot completeness** | Were all `ZavaSupportTicket` required fields filled before submission, with values that pass Pydantic validation? Schema enforcement makes this binary and easy to score. |
| 3 | **Right-source-for-fact** | Was each fact pulled from the specialist that owns it, not invented or asked of the user when a system knows it? E.g., `warranty_status` came from `ask_catalog_specialist`, not from the user. |
| 4 | **Right-channel-for-question** | Was each unknown delegated (machine-knowable) or asked (only the user knows) — not swapped? E.g., `actual_behavior` from the user, `referenced_doc_ids` from KB. |
| 5 | **Final action correctness** | Does the submitted ticket satisfy every rule in `expected_final_action.ticket_must_have`? |
| 6 | **Context discipline** (supervisor only) | Did the lead's message history stay free of raw subagent tool output? Measured by scanning the lead's transcript for tokens that should only exist inside a subagent (e.g., full JSON dumps from `lookup_order`). |

### `run_eval.py` loop

```python
for entry in queries:
    thread_id = uuid()
    transcript = []
    # Initial user message
    response, tool_calls = run_one_turn(agent, entry["query"], thread_id)
    transcript.append({"role": "user", "content": entry["query"]})
    transcript.append({"role": "assistant", "content": response, "tool_calls": tool_calls})

    # Loop: if the assistant ended with a question, feed the simulator's reply
    while looks_like_question_to_user(response):
        sim_reply = user_simulator.reply(entry["simulated_user"], transcript)
        response, tool_calls = run_one_turn(agent, sim_reply, thread_id)
        transcript.extend([
            {"role": "user", "content": sim_reply},
            {"role": "assistant", "content": response, "tool_calls": tool_calls},
        ])

    score = score_transcript(transcript, entry)
    write_report(entry["id"], transcript, score)
```

### Comparison output

Per run:
- Score table: query_id × rubric_criterion × mode (handoffs / supervisor).
- Aggregates: per-criterion mean, per-mode totals.
- Cost/latency: tokens consumed, wall-clock per query, total tool calls.

The expected outcome (worth verifying, not assuming):
- Supervisor wins on **#1 disambiguation**, **#3 right-source-for-fact**, **#4 right-channel-for-question**, **#6 context discipline**.
- Handoffs wins on **cost** and **time-to-first-token**.
- They tie on **#5 final action correctness** for simple queries; supervisor pulls ahead on multi-source queries.

## 7. Migration & rollout

### Phase 1: Build supervisor side-by-side (do not delete handoffs)

1. Create the new files under `app/agents/` and `app/tools/delegates.py`.
2. Wire `/api/chat?mode=supervisor` to the lead, `/api/chat` (no param) keeps current handoffs.
3. UI gets a toggle (single dropdown). No data changes required.

### Phase 2: Build the eval framework

1. Add `eval/` skeleton.
2. Author `eval/queries.jsonl` with 10 supervisor-style queries. These are a **new sibling set** to the existing `EVAL_QUERIES.md` — the latter targets handoffs-style routing decisions; the supervisor queries target multi-source slot-filling and disambiguation (see the S01–S10 examples discussed alongside this plan). The two sets are not interchangeable.
3. Build `user_simulator.py` (nano with a persona prompt + ground-truth facts).
4. Build `score.py` against the rubric above, including the structured `ticket_must_have` checks.

### Phase 3: Run both modes, compare, decide

1. Run all 10 queries × 2 modes × 3 seeds for stability.
2. Read the score deltas.
3. Read 3–4 transcripts per mode by hand to sanity-check the rubric.

### Phase 4: Port to the real backend

Once the supervisor pattern is validated on Zava:
1. Replace subagent tool implementations with the real backend (CodeBeamer API calls, etc.).
2. Replace `queries.jsonl` with real test-case scenarios.
3. Keep the rubric and `score.py` unchanged.

The framework is the deliverable. Zava is the dev-loop dataset.

## 8. Quick start (concrete first steps)

1. Branch: `git checkout -b supervisor-prototype` (already on `Orchestrator-worker-architecture`).
2. Add `app/agents/subagents.py` — three `create_agent` calls + their short prompts (~60 lines, using the documented pattern from §4).
3. Add `app/tools/delegates.py` — three `@tool` wrappers, each just `subagent.ainvoke({"messages":[{"role":"user","content":question}]}); return response["messages"][-1].content` (~30 lines).
4. Add `app/agents/lead.py` — the lead `create_agent` with the middleware stack from §5 (~60 lines).
5. Modify `app/main.py` to honour `?mode=handoffs|supervisor` and select the right agent at request time.
6. Smoke-test from the UI with one ambiguous query (`"having trouble with 7"`) and one slot-fill (`"open a ticket about my last order"`).
7. Move on to the eval framework.

Estimated effort to working prototype: **half a day**. Most of it is prompt iteration on the lead, not boilerplate — the boilerplate is library-supported.

### What you'll import (so the build is obviously library-driven)

```python
from langchain.agents import create_agent, tool
from langchain.agents.middleware import (
    SummarizationMiddleware,
    ToolCallLimitMiddleware,
    HumanInTheLoopMiddleware,
)
from langgraph.checkpoint.memory import MemorySaver
```

That's the full surface. Everything else in the supervisor implementation is data — prompts and a tool list.

## 9. Open questions — most are now resolved by built-ins

| Question | Resolution |
|---|---|
| Per-turn cost ceiling — should we cap subagent depth? | **Resolved.** `ToolCallLimitMiddleware(tool_name="ask_X_specialist", run_limit=3)`. One middleware per delegate. |
| Confirmation gating before destructive actions (returns, tickets, escalation)? | **Resolved.** `HumanInTheLoopMiddleware(tool_names=[...])`. The framework intercepts the tool call, surfaces a confirmation, and only proceeds on approval. |
| Subagent state access — should subagents read `state["customer_id"]`? | **Resolved by convention.** Lead includes the customer_id in the delegated question string. Subagents do **not** share parent state. This is what makes context isolation work. |
| Should the lead see streaming tokens from subagents? | **No.** The lead's value is hiding that chatter. For debugging, expose subagent runs via LangSmith tracing — they're already there with `name=...` on each `create_agent`. |
| Failure mode if a subagent returns "MISSING: X" or garbage? | **Defer.** Test in eval. Likely the lead's prompt + a brief retry instruction is enough; only escalate to framework-level retry if the rubric shows a problem. |
| Should subagents have their own checkpointer? | **No.** Pass `checkpointer=True` on each subagent so they inherit the lead's `MemorySaver`. Per-invocation persistence is sufficient. |

## 10. Decision: bare subagent-as-tool vs `langgraph-supervisor` package

The [`langgraph-supervisor`](https://github.com/langchain-ai/langgraph-supervisor-py) package offers `create_supervisor(...)` which auto-generates handoff tools, plus extras like `create_forward_message_tool` (forwards a worker's reply verbatim, no paraphrasing) and `output_mode="last_message"` (keeps the parent's history compact).

**Use it when:** you want minimum code and the default supervisor semantics fit (workers may end the conversation themselves; supervisor is mostly a router).

**Don't use it when:**
- You need a custom middleware stack on the supervisor itself (this repo already has `refine_query`, `validate_response`, and we want to add the built-ins above).
- The lead must always own the user-facing reply — `create_supervisor` is designed around a *swarm* feel where workers can speak directly, which is exactly what we are trying to suppress.
- You need to plug into a custom streaming pipeline like the NDJSON one in `app/main.py`; bare `create_agent` is more predictable here.

**Decision for this repo: bare subagent-as-tool.** Same primitive (`create_agent`), same delegation pattern (the documented one), more control over middleware ordering and streaming. We will still copy two ideas *from* `langgraph-supervisor` even though we don't import it:

1. **`output_mode="last_message"` semantics** — by returning only `response["messages"][-1].content` from the delegate, we already match it.
2. **`add_handoff_messages=False` semantics** — by *not* emitting `ToolMessage`-style "transferred to X" bookkeeping rows into the lead's history, we keep it concise.

If we later decide we want the package's ergonomics, swapping in `create_supervisor` is a 30-line change because the subagent definitions don't change.

### When Deep Agents becomes the right answer

[Deep Agents](https://docs.langchain.com/oss/python/langgraph/overview) is "an agent harness providing planning, subagents, filesystem tools, and context management on top of LangGraph". Reach for it if:

- The lead needs an explicit *plan* surface (todo list, scratchpad) rather than reasoning in messages.
- Subagents need to share files / artifacts with the lead.
- You want context-management middleware as a packaged stack rather than configured per agent.

For this iteration we don't need any of that; bare `create_agent` + delegate-tools + built-in middleware is enough. Revisit if the eval shows the lead struggles with multi-step plans.

## 11. Structured ticket schema — enforcing good-quality tickets

### The problem

A "good ticket" has required fields. The current `create_support_ticket(summary: str, category: str)` lets the model dump anything into `summary`. That means:

- Summaries may omit important facts (which order? which product? what's the expected behavior?).
- Required context isn't enforced at the framework level — the agent can submit incomplete tickets just because its prompt was vague.
- Downstream consumers (humans triaging tickets, automation) can't depend on the shape.

For the CodeBeamer use case, the target schema is fixed:

```
test_environment, software_version, error_codes, test_case,
requirements, additional_information, actual_behavior, target_behavior
```

The lead must gather every field, sourcing some from specialists (CodeBeamer for `test_case`, `requirements`), some from the user (`test_environment`, `actual_behavior`, observed `error_codes`). A "good ticket" = all eight fields filled with grounded values.

### The solution: schema lives in the tool signature

LangChain v1 reads tool argument type hints and generates the JSON Schema the LLM sees. By making `create_support_ticket` take a Pydantic model with required fields, **the LLM cannot call the tool until every required field is present** — Pydantic validation fires before the tool body runs, and the LLM gets the validation error as a tool message it has to respond to.

This pushes schema enforcement out of the prompt (soft, model can ignore) and into the framework (hard, model gets a structured error).

### Pattern for the Zava domain (testable in this repo)

A faithful mapping of the CodeBeamer schema onto Zava data:

| CodeBeamer field | Zava equivalent | Source |
|---|---|---|
| `test_environment` | Customer context — where/how the product is used | User |
| `software_version` | Product SKU + purchase date | Orders / catalog specialist |
| `error_codes` | Observed symptoms (free-text or enum) | User |
| `test_case` | Order id + item_index | Orders specialist |
| `requirements` | Expected behavior — warranty terms / KB article ids | Catalog + KB specialists |
| `additional_information` | Free-text user context | User |
| `actual_behavior` | What the customer is observing | User |
| `target_behavior` | What the spec / warranty says should happen | KB + warranty |

Zava schema:

```python
# app/agents/ticket_schema.py
from pydantic import BaseModel, Field
from typing import Literal

class ZavaSupportTicket(BaseModel):
    # CUSTOMER CONTEXT
    customer_id: int = Field(description="Resolved customer id (from email lookup or state).")
    customer_email: str

    # PRODUCT CONTEXT  (≈ test_environment + software_version)
    product_sku: str = Field(description="SKU of the affected product.")
    product_name: str
    order_id: int
    item_index: int = Field(description="0-based index of the affected line item in the order.")
    purchase_date: str = Field(description="ISO date string of the order_date.")

    # ISSUE  (≈ error_codes + test_case)
    symptom_summary: str = Field(description="One-line description of the customer's issue.")
    observed_symptoms: list[str] = Field(description="Specific symptoms in the customer's words.")

    # SPECIFICATION  (≈ requirements + target_behavior)
    expected_behavior: str = Field(description="What the product should do, sourced from KB or warranty.")
    referenced_doc_ids: list[str] = Field(description="KB article ids cited as the source of expected behavior.")
    warranty_status: Literal["covered", "not_covered", "lifetime", "unknown"]

    # USER REPORT  (≈ actual_behavior + additional_information)
    actual_behavior: str = Field(description="What the customer is observing, in their words.")
    reproduction_steps: str = Field(description="How to reproduce — empty string if not applicable.")
    additional_context: str = Field(description="Free text — empty string if none.")

    # CLASSIFICATION
    severity: Literal["low", "medium", "high", "safety_critical"]
    category: Literal["warranty_claim", "damaged_shipping", "wrong_item",
                      "general_defect", "billing", "other"]
```

CodeBeamer schema (for your real use case — same architecture, swap the class):

```python
class IncidentTicket(BaseModel):
    test_environment: str = Field(description="Test rig / bench / setup the user is running on.")
    software_version: str = Field(description="Build/version under test.")
    error_codes: list[str] = Field(description="Error codes the user observed during the run.")
    test_case: str = Field(description="CodeBeamer test case id (resolved by CB specialist).")
    requirements: list[str] = Field(description="CodeBeamer requirement ids the test case maps to.")
    actual_behavior: str = Field(description="What the user observed.")
    target_behavior: str = Field(description="What the spec / requirement says should happen.")
    additional_information: str = Field(description="Free-text context — empty string if none.")
```

### Updated tool

```python
# app/tools/tickets.py
from app.agents.ticket_schema import ZavaSupportTicket

@tool
def create_support_ticket(
    ticket: ZavaSupportTicket,                  # ← Pydantic model, required
    runtime: ToolRuntime[None, LeadState],
) -> str:
    """Submit a structured support ticket. ALL fields in ZavaSupportTicket are
    required. Gather them by delegating to specialists or asking the user
    before calling this tool. Pydantic will reject the call if any required
    field is missing or malformed; you'll get a tool-message error and must
    gather the missing data before retrying.
    """
    data = get_app_data()
    record = {
        "ticket_id": f"T-{uuid.uuid4().hex[:8].upper()}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        **ticket.model_dump(),
    }
    data.tickets.append(record)
    return f"Ticket {record['ticket_id']} submitted with {len(ticket.model_dump())} fields."
```

What this gives you for free:

- **No field omissions.** If the lead omits `product_sku`, the tool call fails with a `ValidationError` before any side-effect runs. The lead sees that error as a tool message and has to gather the missing field.
- **Type safety.** `severity` is a `Literal` — the LLM cannot pass "very urgent"; only the four enumerated values.
- **List structure.** `observed_symptoms` is `list[str]` — the LLM is forced to enumerate rather than dump a paragraph.
- **Provenance.** `referenced_doc_ids` is required, so the lead cannot claim an `expected_behavior` value without backing it with a KB doc id. This is groundedness enforcement at the schema level — complements the `validate_response` middleware.

### Lead prompt addition

Add this block to `app/agents/prompts/lead.txt`:

```
## Ticket schema

When the customer's situation warrants a ticket (warranty claim, damaged item,
defect report, complaint), you must call `create_support_ticket(ticket=...)`.
The ticket schema has these required fields:

  customer_id, customer_email          — from email lookup or state.customer_id
  product_sku, product_name            — from orders + catalog specialist
  order_id, item_index, purchase_date  — from orders specialist
  symptom_summary                      — you compose from the conversation
  observed_symptoms                    — ask the user, list of short strings
  expected_behavior                    — from kb specialist (with doc ids)
  referenced_doc_ids                   — kb article ids that back expected_behavior
  warranty_status                      — from catalog specialist (check_warranty)
  actual_behavior                      — ask the user
  reproduction_steps                   — ask the user (empty string if N/A)
  additional_context                   — ask the user (empty string if N/A)
  severity, category                   — you classify based on the gathered facts

## Slot-filling protocol

Before calling create_support_ticket:
  1. List which schema fields you do not yet have.
  2. For each missing field, decide:
       - SYSTEM-KNOWABLE  → delegate to the right specialist with a self-contained question.
       - USER-KNOWABLE    → ask the customer one short question at a time.
  3. Repeat until all required fields are gathered.
  4. Show the customer a one-paragraph summary of the ticket you're about to file.
  5. Call create_support_ticket with the full structured object.

Never call create_support_ticket with placeholder text like "unknown" or
"see conversation". If you cannot gather a field, ask the user. If the user
refuses, file an escalation via escalate_to_human instead of submitting an
incomplete ticket.
```

### Confirmation gate via `HumanInTheLoopMiddleware`

```python
HumanInTheLoopMiddleware(
    tool_names=["create_support_ticket", "initiate_return",
                "escalate_to_human", "request_csat"],
)
```

With this middleware active, when the lead calls `create_support_ticket(ticket=ZavaSupportTicket(...))`, the framework:

1. Pauses the run before executing the tool body.
2. Surfaces the structured ticket back to the streaming layer (visible to the user).
3. Resumes only when the user explicitly approves.

This is the natural place to render the ticket as a formatted card in the UI (read out the 8 fields, with edit affordances if you want).

### Walk-through: how the lead reasons over the schema

User: *"My drill stopped working. Order 50."*

```
Lead's plan:
  schema_status = {
    customer_id: ? need email or state
    product_sku: ? need order's drill item
    order_id: 50 ✓ (from user)
    item_index: ? need order details
    purchase_date: ? need order details
    symptom_summary: "drill stopped working" ✓ (compose later)
    observed_symptoms: ? need user detail
    expected_behavior: ? need kb specialist
    referenced_doc_ids: ? need kb specialist
    warranty_status: ? need catalog specialist
    actual_behavior: ? need user
    reproduction_steps: ? need user
    severity: ? classify after facts
    category: ? classify after facts
  }

Step 1 → ask user for email (USER-KNOWABLE: customer_id, customer_email)
Step 2 → lookup_customer_by_email (fills customer_id, customer_email)
Step 3 → ask_orders_specialist("Order 50: list items with item_index, product
         category, product_id, unit_price, order_date.")
         (fills product_name candidates, item_index candidates, purchase_date)
Step 4 → if multiple drills in order: ask user "which drill?" (resolves item_index, product_sku)
Step 5 → ask_kb_specialist("Drill stopped working — what does our help center
         say should happen and what troubleshooting applies? Return article ids.")
         (fills expected_behavior, referenced_doc_ids)
Step 6 → ask_catalog_specialist("Warranty terms for SKU <sku>?")
         (fills warranty_status)
Step 7 → ask user: "What exactly happens — does it not power on at all, or does
         it die mid-task? Anything specific you tried?"
         (fills observed_symptoms, actual_behavior, reproduction_steps)
Step 8 → ask user: "Anything else useful?" (fills additional_context)
Step 9 → classify severity + category from gathered facts
Step 10 → show user the structured ticket summary
Step 11 → call create_support_ticket(ticket=ZavaSupportTicket(...))
Step 12 → HumanInTheLoopMiddleware pauses, user confirms, ticket created.
```

That is the slot-fill loop you described for CodeBeamer, on Zava data, with each field traceable to its source (user vs specialist).

### Why this beats free-form `summary: str`

- **Auditable.** Every ticket has the same shape. You can grep, query, and dashboard.
- **Source-traceable.** `referenced_doc_ids` records the KB articles that justified `expected_behavior`. If a downstream reader doubts the ticket, the citations are right there.
- **Replayable in eval.** The scoring rubric in §6 can check, per field, whether the lead filled it from the right channel (system vs user) and whether the value is grounded.
- **Maps 1:1 to CodeBeamer.** When you port: swap `ZavaSupportTicket` for `IncidentTicket`, swap the specialist tools for CB API calls, keep the lead prompt structure. The slot-fill loop is identical.

### Cross-references (§3 already lists these files)

The files this section introduces are already in §3:

- `app/agents/ticket_schema.py` — listed under "Files to add".
- `app/tools/tickets.py` — listed under "Files to modify" (signature now takes `ticket: ZavaSupportTicket`).
- `app/agents/prompts/lead.txt` — listed under "Files to add"; the schema block and slot-fill protocol live here.
- Lead's middleware list — `create_support_ticket` is in `HumanInTheLoopMiddleware.tool_names` (see §5).

### How this interacts with the eval rubric

The scoring rubric in §6 doesn't gain a new criterion — the schema **sharpens existing ones**:

- **Criterion #2 (Slot completeness)** becomes binary and trivially scorable: did the lead submit a `ZavaSupportTicket` that passes Pydantic validation?
- **Criterion #3 (Right-source-for-fact)** becomes field-by-field auditable: e.g., did `referenced_doc_ids` actually come from `ask_kb_specialist` rather than being invented?
- **Criterion #5 (Final action correctness)** uses the `ticket_must_have` rule set (see §6 example) — checks specific schema fields rather than free-text inclusion.

The schema is what makes the rubric precise. Without it, criterion #2 is a fuzzy human judgement; with it, every field's presence and provenance is a programmatic check.

## 12. Worked examples — supervisor flows on Zava data

Six concrete queries showing how the lead orchestrates the subagents and user against the actual data files. Each example uses real customer emails (from `data/customers.json`), real order ids (1–200), real product SKUs (from `data/products.json`), real KB article ids (`kb-001` through `kb-012`), and real warranty categories. These can be lifted directly into `eval/queries.jsonl` as starting fixtures.

> **Today's date for all examples: 2026-05-21.** The order_date range in the dataset is 2024-11-26 through 2025-11-19, so every order is past the 30-day return window. The eligibility branches below reflect that.

### SW01 — Ambiguous identifier (the EH_EC_PWR analog)

**Query:** *"having trouble with 7. can you fix it?"*

**Initial slot tracker:**
```
ambiguous_token = "7"   # could be customer_id, order_id, or product_id
goal             = unknown   # need to disambiguate the token before any planning
```

**Tool sequence:**

1. `ask_orders_specialist("Is there an order with order_id=7? If yes, return status, customer_id, order_date, item count.")`
   → *"Order 7 exists. customer_id=22, status=SHIPPED, order_date=2025-04-11, 2 items."*
2. `ask_catalog_specialist("Is there a product whose internal product_id maps to 7 in the catalog? Return product_name, sku, category.")`
   → *"Product_id 7 maps to Cordless Drill 18V Li-Ion (sku=PTDR018000, category=POWER TOOLS)."*
3. Lead has two viable referents → asks user.

**User exchange:**
> *Lead:* "Just to make sure I help with the right thing — when you say '7', do you mean order #7 (shipped April 2025, 2 items) or product #7 (Cordless Drill 18V Li-Ion)? Or is 7 something else for you?"
> *User:* "Order 7."

**Continuation:** Lead proceeds with order context — asks the user what's wrong, then delegates as appropriate.

**Patterns exercised:** disambiguation; "ask the user only after probing all system-knowable interpretations".

---

### SW02 — Expected-vs-observed (battery dies fast)

**Query:** *"My Cordless Drill 18V Li-Ion battery dies in 30 minutes per charge. Order 142. Is that normal?"*

**Initial slot tracker (subset of `ZavaSupportTicket`):**
```
order_id           = 142
actual_behavior    = "battery dies in 30 minutes per charge"
product_sku        = ?    (need orders → catalog)
item_index         = ?    (need orders)
expected_behavior  = ?    (need kb)
referenced_doc_ids = ?    (need kb)
warranty_status    = ?    (need catalog)
customer_id        = ?    (need email)
```

**Tool sequence:**

1. `ask_orders_specialist("Order 142: list items with item_index, product_id, category, unit_price, order_date.")`
   → *"Order 142, ordered 2025-08-03, customer_id=18. Items: [0] Cordless Drill 18V Li-Ion (pid=9, POWER TOOLS, $189.99), [1] Drywall Screws 1-5/8 inch (pid=24, HARDWARE, $12.49)."*
2. `ask_catalog_specialist("Warranty terms for sku=PTDR018000 (Cordless Drill 18V Li-Ion).")`
   → *"POWER TOOLS, 24 months. Covers motor/switch/gearbox failure. Excludes consumables, batteries (separate 24-month warranty), drop damage."*
3. `ask_kb_specialist("What does our help center say about expected battery life and care for cordless tool batteries? Return article ids.")`
   → *"[kb-006] Lithium-ion batteries last longest at 30-50% charge in cool dry place. Never leave on charger long-term. Replace if no longer holds charge. Batteries warranted 2 years from purchase."*

**User exchange:**
> *Lead:* "Quick checks so I can file this accurately — what's your email on file, how old is the drill, what kind of work are you doing when the battery dies, and do you typically leave the battery on the charger between uses?"
> *User:* "ezra.kim2231@example.com. Bought it last summer. Drilling 2x4 framing. Yes I leave it on the charger."

4. `lookup_customer_by_email("ezra.kim2231@example.com")` → customer_id=18, customer_email confirmed.

**Final structured ticket:**
```python
ZavaSupportTicket(
    customer_id=18,
    customer_email="ezra.kim2231@example.com",
    product_sku="PTDR018000",
    product_name="Cordless Drill 18V Li-Ion",
    order_id=142,
    item_index=0,
    purchase_date="2025-08-03",
    symptom_summary="Cordless Drill 18V Li-Ion battery dies after ~30 min of framing work.",
    observed_symptoms=["battery dies after ~30 min", "left on charger between uses"],
    expected_behavior="Battery should retain useful charge for typical work loads; storage on charger long-term is known to degrade lithium-ion cells (kb-006).",
    referenced_doc_ids=["kb-006"],
    warranty_status="covered",   # separate 24-month battery warranty applies
    actual_behavior="Battery dies in 30 min during framing.",
    reproduction_steps="Charge fully, use for ~30 min drilling into 2x4 lumber, battery dies.",
    additional_context="User leaves battery on charger long-term, which kb-006 cites as a cause of degraded capacity.",
    severity="medium",
    category="warranty_claim",
)
```

**Patterns exercised:** expected-vs-observed reconciliation; warranty source vs KB source merged; the `additional_context` field captures the actionable insight from `kb-006`.

---

### SW03 — Damaged-in-shipping with missed window

**Query:** *"order 198 arrived but the saw inside has a cracked blade guard. I want a replacement."*

**Tool sequence:**

1. `ask_orders_specialist("Order 198: status, order_date, items, find any saw line item with item_index.")`
   → *"Order 198 status=DELIVERED, ordered 2025-11-19, customer_id=7. Items include Circular Saw 7-1/4 inch at item_index=0 (sku=PTSC000714, POWER TOOLS, $129.99)."*
2. `ask_kb_specialist("Damaged-on-arrival policy. What's the reporting window and what does the user need to do?")`
   → *"[kb-009] Items arriving damaged must be reported within 7 days. Photos of box and product required. Replacement shipped immediately, pickup arranged — no return label needed at user's end."*
3. **Lead computes:** today (2026-05-21) − order_date (2025-11-19) ≈ 183 days. Far outside the 7-day window in kb-009.

**User exchange:**
> *Lead:* "Quick check — when did you first notice the damage, and did you take any photos of the package and the saw at that time?"
> *User:* "When it arrived in November. No photos. Just never got around to reporting it."
> *Lead:* "Got it. The standard damaged-in-shipping process needs to happen inside 7 days with photos, so I can't kick that off automatically. I can file a ticket explaining the situation and request goodwill consideration. Want me to do that?"
> *User:* "yes"

4. `ask_catalog_specialist("Warranty terms for sku=PTSC000714 — does manufacturing-defect coverage on POWER TOOLS apply to a cracked blade guard?")`
   → *"POWER TOOLS, 24 months, covers motor/switch/gearbox failure. A cracked guard from shipping is not a manufacturing defect — kb-009 was the right path."*

**Final structured ticket:**
```python
ZavaSupportTicket(
    customer_id=7,
    customer_email="jennifer.gonzalez6055@example.com",
    product_sku="PTSC000714",
    product_name="Circular Saw 7-1/4 inch",
    order_id=198,
    item_index=0,
    purchase_date="2025-11-19",
    symptom_summary="Circular saw arrived with cracked blade guard, customer missed the 7-day damaged-shipping reporting window.",
    observed_symptoms=["cracked blade guard on arrival", "no photos taken at the time"],
    expected_behavior="kb-009 sets a 7-day reporting window with photo evidence for damaged-on-arrival items; standard path is replacement shipped + pickup arranged.",
    referenced_doc_ids=["kb-009"],
    warranty_status="not_covered",   # shipping damage out of warranty AND past the 7-day reporting window
    actual_behavior="Saw was damaged on arrival 6 months ago; customer is reporting now, no photographic evidence.",
    reproduction_steps="",
    additional_context="Customer requesting goodwill consideration. Standard damaged-shipping process is unavailable because kb-009's 7-day window has lapsed.",
    severity="medium",
    category="damaged_shipping",
)
```

**Patterns exercised:** policy-window arithmetic; refusing to silently apply the standard process when the precondition isn't met; honest categorisation (`warranty_status="not_covered"`, `category="damaged_shipping"` with `additional_context` explaining the missed window).

---

### SW04 — Multi-drill disambiguation inside an order

**Query:** *"I want to return whatever drill I bought from my most recent order. I'm thomas.wilson3364@example.com."*

**Tool sequence:**

1. `lookup_customer_by_email("thomas.wilson3364@example.com")` → customer_id=9.
2. `ask_orders_specialist("List recent orders for customer_id=9. Return order_id, order_date, status, total_amount.")`
   → *"Most recent: order 87, 2025-09-04, status=DELIVERED, total=$412.30. Older: order 31, 2024-12-08, DELIVERED."*
3. `ask_orders_specialist("Order 87: list items with item_index, product_id, product category, unit_price.")`
   → *"Items: [0] Cordless Drill 18V Li-Ion (pid=9, POWER TOOLS, $189.99), [1] Hammer Drill 1/2-inch (pid=10, POWER TOOLS, $159.99), [2] Common Nail Assortment (pid=27, HARDWARE, $18.49)."*

**User exchange:**
> *Lead:* "Order #87 from September has two drills — a Cordless Drill 18V Li-Ion and a Hammer Drill 1/2-inch. Which one do you want to return, and what's the issue?"
> *User:* "The hammer drill. It vibrates way too much, can barely hold it."

4. `ask_orders_specialist("Check return eligibility for order_id=87, item_index=1.")`
   → *"Not eligible — order was placed 259 days ago. Return window is 30 days."*
5. `ask_catalog_specialist("Warranty terms for sku=PTHD000050 (Hammer Drill 1/2-inch).")`
   → *"POWER TOOLS, 24 months from purchase. Covers motor/switch/gearbox failure. Excludes drop damage and consumables."*
6. `ask_kb_specialist("Hammer drill excessive vibration — troubleshooting?")`
   → *"No exact match. Closest article [kb-003] covers drill not turning on, not vibration. No grounded article exists for this symptom."*

**Lead reasoning:** return window is closed, but the drill is inside the 24-month power-tool warranty. Vibration could be a gearbox/motor symptom (covered) — but the lead has no KB grounding for that diagnosis specifically. Honest move is to file a warranty-investigation ticket rather than promise a refund.

**User exchange (continued):**
> *Lead:* "Two things: (a) the 30-day return window closed in October so I can't initiate a refund, but (b) the drill is still under the 24-month power-tool warranty, which covers motor and gearbox failures. Excessive vibration could be either of those. Want me to open a warranty-investigation ticket so an engineer can look at it?"
> *User:* "yes"

**Final structured ticket:**
```python
ZavaSupportTicket(
    customer_id=9,
    customer_email="thomas.wilson3364@example.com",
    product_sku="PTHD000050",
    product_name="Hammer Drill 1/2-inch",
    order_id=87,
    item_index=1,
    purchase_date="2025-09-04",
    symptom_summary="Hammer Drill 1/2-inch vibrates excessively, hard for user to hold during operation.",
    observed_symptoms=["excessive vibration", "hard to control during use"],
    expected_behavior="Drill should run smoothly; motor/switch/gearbox issues are covered under the 24-month POWER TOOLS warranty.",
    referenced_doc_ids=[],   # no KB article matches "excessive vibration" — lead honestly reports empty
    warranty_status="covered",
    actual_behavior="User reports excessive vibration; cannot continue using the tool.",
    reproduction_steps="Power on, attempt normal drilling — vibration starts immediately.",
    additional_context="Return window (30 days) is closed (ordered 259 days ago). Investigating under 24-month power-tool warranty instead.",
    severity="medium",
    category="warranty_claim",
)
```

**Patterns exercised:** multi-item disambiguation forces a user turn; honest "no KB article matched" (`referenced_doc_ids=[]`) instead of inventing a citation; correct routing from return-failure to warranty-claim path.

---

### SW05 — Wrong-premise correction

**Query:** *"order 50 hasn't arrived in 3 weeks. did you forget about me?"*

**Tool sequence:**

1. `ask_orders_specialist("Order 50: status, order_date, items.")`
   → *"Order 50 status=DELIVERED, ordered 2025-06-22, customer_id=14. Items: [0] Cordless Drill 18V Li-Ion, [1] Phillips Screwdriver Set."*

**Lead reasoning:** the order is marked DELIVERED. The customer's premise (hasn't arrived) is wrong, OR the package was lost / stolen / mis-delivered after carrier handoff. Don't accept the premise, don't defend it — clarify.

**User exchange:**
> *Lead:* "I checked — order #50 is marked DELIVERED in our system, with the order placed in June. A couple of possibilities: the carrier marked it delivered but it never arrived, it was left somewhere you haven't found yet, or it was lost/stolen after delivery. Which sounds closer to what you're seeing?"
> *User:* "Never arrived. Marked delivered on June 25th but nothing showed up."

2. `ask_kb_specialist("Package marked delivered but not received — what's the process?")`
   → *"No exact KB match. [kb-002] covers tracking activation, [kb-011] covers status meanings, [kb-009] covers damaged-on-arrival — none cover 'delivered but missing'. This is an escalation case."*

**User exchange (continued):**
> *Lead:* "There's no self-serve path for a missing-but-marked-delivered package — I'll connect you with our claims team who can investigate the carrier record. Want me to escalate now?"
> *User:* "yes please"

**Final action:** `escalate_to_human(reason="Order #50 marked DELIVERED 2025-06-25, customer reports never received. No applicable KB article for missing-delivery investigation; needs carrier-trace and possible refund/reship from claims team.")`

**Patterns exercised:** wrong-premise correction (don't accept "you forgot"); honest KB gap (`no exact match`); escalation when the right answer is clearly outside the agent's tools.

---

### SW06 — Compound intent in one message

**Query:** *"I'm robert.miller3953@example.com. Two things: I want to return order 100 entirely AND I'd like to know what hand tools you sell with a lifetime warranty."*

**Tool sequence:**

1. `lookup_customer_by_email("robert.miller3953@example.com")` → customer_id=10.
2. `ask_orders_specialist("Order 100: items, order_date, status.")`
   → *"Order 100 status=DELIVERED, ordered 2025-01-14, customer_id=10. Items: [0] Sledge Hammer 3lb (HAND TOOLS), [1] Exterior Acrylic Paint (PAINT & FINISHES)."*
3. `ask_orders_specialist("Check return eligibility for order_id=100, item_index=0.")`
   → *"Not eligible — 492 days ago, window is 30 days."*
4. `ask_orders_specialist("Check return eligibility for order_id=100, item_index=1.")`
   → *"Not eligible — 492 days ago."*

   *(Lead acknowledges the return path is closed on both items.)*

5. `ask_catalog_specialist("List HAND TOOLS in catalog with their SKUs and confirm the category warranty.")`
   → *"HAND TOOLS in catalog: Professional Claw Hammer 16oz (HTHM001600), Ball Peen Hammer 12oz (HTBP001200), Finishing Hammer 13oz (HTFH001300), Sledge Hammer 3lb (HTSH000300), Phillips/Flathead/Precision/Insulated Screwdriver sets. Category warranty: 999 months (effectively lifetime), covers manufacturing defects, head separation, handle splitting. Excludes normal wear, abuse, modification."*
6. `ask_kb_specialist("Warranty claim process — how does the customer file one?")`
   → *"[kb-004] Provide order number + brief defect description. Hand tools have lifetime warranty against manufacturing defects; power tools 2 years; consumables not covered."*

**User exchange:**
> *Lead:* "Two things in one go: on the return, both items in order #100 are past the 30-day window (the order is from January 2025), so I can't process a return automatically. If something specific is defective on either item, the sledge hammer is hand-tool category so it's actually under our lifetime warranty (kb-004) — let me know if that applies. On hand tools with lifetime coverage: every HAND TOOLS SKU we sell qualifies, including all four hammer styles (Claw 16oz, Ball Peen 12oz, Finishing 13oz, Sledge 3lb) and all four screwdriver sets (Phillips, Flathead, Precision, Insulated). Want me to file anything?"
> *User:* "Just file a ticket about the return being too late — buyer's remorse on the paint can. Don't need anything for the hammer."

**Final structured ticket:**
```python
ZavaSupportTicket(
    customer_id=10,
    customer_email="robert.miller3953@example.com",
    product_sku="PFEA000100",   # Exterior Acrylic Paint
    product_name="Exterior Acrylic Paint",
    order_id=100,
    item_index=1,
    purchase_date="2025-01-14",
    symptom_summary="Buyer's remorse — customer wants to return Exterior Acrylic Paint, well past the 30-day return window.",
    observed_symptoms=["customer no longer wants the item"],
    expected_behavior="Standard return policy is 30 days from order_date (kb-001).",
    referenced_doc_ids=["kb-001"],
    warranty_status="unknown",   # paint has no warranty entry in warranty_terms.json
    actual_behavior="Customer requesting return 492 days post-purchase.",
    reproduction_steps="",
    additional_context="Customer also asked which hand tools have lifetime warranty — answered inline, all HAND TOOLS SKUs qualify per kb-004. No action requested on the hammer.",
    severity="low",
    category="other",
)
```

**Patterns exercised:** compound intent in one message → two parallel investigation tracks; answering one in-line and ticketing the other; `warranty_status="unknown"` for paint (no entry in `warranty_terms.json`), correctly reporting the absence rather than guessing.

---

### How these map to the eval framework

Each example translates directly into a `queries.jsonl` entry:

```json
{
  "id": "SW04",
  "query": "I want to return whatever drill I bought from my most recent order. I'm thomas.wilson3364@example.com.",
  "simulated_user": {
    "persona_facts": {
      "email": "thomas.wilson3364@example.com",
      "customer_id": 9,
      "wants_to_return": "hammer drill",
      "defect": "excessive vibration",
      "consents_to_warranty_ticket": true
    }
  },
  "expected_tool_plan_summary": [
    "lookup_customer_by_email",
    "ask_orders_specialist (recent orders)",
    "ask_orders_specialist (items in order 87)",
    "ask user to disambiguate which drill",
    "ask_orders_specialist (eligibility — must report ineligible)",
    "ask_catalog_specialist (warranty terms PTHD000050)",
    "ask_kb_specialist (vibration troubleshooting — must honestly return empty)",
    "ask user to confirm warranty ticket",
    "create_support_ticket"
  ],
  "expected_final_action": {
    "tool": "create_support_ticket",
    "ticket_must_have": {
      "customer_id": 9,
      "order_id": 87,
      "item_index": 1,
      "product_name_contains": "Hammer Drill",
      "category": "warranty_claim",
      "warranty_status": "covered",
      "referenced_doc_ids_empty_or_irrelevant": true,
      "additional_context_mentions_window": true
    }
  }
}
```

The six examples together cover every pattern in §1 ("Where handoffs breaks down"):

| Pattern | Demonstrated by |
|---|---|
| Multi-source synthesis | SW02 (KB + catalog + orders + user) |
| Slot-filling for tickets | SW02, SW03, SW04, SW06 |
| Disambiguation loops | SW01 (token-level), SW04 (item-level) |
| Context isolation | All — only string returns enter lead history |
| Single voice | All — lead is the only speaker the user hears |
| Wrong-premise correction | SW05 |
| Honest gaps (no KB match) | SW04, SW05 |
| Policy-window arithmetic | SW03 (7-day kb-009), SW04/SW06 (30-day returns) |
| Compound intents | SW06 |

## 13. Worked traces — CodeBeamer-style slot-fill on Zava data

Two queries on the Zava dataset that produce a trace with the same shape as the canonical CodeBeamer example:

> *"I'm testing test case ID 1497473 and got additional error codes during execution: EH_EC_INVTEMP_CUTOFF, EH_EC_INVTEMP_CUTOFFSIL, EH_EC_PWRSWITCHDRVR_POWERSUPPLY_FAULT."*

That trace's key features are:
- User references one ID + reports observed behavior in the same message.
- Lead recognises which schema fields are partially filled.
- Lead delegates to the right specialist to look up authoritative info about the referenced ID.
- Specialist composes multiple internal tool calls (resolve ID → fetch details → map sub-IDs to names) and proactively returns related context.
- Lead synthesises actual vs expected, identifies remaining gaps, asks the user only what no source can answer.

The two Zava analogs below produce that exact shape on the data already in the repo.

### Trace map: CodeBeamer concept → Zava analog

| CodeBeamer | Zava (this trace) |
|---|---|
| Test case ID `1497473` | Order ID (e.g. `#87`) or SKU |
| Observed error codes `EH_EC_*` | Observed symptoms in the user's words (motor cuts out, chuck won't lock, battery hot) |
| Test case description (CodeBeamer fetch) | KB articles + product spec + warranty terms |
| Expected error codes from test description | Expected behavior grounded in KB + warranty |
| Map error code IDs → names (extra MCP call) | Map `product_id` → SKU → product name via orders + catalog |
| CodeBeamer specialist | Combination of orders + catalog + kb specialists |
| Mismatch between expected and observed | Drives `category`, `severity`, and `referenced_doc_ids` on the ticket |

### SW07 — Order-referenced defect with multi-symptom report

**Query:** *"I'm using the drill I bought in order #87 and it's behaving weird — the motor cuts out after about 2 minutes, the chuck won't lock onto bits, and the battery feels really hot when this happens."*

#### Turn 1: Lead's SCAN + ENRICH + PLAN

Lead's mental classification of the user's message against `ZavaSupportTicket`:

| Field | Status from this message |
|---|---|
| `actual_behavior` | FILLED — "motor cuts out after 2 min, chuck won't lock, battery hot during use" |
| `observed_symptoms` | FILLED — `["motor cuts out after ~2 min", "chuck won't lock", "battery hot"]` |
| `order_id` | FILLED — 87 |
| `product_name` | PARTIAL — "drill" (need order lookup to disambiguate which drill) |
| `reproduction_steps` | PARTIAL — "during use" (specifics missing) |
| `customer_id`, `customer_email` | depends on UI dropdown / state |
| `product_sku`, `item_index`, `purchase_date` | EMPTY — system-knowable via orders specialist |
| `expected_behavior`, `referenced_doc_ids` | EMPTY — system-knowable via kb specialist (NOT user opinion) |
| `warranty_status` | EMPTY — system-knowable via catalog specialist |
| `severity` | HINT — "battery hot" suggests potential safety concern, lean high |
| `category` | HINT — likely `warranty_claim` |
| `additional_context` | EMPTY — user-knowable, ask at the end |

PLAN: two independent delegations can run in parallel — orders (to resolve the drill identity) and kb (to ground expected behavior). Catalog will follow once SKU is known.

#### Turn 1 delegations (parallel)

**Delegation 1:** `ask_orders_specialist`

```
Goal: For order #87, find the drill line item. Return its item_index,
product_id, SKU, product_name, unit_price, and the order_date.
Known facts: order_id=87
```

Orders specialist composes:
- `lookup_order(87)` → full order with all items (specialist has all this in hand)
- Picks the drill from the items list by category match

Specialist reply:
```
Order 87 (placed 2025-04-11, status=DELIVERED, customer_id=22): the drill is
at item_index=0 — product_id=9, SKU PTDR018000, "Cordless Drill 18V Li-Ion",
unit_price $189.99.

Related context:
  - Order also contains item_index=1: "Hammer Drill 1/2-inch" (SKU PTHD000050,
    POWER TOOLS, $159.99) — not the one the user mentioned.
  - order_date 2025-04-11 → ~13 months ago, outside the 30-day return window
    but well inside any 24-month warranty.
  - Order total $349.98.
```

**Delegation 2 (parallel):** `ask_kb_specialist`

```
Goal: Find help-center articles covering a cordless drill where the motor cuts
out during use, the chuck fails to lock onto bits, and the battery gets
unusually hot. We need the [doc_id] tags for ticket citations.
Known facts: (none required)
```

KB specialist composes:
- `search_help_center("cordless drill motor cuts out chuck wont lock battery overheat")` (one focused search)
- Reads ALL returned hits — judges by content, not rank

Specialist reply:
```
Primary matches by content:
  [kb-003] Drill not turning on — troubleshooting checklist. Battery seated and
  charged, lock-off button unlocked, contacts clean, switch position. "If none
  of these resolve the issue, the tool may be defective and is covered under
  our 2 year power-tool warranty."
  [kb-006] Battery care and storage. "Lithium-ion batteries last longest when
  stored at 30–50% charge in a cool dry place... Never leave a battery on the
  charger long-term. Replace if it no longer holds a charge — most Zava
  batteries are warranted for 2 years from purchase."

Related context:
  [kb-004] How to file a warranty claim — needed if kb-003 troubleshooting
  doesn't resolve. "Hand tools have lifetime warranty; power tools have a
  2-year limited warranty; consumables are not covered."
```

#### Turn 2: Lead's next SCAN + ENRICH + PLAN

Lead now has (filled this turn):
- `product_sku` = `PTDR018000`, `product_name` = `Cordless Drill 18V Li-Ion`, `item_index` = 0, `purchase_date` = `2025-04-11`, `customer_id` = 22
- `referenced_doc_ids` = `[kb-003, kb-006]` (kb-004 in reserve for related context)
- `expected_behavior` — grounded in kb-003 + kb-006: drill should run continuously under normal use; the kb-003 checklist defines the expected non-defective state; kb-006 sets battery lifetime expectations.

Still empty:
- `warranty_status` → next delegate (catalog).
- `customer_email` → need to ask user OR use lookup-by-email if user gave one (this trace assumes UI dropdown set both customer_id and customer_email).
- `reproduction_steps` (more specific), `additional_context`, final `severity` and `category` confirmations — user-knowable, batch at the end.

PLAN: one more delegation (catalog), then final batched question to user.

#### Turn 2 delegation

**Delegation 3:** `ask_catalog_specialist`

```
Goal: What is the warranty for SKU PTDR018000 (Cordless Drill 18V Li-Ion)?
Quote the exact months, every covered defect, every exclusion, and the
product category.
Known facts: sku=PTDR018000
```

Catalog specialist composes:
- `check_warranty("PTDR018000")` → returns full warranty terms

Specialist reply:
```
PTDR018000 (Cordless Drill 18V Li-Ion) — category POWER TOOLS — 24-month warranty.
Covered defects: motor failure, switch failure, gearbox failure.
Exclusions: consumables (blades, bits); battery (separate 24-month warranty);
drop damage.

Related context:
  - The reported symptoms split across two warranty regimes:
      * motor cuts out + chuck won't lock → POWER TOOLS 24-month (covered).
      * battery overheats → separate 24-month battery warranty (covered, but
        a distinct claim).
  - Category POWER TOOLS commonly maps to affected_components: motor /
    switch / gearbox / battery subsystems.
```

#### Turn 3: Lead's final batch question to user

Lead has now resolved every system-knowable field. What remains is purely user-knowable:

- `reproduction_steps` — more specific (every time? only under load? specific bit size?)
- `severity` confirmation — the lead's hint is "high" because of battery heat; needs user confirmation
- `additional_context` — anything else worth recording

Per the pacing rule (1–3 user-knowable slots left), batch:

> *Lead → user:* "Thanks — I have what I need from our records. Before I file the ticket, three quick things: (1) Does the motor cut out every time you use it, or only under load (drilling into hard material)? (2) Is the battery actually too hot to comfortably touch — that's a safety concern I want to flag clearly. (3) Anything else useful I should include?"
>
> *User:* "Every time, even drilling pine. Battery is hot but not burning, more like uncomfortably warm. Nothing else."

#### Turn 4: Lead composes and submits

```python
create_support_ticket(ticket=ZavaSupportTicket(
    customer_id=22,
    customer_email="<from state>",
    product_sku="PTDR018000",
    product_name="Cordless Drill 18V Li-Ion",
    order_id=87,
    item_index=0,
    purchase_date="2025-04-11",
    symptom_summary="Cordless Drill 18V Li-Ion fails during normal use: motor cuts out after ~2 min, chuck won't lock onto bits, battery runs uncomfortably warm.",
    observed_symptoms=[
        "motor cuts out after ~2 minutes of use",
        "chuck won't lock onto bits",
        "battery becomes uncomfortably warm during operation",
    ],
    expected_behavior=(
        "Per kb-003, a non-defective drill should run continuously and respond to a "
        "properly seated chuck; per kb-006, lithium-ion batteries should not run "
        "uncomfortably warm under normal load and are covered by a separate 24-month "
        "battery warranty."
    ),
    referenced_doc_ids=["kb-003", "kb-006", "kb-004"],
    warranty_status="covered",   # POWER TOOLS 24mo + separate battery warranty
    actual_behavior="Motor cuts out after ~2 minutes EVERY time, including light loads (pine). Chuck fails to lock onto bits. Battery becomes uncomfortably warm but not burning.",
    reproduction_steps="Charge battery, attach bit, drill into soft pine — motor cuts out around 2-minute mark every time.",
    additional_context="Symptoms span two warranty regimes (POWER TOOLS for motor/chuck, separate battery warranty for the heat). Lead recommends both be investigated.",
    severity="medium",   # not safety_critical (battery warm, not burning) but high impact on tool usability
    category="warranty_claim",
))
```

`HumanInTheLoopMiddleware` intercepts, shows the customer the full structured ticket, and submits only on approval.

#### Trace shape vs CodeBeamer original

| Step | CodeBeamer original | This Zava trace |
|---|---|---|
| User reports actual + references an ID | "Testing TC 1497473 got error codes EH_EC_*" | "Drill from order #87, motor cuts out, chuck won't lock, battery hot" |
| Lead identifies which fields the message touches | actual_behavior, observed_codes, test_cases | actual_behavior, observed_symptoms, order_id, partial product_name |
| Lead recognises target_behavior must come from docs | "fetch test case description, see expected error codes" | "look up KB + warranty for expected drill/battery behavior" |
| Specialist composes internal tool calls + maps IDs → names | CB MCP: get description → map error code IDs → names | Orders: lookup_order → map product_id → SKU → product name |
| Specialist proactively returns related context | "by the way, this test case is owned by team X" | "by the way, order also has a Hammer Drill (not the one reported); 13 months past 30-day return window but inside 24-month warranty" |
| Lead synthesises actual vs expected | error codes seen vs expected | observed symptoms vs kb-003/kb-006 expectations |
| Lead asks final 1–2 questions to user | residual user-only fields | every time vs under load; how hot is "hot"; anything else |
| Ticket submitted with all fields filled | structured ticket | structured ZavaSupportTicket via Pydantic |

### SW08 — Hand-tool defect with simpler shape (1 specialist round-trip)

**Query:** *"I bought a Sledge Hammer 3lb in my last order and the head wobbled loose after two swings."*

Same shape, fewer turns. Demonstrates that the same protocol scales down when the goal is simpler.

#### Tool sequence

1. `ask_orders_specialist`:
   ```
   Goal: For customer 10, list recent orders and find the one containing a
   "Sledge Hammer 3lb". Return order_id, item_index, order_date, product_id,
   SKU.
   Known facts: customer_id=10
   ```
   Reply: *"Order 100, ordered 2025-01-14, status=DELIVERED. Sledge Hammer 3lb at item_index=0, SKU=HTSH000300, $29.99. Related context: order also contained Exterior Acrylic Paint at item_index=1. Order is 492 days old → past 30-day return window."*

2. `ask_kb_specialist`:
   ```
   Goal: Help-center articles on hammer head coming loose. What's the repair vs
   warranty-replacement policy? Include any related warranty-claim guidance.
   ```
   Reply: *"`[kb-007]` Hammer head loose on handle. Wood-handled hammers: drive a new metal wedge into the existing slot. Fiberglass-handled: epoxy bond has failed → return under lifetime warranty rather than repair. Related context: `[kb-004]` warranty claim process — order number + brief defect description; hand tools have lifetime warranty against manufacturing defects."*

3. `ask_catalog_specialist`:
   ```
   Goal: Warranty for SKU HTSH000300 (Sledge Hammer 3lb). Quote exact months,
   covered defects, exclusions, category.
   Known facts: sku=HTSH000300
   ```
   Reply: *"HTSH000300 (Sledge Hammer 3lb) — category HAND TOOLS — 999-month (effectively lifetime) warranty. Covered: manufacturing defects, head separation, handle splitting. Excluded: normal wear, abuse, modification. Related context: handle construction (wood vs fiberglass) determines whether this is a wedge-repair fix (wood) or a warranty replacement (fiberglass) per kb-007 above. Category HAND TOOLS → affected_components: head, handle, wedge."*

4. **Lead's final batch:** *"I have what I need. Two quick things: (1) Is the handle wood or fiberglass — that determines whether this is a quick wedge repair you can do yourself or a warranty replacement we ship. (2) Anything else useful?"*

5. **Submit** `ZavaSupportTicket(..., warranty_status="lifetime", category="warranty_claim", referenced_doc_ids=["kb-007", "kb-004"], severity="low", ...)`.

This trace is the slimmer version of the same pattern: lead identifies what the message gives it, asks specialists for everything authoritative, batches the user-only question at the end, submits a complete ticket.

### SW09 — ID-to-name resolution inside a specialist (the closest CB analog)

This trace exhibits the **specific** behavior from your CodeBeamer example where the spec stores a referenced thing as an ID, the user reports it by name, and the specialist must do ID → name mapping (via additional tool calls within its own subgraph) to corroborate before reporting back.

**Query:** *"I'm having issues with the drill in my order #87 — it makes a high-pitched whine after a few minutes of use. What does your help center say about it, and is this covered under warranty?"*

#### Why this is the closest analog

| CodeBeamer behavior | Zava behavior in this trace |
|---|---|
| User says error codes by NAME (`EH_EC_*`) | User says product by NAME ("the drill") |
| Test case description holds expected codes as IDs | Order items hold products as `product_id` integers (raw schema) |
| CB specialist fetches description, sees IDs, makes ADDITIONAL MCP calls to resolve them to names | Orders specialist fetches the order, sees `product_id`s in raw items, **resolves each `product_id` → SKU → product_name** before reporting back |
| Corroborate observed names vs resolved expected names | Confirm "the drill" the user meant is in fact `item_index=N` with the right product_name |
| Specialist also volunteers context discovered along the way | Orders specialist surfaces other items in the order; KB specialist follows references inside a primary article to fetch a related one |

#### Trace

**Turn 1 — Lead delegation 1**

```
ask_orders_specialist(
  goal="For order #87, find the drill the user means. The user said 'the drill' by name. "
       "Return item_index, product_id, SKU, product_name (with the resolution from product_id "
       "made explicit so we can corroborate), unit_price, purchase_date, and category. "
       "If multiple drill items exist in this order, list ALL candidates so we can disambiguate.",
  order_id=87,
)
```

**Orders specialist reasoning + tool composition:**

The specialist's first call returns the order, but the raw `items` array uses `product_id` integers — *no product names*. It is the specialist's responsibility to resolve each product_id (via the catalog mapping it has access to internally) to a SKU and a product_name. It does that resolution **before** replying so it can corroborate against the user's word "drill."

**Orders specialist reply:**

```
Order 87 (placed 2025-04-11, status DELIVERED, customer_id=22) — one drill candidate matched:

  - item_index=0: product_id=9 → SKU "PTDR018000" → "Cordless Drill 18V Li-Ion"
    (resolved via products_by_id), unit_price $189.99, category POWER TOOLS.

No other drill products in this order. Single match — no disambiguation needed.

Related context:
  - Order also contains item_index=1: product_id=10 → SKU "PTHD000050" → "Hammer Drill 1/2-inch"
    (POWER TOOLS, $159.99). NOT the item the user referenced (they said "drill", not
    "hammer drill"), but flagging in case the lead later needs warranty cross-reference
    — same warranty regime applies.
  - Resolution note: I had to resolve product_id 9 → product_name explicitly because the
    order's raw items array only stores product_id. If the user's report had said "the
    hammer drill" or "the cordless one", the same resolution step would have produced
    item_index=1 as the match instead.
  - order_date 2025-04-11 → ~13 months ago, outside the 30-day return window but inside
    any 24-month warranty.
```

The "Resolution note:" line makes the ID→name reasoning visible to the lead and to any human reading the trace later — same role as a CodeBeamer specialist explicitly stating "I resolved error code ID 4471 → name EH_EC_INVTEMP_CUTOFF before corroborating."

**Turn 1 — Lead delegation 2 (parallel)**

```
ask_kb_specialist(
  goal="Help-center articles covering a cordless drill that develops a high-pitched whine "
       "after a few minutes of use. Include any related articles on warranty-claim process "
       "if a primary article references warranty.",
)
```

**KB specialist reasoning + tool composition:**

Search returns several hits. The specialist reads them by content, not by rank, and discovers that **no article directly covers "whine" symptoms**. The closest is `kb-003` (drill not turning on — troubleshooting), which mentions warranty coverage but doesn't explain the claim process. The specialist follows that reference and pulls `kb-004` proactively — same pattern as a CodeBeamer worker fetching the names for IDs mentioned inside the primary doc.

**KB specialist reply:**

```
No help-center article directly covers "high-pitched whine" symptoms in a cordless drill.
Closest by content (NOT by top rank — judged article-by-article):

  [kb-003] Drill not turning on — troubleshooting checklist (battery seated, lock-off,
  contacts, switch position). Only tangentially relevant: doesn't address abnormal
  sounds, but notes: "If none of these resolve the issue, the tool may be defective
  and is covered under our 2 year power-tool warranty."

Related context:
  - kb-003 REFERENCES warranty without explaining the claim process. Pulled [kb-004] for
    completeness: "you'll need your order number and a brief description of the defect.
    Hand tools have a lifetime warranty against manufacturing defects; power tools have
    a 2 year limited warranty; consumables (blades, drill bits) are not covered."
  - [kb-006] Battery care — not relevant to a whine symptom; excluded.
  - [kb-010] Saw safety basics — not relevant; excluded.
  - Honest assessment: the lead should flag in the ticket that no KB article matched
    the whine symptom directly; this may suggest a defect the KB doesn't cover yet.
```

**Turn 2 — Lead delegation 3 (sequential, needs SKU from Turn 1)**

```
ask_catalog_specialist(
  goal="Warranty for SKU PTDR018000 (Cordless Drill 18V Li-Ion). Quote exact months, every "
       "covered defect, every exclusion, and the category.",
  sku="PTDR018000",
)
```

**Catalog specialist reply:**

```
PTDR018000 (Cordless Drill 18V Li-Ion) — category POWER TOOLS — 24-month warranty.
Covered defects: motor failure, switch failure, gearbox failure.
Exclusions: consumables (blades, bits); battery (separate 24-month warranty); drop damage.

Related context:
  - The "high-pitched whine" symptom is most likely a motor or gearbox issue — both are
    explicitly covered_defects on this category. The lead can confidently set
    warranty_status=covered and category=warranty_claim.
  - Category POWER TOOLS typically maps to affected_components: motor, switch, gearbox,
    chuck assembly.
```

**Turn 3 — Lead's final batch to user**

> *"I have what I need from our records. Two quick things before I file: (1) Does the whine happen every time you use it, or only under load (e.g. drilling into hard material)? (2) Anything else useful I should include — strange smells, vibration, when it started?"*

**Turn 4 — Lead submits**

```python
create_support_ticket(ticket=ZavaSupportTicket(
    customer_id=22,
    customer_email="<from state>",
    product_sku="PTDR018000",
    product_name="Cordless Drill 18V Li-Ion",
    order_id=87,
    item_index=0,
    purchase_date="2025-04-11",
    symptom_summary="Cordless Drill 18V Li-Ion develops a high-pitched whine after a few minutes of use.",
    observed_symptoms=["high-pitched whine after a few minutes of use"],
    expected_behavior=(
        "Per kb-003 troubleshooting checklist, a non-defective drill should run quietly "
        "and consistently after the standard checks (battery, lock-off, contacts, switch). "
        "No KB article directly addresses an abnormal-sound symptom, so target behavior is "
        "'no abnormal sounds' inferred from the absence of any documented exception."
    ),
    referenced_doc_ids=["kb-003", "kb-004"],
    warranty_status="covered",
    actual_behavior="High-pitched whine starts after a few minutes; happens consistently per user.",
    reproduction_steps="<user's answer about every-time vs under-load>",
    additional_context=(
        "Resolution note from orders worker: order #87 stored the drill as product_id=9; "
        "resolved via catalog to SKU PTDR018000 / Cordless Drill 18V Li-Ion to corroborate. "
        "KB worker flagged that no help-center article directly covers a 'whine' symptom, "
        "so the defect may not be documented yet — worth surfacing to whoever triages this."
    ),
    severity="medium",
    category="warranty_claim",
))
```

#### What this trace demonstrates

1. **ID-to-name resolution inside the specialist** — orders specialist sees `product_id=9` in the raw item, explicitly resolves to "Cordless Drill 18V Li-Ion" via the catalog mapping, surfaces that reasoning in a "Resolution note:" line.

2. **Reference-following inside the specialist** — kb specialist sees `kb-003` references warranty without explaining the claim process, proactively pulls `kb-004` in the same reply.

3. **Honest reporting of gaps** — kb specialist explicitly says "no article matched the whine symptom directly" instead of forcing an irrelevant article to fit.

4. **Cross-specialist synthesis at the lead** — the lead receives all three structured replies, identifies the remaining user-only fields, batches them into one short message, then submits the full structured ticket with the ID-resolution audit trail preserved in `additional_context`.

This is the structural twin of: *"CB specialist fetches test case description, sees error code IDs in it, makes additional MCP calls to map IDs → names, corroborates against user-supplied names, and reports the full picture back to the lead."*
