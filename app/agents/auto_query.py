"""Derive a delegate's Pydantic input schema + docstring from the subagent's tools.

The principle: the lead's view of a specialist is computed, not hand-written.
Add a tool to a subagent → restart → the delegate's input schema and the
delegate's docstring automatically reflect the new arg (and which subagent
tools require it). No prose drift, no manual edits.

Pydantic types in the generated query model are all OPTIONAL — the subagent
decides which it actually needs for any given goal and returns "MISSING: X"
if a required field for the tool it picked wasn't provided. That keeps the
lead/subagent decoupling: the lead expresses the goal, the subagent picks how.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, create_model

# Framework-injected params — these are NOT exposed to the LLM and so should
# not appear in our auto-derived schema or docstring.
_INJECTED_PARAMS = {"runtime", "tool_call_id", "state"}


def _json_type_to_python(info: dict[str, Any]) -> type:
    """Best-effort JSON-Schema-type → Python-type mapping for create_model."""
    t = info.get("type")
    if t == "integer":
        return int
    if t == "number":
        return float
    if t == "boolean":
        return bool
    if t == "array":
        return list
    if t == "object":
        return dict
    return str


def _walk_subagent_tools(subagent) -> dict[str, dict[str, Any]]:
    """Return {arg_name: {"type": <Py type>, "description": str, "required_for": [tool_names]}}.

    Uses `tool.tool_call_schema` (the LLM-facing schema) which already strips
    framework-injected params like `runtime` and `tool_call_id`.
    """
    aggregated: dict[str, dict[str, Any]] = {}
    for t in getattr(subagent, "tools", []) or []:
        # tool_call_schema is what the LLM sees — runtime/tool_call_id already stripped.
        schema_cls = getattr(t, "tool_call_schema", None) or getattr(t, "args_schema", None)
        if schema_cls is None:
            continue
        try:
            schema = schema_cls.model_json_schema()
        except Exception:
            continue
        required = set(schema.get("required", []))
        for arg, info in schema.get("properties", {}).items():
            if arg in _INJECTED_PARAMS:
                continue
            entry = aggregated.setdefault(
                arg,
                {
                    "type": _json_type_to_python(info),
                    "description": info.get("description", ""),
                    "required_for": [],
                },
            )
            if arg in required:
                entry["required_for"].append(t.name)
            # Prefer the longer description if we see this arg in multiple tools.
            new_desc = info.get("description", "")
            if len(new_desc) > len(entry["description"]):
                entry["description"] = new_desc
    return aggregated


def build_query_model(name: str, subagent) -> type[BaseModel]:
    """Build a Pydantic Query class: `goal` + every non-injected arg the worker tools accept.

    All worker args are OPTIONAL at the delegate level. The subagent will say
    "MISSING: X" if its chosen tool actually needed something the lead didn't
    pass. That preserves goal-oriented delegation.
    """
    fields: dict[str, tuple[Any, Any]] = {
        "goal": (
            str,
            Field(
                description=(
                    "High-level natural-language description of what you want the worker to do. "
                    "Be specific. Examples: "
                    "'Find the most recent order for customer 5 that contains a drill; return its order_id and item_index for the drill.' "
                    "'For order 87, list items with their item_index, product category, and unit price.' "
                    "'Is line item at item_index 1 of order 100 still eligible to return?'"
                ),
            ),
        ),
    }

    for arg, meta in _walk_subagent_tools(subagent).items():
        required_for = meta["required_for"]
        suffix = (
            f"  Required by worker tools: {', '.join(required_for)}." if required_for else ""
        )
        full_desc = f"{meta['description']}{suffix}".strip()
        fields[arg] = (
            meta["type"] | None,  # always optional at delegate level
            Field(default=None, description=full_desc),
        )

    return create_model(f"{name}Query", **fields)


def build_delegate_docstring(purpose: str, subagent) -> str:
    """Generate the delegate's user-facing description (what the lead's LLM sees).

    Includes a per-tool capability summary so the lead can pick the right
    specialist by capability, without seeing the actual tool signatures.
    """
    lines = [purpose.strip(), ""]
    lines.append("Worker capabilities (auto-generated from the worker's tools):")
    for t in getattr(subagent, "tools", []) or []:
        desc = (t.description or "").strip().replace("\n", " ")
        first_sentence = desc.split(".")[0].strip()
        lines.append(f"  - {t.name}: {first_sentence}")
    lines.append("")
    lines.append(
        "Provide a clear 'goal' in plain English. Include any of the known-fact "
        "arguments that you already have; omit those you don't. The worker will "
        "compose its tools to answer, or reply 'MISSING: <field>' if it needed "
        "something you didn't pass."
    )
    return "\n".join(lines)


def format_query_message(query_kwargs: dict[str, Any]) -> str:
    """Serialise the lead's structured query into a plain message for the subagent."""
    goal = query_kwargs.get("goal") or ""
    facts = {k: v for k, v in query_kwargs.items() if k != "goal" and v is not None}
    if not facts:
        return f"Goal: {goal}".strip()
    facts_block = "\n".join(f"  - {k}: {v}" for k, v in facts.items())
    return f"Goal: {goal}\n\nKnown facts:\n{facts_block}"
