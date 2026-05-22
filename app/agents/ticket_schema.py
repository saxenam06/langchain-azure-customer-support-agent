"""Structured ticket schema enforced at the framework level.

The lead agent must call `create_support_ticket(ticket=ZavaSupportTicket(...))`
with every field populated. Pydantic validation runs before the tool body, so
the LLM cannot submit a partially-filled ticket; it gets a validation error as
a tool message and has to gather the missing data.

Maps directly onto the CodeBeamer schema the user is targeting next:
  test_environment   ← product + customer context
  software_version   ← product_sku + purchase_date
  error_codes        ← observed_symptoms
  test_case          ← order_id + item_index
  requirements       ← referenced_doc_ids (KB) + expected_behavior
  actual_behavior    ← actual_behavior (verbatim)
  target_behavior    ← expected_behavior (verbatim)
  additional_info    ← additional_context
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ZavaSupportTicket(BaseModel):
    """A complete support ticket. Every required field must be sourced and present."""

    # --- Customer context ---
    customer_id: int = Field(description="Customer id, resolved from email lookup or state.")
    customer_email: str = Field(description="Customer's email address.")

    # --- Product context (≈ test_environment + software_version) ---
    product_sku: str = Field(description="SKU of the affected product.")
    product_name: str = Field(description="Human-readable product name.")
    order_id: int = Field(description="Order id the product belongs to.")
    item_index: int = Field(description="0-based index of the line item in the order.")
    purchase_date: str = Field(description="ISO date string of the order_date.")

    # --- Issue identification (≈ error_codes + test_case) ---
    symptom_summary: str = Field(
        description="One-line description of the customer's issue, in the lead's own words.",
    )
    observed_symptoms: list[str] = Field(
        description=(
            "Concrete symptoms in the customer's words. Each item should be a short phrase. "
            "E.g. ['grinding noise', 'starts then stalls', 'battery dies in 30 min']."
        ),
    )

    # --- Specification side (≈ requirements + target_behavior) ---
    expected_behavior: str = Field(
        description=(
            "What the product is supposed to do, grounded in KB or warranty. "
            "Must be backed by at least one entry in referenced_doc_ids when applicable."
        ),
    )
    referenced_doc_ids: list[str] = Field(
        description=(
            "KB article ids (e.g. ['kb-003','kb-006']) that justify expected_behavior. "
            "Empty list ONLY if you honestly searched and no article matched."
        ),
    )
    warranty_status: Literal["covered", "not_covered", "lifetime", "unknown"] = Field(
        description=(
            "Coverage state for the affected item. 'unknown' is for categories with no entry "
            "in warranty_terms.json (e.g. PAINT & FINISHES)."
        ),
    )

    # --- User report (≈ actual_behavior + additional_information) ---
    actual_behavior: str = Field(
        description="What the customer is observing, in their words.",
    )
    reproduction_steps: str = Field(
        description="How to reproduce. Empty string if not applicable.",
    )
    additional_context: str = Field(
        description="Free-text context worth recording. Empty string if none.",
    )

    # --- Classification ---
    severity: Literal["low", "medium", "high", "safety_critical"] = Field(
        description="low/medium/high for ordinary defects; safety_critical for hazards (bulging battery, exposed wiring, etc.).",
    )
    category: Literal[
        "warranty_claim",
        "damaged_shipping",
        "wrong_item",
        "general_defect",
        "billing",
        "other",
    ] = Field(
        description=(
            "warranty_claim: covered defect. damaged_shipping: arrived damaged (kb-009). "
            "wrong_item: kb-008. general_defect: defect outside warranty. billing: price/refund. other: catch-all."
        ),
    )
