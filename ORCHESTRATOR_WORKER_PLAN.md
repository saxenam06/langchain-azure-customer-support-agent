# Orchestrator-worker refactor plan

A plan to transition this repo from its current **handoffs** architecture to an **orchestrator-worker (supervisor)** architecture, with an evaluation framework to compare the two. The end goal is a reusable framework that can be ported to a CodeBeamer-backed (or any other multi-source) customer-support problem.

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
                ┌────────────────────────────────────────────────────┐
                │           Lead agent (single voice)                │
                │  - holds user conversation                         │
                │  - maintains slot list / known facts               │
                │  - decides: delegate vs ask user vs answer         │
                │                                                    │
                │  Tools visible to lead:                            │
                │    ask_orders_specialist(question) -> str          │
                │    ask_catalog_specialist(question) -> str         │
                │    ask_kb_specialist(question) -> str              │
                │    lookup_customer_by_email(email) -> str          │
                │    create_support_ticket(summary, category) -> str │
                │    request_csat(ticket_id) -> str                  │
                │    escalate_to_human(reason) -> str                │
                └─────┬────────────┬─────────────────┬───────────────┘
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
- **Only a string (or structured field) bubbles back** to the lead via the delegate tool's return value. Subagent tool calls and intermediate results are invisible to the lead.
- **No `current_step`, no `set_intent`, no `back_to_triage`.** The state machine goes away.

## 3. File-by-file changes

### Files to delete

- `app/middleware/steps.py` — no more step-based prompt/tool filtering.
- `app/prompts/triage.txt`, `order_lookup.txt`, `returns.txt`, `tech_support.txt`, `product_qna.txt`, `resolution.txt` — replaced by one lead prompt + 3 subagent prompts.

### Files to modify

- `app/state.py` — drop `current_step`, `intent`. Keep `customer_id`, `customer_email`, `order_id`, `last_retrieved_docs`, `awaiting_escalation_confirmation`. Add `known_facts: NotRequired[dict[str, str]]` if we want explicit slot tracking.
- `app/tools/workflow.py` — drop `set_intent`, `back_to_triage`. Keep `lookup_customer_by_email`, `escalate_to_human`.
- `app/tools/__init__.py` — re-export only the tools that survive (delegate wrappers + workflow + ticket tools).
- `app/middleware/__init__.py` — drop the `apply_step_config` export.
- `app/agent.py` → split into:
  - `app/agents/subagents.py` — three `create_agent` calls + their prompts.
  - `app/agents/lead.py` — the lead `create_agent` call + its prompt.
  - `app/agent.py` — thin facade that builds and returns the lead.
- `app/main.py` — replace the single `agent` with the lead; add `?mode=handoffs|supervisor` query parameter so the existing UI can hit either implementation during evaluation.

### Files to add

- `app/tools/delegates.py` — three async wrapper tools that invoke each subagent and return its final string.
- `app/agents/prompts/lead.txt` — the orchestrator prompt.
- `app/agents/prompts/orders_specialist.txt`, `catalog_specialist.txt`, `kb_specialist.txt`.
- `eval/` directory (see Section 6).

### Files to keep unchanged

- `app/tools/orders.py`, `catalog.py`, `kb.py`, `tickets.py` — tool implementations don't change.
- `app/data_loader.py` — data layer is fine.
- `app/streaming.py` — stream events are still emitted the same way; only the source agent differs.
- `data/*` — no schema changes required (overlap of integer IDs across `customer_id` / `order_id` / `product_id` already gives us enough disambiguation cases).

## 4. Sub-agent design

Each subagent is a `create_agent` with:

- A small **prompt** that defines its role narrowly: *"You are a worker. Answer the lead's question using your tools. Return one concise sentence (or a short structured block). Do not chat. Do not greet."*
- Its **own state schema** (subclass of `AgentState`) so its messages list is isolated from the lead's.
- Its **own checkpointer** — actually, no checkpointer. Each delegation is a fresh run; subagent memory doesn't persist across calls.

### Example: orders subagent

```python
# app/agents/subagents.py
from langchain.agents import AgentState, create_agent

class OrdersSubState(AgentState):
    pass  # nothing extra; messages are scoped to this delegation only

ORDERS_SPECIALIST_PROMPT = """
You are the orders worker. You answer one specific question from the lead and stop.

Tools:
- lookup_order(order_id)         — full order detail with item_index per line item
- list_my_orders()                — recent orders for state.customer_id (lead must set it)
- get_order_status(order_id)      — status string only
- check_return_eligibility(order_id, item_index)
- initiate_return(order_id, item_index, reason)  — only if lead's question explicitly says to

Rules:
1. Use the minimum tools needed to answer.
2. Reply with ONE concise factual statement (a sentence, a list, or JSON). No greetings.
3. If the question is missing required IDs, reply with "MISSING:" and the missing field name.
4. Do not call initiate_return unless the lead's question contains the word "initiate".
"""

orders_subagent = create_agent(
    model=nano_model,  # or main_model for harder reasoning
    tools=[lookup_order, list_my_orders, get_order_status,
           check_return_eligibility, initiate_return],
    state_schema=OrdersSubState,
    system_prompt=ORDERS_SPECIALIST_PROMPT,
)
```

Same shape for `catalog_subagent` (tools: `semantic_search_products`, `check_warranty`) and `kb_subagent` (tools: `search_help_center`).

### The delegate wrapper

```python
# app/tools/delegates.py
from langchain.tools import tool
from langchain_core.messages import HumanMessage

@tool
async def ask_orders_specialist(question: str) -> str:
    """Delegate one order-related question to the orders worker.
    Provide a self-contained question that includes any known order_id /
    customer_id. Returns a concise factual answer or "MISSING: <field>".
    Use this for: order lookup, listing a customer's orders, status checks,
    return eligibility, initiating a return.
    """
    state = {"messages": [HumanMessage(content=question)]}
    # If lead's state has customer_id, pass it through:
    # (do this by reading runtime in the wrapper signature)
    result = await orders_subagent.ainvoke(state)
    return result["messages"][-1].content
```

The lead never sees `lookup_order`'s raw JSON. It sees only the worker's distilled reply.

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
  - create_support_ticket(summary, category)
  - request_csat(ticket_id)
  - escalate_to_human(reason)          — only after the user confirms

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
- Confirm before any destructive action (initiate_return, create_support_ticket,
  escalate_to_human).
```

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

### Middleware on the lead

Keep:
- `refine_query` — still useful at the lead boundary.
- `validate_response` — but broaden scope: now runs on every reply, not just `tech_support`/`product_qna` (since there are no steps).
- `SummarizationMiddleware` — still useful.

Drop:
- `apply_step_config` — gone.

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
    "ask_orders_specialist (most recent order)",
    "ask_orders_specialist (items in that order)",
    "ask user to identify item",
    "ask user for defect description",
    "ask_catalog_specialist (warranty category)",
    "create_support_ticket"
  ],
  "expected_final_action": {
    "tool": "create_support_ticket",
    "args_must_include": ["grinding", "drill", "order 50"]
  }
}
```

### Scoring rubric (each query scored 0–6)

| # | Criterion | What it measures |
|---|---|---|
| 1 | **Disambiguation** | Did the lead resolve ambiguous identifiers (numeric IDs, "the cordless one") correctly and not silently guess? |
| 2 | **Slot completeness** | Were all required fields filled before the final action? |
| 3 | **Right-source-for-fact** | Was each fact pulled from the specialist that owns it, not invented or asked of the user when a system knows it? |
| 4 | **Right-channel-for-question** | Was each unknown delegated (machine-knowable) or asked (only the user knows) — not swapped? |
| 5 | **Final action correctness** | Does the final tool call match `expected_final_action`? |
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
2. Author `eval/queries.jsonl` with 10 supervisor-style queries (see `EVAL_QUERIES.md` for the format precedent — these will be a sibling set).
3. Build `user_simulator.py` (nano with a persona prompt + ground-truth facts).
4. Build `score.py` against the rubric above.

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

1. Branch: `git checkout -b supervisor-prototype`.
2. Add `app/agents/subagents.py` with all three subagents (~80 lines including prompts).
3. Add `app/tools/delegates.py` with the three wrapper tools (~40 lines).
4. Add `app/agents/lead.py` with the lead `create_agent` call + prompt (~50 lines).
5. Modify `app/main.py` to honour `?mode=` and select the right agent.
6. Smoke-test with one query (`"open a ticket about my last order"`) from the UI.
7. Move on to the eval framework.

Estimated effort to working prototype: **half a day**. Estimated effort to working eval framework: **another day**.

## 9. Open questions worth deciding before coding

- **Should subagents have access to `state["customer_id"]`?** If yes, pass it explicitly via the delegate tool call (not by sharing state). If no, the lead must include the customer id in every question.
- **Should the lead see streaming tokens from subagents?** Probably no — the lead's value is hiding that chatter. But for debugging, the UI's debug drawer could show subagent transcripts behind a toggle.
- **Per-turn cost ceiling?** Supervisor naturally costs more. Decide upfront whether to cap subagent depth (e.g., a delegate tool may not itself call another delegate tool) to bound worst-case spend.
- **Failure mode if a subagent returns garbage.** Should the lead retry with a clarified question, or surface the failure to the user? Test both with the eval rubric.

These don't block prototyping; flag them and decide once you've read the first batch of transcripts.
