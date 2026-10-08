"""Application-owned retrieval limits and bounded evidence context."""
import json
from collections import defaultdict
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RetrievalLimits:
    chunks: int
    recent_sources: int
    tasks: int
    decisions: int


DEPTHS = {
    "focused": RetrievalLimits(6, 5, 10, 10),
    "normal": RetrievalLimits(12, 10, 20, 20),
    "broad": RetrievalLimits(18, 15, 30, 30),
}


def effective_limits(plan):
    limits = asdict(DEPTHS[plan.retrieval_depth])
    if plan.recent_sources_limit is not None:
        limits["recent_sources"] = min(limits["recent_sources"], plan.recent_sources_limit)
    return limits


def diverse_chunks(items, limit):
    """Round-robin projects, each bucket ordered by cosine relevance; null is a bucket."""
    groups = defaultdict(list)
    for item in sorted(items, key=lambda x: (x["distance"], x["chunk_id"])):
        groups[item.get("project_id")].append(item)
    buckets = sorted(groups.values(), key=lambda group: (group[0]["distance"], group[0]["chunk_id"]))
    selected = []
    while buckets and len(selected) < limit:
        remaining = []
        for bucket in buckets:
            if len(selected) == limit:
                break
            selected.append(bucket.pop(0))
            if bucket:
                remaining.append(bucket)
        buckets = remaining
    return selected


def context_size(context):
    return len(json.dumps(context, ensure_ascii=False))


def bound_context(context, plan, limits, max_chars):
    keys = ("tasks", "decisions", "updates", "chunks", "recent_sources", "projects", "project_memories", "new_sources", "task_states")
    original = {key: list(context.get(key, [])) for key in keys}
    result = {key: value for key, value in context.items() if key not in keys}
    result["warnings"] = list(context["warnings"])
    result.update({key: [] for key in keys})
    info = {"retrieval_depth": plan.retrieval_depth, "effective_limits": limits,
            "candidate_counts": {key: len(original[key]) for key in keys},
            "context_max_chars": max_chars, "budget_reduced": False}
    result["retrieval"] = info
    # Reserve space for counts, coverage warnings and final length metadata.
    reserve = 1200
    ordered = [("task_states", item) for item in original["task_states"]]
    ordered += [("new_sources", item) for item in original["new_sources"]]
    ordered += [("project_memories", item) for item in original["project_memories"]]
    for index in range(max(len(original[k]) for k in ("tasks", "decisions", "updates"))):
        for key in ("tasks", "decisions", "updates"):
            if index < len(original[key]):
                ordered.append((key, original[key][index]))
    ordered += [(key, item) for key in ("chunks", "recent_sources", "projects") for item in original[key]]
    accepted = []
    for key, item in ordered:
        if key == "project_memories" and item.get("is_dirty"):
            required = [delta for delta in original["new_sources"] if delta.get("project_id") == item.get("project_id")]
            if any(delta not in result["new_sources"] for delta in required):
                info["budget_reduced"] = True
                continue  # Never keep old memory when its conflicting delta could not fit.
        result[key].append(item)
        if context_size(result) > max_chars - reserve:
            result[key].pop()
            info["budget_reduced"] = True
        else:
            accepted.append(key)
    if len(result["task_states"]) < len(original["task_states"]):
        result["sql_state_coverage"] = {"complete": False}
    if len(result["chunks"]) < len(original["chunks"]) and result.get("semantic_coverage", {}).get("requested"):
        result["semantic_coverage"] = result["semantic_coverage"] | {"material": True, "status": "budget_limited"}
    if info["budget_reduced"]:
        result["warnings"].append("Cobertura parcial: el presupuesto de contexto redujo la evidencia recuperada.")
    info["counts"] = {key: len(result[key]) for key in keys}
    info["context_chars"] = 0
    for _ in range(3):
        info["context_chars"] = context_size(result)
    while context_size(result) > max_chars and accepted:
        result[accepted.pop()].pop()
        info["budget_reduced"] = True
        info["counts"] = {key: len(result[key]) for key in keys}
        info["context_chars"] = context_size(result)
    if len(result["task_states"]) < len(original["task_states"]):
        result["sql_state_coverage"] = {"complete": False}
    if len(result["chunks"]) < len(original["chunks"]) and result.get("semantic_coverage", {}).get("requested"):
        result["semantic_coverage"] = result["semantic_coverage"] | {"material": True, "status": "budget_limited"}
    if context_size(result) > max_chars:
        raise ValueError("Context metadata exceeds the configured budget.")
    return result
