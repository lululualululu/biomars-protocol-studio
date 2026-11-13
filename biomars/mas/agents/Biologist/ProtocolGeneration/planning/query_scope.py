"""Apply user scope, verify preservation, restore when required, and reconcile."""
from biomars.mas.assets.protocol_generation.prompts.planning_query_scope import SYSTEM_PROMPT_452

import copy
import json
import re
from logging import Logger
from typing import Any, Dict, List, Optional

from ..protocol_schemas import ProtocolPlan
from .interfaces import PlanningHooks
from .state import PlanningInputs, RepairResult


async def apply_query_scope(
    planner: PlanningHooks,
    inputs: PlanningInputs,
    repaired: RepairResult,
    *,
    logger: Logger,
) -> ProtocolPlan:
    """Apply user scope, verify preservation, restore when required, and reconcile."""
    domain_pack = inputs.domain_pack
    planning_trace = inputs.planning_trace
    analyses = inputs.analyses
    segments_dir = inputs.segments_dir
    cleaned_procedure = repaired.cleaned_procedure
    shape_contract = inputs.shape_contract
    plan = repaired.plan

    query_focus_profile: Dict[str, Any] = {}
    query_focus_applied = False
    query_focus_reverted = False
    query_focus_scope_report: Dict[str, Any] = {}
    query_focus_restored_sections: List[str] = []
    query_focus_steps_before = plan.step_count()
    try:
        pre_focus_plan = copy.deepcopy(plan)
        query_focus_profile = await planner._extract_query_focus_profile(
            plan=plan,
            task_query=domain_pack.task.query,
        )
        if query_focus_profile:
            plan.query_focus_profile = query_focus_profile
            planner._apply_query_focus_profile(plan, query_focus_profile)
            query_focus_applied = True
            try:
                query_focus_scope_report = await planner._verify_query_focus_preservation(
                    task_query=domain_pack.task.query,
                    pre_focus_plan=pre_focus_plan,
                    post_focus_plan=plan,
                    query_focus_profile=query_focus_profile,
                )
                if not query_focus_scope_report:
                    logger.warning(
                        "Query focus scope verification returned no usable decision; reverting."
                    )
                    plan = pre_focus_plan
                    plan.query_focus_profile = query_focus_profile
                    query_focus_reverted = True
                elif not query_focus_scope_report.get("preserves_query_scope", False):
                    query_focus_restored_sections = planner._restore_query_focus_sections(
                        plan=plan,
                        pre_focus_plan=pre_focus_plan,
                        section_names=query_focus_scope_report.get("restore_section_names") or [],
                    )
                    if not query_focus_restored_sections:
                        logger.warning(
                            "Query focus pruning lost required query scope without a valid selective restore; reverting. missing_facets=%s",
                            query_focus_scope_report.get("missing_query_facets", []),
                        )
                        plan = pre_focus_plan
                        plan.query_focus_profile = query_focus_profile
                        query_focus_reverted = True
            except Exception as scope_guard_e:
                logger.warning(
                    "Post-focus query-scope verification failed; reverting: %s",
                    scope_guard_e,
                )
                plan = pre_focus_plan
                plan.query_focus_profile = query_focus_profile
                query_focus_reverted = True

            try:
                post_focus_guard = await planner._verify_parsed_plan_structure(
                    task_query=domain_pack.task.query,
                    procedure_text=cleaned_procedure,
                    plan=plan,
                    analyses=analyses,
                    segments_dir=segments_dir,
                    shape_contract=shape_contract,
                )
                if post_focus_guard.get("should_reparse"):
                    logger.warning(
                        "Query focus pruning introduced structural loss; reverting to pre-focus plan. missing=%s",
                        post_focus_guard.get("missing_structures", []),
                    )
                    plan = pre_focus_plan
                    plan.query_focus_profile = query_focus_profile
                    query_focus_reverted = True
            except Exception as focus_guard_e:
                logger.warning(f"Post-focus structural verification failed (non-blocking): {focus_guard_e}")
    except Exception as e:
        logger.warning(f"Query focus extraction failed (non-blocking): {e}")

    query_focus_reconciled_sections: List[str] = []
    if query_focus_profile:
        query_focus_reconciled_sections = (
            planner._reconcile_query_focus_profile_with_plan(
                plan,
                query_focus_profile,
                restored_section_names=query_focus_restored_sections,
                full_revert=query_focus_reverted,
            )
        )

    planning_trace["checks"]["query_focus"] = {
        "profile_extracted": bool(query_focus_profile),
        "applied": query_focus_applied,
        "reverted": query_focus_reverted,
        "steps_before": query_focus_steps_before,
        "steps_after": plan.step_count(),
        "scope_checked": bool(query_focus_scope_report),
        "missing_query_facets": list(
            query_focus_scope_report.get("missing_query_facets") or []
        ),
        "safe_to_drop_sections": list(
            query_focus_scope_report.get("safe_to_drop_section_names") or []
        ),
        "scope_reason": str(query_focus_scope_report.get("reason") or ""),
        "restored_sections": query_focus_restored_sections,
        "removed_from_drop_after_restore": query_focus_reconciled_sections,
    }

    return plan


# Scope selection and response normalization.


async def extract_query_focus_profile(
    planner: PlanningHooks,
    plan: ProtocolPlan,
    task_query: str,
) -> Dict[str, Any]:
    if not plan or not (plan.sections or plan.blocks):
        return {}

    context_json = json.dumps(planner._build_query_focus_context(plan), indent=2, ensure_ascii=False)
    schema = {
        "type": "object",
        "properties": {
            "scope_mode": {"type": "string"},
            "focus_summary": {"type": "string"},
            "primary_section_names": {"type": "array", "items": {"type": "string"}},
            "supporting_section_names": {"type": "array", "items": {"type": "string"}},
            "drop_section_names": {"type": "array", "items": {"type": "string"}},
            "must_preserve_assessment_readouts": {"type": "boolean"},
            "must_preserve_negative_results": {"type": "boolean"},
            "de_emphasize_notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "scope_mode",
            "focus_summary",
            "primary_section_names",
            "supporting_section_names",
            "drop_section_names",
            "must_preserve_assessment_readouts",
            "must_preserve_negative_results",
            "de_emphasize_notes",
        ],
    }
    prompt = (
        "You are a scientific protocol scope planner.\n"
        "Your job is to identify the true operational focus of the user's query before SOP writing.\n\n"
        f"Task query:\n{task_query}\n\n"
        "Current plan structure (JSON):\n"
        f"{context_json}\n\n"
        "Instructions:\n"
        "1. First decompose every independently requested operation, target, starting material, sample type, modality, and stage. Coordinated requests must remain separate scope facets.\n"
        "2. Match every scope facet to exact Plan section names using section goals and executable step previews rather than title similarity alone. A different earlier, later, or related stage cannot substitute for the requested one.\n"
        "3. Decide whether the complete request is a bounded subprotocol or a full workflow. Do not prefer a smaller output merely because it is shorter.\n"
        "4. Select every core section matched to an explicit query facet as `primary_section_names`; it cannot also be dropped.\n"
        "5. Select exact section names as `supporting_section_names` only when their source-defined steps create the physical input or state consumed by a retained core section.\n"
        "6. Select exact section names as `drop_section_names` only when they are confidently unrelated to every query facet and are not an immediate executable prerequisite.\n"
        "7. Preserve assessment/readout content only if the query requests or operationally depends on it.\n"
        "8. Preserve negative-result logic if it changes what should not be treated as the default SOP.\n"
        "9. Do not invent new section names. Use only exact names from the plan.\n"
        "10. Keep the result biology-general and query-aware, not paper-summary-oriented.\n"
    )
    data: Any = {}
    minimum_keys = {
        "primary_section_names",
        "supporting_section_names",
        "drop_section_names",
    }
    for attempt in range(2):
        data = await planner._call_model(
            system_prompt="You are a query-scope assistant for biological protocol planning.",
            prompt=(
                prompt
                if attempt == 0
                else prompt
                + "\nThe previous response was not a complete JSON object. Return one object with every required key.\n"
            ),
            schema=schema,
            temperature=0.0,
        )
        if isinstance(data, dict) and minimum_keys.issubset(data):
            break
    if not isinstance(data, dict) or not minimum_keys.issubset(data):
        return {}
    return planner._normalize_query_focus_profile(data)


def build_query_focus_context(plan: ProtocolPlan) -> Dict[str, Any]:
    sections = [
        {
            "name": str(section.name or ""),
            "goal": str(section.goal or ""),
            "step_count": len(section.step_ids or []),
        }
        for section in (plan.sections or [])
    ]
    blocks = []
    for block in plan.blocks or []:
        step_previews: List[Dict[str, str]] = []
        for node in block.timeline_nodes or []:
            for step in node.steps or []:
                step_previews.append(
                    {
                        "step_id": str(step.step_id or ""),
                        "timepoint": str(node.timepoint_label or ""),
                        "title": str(step.title or "")[:180],
                        "action": str(step.action or "")[:180],
                    }
                )
        if len(step_previews) > 6:
            step_previews = step_previews[:4] + step_previews[-2:]
        blocks.append(
            {
                "block_id": str(block.block_id or ""),
                "block_type": str(block.block_type or ""),
                "source_preview": str(block.block_source_text or "")[:240],
                "timepoints": [
                    str(node.timepoint_label or "")
                    for node in (block.timeline_nodes or [])
                ],
                "step_previews": step_previews,
            }
        )
    return {
        "sections": sections,
        "blocks": blocks,
        "timeline": {
            "milestones": [
                {
                    "label": str(m.label or ""),
                    "expected_timepoint": str(m.expected_timepoint or ""),
                }
                for m in ((plan.timeline.milestones or []) if plan.timeline else [])
            ],
        },
    }


def normalize_query_focus_profile(planner: PlanningHooks, data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}

    focused_subprotocol = data.get("focused_subprotocol")
    scope_mode = str(data.get("scope_mode") or "").strip().lower()
    if not scope_mode:
        scope_mode = "focused_subprotocol" if focused_subprotocol else "full_workflow"

    operational_focus = data.get("operational_focus") or {}
    focus_summary = str(data.get("focus_summary") or "").strip()
    if not focus_summary and isinstance(operational_focus, dict):
        focus_summary = str(operational_focus.get("core_task") or "").strip()
    primary_section_names = [str(x) for x in (data.get("primary_section_names") or []) if str(x).strip()]
    supporting_section_names = [str(x) for x in (data.get("supporting_section_names") or []) if str(x).strip()]
    drop_section_names = [str(x) for x in (data.get("drop_section_names") or []) if str(x).strip()]
    retained_keys = {
        planner._normalize_section_label(name)
        for name in primary_section_names + supporting_section_names
    }
    drop_section_names = [
        name
        for name in drop_section_names
        if planner._normalize_section_label(name) not in retained_keys
    ]
    if scope_mode == "full_workflow" and primary_section_names and drop_section_names:
        scope_mode = "focused_subprotocol"
    if not focus_summary and primary_section_names:
        focus_summary = primary_section_names[0]

    normalized = {
        "scope_mode": scope_mode,
        "focus_summary": focus_summary,
        "primary_section_names": primary_section_names,
        "supporting_section_names": supporting_section_names,
        "drop_section_names": drop_section_names,
        "must_preserve_assessment_readouts": bool(
            data.get("must_preserve_assessment_readouts")
            if data.get("must_preserve_assessment_readouts") is not None
            else ("recovery" in focus_summary.lower() or "assessment" in str(operational_focus).lower())
        ),
        "must_preserve_negative_results": bool(data.get("must_preserve_negative_results")),
        "de_emphasize_notes": [str(x) for x in (data.get("de_emphasize_notes") or []) if str(x).strip()],
    }
    if isinstance(operational_focus, dict) and operational_focus:
        normalized["operational_focus"] = operational_focus
    return normalized


def normalize_section_label(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


# Apply scope, verify removed sections, and restore when required.


def apply_query_focus_profile(
    planner: PlanningHooks,
    plan: ProtocolPlan,
    profile: Dict[str, Any],
) -> None:
    if not plan or not isinstance(profile, dict):
        return

    scope_mode = str(profile.get("scope_mode") or "").strip().lower()
    if scope_mode == "full_workflow":
        return

    section_name_map = {
        planner._normalize_section_label(section.name): str(section.name or "")
        for section in (plan.sections or [])
        if str(section.name or "").strip()
    }
    primary = {
        section_name_map[planner._normalize_section_label(name)]
        for name in (profile.get("primary_section_names") or [])
        if planner._normalize_section_label(name) in section_name_map
    }
    supporting = {
        section_name_map[planner._normalize_section_label(name)]
        for name in (profile.get("supporting_section_names") or [])
        if planner._normalize_section_label(name) in section_name_map
    }
    dropped = {
        section_name_map[planner._normalize_section_label(name)]
        for name in (profile.get("drop_section_names") or [])
        if planner._normalize_section_label(name) in section_name_map
    }

    keep_names = (primary | supporting) - dropped
    if not keep_names:
        return

    plan.sections = [section for section in (plan.sections or []) if str(section.name or "") in keep_names]
    plan.blocks = [block for block in (plan.blocks or []) if str(block.block_type or "") in keep_names]

    block_ids = {str(block.block_id or "") for block in (plan.blocks or [])}
    section_names = {str(section.name or "") for section in (plan.sections or [])}
    for block in (plan.blocks or []):
        new_nodes = []
        for node in (block.timeline_nodes or []):
            node.steps = [
                step
                for step in (node.steps or [])
                if (
                    (str(step.phase or "") and str(step.phase or "") in section_names)
                    or (
                        str(step.block_ref_id or "")
                        and str(step.block_ref_id or "") in block_ids
                    )
                )
            ]
            if node.steps:
                new_nodes.append(node)
        block.timeline_nodes = new_nodes

    plan.bind_hierarchical_steps_as_canonical()
    kept_step_ids = set(plan.step_index())
    for section in (plan.sections or []):
        section.step_ids = [
            sid for sid in (section.step_ids or []) if sid in kept_step_ids
        ]


async def verify_query_focus_preservation(
    planner: PlanningHooks,
    task_query: str,
    pre_focus_plan: ProtocolPlan,
    post_focus_plan: ProtocolPlan,
    query_focus_profile: Dict[str, Any],
) -> Dict[str, Any]:
    """Check that focus pruning kept every scope facet stated by the query."""
    before_sections = [
        str(section.name or "").strip()
        for section in (pre_focus_plan.sections or [])
        if str(section.name or "").strip()
    ]
    after_sections = {
        str(section.name or "").strip()
        for section in (post_focus_plan.sections or [])
        if str(section.name or "").strip()
    }
    removed_sections = [
        name for name in before_sections if name not in after_sections
    ]
    if not removed_sections:
        return {
            "preserves_query_scope": True,
            "required_query_facets": [],
            "missing_query_facets": [],
            "safe_to_drop_section_names": [],
            "restore_section_names": [],
            "reason": "focus_pruning_removed_no_sections",
        }

    schema = {
        "type": "object",
        "properties": {
            "preserves_query_scope": {"type": "boolean"},
            "required_query_facets": {
                "type": "array",
                "items": {"type": "string"},
            },
            "missing_query_facets": {
                "type": "array",
                "items": {"type": "string"},
            },
            "safe_to_drop_section_names": {
                "type": "array",
                "items": {"type": "string"},
            },
            "restore_section_names": {
                "type": "array",
                "items": {"type": "string"},
            },
            "reason": {"type": "string"},
        },
        "required": [
            "preserves_query_scope",
            "required_query_facets",
            "missing_query_facets",
            "safe_to_drop_section_names",
            "restore_section_names",
            "reason",
        ],
    }
    system_prompt = (
        SYSTEM_PROMPT_452
    )
    prompt = (
            f"Task query:\n{task_query}\n\n"
            "Query-focus profile (JSON):\n"
            f"{json.dumps(query_focus_profile or {}, indent=2, ensure_ascii=False)}\n\n"
            "Plan before focus pruning (JSON):\n"
            f"{json.dumps(planner._build_query_focus_context(pre_focus_plan), indent=2, ensure_ascii=False)}\n\n"
            "Plan after focus pruning (JSON):\n"
            f"{json.dumps(planner._build_query_focus_context(post_focus_plan), indent=2, ensure_ascii=False)}\n\n"
            f"Exact removed section names:\n{json.dumps(removed_sections, ensure_ascii=False)}\n\n"
            "Instructions:\n"
            "1. Decompose coordinated operations, targets, starting materials, sample types, modalities, and requested stages into separate facets. Distinct requested operations or stages cannot substitute for one another.\n"
            "2. Check whether the post-pruning plan still covers every facet with executable steps, not merely with a related title.\n"
            "3. For each removed section, decide individually whether it is safe to drop or must be restored. Every exact removed section name must appear in exactly one of `safe_to_drop_section_names` or `restore_section_names`.\n"
            "4. Restore a removed immediate prerequisite when its steps create the physical input or state consumed by retained steps and no retained section provides that input.\n"
            "5. A retained earlier, later, or related operation does not satisfy a different requested operation. A generic shared workflow does not replace a removed sample-specific preparation when the query names that sample type.\n"
            "6. Compare the step previews before restoring. Do not restore an umbrella or mixed-purpose section merely because one or two of its actions duplicate actions already present in a retained facet-specific section. Restore a whole section only when its main workflow is required and materially absent.\n"
            "7. Mark background, optional validation, unrelated downstream applications, genuinely unrequested lifecycle sections, and redundant mixed wrappers as safe to drop.\n"
            "8. Use only exact names from `Exact removed section names`; do not invent section names or domain requirements.\n"
            "Return JSON only with every key in this shape:\n"
            '{"preserves_query_scope": true, "required_query_facets": ["..."], '
            '"missing_query_facets": [], "safe_to_drop_section_names": ["..."], '
            '"restore_section_names": [], "reason": "..."}\n'
    )
    result: Any = {}
    required_keys = set(schema["required"])
    for attempt in range(2):
        result = await planner._call_model(
            system_prompt=system_prompt,
            prompt=(
                prompt
                if attempt == 0
                else prompt
                + "\nThe previous response was structurally invalid. Return one complete object matching the schema.\n"
            ),
            schema=schema,
            temperature=0.0,
        )
        if isinstance(result, dict) and required_keys.issubset(result):
            break
    if not isinstance(result, dict) or not required_keys.issubset(result):
        return {}

    removed_by_key = {
        planner._normalize_section_label(name): name for name in removed_sections
    }
    restore_names: List[str] = []
    for value in result.get("restore_section_names") or []:
        exact = removed_by_key.get(planner._normalize_section_label(str(value)))
        if exact and exact not in restore_names:
            restore_names.append(exact)
    safe_to_drop_names: List[str] = []
    for value in result.get("safe_to_drop_section_names") or []:
        exact = removed_by_key.get(planner._normalize_section_label(str(value)))
        if exact and exact not in safe_to_drop_names:
            safe_to_drop_names.append(exact)

    # Every destructive decision must be explicit. Restore wins when the
    # model placed a section in both lists, and any omitted section is
    # conservatively restored instead of being silently discarded.
    restore_keys = {
        planner._normalize_section_label(name) for name in restore_names
    }
    safe_to_drop_names = [
        name
        for name in safe_to_drop_names
        if planner._normalize_section_label(name) not in restore_keys
    ]
    classified_keys = restore_keys | {
        planner._normalize_section_label(name) for name in safe_to_drop_names
    }
    unclassified_names = [
        name
        for name in removed_sections
        if planner._normalize_section_label(name) not in classified_keys
    ]
    restore_names.extend(unclassified_names)
    missing_facets = [
        str(value).strip()
        for value in (result.get("missing_query_facets") or [])
        if str(value).strip()
    ]
    preserves_scope = bool(result.get("preserves_query_scope"))
    if missing_facets or restore_names:
        preserves_scope = False
    reason = str(result.get("reason") or "").strip()
    if unclassified_names:
        reason = (
            f"{reason}; " if reason else ""
        ) + "unclassified_removed_sections_restored"
    return {
        "preserves_query_scope": preserves_scope,
        "required_query_facets": [
            str(value).strip()
            for value in (result.get("required_query_facets") or [])
            if str(value).strip()
        ],
        "missing_query_facets": missing_facets,
        "safe_to_drop_section_names": safe_to_drop_names,
        "restore_section_names": restore_names,
        "reason": reason,
    }


def restore_query_focus_sections(
    planner: PlanningHooks,
    plan: ProtocolPlan,
    pre_focus_plan: ProtocolPlan,
    section_names: List[str],
) -> List[str]:
    """Restore only query-required sections from the pre-pruning snapshot."""
    requested = {
        planner._normalize_section_label(name)
        for name in (section_names or [])
        if planner._normalize_section_label(name)
    }
    if not requested:
        return []

    restore_block_ids = {
        str(block.block_id or "")
        for block in (pre_focus_plan.blocks or [])
        if planner._normalize_section_label(str(block.block_type or "")) in requested
    }
    current_block_ids = {
        str(block.block_id or "") for block in (plan.blocks or [])
    }
    selected_section_names = {
        planner._normalize_section_label(str(section.name or ""))
        for section in (plan.sections or [])
    } | requested
    selected_block_ids = current_block_ids | restore_block_ids
    if not restore_block_ids:
        return []

    plan.blocks = [
        copy.deepcopy(block)
        for block in (pre_focus_plan.blocks or [])
        if str(block.block_id or "") in selected_block_ids
    ]
    plan.sections = [
        copy.deepcopy(section)
        for section in (pre_focus_plan.sections or [])
        if planner._normalize_section_label(str(section.name or ""))
        in selected_section_names
    ]
    plan.bind_hierarchical_steps_as_canonical()

    restored_names: List[str] = []
    for block in plan.blocks or []:
        name = str(block.block_type or "").strip()
        if str(block.block_id or "") in restore_block_ids and name not in restored_names:
            restored_names.append(name)
    return restored_names


def reconcile_query_focus_profile_with_plan(
    planner: PlanningHooks,
    plan: ProtocolPlan,
    profile: Dict[str, Any],
    *,
    restored_section_names: Optional[List[str]] = None,
    full_revert: bool = False,
) -> List[str]:
    """Remove stale drop decisions for blocks restored by a safety guard."""
    active_block_keys = {
        planner._normalize_section_label(str(block.block_type or ""))
        for block in (plan.blocks or [])
        if str(block.block_type or "").strip()
    }
    restored_keys = {
        planner._normalize_section_label(str(name))
        for name in (restored_section_names or [])
        if str(name).strip()
    }
    drop_names = [
        str(name).strip()
        for name in (profile.get("drop_section_names") or [])
        if str(name).strip()
    ]
    restored = [
        name
        for name in drop_names
        if planner._normalize_section_label(name) in active_block_keys
        and (
            full_revert
            or planner._normalize_section_label(name) in restored_keys
        )
    ]
    if restored:
        restored_drop_keys = {
            planner._normalize_section_label(name) for name in restored
        }
        profile["drop_section_names"] = [
            name
            for name in drop_names
            if planner._normalize_section_label(name) not in restored_drop_keys
        ]
    plan.query_focus_profile = profile
    return restored
