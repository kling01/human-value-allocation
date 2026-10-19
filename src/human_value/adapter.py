"""Public message adapter. Every analysis unit passes through the shared cleaner."""
from __future__ import annotations

from copy import deepcopy
import json

from human_value._engine.core.cleaner import run_pipeline

ROLES = {"user": "human", "assistant": "ai", "system": "system", "tool": "tool"}


def validate_dialogue(data: dict) -> dict:
    if not isinstance(data, dict) or set(data) - {"id", "messages"}:
        raise ValueError("Input must be an object containing only id and messages.")
    if not isinstance(data.get("id"), str) or not data["id"].strip():
        raise ValueError("A nonempty, full session id is required.")
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("messages must be a nonempty list.")
    seen = set()
    for m in messages:
        if not isinstance(m, dict) or set(m) != {"id", "role", "content"}:
            raise ValueError("Each message must contain exactly id, role and content.")
        if not isinstance(m["id"], str) or not m["id"].strip() or m["id"] in seen:
            raise ValueError("Message ids must be nonempty, unique strings.")
        seen.add(m["id"])
        if not isinstance(m["role"], str) or m["role"] not in ROLES or not isinstance(m["content"], str):
            raise ValueError("Supported roles: user, assistant, system, tool; content must be text.")
    human_count = sum(m["role"] == "user" for m in messages)
    if not human_count:
        raise ValueError("No human messages to analyze.")
    if human_count > 50 or sum(len(m["content"]) for m in messages) > 200_000:
        raise ValueError("Preview limit: 50 human turns and 200,000 characters; split explicitly. Nothing was truncated.")
    return deepcopy(data)


def analysis_units(data: dict) -> list[dict]:
    """One prefix per human message, including its immediate follow-up interval.

    Later human turns are excluded. IDs are serialized tuples, not truncated
    prefixes. The original source IDs are retained verbatim in evaluation_context.
    """
    data = validate_dialogue(data)
    messages = data["messages"]
    positions = [i for i, m in enumerate(messages) if m["role"] == "user"]
    raw = []
    for turn, pos in enumerate(positions):
        end = positions[turn + 1] if turn + 1 < len(positions) else len(messages)
        mid = messages[pos]["id"]
        unit_id = json.dumps([data["id"], mid], ensure_ascii=False, separators=(",", ":"))
        context = {
            "session_id": data["id"], "target_message_id": mid,
            "history": messages[:pos], "human_message": messages[pos],
            "followup": messages[pos + 1:end],
        }
        normalized = [{"type": ROLES[m["role"]], "content": m["content"]} for m in messages[:end]]
        raw.append({
            "id": unit_id, "thread_id": data["id"], "timestamp": "",
            "evaluation_context": {"Metadata": context},
            "observations": [{
                "id": unit_id, "parentObservationId": None, "type": "CHAIN",
                "name": "conversation", "input": {"messages": normalized},
                "output": {"messages": []},
            }],
        })
    cleaned = run_pipeline(raw)
    if len(cleaned) != len(positions):
        raise ValueError("The cleaner dropped an analysis unit; refusing partial input.")
    return cleaned
