# Complex multi-hop test queries

Ten queries grounded in the actual data files (`data/customers.json`, `data/orders.json`, `data/products.json`, `data/warranty_terms.json`, `data/kb_articles.json`). Each query exercises multiple specialists, conditional branches, and tools in `app/tools/`.

## Anchors from the data

- **Real customer emails (a few):**
  - `richard.lopez8911@example.com` (id 1)
  - `michael.jones3502@example.com` (id 2)
  - `jane.moore7739@example.com` (id 5)
  - `jennifer.gonzalez6055@example.com` (id 7)
- **Real orders:** ids 1–200. Status mix → 96 DELIVERED, 35 PLACED, 35 PROCESSING, 34 SHIPPED. (Most order_dates are 2024–2025; under today's 2026-05-21, eligibility will mostly fail — the agent should detect that.)
- **Real products:** Professional Claw Hammer 16oz (SKU `HTHM001600`, HAND TOOLS), Sledge Hammer 3lb, Cordless Drill 18V Li-Ion, Hammer Drill 1/2-inch, Circular Saw 7-1/4 inch, Worm Drive Saw, Drywall Screws 1-5/8 inch, etc.
- **Warranty by category:** HAND TOOLS = 999 mo (effectively lifetime), POWER TOOLS = 24 mo, POWER TOOL ACCESSORIES = 12 mo, FASTENERS = 12 mo. HARDWARE / PAINT & FINISHES have **no entry** → `check_warranty` returns "No warranty terms recorded".
- **KB articles:** `kb-001` returns policy, `kb-003` drill not turning on, `kb-004` warranty claims, `kb-005` saw blade replacement, `kb-006` battery care, `kb-007` hammer head loose, `kb-008` wrong item, `kb-009` damaged in shipping, `kb-010` saw safety, `kb-011` order status meanings, `kb-012` price match.

---

## The queries

### 1. Identity → list → conditional return
> *"I'm richard.lopez8911@example.com. Pull up my most recent order. If it's been DELIVERED for more than a month I want to return whatever hammer or hand tool was inside — the head's gone loose."*

- **Hits:** identity resolution, status interpretation, KB cross-check on `kb-007` (hammer head loose), eligibility branch that legitimately fails.
- **Flow:** triage → `lookup_customer_by_email` → `set_intent("order_status")` → `list_my_orders` → `lookup_order(latest)` → branch on `status` and `order_date` → `set_intent("tech_support")` → `search_help_center("hammer head loose")` → cite `[kb-007]` → `back_to_triage` → `set_intent("return_or_refund")` → `check_return_eligibility` → expected: **not eligible** (date too old) → `escalate_to_human` or `create_support_ticket`.

---

### 2. Battery exclusion vs separate battery warranty
> *"My Cordless Drill 18V Li-Ion stopped holding a charge. Look up your help center, and tell me whether the battery is covered under the drill's 24-month warranty or under something separate. Then check whether SKU-based warranty for any drill in your catalog says the same thing."*

- **Hits:** nuanced warranty reading — `warranty_terms.json` for POWER TOOLS explicitly excludes "battery (separate 24-month warranty)"; `kb-006` repeats the 2-year battery warranty.
- **Flow:** triage → `set_intent("tech_support")` → `search_help_center("battery care charge")` → cites `[kb-006]` → `check_warranty("PTDR018000")` (or whichever cordless-drill SKU `semantic_search_products` surfaces) → reconcile the two sources → `back_to_triage`.

---

### 3. Multi-product warranty comparison
> *"Do you sell sledgehammers and cordless drills? For the top match in each category, quote the warranty terms verbatim and tell me which has the longer coverage."*

- **Hits:** parallel catalog searches, two `check_warranty` calls, cross-category comparison (HAND TOOLS 999 mo vs POWER TOOLS 24 mo).
- **Flow:** triage → `set_intent("product_question")` → `semantic_search_products("sledge hammer")` → grab top SKU (e.g. Sledge Hammer 3lb) → `check_warranty(sku_hand)` → `semantic_search_products("cordless drill")` → `check_warranty(sku_power)` → compare → `back_to_triage`.

---

### 4. Eligibility miss → goodwill ticket
> *"I want to return the hand-tool item from order #1. If it's past the window, just open a support ticket on my behalf asking for goodwill credit and explain why in the summary."*

- **Hits:** explicit eligibility check, conditional ticket creation in resolution.
- **Flow:** triage → `set_intent("return_or_refund")` → `lookup_order(1)` (customer 35, 2025-02-25, $306.25, DELIVERED, 4 items) → identify hand-tool line item → `check_return_eligibility(1, item_index)` → **not eligible (over 30 days)** → `back_to_triage` → resolution → `create_support_ticket("missed-window goodwill credit request — order #1")`.

---

### 5. Validate middleware trigger (no KB coverage)
> *"How do I re-grease the planetary gears inside my Hammer Drill 1/2-inch? Need the exact torque spec for the clutch nut."*

- **Hits:** the `validate_response` middleware. `search_help_center` will return `kb-003`/`kb-006`/`kb-004` (none cover gearbox/torque). The model's reply won't cite a matching `doc_id`, so validate rewrites it to `ASK_BEFORE_ESCALATE`.
- **Flow:** triage → `set_intent("tech_support")` → `search_help_center("hammer drill gearbox grease torque")` → LLM tries to answer → validate sees no grounded citation → response becomes "I couldn't find that in our help center… would you like a human?" → user says "yes" → `escalate_to_human`.

---

### 6. Two-branch status meaning vs CSAT closeout
> *"I'm jane.moore7739@example.com. For my most recent order — if it's PROCESSING or SHIPPED, explain what that status actually means using your help center. If it's DELIVERED, just close out with a satisfaction survey."*

- **Hits:** completely different specialist trajectories depending on a tool result; cites `kb-011`.
- **Flow:** triage → `lookup_customer_by_email` → `set_intent("order_status")` → `list_my_orders` → `lookup_order(latest)` → **branch on status:**
  - PROCESSING/SHIPPED: stay → `back_to_triage` → `set_intent("tech_support")` → `search_help_center("order status meanings")` → cite `[kb-011]`.
  - DELIVERED: `back_to_triage` → resolution → `create_support_ticket(...)` → `request_csat(ticket_id)`.

---

### 7. Saw-blade safety + replacement chained
> *"My Circular Saw 7-1/4 inch is making a wobble. Search the help center for blade-related articles, walk me through changing the blade safely, and warn me about the safety stuff I should do first."*

- **Hits:** chained KB retrieval where two articles are both relevant (`kb-005` blade replacement + `kb-010` saw safety basics), citations from both.
- **Flow:** triage → `set_intent("tech_support")` → `search_help_center("saw blade replacement safety")` → top results include `kb-005` and `kb-010` → assistant cites both → `back_to_triage`.

---

### 8. Wrong-item vs damaged-in-shipping disambiguation
> *"I'm michael.jones3502@example.com and my order #2 arrived but the saw inside isn't the one I ordered AND it has a cracked guard. Do those two situations follow the same process or different ones? Then file a support ticket capturing both."*

- **Hits:** distinguishing `kb-008` (wrong item) from `kb-009` (damaged in shipping), citing both, then opening a ticket.
- **Flow:** triage → `lookup_customer_by_email` → `set_intent("tech_support")` → `search_help_center("wrong item damaged")` → cites `[kb-008]` and `[kb-009]` → `lookup_order(2)` to confirm details → `back_to_triage` → resolution → `create_support_ticket("wrong item + damaged guard, order #2")`.

---

### 9. Price-match flow with order context
> *"I bought a Professional Claw Hammer 16oz on order #1 and just saw a competitor's site listing it cheaper. Walk me through your price-match policy citing your own help center, and if I'm still within the window, open a ticket for the refund difference."*

- **Hits:** KB-grounded answer (`kb-012`), date-window reasoning, conditional ticket.
- **Flow:** triage → `set_intent("tech_support")` (or treat as billing → resolution) → `search_help_center("price match")` → cite `[kb-012]` → `lookup_order(1)` to confirm date → branch on 14-day window → resolution → `create_support_ticket("price-match refund request — Professional Claw Hammer 16oz, order #1")`.

---

### 10. Full-pipeline end-to-end (≈4 specialists, ~8 tools)
> *"Hi, I'm jennifer.gonzalez6055@example.com. Three things in one go:*
> *1. Show me my most recent order.*
> *2. If any line item is a power tool, quote its warranty terms; if it's a hand tool, just confirm it's lifetime.*
> *3. While you're searching the help center for warranty-claim instructions, also recommend one similar product still in your catalog in case I want a backup. End with a support ticket summarising everything and a CSAT survey."*

- **Hits:** the entire architecture — identity, orders, warranty branching by category, KB grounding (`kb-004`), catalog semantic search, ticket + CSAT.
- **Flow:** triage → `lookup_customer_by_email` → `set_intent("order_status")` → `list_my_orders` → `lookup_order(latest)` → branch on product category for the line items → `back_to_triage` → `set_intent("tech_support")` → `search_help_center("warranty claim")` → cite `[kb-004]` → `check_warranty(sku)` for each item → `back_to_triage` → `set_intent("product_question")` → `semantic_search_products("<similar product>")` → `back_to_triage` → resolution → `create_support_ticket(...)` → `request_csat(ticket_id)`.

---

## How each one exercises the architecture

| # | Specialists touched | Tools called | What it stresses |
|---|---|---|---|
| 1 | triage, order_lookup, tech_support, returns, resolution | 6 | conditional branching on status + date |
| 2 | triage, tech_support | 3 | warranty-vs-KB reconciliation across two sources |
| 3 | triage, product_qna | 4 | parallel searches + cross-category comparison |
| 4 | triage, returns, resolution | 4 | tool-result-driven specialist switch |
| 5 | triage, tech_support, resolution | 3 | `validate_response` post-call rewrite + escalation |
| 6 | triage, order_lookup, tech_support OR resolution | 4–5 | two divergent trajectories from one decision point |
| 7 | triage, tech_support | 2 | multi-citation grounding |
| 8 | triage, tech_support, resolution | 4 | KB disambiguation between similar topics |
| 9 | triage, tech_support, resolution | 4 | date-window reasoning over order metadata |
| 10 | All five non-resolution specialists | ≥8 | full pipeline; tests handoffs in both directions |

For queries 1, 2, 6, 8, 10 you'll get cleaner runs if you pre-select the matching customer in the UI dropdown so `customer_id` is in state — otherwise the agent will rely on `lookup_customer_by_email` to find them, which also works but adds an extra tool call.
