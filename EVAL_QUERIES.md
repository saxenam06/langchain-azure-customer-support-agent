# Evaluation queries — complex / multi-hop

15 queries designed to stress the Zava customer-support agent. Every query is solvable using only the tools registered in `app/tools/__init__.py` (`set_intent`, `lookup_customer_by_email`, `back_to_triage`, `escalate_to_human`, `lookup_order`, `list_my_orders`, `get_order_status`, `check_return_eligibility`, `initiate_return`, `semantic_search_products`, `check_warranty`, `search_help_center`, `create_support_ticket`, `request_csat`).

Emails reference real rows from `data/customers.json`; order ids fall within the 1–200 range; product names and KB article ids match `data/products.json` and `data/kb_articles.json`.

> Note: today's date (2026-05-21) is past the 30-day window for **every** order in `data/orders.json` (latest order_date 2025-11-19). All return-eligibility checks will fail — the test is whether the agent correctly *detects* that and branches to the right fallback (warranty claim, escalation, or goodwill ticket).

## Query set

```json
[
  {
    "id": "q01",
    "query": "hey it's jane.moore7739@example.com again. I need to send back the cordless one from the last big order I placed — the trigger feels weird. don't have the order number off the top of my head sorry",
    "patterns": ["EMAIL→ORDER→ITEM→ELIGIBILITY", "MULTI-ITEM DISAMBIGUATION", "POLICY-NUMBERS RECALL"],
    "expected_tool_plan": [
      "lookup_customer_by_email(email='jane.moore7739@example.com')",
      "set_intent('order_status')",
      "list_my_orders()",
      "lookup_order(order_id=<most recent>)",
      "// 'the cordless one' may match Cordless Drill 18V Li-Ion OR Cordless Circular Saw 6-1/2 — clarify if both present",
      "set_intent('return_or_refund')",
      "check_return_eligibility(order_id=..., item_index=<resolved>)",
      "// branch: not eligible (>30d) → back_to_triage → create_support_ticket('out-of-window return — defective trigger')"
    ],
    "branch_points": [
      "If most recent order has both a cordless drill AND a cordless saw, ask the customer which one",
      "If trigger defect could be a defect-in-manufacture, mention 24-month power-tool warranty covers switch failure",
      "Eligibility will fail (>30d) → must NOT silently say 'sure, I started the return' — offer warranty claim or escalation"
    ],
    "trap": "'The cordless one' is ambiguous; agent must reconcile description → product → item_index, possibly via semantic_search_products, before calling check_return_eligibility."
  },
  {
    "id": "q02",
    "query": "my drill just stopped working mid-job. won't even turn on. what do I do",
    "patterns": ["SYMPTOM→KB→WARRANTY BRANCH"],
    "expected_tool_plan": [
      "set_intent('tech_support')",
      "search_help_center(query='drill not turning on') // expect kb-003 hit",
      "// walk customer through the kb-003 checklist (battery seated, lock-off, contacts, switch position)",
      "// if customer reports steps don't help: check_warranty(sku=<top drill SKU>) → POWER TOOLS, 24 months, switch/motor covered",
      "back_to_triage()",
      "// resolution → create_support_ticket if warranty claim is appropriate"
    ],
    "branch_points": [
      "Troubleshooting works → close out without escalation",
      "Troubleshooting fails → switch to warranty path",
      "Customer mentions a drop → warranty excludes drop damage → escalate or offer paid repair info"
    ],
    "trap": "A bad agent skips kb-003 and immediately offers a warranty claim. The KB checklist must come first per the tech_support prompt."
  },
  {
    "id": "q03",
    "query": "is the battery in my Cordless Drill 18V Li-Ion covered under the warranty? bought it like 5 months ago, it stopped holding a charge after an hour of use",
    "patterns": ["CATEGORY-DEPENDENT ANSWER", "POLICY-NUMBERS RECALL"],
    "expected_tool_plan": [
      "set_intent('tech_support')",
      "search_help_center(query='battery care charge cordless') // expect kb-006 hit",
      "semantic_search_products(query='Cordless Drill 18V Li-Ion') // resolve SKU",
      "check_warranty(sku=<resolved>) // POWER TOOLS 24mo, battery EXCLUDED",
      "// must reconcile: drill body = 24mo power-tool warranty; battery = SEPARATE 24mo warranty per kb-006",
      "back_to_triage()",
      "create_support_ticket(summary='battery warranty claim — Cordless Drill 18V Li-Ion')"
    ],
    "branch_points": [
      "Customer assumes 'it's a power tool so the battery is covered' — agent must correct this and cite both sources",
      "5 months in = inside the 24-month battery warranty window → claim path, NOT return"
    ],
    "trap": "warranty_terms.json explicitly excludes 'battery (separate 24-month warranty)' under POWER TOOLS. The agent has to merge that with kb-006's mention of the separate 2-year battery warranty rather than just reading one source."
  },
  {
    "id": "q04",
    "query": "I want to cancel my latest order, or return it, whichever applies. I'm thomas.wilson3364@example.com",
    "patterns": ["STATUS-DEPENDENT BRANCH", "EMAIL→ORDER→ITEM→ELIGIBILITY"],
    "expected_tool_plan": [
      "lookup_customer_by_email(email='thomas.wilson3364@example.com')",
      "set_intent('order_status')",
      "list_my_orders()",
      "lookup_order(order_id=<most recent>)",
      "// branch on status:",
      "//  - PLACED/PROCESSING → no cancel tool exists → escalate_to_human('cancel pre-ship order') after confirmation",
      "//  - SHIPPED → tell customer to wait for delivery, then return; offer to set a reminder via ticket",
      "//  - DELIVERED → set_intent('return_or_refund') → check_return_eligibility → likely fails (>30d) → escalate or ticket"
    ],
    "branch_points": [
      "Status PLACED → there is no cancel_order tool; agent must NOT pretend one exists. Correct move is escalate_to_human or create_support_ticket.",
      "Status SHIPPED → can't return yet; tracking guidance via kb-002 may help",
      "Status DELIVERED → eligibility check + 30-day window applies"
    ],
    "trap": "Customer asks for 'cancel OR return' as if they're the same tool path. They aren't, and the right one depends entirely on order status — agent must look it up before promising anything."
  },
  {
    "id": "q05",
    "query": "two things: 1) where's order 87? and 2) do you sell impact drivers, and how long is the warranty",
    "patterns": ["COMPOUND INTENT"],
    "expected_tool_plan": [
      "set_intent('order_status')",
      "get_order_status(order_id=87) // or lookup_order(87) for richer detail",
      "back_to_triage()",
      "set_intent('product_question')",
      "semantic_search_products(query='impact driver')",
      "check_warranty(sku=<top result>) // POWER TOOLS, 24mo"
    ],
    "branch_points": [
      "If order 87 has shipped, surface tracking guidance from kb-002",
      "If catalog has no exact 'impact driver' but has Impact Drill 20V, note it as closest match"
    ],
    "trap": null
  },
  {
    "id": "q06",
    "query": "package showed up completely smashed, the saw inside is broken. order #44. what do I do and how fast do I need to act?",
    "patterns": ["POLICY-NUMBERS RECALL", "STATUS-DEPENDENT BRANCH"],
    "expected_tool_plan": [
      "set_intent('tech_support')",
      "search_help_center(query='damaged in shipping') // expect kb-009 hit",
      "lookup_order(order_id=44) // verify it's DELIVERED and confirm date",
      "// kb-009 says: 7-day reporting window, photos required, no return label needed, replacement shipped immediately",
      "back_to_triage()",
      "create_support_ticket(summary='damaged-in-shipping — saw, order #44', category='damaged_shipping')",
      "// if outside 7-day window: escalate_to_human('damaged delivery, outside 7-day window')"
    ],
    "branch_points": [
      "Inside 7-day window → ticket + 'we ship replacement, no return label needed'",
      "Outside 7-day window → escalate; do not invoke standard returns flow (kb-001), it's a different process",
      "If order #44 status is not DELIVERED, customer is mistaken — clarify"
    ],
    "trap": "Damaged-in-shipping (kb-009, 7-day window, no return label) is a DIFFERENT path from a standard 30-day return (kb-001). The agent must not collapse them."
  },
  {
    "id": "q07",
    "query": "this is ridiculous. third time I'm reaching out about order #112. nothing's working. just get me a person",
    "patterns": ["ESCALATION TRIGGER"],
    "expected_tool_plan": [
      "// triage detects explicit human request",
      "// per validate.py default behavior: confirm reason BEFORE escalating",
      "escalate_to_human(reason='customer requested human after multiple unresolved contacts on order #112')"
    ],
    "branch_points": [
      "Explicit 'get me a person' = direct escalation, no need to attempt KB lookup first",
      "Should NOT first try to solve order #112 — frustration + explicit request override the usual triage flow"
    ],
    "trap": "A diligent-but-wrong agent will try to look up order #112 first and 'help'. The triage prompt explicitly says 'I want to talk to a human' → call escalate_to_human directly."
  },
  {
    "id": "q08",
    "query": "hi, robert.miller3953@example.com here. last order had paint and some screws. the paint can showed up dented — need to swap that one. the screws are fine",
    "patterns": ["EMAIL→ORDER→ITEM→ELIGIBILITY", "MULTI-ITEM DISAMBIGUATION", "POLICY-NUMBERS RECALL"],
    "expected_tool_plan": [
      "lookup_customer_by_email(email='robert.miller3953@example.com')",
      "set_intent('order_status')",
      "list_my_orders()",
      "lookup_order(order_id=<most recent>) // identify paint line item by category PAINT & FINISHES",
      "set_intent('tech_support')",
      "search_help_center(query='damaged in shipping paint can') // expect kb-009",
      "back_to_triage()",
      "create_support_ticket(summary='damaged paint can, order #<n>, item #<paint_index>', category='damaged_shipping')"
    ],
    "branch_points": [
      "Paint has NO warranty entry (HARDWARE/PAINT & FINISHES not in warranty_terms.json) → check_warranty would return 'no terms recorded' → don't go down that branch",
      "Damaged-on-arrival = kb-009 process, NOT 30-day return; 7-day window",
      "Screws are not part of this request — must not include them in the return/ticket"
    ],
    "trap": "Customer says 'swap' which sounds like a standard return — it isn't. Damaged-in-shipping has its own KB path (kb-009) with a 7-day window and no return label. Agent must not call check_return_eligibility or initiate_return as the primary action."
  },
  {
    "id": "q09",
    "query": "the head of my hammer is loose again. like, wobbling. cheapy build right? lol. anyway what are my options",
    "patterns": ["SYMPTOM→KB→WARRANTY BRANCH", "CATEGORY-DEPENDENT ANSWER"],
    "expected_tool_plan": [
      "set_intent('tech_support')",
      "search_help_center(query='hammer head loose') // expect kb-007",
      "semantic_search_products(query='claw hammer') // resolve a hammer SKU for warranty quote",
      "check_warranty(sku=<HTHM001600 or similar>) // HAND TOOLS = 999 months, covers head separation",
      "// kb-007: wood handle → drive new wedge; fiberglass → return under lifetime warranty",
      "back_to_triage()",
      "create_support_ticket(summary='lifetime warranty claim — loose hammer head')"
    ],
    "branch_points": [
      "If wood handle: kb-007 suggests self-repair with a wedge — offer that path",
      "If fiberglass handle: warranty replacement is the right answer",
      "HAND TOOLS warranty is LIFETIME — agent must not quote 24mo (that's power tools) or 12mo"
    ],
    "trap": "Customer's tone ('cheapy build lol') invites either defensive product-quality talk OR over-promising. The right answer is calmly cite kb-007 + the lifetime hand-tool warranty."
  },
  {
    "id": "q10",
    "query": "jennifer.gonzalez6055@example.com. my most recent order should have a drill and some drywall screws in it. need to return the drill — I bought the wrong voltage. also are those drywall screws stainless? planning to use them outdoors",
    "patterns": ["EMAIL→ORDER→ITEM→ELIGIBILITY", "MULTI-ITEM DISAMBIGUATION", "COMPOUND INTENT"],
    "expected_tool_plan": [
      "lookup_customer_by_email(email='jennifer.gonzalez6055@example.com')",
      "set_intent('order_status')",
      "list_my_orders()",
      "lookup_order(order_id=<most recent>) // resolve which drill (Cordless / Hammer / Impact / Right Angle / Drill Press)",
      "set_intent('return_or_refund')",
      "check_return_eligibility(order_id=..., item_index=<drill_idx>) // expected: not eligible (>30d)",
      "// branch: not eligible → back_to_triage → create_support_ticket('wrong-voltage drill, past return window')",
      "set_intent('product_question')",
      "semantic_search_products(query='drywall screws stainless outdoor') // resolve product attributes",
      "// agent must report honestly: Drywall Screws 1-5/8 inch are interior fasteners; if catalog data doesn't specify stainless, say so",
      "back_to_triage()"
    ],
    "branch_points": [
      "Multiple drill SKUs may match → clarify which one",
      "Eligibility fails → must NOT call initiate_return; offer ticket or escalation",
      "Stainless/outdoor question must be answered from catalog data only — no hallucinating coatings"
    ],
    "trap": "Two unrelated requests in one message. Agent must call set_intent twice (or chain back_to_triage between specialists), not lump them into one tool call. 'The drill' is also ambiguous if the order contains more than one drill SKU."
  },
  {
    "id": "q11",
    "query": "saw your competitor selling the same hammer for cheaper — the Professional Claw Hammer 16oz I bought 8 days ago in order 138. can I still get the difference back?",
    "patterns": ["POLICY-NUMBERS RECALL", "STATUS-DEPENDENT BRANCH"],
    "expected_tool_plan": [
      "set_intent('tech_support')",
      "search_help_center(query='price match policy') // expect kb-012",
      "lookup_order(order_id=138) // verify date and that hammer is in items",
      "// kb-012: 14-day window from purchase, screenshot needed, identical SKU, excludes clearance/bundles",
      "// branch on age:",
      "//   ≤14 days → ask customer for screenshot, then create_support_ticket('price-match refund — order #138')",
      "//   >14 days → explain window passed, decline; offer goodwill via escalate or ticket"
    ],
    "branch_points": [
      "Inside 14 days → process refund request via ticket",
      "Outside 14 days → no refund per policy",
      "If order date in JSON does not actually match '8 days ago', surface the discrepancy"
    ],
    "trap": "Customer's '8 days ago' may or may not match the actual order_date in orders.json. Order #138 is from 2024–2025 (well over a year). Agent should not just take the customer's word — must verify via lookup_order and tell the truth even if it contradicts the user."
  },
  {
    "id": "q12",
    "query": "order 23 — one of the saw blades I got is dull already after like 5 cuts. that's covered right?",
    "patterns": ["CATEGORY-DEPENDENT ANSWER", "MULTI-ITEM DISAMBIGUATION", "POLICY-NUMBERS RECALL"],
    "expected_tool_plan": [
      "set_intent('tech_support')",
      "lookup_order(order_id=23) // identify saw-blade line item (semantic_search may be needed if naming is ambiguous)",
      "semantic_search_products(query='saw blade') // resolve SKU + category",
      "check_warranty(sku=<resolved>) // expect POWER TOOL ACCESSORIES (12mo) or similar; consumables/normal wear EXCLUDED",
      "// agent must explain blade dulling = normal wear, not a covered defect",
      "back_to_triage()"
    ],
    "branch_points": [
      "If catalog has no 'saw blade' SKU (it doesn't in the 30-SKU set — blades are mentioned only in KB and warranty docs), agent must say so and not invent",
      "Dulling = wear → not covered; manufacturing defect (cracked, missing teeth) = different story",
      "Warranty exclusion 'consumables (blades, bits)' is from POWER TOOLS terms, not POWER TOOL ACCESSORIES — agent should quote the right one"
    ],
    "trap": "Customer expects coverage. Blades are explicitly called out as CONSUMABLES in the POWER TOOLS warranty exclusions. Dulling is normal wear, also excluded. The honest answer is 'not covered', not 'let me start a claim'."
  },
  {
    "id": "q13",
    "query": "thomas.jones8659@example.com. need help with two things and honestly I'm getting frustrated. one — the ticket I had open about my drill never went anywhere. two — I want a refund on the saw from order #155 because it's making weird grinding noises. can you actually do something or do I need a manager?",
    "patterns": ["COMPOUND INTENT", "ESCALATION TRIGGER", "EMAIL→ORDER→ITEM→ELIGIBILITY"],
    "expected_tool_plan": [
      "lookup_customer_by_email(email='thomas.jones8659@example.com')",
      "// the agent has no list_tickets tool — must acknowledge it can't pull history",
      "set_intent('tech_support')",
      "search_help_center(query='saw grinding noise')",
      "lookup_order(order_id=155) // identify the saw item and date",
      "set_intent('return_or_refund')",
      "check_return_eligibility(order_id=155, item_index=<saw_idx>) // expected: not eligible (>30d)",
      "// frustration + ambiguous prior-ticket complaint + ineligible return → confirm with user, then:",
      "escalate_to_human(reason='unresolved prior ticket on drill + out-of-window saw refund request, customer frustrated')"
    ],
    "branch_points": [
      "If saw noise matches kb-010 (safety) or warranty-covered defect, offer a claim path before escalating",
      "Past 30-day window → escalation is the right answer, not silent ticket creation",
      "Should NOT pretend to look up the prior ticket — no tool for that"
    ],
    "trap": "Customer mentions 'the ticket I had' — agent has no way to retrieve prior tickets and must acknowledge that gap rather than fabricating history."
  },
  {
    "id": "q14",
    "query": "hi I'm elizabeth.wilson6417@example.com. don't have my order number but I want to start a return on the latest thing I bought. it's just not what I expected",
    "patterns": ["EMAIL→ORDER→ITEM→ELIGIBILITY"],
    "expected_tool_plan": [
      "lookup_customer_by_email(email='elizabeth.wilson6417@example.com')",
      "set_intent('return_or_refund')",
      "list_my_orders()",
      "// if multiple orders returned: ask which (or take the most recent and confirm)",
      "lookup_order(order_id=<resolved>)",
      "// if multiple line items: ask which one",
      "check_return_eligibility(order_id=..., item_index=<resolved>) // expected: not eligible (>30d)",
      "// branch: not eligible → back_to_triage → create_support_ticket('out-of-window return, buyer's remorse')",
      "// do NOT call initiate_return without an eligible result + explicit yes from customer"
    ],
    "branch_points": [
      "Eligible (hypothetically) → confirm with customer → initiate_return → back_to_triage → resolution",
      "Not eligible → ticket or escalation; no special exception",
      "'Not what I expected' = buyer's remorse, not a defect → no warranty path"
    ],
    "trap": "Looks like a clean standard-return ask. The trap is that none of the orders in the dataset are within 30 days of today (2026-05-21), so the agent must report the failure honestly instead of silently initiating a return that policy doesn't allow."
  },
  {
    "id": "q15",
    "query": "my brand new cordless circular saw cut fine for an hour then the blade stopped spinning even though the motor sounded like it was still running. is the gearbox something you cover or is it like the blade thing where it's a consumable",
    "patterns": ["SYMPTOM→KB→WARRANTY BRANCH", "CATEGORY-DEPENDENT ANSWER", "POLICY-NUMBERS RECALL"],
    "expected_tool_plan": [
      "set_intent('tech_support')",
      "search_help_center(query='circular saw blade stopped spinning gearbox')",
      "// kb-005 (saw blade replacement) and kb-010 (saw safety) may surface; neither covers gearbox specifically",
      "semantic_search_products(query='cordless circular saw') // resolve a SKU (Cordless Circular Saw 6-1/2)",
      "check_warranty(sku=<resolved>) // POWER TOOLS, 24mo, covered_defects includes 'gearbox failure'",
      "search_help_center(query='warranty claim') // kb-004",
      "back_to_triage()",
      "create_support_ticket(summary='power-tool warranty claim — gearbox failure, Cordless Circular Saw 6-1/2', category='warranty')"
    ],
    "branch_points": [
      "If the symptom is actually a slipped/broken blade (consumable), no claim — clarify with customer first",
      "Gearbox failure IS in the covered_defects list → claim path",
      "If symptom suggests drop damage (excluded), pivot to declining the claim with reason"
    ],
    "trap": "Customer hedges: 'is it like the blade thing'. The trap is that blades ARE excluded (consumables) but gearbox IS covered. Agent must classify correctly using warranty_terms.json rather than lumping them together."
  }
]
```

## Pattern coverage matrix

| Pattern | Queries | Count |
|---|---|---|
| 1. EMAIL→ORDER→ITEM→ELIGIBILITY | q01, q04, q08, q10, q13, q14 | 6 |
| 2. SYMPTOM→KB→WARRANTY BRANCH | q02, q09, q15 | 3 |
| 3. CATEGORY-DEPENDENT ANSWER | q03, q09, q12, q15 | 4 |
| 4. STATUS-DEPENDENT BRANCH | q04, q06, q11 | 3 |
| 5. MULTI-ITEM DISAMBIGUATION | q01, q08, q10, q12 | 4 |
| 6. COMPOUND INTENT | q05, q10, q13 | 3 |
| 7. ESCALATION TRIGGER | q07, q13 | 2 |
| 8. POLICY-NUMBERS RECALL | q01, q03, q06, q08, q10, q11, q12, q15 | 8 |

## Tool-chain depth

Queries that chain **≥4 tool calls** (target: ≥5 of 15):

- q01: lookup_customer_by_email → list_my_orders → lookup_order → semantic_search_products → check_return_eligibility → create_support_ticket (6)
- q03: search_help_center → semantic_search_products → check_warranty → create_support_ticket (4)
- q04: lookup_customer_by_email → list_my_orders → lookup_order → escalate_to_human (4)
- q08: lookup_customer_by_email → list_my_orders → lookup_order → search_help_center → create_support_ticket (5)
- q09: search_help_center → semantic_search_products → check_warranty → create_support_ticket (4)
- q10: lookup_customer_by_email → list_my_orders → lookup_order → check_return_eligibility → semantic_search_products → create_support_ticket (6)
- q13: lookup_customer_by_email → search_help_center → lookup_order → check_return_eligibility → escalate_to_human (5)
- q14: lookup_customer_by_email → list_my_orders → lookup_order → check_return_eligibility → create_support_ticket (5)
- q15: search_help_center → semantic_search_products → check_warranty → search_help_center → create_support_ticket (5)

That's 9 queries with ≥4 tool calls.

## Traps (queries that look one way but require another path)

- **q01** — "the cordless one" is ambiguous when an order has both a cordless drill and a cordless saw.
- **q03** — Batteries are EXCLUDED from the power-tool warranty; covered under a separate 24-month battery warranty.
- **q04** — Customer says "cancel or return"; no `cancel_order` tool exists, and the correct path depends on status.
- **q06** — Damaged-in-shipping is kb-009 (7-day window, no return label), NOT the standard 30-day return process (kb-001).
- **q07** — Diligent-but-wrong agent will try to look up order #112 first; explicit human request should short-circuit to escalation.
- **q08** — "Swap" sounds like a standard return; damaged paint actually goes through kb-009. Paint has no warranty entry.
- **q09** — Hand tools are LIFETIME warranty, not 24-month; customer's defensive tone shouldn't change the answer.
- **q10** — Two unrelated intents; "the drill" is ambiguous if the order has multiple drill SKUs.
- **q11** — Customer's claimed "8 days ago" likely doesn't match the actual order_date; agent must verify, not believe.
- **q12** — Blade dulling is normal wear AND blades are listed as consumables — explicitly NOT covered.
- **q13** — Agent has no `list_tickets` tool; must acknowledge the gap rather than fabricate prior-ticket history.
- **q14** — Standard-looking return; every order in the dataset is past the 30-day window, so the agent must report eligibility failure honestly.
- **q15** — Gearbox IS covered under power-tool warranty even though blades (which customer mentions) are not.
