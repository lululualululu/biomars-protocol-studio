"""Shared Plan checks: timeline coverage, structure, and source evidence.

Checks report evidence and omissions; accepted Plan mutations live in plan_repairs.
The Agent retains compatibility delegates for these functions.
"""

import json
import re
from logging import Logger
from typing import Any, Dict, List, Optional, Set

from ..protocol_schemas import OperationBlock, ProtocolPlan
from .interfaces import PlanningHooks


# Timeline and structure coverage.


def audit_semantic_timeline_coverage(
    planner: PlanningHooks,
    anchors: List[Dict[str, Any]],
    plan: ProtocolPlan,
) -> Dict[str, Any]:
    """Verify the LLM timing inventory through a strict, non-biological contract."""
    expected = [
        item
        for item in anchors
        if isinstance(item, dict)
        and str(item.get("anchor_id") or "").strip()
        and str(item.get("label") or "").strip()
    ]
    if not expected:
        return {"covered_anchor_ids": [], "missing_anchor_ids": []}
    observed: List[str] = []
    for block in plan.blocks or []:
        for node in block.timeline_nodes or []:
            observed.append(
                planner._normalize_for_match(str(node.timepoint_label or ""))
            )
    used_node_indices: Set[int] = set()
    covered: List[str] = []
    missing: List[str] = []
    for item in expected:
        anchor_id = str(item.get("anchor_id") or "")
        label_key = planner._normalize_for_match(str(item.get("label") or ""))
        candidates = [
            index
            for index, node in enumerate(observed)
            if index not in used_node_indices
            and label_key
            and (
                label_key == node
                or (len(label_key) >= 5 and label_key in node)
                or (len(node) >= 5 and node in label_key)
            )
        ]
        if candidates:
            used_node_indices.add(candidates[0])
            covered.append(anchor_id)
        else:
            missing.append(anchor_id)
    return {
        "covered_anchor_ids": covered,
        "missing_anchor_ids": missing,
        "semantic_audit_applied": False,
    }


def summarize_plan_outline(plan: ProtocolPlan) -> Dict[str, Any]:
    blocks: List[Dict[str, Any]] = []
    for block in (plan.blocks or []):
        node_summaries: List[Dict[str, Any]] = []
        for node in (block.timeline_nodes or []):
            node_summaries.append(
                {
                    "timepoint_label": str(node.timepoint_label or ""),
                    "step_titles": [str(step.title or step.description or "") for step in (node.steps or [])[:5]],
                }
            )
        blocks.append(
            {
                "block_type": str(block.block_type or ""),
                "timeline_nodes": node_summaries,
            }
        )
    return {"blocks": blocks}


async def verify_parsed_plan_structure(
    planner: PlanningHooks,
    task_query: str,
    procedure_text: str,
    plan: ProtocolPlan,
    analyses: List[Dict[str, Any]],
    segments_dir: str,
    shape_contract: Optional[Dict[str, Any]] = None,
    *,
    logger: Logger,
) -> Dict[str, Any]:
    shape_contract = shape_contract or planner._infer_protocol_shape_contract(task_query, procedure_text)
    schema = {
        "type": "object",
        "properties": {
            "required_structures": {"type": "array", "items": {"type": "string"}},
            "missing_structures": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "type": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["label", "type", "reason"],
                },
            },
            "collapsed_branches": {"type": "array", "items": {"type": "string"}},
            "collapsed_cycles": {"type": "array", "items": {"type": "string"}},
            "rescue_segment_ids": {"type": "array", "items": {"type": "string"}},
            "should_reparse": {"type": "boolean"},
        },
        "required": [
            "required_structures",
            "missing_structures",
            "collapsed_branches",
            "collapsed_cycles",
            "rescue_segment_ids",
            "should_reparse",
        ],
    }
    try:
        result = await planner._call_model(
            system_prompt="You audit parsed protocol plans for structure preservation.",
            prompt=(
                "Task: verify whether the parsed plan preserves the required structure from the selected procedure view.\n\n"
                f"Query:\n{task_query}\n\n"
                f"Procedure headings:\n{json.dumps(planner._extract_view_headings(procedure_text), ensure_ascii=False)}\n\n"
                f"Procedure excerpt:\n{procedure_text[:7000]}\n\n"
                f"Shape contract (JSON):\n{json.dumps(shape_contract, indent=2, ensure_ascii=False)}\n\n"
                f"Parsed plan summary (JSON):\n{json.dumps(planner._summarize_plan_outline(plan), indent=2, ensure_ascii=False)}\n\n"
                f"Available segment catalog (JSON):\n{json.dumps(planner._compact_segment_catalog(analyses), indent=2, ensure_ascii=False)}\n\n"
                "Instructions:\n"
                "1. Determine whether the plan preserves setup, early timeline stages, explicit branches, cyclic logic, and any shape-contract structures required by the query and procedure view.\n"
                "2. If the plan dropped or collapsed required structure, return it explicitly.\n"
                "3. If reparsing could help, set should_reparse=true and return only necessary rescue_segment_ids from the catalog, ordered by importance. The character budget limits repair size.\n"
                "4. Do not invent segment ids.\n"
                "5. Only executable workflow structure is required. Background, introduction, preamble, rationale, materials/equipment lists, results, discussion, journal headers, and references are not missing SOP stages. An actual setup operation remains executable even if it uses materials or equipment.\n"
                "6. A note attached to an existing operation is supporting evidence, not an independent stage merely because it appears early or late in the source.\n"
                "7. Every reported label must be grounded in the procedure text or the supplied semantic shape contract; do not invent a paraphrased section.\n"
                "8. A document-container heading does not require a same-named plan block when its executable child stages are already represented. Report it missing only when executable content under that container is absent.\n"
            ),
            schema=schema,
            temperature=0.0,
        )
        if isinstance(result, dict):
            normalized_missing: List[Dict[str, str]] = []
            for item in (result.get("missing_structures") or []):
                if isinstance(item, dict):
                    label = str(item.get("label") or item.get("missing_item") or "").strip()
                    kind = str(item.get("type") or "").strip() or "structure"
                    reason = str(item.get("reason") or "").strip()
                else:
                    continue
                if label:
                    normalized_missing.append({"label": label, "type": kind, "reason": reason})
            contract_by_label = {
                str(item.get("label") or "").strip().lower(): [
                    str(alias)
                    for alias in (item.get("aliases") or [])
                    if str(alias).strip()
                ]
                for item in (shape_contract.get("required_structures") or [])
                if isinstance(item, dict)
                and str(item.get("label") or "").strip()
            }
            source_lower = (procedure_text or "").lower()
            normalized_missing = [
                item
                for item in normalized_missing
                if (
                    str(item.get("label") or "").strip().lower() in source_lower
                    or str(item.get("label") or "").strip().lower() in contract_by_label
                )
            ]
            result["missing_structures"] = normalized_missing
            result["required_structures"] = [
                str(item.get("label") or item.get("missing_item") or "").strip() if isinstance(item, dict) else str(item).strip()
                for item in (result.get("required_structures") or [])
            ]
            result["required_structures"] = [item for item in result["required_structures"] if item]
            result["collapsed_branches"] = [str(item).strip() for item in (result.get("collapsed_branches") or []) if str(item).strip()]
            result["collapsed_cycles"] = [str(item).strip() for item in (result.get("collapsed_cycles") or []) if str(item).strip()]
            allowed_segment_ids = {
                str(item.get("segment_id") or "")
                for item in (analyses or [])
                if isinstance(item, dict) and str(item.get("segment_id") or "")
            }
            rescue_segment_ids = [
                str(x)
                for x in (result.get("rescue_segment_ids") or [])
                if str(x).strip() in allowed_segment_ids
            ]
            result["rescue_segment_ids"] = rescue_segment_ids
            result["should_reparse"] = bool(
                result["missing_structures"]
                or result["collapsed_branches"]
                or result["collapsed_cycles"]
            )
            return result
    except Exception as e:
        logger.warning(
            "Semantic plan verifier unavailable; leaving the parsed plan unchanged: %s",
            e,
        )
    return {
        "required_structures": [],
        "missing_structures": [],
        "collapsed_branches": [],
        "collapsed_cycles": [],
        "rescue_segment_ids": [],
        "should_reparse": False,
    }


def structure_issue_score(guard: Dict[str, Any]) -> int:
    if not isinstance(guard, dict):
        return 0
    return (
        len(guard.get("missing_structures") or [])
        + len(guard.get("collapsed_branches") or [])
        + len(guard.get("collapsed_cycles") or [])
    )


# Source evidence and traceability.


def text_supports_span(
    planner: PlanningHooks,
    reference_text: str,
    candidate_text: str,
) -> bool:
    ref = planner._normalize_for_match(reference_text.replace("*", ""))
    cand = planner._normalize_for_match(candidate_text.replace("*", ""))
    if not ref or not cand:
        return False
    if cand in ref:
        return True
    probe = cand[:120].strip()
    if probe and probe in ref:
        return True
    tokens = [tok for tok in re.findall(r"[a-z0-9]+", cand) if len(tok) >= 3]
    if not tokens:
        return False
    window = " ".join(tokens[:8]).strip()
    if window and window in ref:
        return True
    unique = []
    seen = set()
    for tok in tokens[:10]:
        if tok not in seen:
            seen.add(tok)
            unique.append(tok)
    matches = sum(1 for tok in unique if tok in ref)
    return matches >= min(6, len(unique)) and len(unique) >= 6


def text_supported_by_grounding_views(
    planner: PlanningHooks,
    candidate_text: str,
    procedure_text: str,
    supporting_view_texts: Optional[Dict[str, str]] = None,
) -> bool:
    if planner._text_supports_span(procedure_text, candidate_text):
        return True
    return any(
        planner._text_supports_span(str(view_text or ""), candidate_text)
        for view_text in (supporting_view_texts or {}).values()
        if str(view_text or "").strip()
    )


def collect_unsupported_steps(
    planner: PlanningHooks,
    plan: ProtocolPlan,
    reference_text: str,
    supporting_view_texts: Optional[Dict[str, str]] = None,
) -> List[str]:
    unsupported: List[str] = []
    for step in plan.iter_steps():
        evidence_text = step.source_sentence or step.source_text or ""
        if not planner._text_supported_by_grounding_views(
            evidence_text,
            reference_text,
            supporting_view_texts,
        ):
            unsupported.append(str(step.step_id or step.title or ""))
    return unsupported


def block_has_traceable_evidence(
    planner: PlanningHooks,
    block: OperationBlock,
    procedure_text: str,
) -> bool:
    for group in (block.timeline_nodes or []):
        for step in (group.steps or []):
            evidence_text = step.source_sentence or step.source_text or ""
            if planner._text_supports_span(procedure_text, evidence_text):
                return True
    return False


def collect_untraceable_block_labels(
    planner: PlanningHooks,
    plan: ProtocolPlan,
    procedure_text: str,
) -> List[Dict[str, str]]:
    issues: List[Dict[str, str]] = []
    proc_lower = (procedure_text or "").lower()
    for block in (plan.blocks or []):
        block_type = str(block.block_type or "").strip()
        if not block_type:
            continue
        if planner._block_has_traceable_evidence(block, procedure_text):
            continue
        block_key = planner._normalize_for_match(block_type)
        if block_key and block_key not in planner._normalize_for_match(proc_lower):
            tokens = [tok for tok in re.findall(r"[a-z0-9]+", block_type.lower()) if len(tok) >= 4]
            if tokens and not any(tok in proc_lower for tok in tokens):
                continue
        issues.append(
            {
                "label": block_type,
                "type": "traceability",
                "reason": f"Block '{block_type}' lacks any step with source_text/source_sentence traceable to the current procedure view.",
            }
        )
    return issues


# Diagnostic report formatting.


def summarize_guard_categories(guard: Dict[str, Any]) -> Dict[str, List[str]]:
    summary = {
        "stage_coverage": [],
        "timeline_coverage": [],
        "branch_coverage": [],
        "tail_coverage": [],
        "traceability_coverage": [],
        "loop_coverage": [],
    }
    for item in (guard.get("missing_structures") or []):
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        kind = str(item.get("type") or "").strip()
        if not label:
            continue
        if kind in {"timepoint"}:
            summary["timeline_coverage"].append(label)
        elif kind in {"branch"}:
            summary["branch_coverage"].append(label)
        elif kind in {"tail_stage"}:
            summary["tail_coverage"].append(label)
        elif kind in {"traceability"}:
            summary["traceability_coverage"].append(label)
        elif kind in {"loop", "final_cycle"}:
            summary["loop_coverage"].append(label)
        else:
            summary["stage_coverage"].append(label)
    for label in (guard.get("collapsed_branches") or []):
        text = str(label).strip()
        if text and text not in summary["branch_coverage"]:
            summary["branch_coverage"].append(text)
    for label in (guard.get("collapsed_cycles") or []):
        text = str(label).strip()
        if text and text not in summary["loop_coverage"]:
            summary["loop_coverage"].append(text)
    return summary
