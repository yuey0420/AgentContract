"""Small, deterministic task planner used as the managed-task fallback.

The planner intentionally does not call an LLM. A model-backed planner can be
injected into ``ProcessManager`` later, while the runtime keeps a bounded and
auditable fallback for simple objectives.
"""

from __future__ import annotations

import re
import json
import hashlib
from typing import Any

from ..contracts.models import TaskContract


_SEPARATORS = re.compile(r"(?:\r?\n+|[;；]+|\s+then\s+|\s+and then\s+)", re.IGNORECASE)


def _parts(objective: str, max_children: int) -> list[str]:
    pieces = [piece.strip(" \t-•") for piece in _SEPARATORS.split(objective or "")]
    pieces = [piece for piece in pieces if piece]
    if not pieces:
        return ["完成受管任务"]
    if len(pieces) <= max_children:
        return pieces
    return pieces[: max_children - 1] + ["；".join(pieces[max_children - 1 :])]


def decompose_objective(
    objective: str,
    task_id: str,
    *,
    max_children: int = 8,
    contract: TaskContract | None = None,
) -> list[dict[str, Any]]:
    """Return a bounded task tree with a root and ordered child steps."""
    if max_children < 1:
        raise ValueError("max_children must be positive")

    pieces = _parts(objective, max_children)
    root = {
        "node_id": task_id,
        "parent_node_id": None,
        "title": objective.strip() or "受管任务",
        "objective": objective.strip() or "完成受管任务",
        "sequence": 0,
        "depends_on": [],
        "status": "pending",
        "depth": 0,
    }
    if len(pieces) == 1:
        return [root]
    nodes: list[dict[str, Any]] = [root]
    for index, piece in enumerate(pieces, start=1):
        nodes.append({
            "node_id": f"{task_id}.{index}",
            "parent_node_id": task_id,
            "title": piece[:120],
            "objective": piece,
            "sequence": index,
            "depends_on": [],
            "status": "pending",
            "depth": 1,
        })
    return nodes


def _parse_json_object(content: Any) -> Any:
    text = str(content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
    return json.loads(text)


def validate_model_plan(
    plan: Any,
    task_id: str,
    *,
    max_children: int,
) -> list[dict[str, Any]]:
    """Normalize and validate model output before it enters the runtime."""
    raw_nodes = plan.get("tasks") if isinstance(plan, dict) else plan
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("planner output must contain a non-empty tasks list")
    if len(raw_nodes) > max_children:
        raise ValueError("planner output exceeds max_subtasks")

    nodes: list[dict[str, Any]] = [{
        "node_id": task_id,
        "parent_node_id": None,
        "title": "根任务",
        "objective": "按规划完成用户目标",
        "sequence": 0,
        "depends_on": [],
        "status": "pending",
        "depth": 0,
    }]
    seen: set[str] = set()
    for index, raw in enumerate(raw_nodes, start=1):
        if not isinstance(raw, dict) or not str(raw.get("objective") or "").strip():
            raise ValueError("each planned task requires an objective")
        node_id = str(raw.get("id") or f"{task_id}.{index}")
        if node_id in seen or node_id == task_id:
            raise ValueError("planner produced duplicate task ids")
        seen.add(node_id)
        dependencies = raw.get("depends_on") or []
        if not isinstance(dependencies, list) or any(str(item) not in seen for item in dependencies):
            raise ValueError("planner dependencies must reference earlier tasks")
        nodes.append({
            "node_id": node_id,
            "parent_node_id": task_id,
            "title": str(raw.get("title") or raw["objective"])[:120],
            "objective": str(raw["objective"]).strip(),
            "sequence": index,
            "depends_on": [str(item) for item in dependencies],
            "acceptance": raw.get("acceptance") or [],
            "status": "pending",
            "depth": 1,
        })
    return nodes


def model_decompose_objective(
    llm: Any,
    objective: str,
    task_id: str,
    contract: TaskContract,
    *,
    max_children: int = 8,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Ask a model for a bounded task DAG and return auditable planner metadata."""
    from langchain_core.messages import HumanMessage, SystemMessage

    system = (
        "You are a task planner. Return JSON only, with a top-level `tasks` array. "
        "Each task must have objective, optional title, depends_on (IDs of earlier tasks), "
        "and optional acceptance. Do not invent permissions, files, tools, or deliverables "
        "outside the contract. Keep the task count within the requested limit."
    )
    request = {
        "objective": objective,
        "contract": {
            "objective": contract.objective,
            "deliverables": contract.deliverables,
            "scope": contract.scope.model_dump(mode="json"),
            "acceptance": [rule.model_dump(mode="json") for rule in contract.acceptance],
        },
        "max_subtasks": max_children,
    }
    response = llm.invoke([
        SystemMessage(content=system),
        HumanMessage(content=json.dumps(request, ensure_ascii=False)),
    ])
    raw_content = getattr(response, "content", response)
    parsed = _parse_json_object(raw_content)
    nodes = validate_model_plan(parsed, task_id, max_children=max_children)
    plan_hash = hashlib.sha256(
        json.dumps(nodes, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return nodes, {
        "mode": "model",
        "plan_hash": f"sha256:{plan_hash}",
        "model_response_type": type(response).__name__,
    }
