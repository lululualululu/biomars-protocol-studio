"""Plan repair orchestration and its timeline, grounding, and structure stages."""

import copy
import json
import os
import re
from bisect import bisect_left
from logging import Logger
from typing import Any, Dict, List, Optional, Set, Tuple

from ..protocol_schemas import (
    GroundedParamStatus, OperationBlock, ProtocolPlan, ProtocolSectionPlan,
    ProtocolStepSkeleton, TimepointGroup,
)
from .interfaces import PlanningHooks
from .state import PlanningInputs, ParsedPlan, RepairResult


async def repair_plan(
    planner: PlanningHooks,
    inputs: PlanningInputs,
    draft: ParsedPlan,
    *,
    logger: Logger,
) -> RepairResult:
    """Repair in the original order, passing each accepted Plan to the next stage."""
    plan = await _repair_timeline(planner, inputs, draft, logger=logger)
    plan = await _normalize_and_ground(planner, inputs, plan, logger=logger)
    return await _repair_structure(planner, inputs, draft, plan, logger=logger)


async def _repair_timeline(
    planner: PlanningHooks,
    inputs: PlanningInputs,
    draft: ParsedPlan,
    *,
    logger: Logger,
) -> ProtocolPlan:
    """Audit timeline anchors, retry, and merge source-grounded missing nodes."""
    domain_pack = inputs.domain_pack
    evidence_bundle = inputs.evidence_bundle
    primary_id = inputs.primary_id
    planning_trace = inputs.planning_trace
    segment_analysis_bundle = inputs.segment_analysis_bundle
    template = inputs.template
    segment_analysis_text = inputs.segment_analysis_text
    cleaned_segment_index = inputs.cleaned_segment_index
    cleaned_procedure = inputs.cleaned_procedure
    parse_max_procedure = inputs.parse_max_procedure
    plan = draft.plan
    parsing_prompt = draft.parsing_prompt
    base_system = draft.base_system
    expected_timeline_anchors = draft.expected_timeline_anchors

    timepoint_retry_applied = False
    timing_audit: Dict[str, Any] = {
        "covered_anchor_ids": [],
        "missing_anchor_ids": [],
    }
    if expected_timeline_anchors:
        timing_audit = planner._audit_semantic_timeline_coverage(
            expected_timeline_anchors,
            plan,
        )
        missing_anchor_ids = list(timing_audit.get("missing_anchor_ids") or [])
        if len(missing_anchor_ids) >= 2:
            anchor_by_id = {
                str(item.get("anchor_id") or ""): item
                for item in expected_timeline_anchors
            }
            missing_labels = [
                str(anchor_by_id[item].get("label") or item)
                for item in missing_anchor_ids
                if item in anchor_by_id
            ]
            logger.warning(
                "Methods-first parsing missed semantic timeline anchors for %s: missing=%s. Retrying once.",
                primary_id,
                missing_anchor_ids,
            )
            retry_system = (
                base_system
                + " A previous parse missed these source-grounded timeline anchors: "
                + ", ".join(missing_labels)
                + ". Preserve them as timeline_nodes in their supplied branch context."
            )
            retry_response = await planner._call_model(
                system_prompt=retry_system,
                prompt=parsing_prompt,
                temperature=0.0,
            )
            plan = planner._parse_block_plan_from_llm(
                retry_response,
                domain_pack,
                evidence_bundle,
                primary_anchor_id=primary_id,
                segment_analysis_bundle=segment_analysis_bundle,
            )
            timepoint_retry_applied = True

    final_timing_audit = timing_audit
    if expected_timeline_anchors and timepoint_retry_applied:
        final_timing_audit = planner._audit_semantic_timeline_coverage(
            expected_timeline_anchors,
            plan,
        )
    final_missing_anchor_ids = list(
        final_timing_audit.get("missing_anchor_ids") or []
    )
    anchor_by_id = {
        str(item.get("anchor_id") or ""): item
        for item in expected_timeline_anchors
    }

    missing_timepoint_repair_applied = False
    if final_missing_anchor_ids:
        try:
            snippets = planner._build_semantic_timepoint_snippets(
                cleaned_procedure,
                expected_timeline_anchors,
                final_missing_anchor_ids,
                max_chars=int(os.getenv("PLANNER_MISSING_TIMEPOINT_SNIPPET_MAX_CHARS", "6500")),
            )
            if snippets:
                missing_method_text = "\n\n".join(
                    (
                        f"Source timing label: {anchor_by_id[anchor_id].get('label')}"
                        + (
                            f" [branch: {anchor_by_id[anchor_id].get('branch_context')}]"
                            if str(anchor_by_id[anchor_id].get("branch_context") or "").strip()
                            else ""
                        )
                        + f"\n{snippets[anchor_id]}"
                    ).strip()
                    for anchor_id in final_missing_anchor_ids
                    if anchor_id in snippets and anchor_id in anchor_by_id
                ).strip()

                missing_prompt = (
                    template.replace("{method_text}", missing_method_text[:parse_max_procedure])
                    .replace("{materials_text}", "")
                    .replace("{reagent_setup_text}", "")
                    .replace("{timing_text}", "")
                    .replace("{troubleshooting_text}", "")
                    .replace("{segment_index}", cleaned_segment_index)
                    .replace("{segment_analysis}", segment_analysis_text or "(No segment analysis available)")
                )
                missing_prompt += (
                    "\n\n---\n"
                    "REQUIRED_SEMANTIC_TIMELINE_ANCHORS (must be covered):\n"
                    + planner._format_required_timeline_anchors(
                        [
                            anchor_by_id[anchor_id]
                            for anchor_id in final_missing_anchor_ids
                            if anchor_id in anchor_by_id
                        ]
                    )
                    + "\n\n"
                    "Create timeline_nodes only for these source-grounded anchors and preserve branch context.\n"
                )

                repair_system = (
                    base_system
                    + " The previous parse missed timepoints. "
                    + "You will ONLY parse the missing timepoint snippets provided and output JSON in the same schema. "
                    + "Do not add new sections, day labels, reagents, preparations, or operational choices unless they are explicit in the provided snippets. "
                    + "Prefer exact wording from the provided procedure snippets over generic lab defaults. "
                    + "For every restored step, populate source_text/source_sentence from the provided snippets rather than leaving them empty."
                )
                repair_response = await planner._call_model(
                    system_prompt=repair_system,
                    prompt=missing_prompt,
                    temperature=0.0,
                )
                repair_plan = planner._parse_block_plan_from_llm(
                    repair_response,
                    domain_pack,
                    evidence_bundle,
                    primary_anchor_id=primary_id,
                    segment_analysis_bundle=segment_analysis_bundle,
                )
                plan = planner._merge_missing_timepoints(plan, repair_plan)
                missing_timepoint_repair_applied = True
        except Exception as e:
            logger.warning(f"Missing timepoint repair failed (non-blocking): {e}")

    post_repair_timing_audit = final_timing_audit
    if expected_timeline_anchors and missing_timepoint_repair_applied:
        post_repair_timing_audit = planner._audit_semantic_timeline_coverage(
            expected_timeline_anchors,
            plan,
        )
    post_repair_missing_anchor_ids = list(
        post_repair_timing_audit.get("missing_anchor_ids") or []
    )

    planning_trace["checks"]["timepoints"] = {
        "required": [
            str(item.get("label") or "") for item in expected_timeline_anchors
        ],
        "required_anchor_ids": [
            str(item.get("anchor_id") or "") for item in expected_timeline_anchors
        ],
        "missing_after_retry": [
            str(anchor_by_id[item].get("label") or item)
            for item in final_missing_anchor_ids
            if item in anchor_by_id
        ],
        "missing_anchor_ids_after_retry": final_missing_anchor_ids,
        "missing_after_repair": [
            str(anchor_by_id[item].get("label") or item)
            for item in post_repair_missing_anchor_ids
            if item in anchor_by_id
        ],
        "missing_anchor_ids_after_repair": post_repair_missing_anchor_ids,
        "semantic_audit_applied": bool(
            post_repair_timing_audit.get("semantic_audit_applied")
        ),
        "verification_error": str(
            post_repair_timing_audit.get("verification_error") or ""
        ),
    }
    planning_trace["repairs"]["timepoint_retry_applied"] = timepoint_retry_applied
    planning_trace["repairs"][
        "missing_timepoint_repair_applied"
    ] = missing_timepoint_repair_applied

    return plan


async def _normalize_and_ground(
    planner: PlanningHooks,
    inputs: PlanningInputs,
    plan: ProtocolPlan,
    *,
    logger: Logger,
) -> ProtocolPlan:
    """Normalize IDs and check source grounding with the existing continuity guard."""
    domain_pack = inputs.domain_pack
    planning_trace = inputs.planning_trace
    analyses = inputs.analyses
    cleaned_procedure = inputs.cleaned_procedure
    grounding_support_views = inputs.grounding_support_views
    semantic_operation_bundle = inputs.semantic_operation_bundle

    try:
        planner._assign_source_line_indices(plan, cleaned_procedure)
        planner._normalize_blocks_and_steps(plan, cleaned_procedure)
    except Exception as e:
        logger.warning(f"Post-parse normalization failed (non-blocking): {e}")

    grounding_filter_report: Dict[str, Any] = {}
    try:
        plan, grounding_filter_report = await planner._apply_grounding_filter_with_continuity_guard(
            task_query=domain_pack.task.query,
            procedure_text=cleaned_procedure,
            plan=plan,
            analyses=analyses,
            semantic_operation_bundle=semantic_operation_bundle,
            supporting_view_texts=grounding_support_views,
        )
        if grounding_filter_report.get("applied"):
            logger.info(
                "Applied grounding filter after initial parse: unsupported_before=%s unsupported_after=%s",
                grounding_filter_report.get("unsupported_before", []),
                grounding_filter_report.get("unsupported_after", []),
            )
    except Exception as e:
        logger.warning(f"Initial grounding filter failed (non-blocking): {e}")
        grounding_filter_report = {"error": str(e)}

    planning_trace["checks"]["initial_grounding"] = {
        "applied": bool(grounding_filter_report.get("applied")),
        "reason": str(grounding_filter_report.get("reason") or ""),
        "unsupported_before": list(
            grounding_filter_report.get("unsupported_before") or []
        ),
        "unsupported_after": list(
            grounding_filter_report.get("unsupported_after") or []
        ),
        "continuity_checked": bool(
            grounding_filter_report.get("continuity_checked")
        ),
        "restored_step_ids": list(
            grounding_filter_report.get("restored_step_ids") or []
        ),
        "continuity_reason": str(
            grounding_filter_report.get("continuity_reason") or ""
        ),
        "error": str(grounding_filter_report.get("error") or ""),
    }

    return plan


async def _repair_structure(
    planner: PlanningHooks,
    inputs: PlanningInputs,
    draft: ParsedPlan,
    plan: ProtocolPlan,
    *,
    logger: Logger,
) -> RepairResult:
    """Try block repair, local repair and optional full reparse under the original guard."""
    domain_pack = inputs.domain_pack
    evidence_bundle = inputs.evidence_bundle
    primary_id = inputs.primary_id
    planning_trace = inputs.planning_trace
    view_procedure_text = inputs.view_procedure_text
    analyses = inputs.analyses
    segments_dir = inputs.segments_dir
    segment_analysis_bundle = inputs.segment_analysis_bundle
    template = inputs.template
    segment_analysis_text = inputs.segment_analysis_text
    cleaned_segment_index = inputs.cleaned_segment_index
    cleaned_procedure = inputs.cleaned_procedure
    grounding_support_views = inputs.grounding_support_views
    parse_max_procedure = inputs.parse_max_procedure
    semantic_operation_bundle = inputs.semantic_operation_bundle
    shape_contract = inputs.shape_contract
    parsing_prompt = draft.parsing_prompt
    base_system = draft.base_system
    expected_timeline_anchors = draft.expected_timeline_anchors
    shape_contract_prompt = draft.shape_contract_prompt

    plan_guard: Dict[str, Any] = {}
    local_repair_applied = False
    try:
        plan_guard = await planner._verify_parsed_plan_structure(
            task_query=domain_pack.task.query,
            procedure_text=cleaned_procedure,
            plan=plan,
            analyses=analyses,
            segments_dir=segments_dir,
            shape_contract=shape_contract,
        )
        if planner._should_attempt_structure_repair(
            plan_guard,
            semantic_operation_bundle,
        ):
            logger.warning(
                "Parsed plan failed structural verification: missing=%s collapsed_branches=%s collapsed_cycles=%s. Attempting local structure repair.",
                plan_guard.get("missing_structures", []),
                plan_guard.get("collapsed_branches", []),
                plan_guard.get("collapsed_cycles", []),
            )
            repaired_procedure_text = await planner._augment_view_with_rescue_segments(
                procedure_view_text=view_procedure_text,
                analyses=analyses,
                segments_dir=segments_dir,
                rescue_segment_ids=plan_guard.get("rescue_segment_ids") or [],
                task_query=domain_pack.task.query,
                missing_structures=plan_guard.get("missing_structures") or [],
            )
            repair_required = list(plan_guard.get("required_structures") or [])
            normalized_repaired_procedure = planner._normalize_planner_input(repaired_procedure_text)
            current_candidate_plan = copy.deepcopy(plan)
            current_candidate_guard = plan_guard

            def unresolved_count(guard: Dict[str, Any], labels: List[str]) -> int:
                target = {
                    planner._normalize_for_match(label)
                    for label in (labels or [])
                    if planner._normalize_for_match(label)
                }
                if not target:
                    return 0
                return sum(
                    1
                    for item in (guard.get("missing_structures") or [])
                    if planner._normalize_for_match(str(item.get("label") or "")) in target
                )

            shape_jobs = planner._build_shape_block_repair_jobs(
                procedure_text=normalized_repaired_procedure,
                missing_structures=list(plan_guard.get("missing_structures") or []),
                shape_contract=shape_contract,
                analyses=analyses,
                segments_dir=segments_dir,
                rescue_segment_ids=plan_guard.get("rescue_segment_ids") or [],
                max_chars=min(parse_max_procedure, int(os.getenv("PLANNER_STRUCTURE_REPAIR_MAX_CHARS", "9000"))),
            )
            if shape_jobs:
                for job in shape_jobs:
                    job_snippet = str(job.get("snippet") or "").strip()
                    target_block_type = str(job.get("target_block_type") or "").strip()
                    required_labels = [str(x).strip() for x in (job.get("required_labels") or []) if str(x).strip()]
                    if not job_snippet or not target_block_type or not required_labels:
                        continue
                    before_unresolved = unresolved_count(current_candidate_guard, required_labels)
                    if before_unresolved <= 0:
                        continue
                    block_repair_prompt = (
                        template.replace("{method_text}", job_snippet[:parse_max_procedure])
                        .replace("{materials_text}", "")
                        .replace("{reagent_setup_text}", "")
                        .replace("{timing_text}", "")
                        .replace("{troubleshooting_text}", "")
                        .replace("{segment_index}", cleaned_segment_index)
                        .replace("{segment_analysis}", segment_analysis_text or "(No segment analysis available)")
                    )
                    if shape_contract_prompt:
                        block_repair_prompt += "\n\n" + shape_contract_prompt + "\n"
                    block_repair_prompt += (
                        "\n\n---\n"
                        f"TARGET_BLOCK_TYPE (output exactly one block with this block_type): {target_block_type}\n"
                        "REQUIRED_STRUCTURES_TO_RESTORE (restore only if explicit in the snippet):\n"
                        + "\n".join(f"- {item}" for item in required_labels)
                        + "\n\n"
                        "CURRENT_PARSED_PLAN_SUMMARY (baseline to preserve; do not re-create unrelated steps):\n"
                        + json.dumps(planner._summarize_plan_outline(current_candidate_plan), indent=2, ensure_ascii=False)
                        + "\n\n"
                        + "BLOCK-LEVEL LOCAL REPAIR INSTRUCTIONS:\n"
                        + "1. Parse ONLY the snippet provided above.\n"
                        + "2. Output ONLY one block for the target block type named above.\n"
                        + "3. Restore ONLY the required structures that are explicit in the snippet.\n"
                        + "4. For every restored step, source_text/source_sentence must quote or closely mirror an explicit sentence from the snippet.\n"
                        + "5. Do not invent plate coating, serum starvation, calibration, pretreatment, or any other routine default unless explicit in the snippet.\n"
                        + "6. Do not emit unrelated blocks or duplicate already-grounded blocks.\n"
                    )
                    repair_response = await planner._call_model(
                        system_prompt=(
                            base_system
                            + " A previous parse collapsed query-critical structures. "
                            + "Repair exactly one missing block from the provided snippet only."
                        ),
                        prompt=block_repair_prompt,
                        temperature=0.0,
                    )
                    block_repair_plan = planner._parse_block_plan_from_llm(
                        repair_response,
                        domain_pack,
                        evidence_bundle,
                        primary_anchor_id=primary_id,
                        segment_analysis_bundle=segment_analysis_bundle,
                    )
                    block_repair_plan = planner._filter_plan_to_grounded_steps(block_repair_plan, job_snippet)
                    if not block_repair_plan.step_count():
                        continue
                    block_repair_plan = planner._collapse_plan_to_single_block(block_repair_plan, target_block_type)
                    candidate_plan = copy.deepcopy(current_candidate_plan)
                    candidate_plan = planner._merge_structure_repairs(candidate_plan, block_repair_plan)
                    planner._assign_source_line_indices(candidate_plan, normalized_repaired_procedure)
                    planner._normalize_blocks_and_steps(candidate_plan, normalized_repaired_procedure)
                    candidate_guard = await planner._verify_parsed_plan_structure(
                        task_query=domain_pack.task.query,
                        procedure_text=normalized_repaired_procedure,
                        plan=candidate_plan,
                        analyses=analyses,
                        segments_dir=segments_dir,
                        shape_contract=shape_contract,
                    )
                    after_unresolved = unresolved_count(candidate_guard, required_labels)
                    if (
                        after_unresolved < before_unresolved
                        and planner._structure_issue_score(candidate_guard) <= planner._structure_issue_score(current_candidate_guard)
                    ):
                        current_candidate_plan = candidate_plan
                        current_candidate_guard = candidate_guard
                if planner._structure_issue_score(current_candidate_guard) < planner._structure_issue_score(plan_guard):
                    plan = current_candidate_plan
                    plan_guard = current_candidate_guard
                    local_repair_applied = True
                    view_procedure_text = repaired_procedure_text
                    cleaned_procedure = normalized_repaired_procedure
                    try:
                        plan, grounding_filter_report = await planner._apply_grounding_filter_with_continuity_guard(
                            task_query=domain_pack.task.query,
                            procedure_text=cleaned_procedure,
                            plan=plan,
                            analyses=analyses,
                            semantic_operation_bundle=semantic_operation_bundle,
                            supporting_view_texts=grounding_support_views,
                        )
                        if grounding_filter_report.get("applied"):
                            logger.info(
                                "Applied grounding filter after shape block repair: unsupported_before=%s unsupported_after=%s",
                                grounding_filter_report.get("unsupported_before", []),
                                grounding_filter_report.get("unsupported_after", []),
                            )
                    except Exception as gf_e:
                        logger.warning(f"Grounding filter after shape block repair failed (non-blocking): {gf_e}")

            repair_snippet = planner._build_structure_repair_snippet(
                procedure_text=normalized_repaired_procedure,
                missing_structures=list(current_candidate_guard.get("missing_structures") or []),
                analyses=analyses,
                segments_dir=segments_dir,
                rescue_segment_ids=current_candidate_guard.get("rescue_segment_ids") or [],
                timing_anchors=expected_timeline_anchors,
                max_chars=min(parse_max_procedure, int(os.getenv("PLANNER_STRUCTURE_REPAIR_MAX_CHARS", "9000"))),
            )
            if not local_repair_applied and repair_snippet.strip():
                local_repair_prompt = (
                    template.replace("{method_text}", repair_snippet[:parse_max_procedure])
                    .replace("{materials_text}", "")
                    .replace("{reagent_setup_text}", "")
                    .replace("{timing_text}", "")
                    .replace("{troubleshooting_text}", "")
                    .replace("{segment_index}", cleaned_segment_index)
                    .replace("{segment_analysis}", segment_analysis_text or "(No segment analysis available)")
                )
                if repair_required:
                    local_repair_prompt += (
                        "\n\n---\n"
                        "REQUIRED_STRUCTURES_TO_RESTORE (restore only if explicit in the snippet):\n"
                        + "\n".join(f"- {item}" for item in repair_required)
                        + "\n\n"
                        "Only restore structures that are explicitly present in the snippet. "
                        "Do not fill implied upstream defaults.\n"
                    )
                local_repair_prompt += (
                    "\n\n---\n"
                    "CURRENT_PARSED_PLAN_SUMMARY (baseline to preserve; do not re-create unrelated steps):\n"
                    + json.dumps(planner._summarize_plan_outline(plan), indent=2, ensure_ascii=False)
                    + "\n\n"
                    + "LOCAL REPAIR INSTRUCTIONS:\n"
                    + "1. Parse ONLY the snippet provided above.\n"
                    + "2. Output ONLY the missing structures directly supported by the snippet.\n"
                    + "3. For every restored step, source_text/source_sentence must quote or closely mirror an explicit sentence from the snippet.\n"
                    + "4. Do not invent plate coating, serum starvation, calibration, pretreatment, or any other routine default unless explicit in the snippet.\n"
                    + "5. Do not re-create blocks that are already covered by the current parsed plan unless the snippet adds a missing structure.\n"
                )
                repair_response = await planner._call_model(
                    system_prompt=(
                        base_system
                        + " A previous parse collapsed query-critical structures. "
                        + "Repair the plan locally from the provided snippet only."
                    ),
                    prompt=local_repair_prompt,
                    temperature=0.0,
                )
                repair_plan = planner._parse_block_plan_from_llm(
                    repair_response,
                    domain_pack,
                    evidence_bundle,
                    primary_anchor_id=primary_id,
                    segment_analysis_bundle=segment_analysis_bundle,
                )
                repair_plan = planner._filter_plan_to_grounded_steps(repair_plan, repair_snippet)
                if repair_plan.step_count():
                    candidate_plan = copy.deepcopy(plan)
                    candidate_plan = planner._merge_structure_repairs(candidate_plan, repair_plan)
                    planner._assign_source_line_indices(candidate_plan, normalized_repaired_procedure)
                    planner._normalize_blocks_and_steps(candidate_plan, normalized_repaired_procedure)
                    candidate_guard = await planner._verify_parsed_plan_structure(
                        task_query=domain_pack.task.query,
                        procedure_text=normalized_repaired_procedure,
                        plan=candidate_plan,
                        analyses=analyses,
                        segments_dir=segments_dir,
                        shape_contract=shape_contract,
                    )
                    if planner._structure_issue_score(candidate_guard) < planner._structure_issue_score(plan_guard):
                        plan = candidate_plan
                        local_repair_applied = True
                        view_procedure_text = repaired_procedure_text
                        cleaned_procedure = normalized_repaired_procedure
                        try:
                            plan, grounding_filter_report = await planner._apply_grounding_filter_with_continuity_guard(
                                task_query=domain_pack.task.query,
                                procedure_text=cleaned_procedure,
                                plan=plan,
                                analyses=analyses,
                                semantic_operation_bundle=semantic_operation_bundle,
                                supporting_view_texts=grounding_support_views,
                            )
                            if grounding_filter_report.get("applied"):
                                logger.info(
                                    "Applied grounding filter after local structure repair: unsupported_before=%s unsupported_after=%s",
                                    grounding_filter_report.get("unsupported_before", []),
                                    grounding_filter_report.get("unsupported_after", []),
                                )
                        except Exception as gf_e:
                            logger.warning(f"Grounding filter after local structure repair failed (non-blocking): {gf_e}")
            if not local_repair_applied and os.getenv("PLANNER_ENABLE_FULL_REPARSE_FALLBACK", "0") == "1":
                logger.warning("Local structure repair did not improve coverage; falling back to full reparse.")
                repaired_parsing_prompt = parsing_prompt.replace(
                    cleaned_procedure[:parse_max_procedure],
                    normalized_repaired_procedure[:parse_max_procedure],
                    1,
                )
                if repair_required:
                    repaired_parsing_prompt += (
                        "\n\n---\n"
                        "REQUIRED_STRUCTURES (must be preserved in the parsed plan):\n"
                        + "\n".join(f"- {item}" for item in repair_required)
                        + "\n\n"
                        "You MUST preserve these structures explicitly in the output JSON. "
                        "Do not collapse setup, branches, or cyclic logic into generic prose.\n"
                    )
                repair_prompt = (
                    repaired_parsing_prompt
                    + "\n\n---\n"
                    + "CURRENT_PARSED_PLAN_SUMMARY (baseline to preserve unless unsupported):\n"
                    + json.dumps(planner._summarize_plan_outline(plan), indent=2, ensure_ascii=False)
                    + "\n\n"
                    + "REPAIR INSTRUCTIONS:\n"
                    + "1. Repair the parsed plan by restoring ONLY the missing required structures that are explicit in the updated procedure view.\n"
                    + "2. Preserve existing grounded sections and step ordering unless the procedure view clearly contradicts them.\n"
                    + "3. Do NOT introduce new stages, preparations, day labels, coatings, pretreatments, or reagent choices unless they are explicitly present in the procedure view.\n"
                    + "4. Prefer exact wording from the procedure view for restored step titles and descriptions.\n"
                    + "5. For every restored step, populate source_text/source_sentence with a verbatim or near-verbatim span from the procedure view.\n"
                    + "6. If a missing structure is only weakly implied, keep the restoration minimal and generic instead of filling in lab-default details.\n"
                )
                repair_response = await planner._call_model(
                    system_prompt=(
                        base_system
                        + " A previous parse collapsed query-critical structures. "
                        + "Repair the parsed plan conservatively and keep it strictly grounded in the updated procedure view."
                    ),
                    prompt=repair_prompt,
                    temperature=0.0,
                )
                plan = planner._parse_block_plan_from_llm(
                    repair_response,
                    domain_pack,
                    evidence_bundle,
                    primary_anchor_id=primary_id,
                    segment_analysis_bundle=segment_analysis_bundle,
                )
                planner._assign_source_line_indices(plan, normalized_repaired_procedure)
                planner._normalize_blocks_and_steps(plan, normalized_repaired_procedure)
                view_procedure_text = repaired_procedure_text
                cleaned_procedure = normalized_repaired_procedure
                try:
                    plan, grounding_filter_report = await planner._apply_grounding_filter_with_continuity_guard(
                        task_query=domain_pack.task.query,
                        procedure_text=cleaned_procedure,
                        plan=plan,
                        analyses=analyses,
                        semantic_operation_bundle=semantic_operation_bundle,
                        supporting_view_texts=grounding_support_views,
                    )
                    if grounding_filter_report.get("applied"):
                        logger.info(
                            "Applied grounding filter after full reparse fallback: unsupported_before=%s unsupported_after=%s",
                            grounding_filter_report.get("unsupported_before", []),
                            grounding_filter_report.get("unsupported_after", []),
                        )
                except Exception as gf_e:
                    logger.warning(f"Grounding filter after full reparse fallback failed (non-blocking): {gf_e}")
    except Exception as e:
        logger.warning(f"Parsed-plan structural verification failed (non-blocking): {e}")

    planning_trace["checks"]["structure_guard"] = (
        planner._compact_structure_guard_trace(plan_guard)
    )
    planning_trace["repairs"][
        "local_structure_repair_applied"
    ] = local_repair_applied

    return RepairResult(plan=plan, cleaned_procedure=cleaned_procedure)


# Repair decisions and source snippets.


def should_attempt_structure_repair(
    plan_guard: Dict[str, Any],
    semantic_operation_bundle: Optional[Dict[str, Any]] = None,
) -> bool:
    # Semantic units guide parsing and grounding; their mere existence does
    # not resolve a structure that the post-parse guard found missing.
    return bool((plan_guard or {}).get("should_reparse"))


def build_semantic_timepoint_snippets(
    text: str,
    anchors: List[Dict[str, Any]],
    target_anchor_ids: List[str],
    max_chars: int,
) -> Dict[str, str]:
    """Slice repair evidence from verified source spans, never label search."""
    if not text or not anchors or not target_anchor_ids:
        return {}
    ordered = sorted(
        [
            item
            for item in anchors
            if isinstance(item, dict)
            and str(item.get("anchor_id") or "").strip()
            and str(item.get("plan_role") or "") == "timeline_node"
            and 0 <= int(item.get("source_start") or 0) < len(text)
        ],
        key=lambda item: (
            int(item.get("source_start") or 0),
            int(item.get("source_end") or 0),
        ),
    )
    targets = set(target_anchor_ids)
    out: Dict[str, str] = {}
    for item in ordered:
        anchor_id = str(item.get("anchor_id") or "")
        if anchor_id not in targets:
            continue
        start = int(item.get("source_start") or 0)
        next_start = next(
            (
                int(candidate.get("source_start") or 0)
                for candidate in ordered
                if int(candidate.get("source_start") or 0) > start
            ),
            len(text),
        )
        snippet = text[start:next_start].strip()
        if len(snippet) > max_chars:
            snippet = snippet[:max_chars].rstrip()
        if snippet:
            out[anchor_id] = snippet
    return out


def uses_shape_block_level_repair(contract: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(contract, dict):
        return False
    return (
        str(contract.get("confidence") or "").lower() == "high"
        and str(contract.get("shape") or "").lower() != "generic"
        and bool(contract.get("required_structures"))
    )


def build_shape_block_repair_jobs(
    planner: PlanningHooks,
    procedure_text: str,
    missing_structures: List[Dict[str, str]],
    shape_contract: Optional[Dict[str, Any]],
    analyses: List[Dict[str, Any]],
    segments_dir: str,
    rescue_segment_ids: List[str],
    max_chars: Optional[int] = None,
) -> List[Dict[str, Any]]:
    if not planner._uses_shape_block_level_repair(shape_contract):
        return []

    max_chars = max_chars or int(os.getenv("PLANNER_STRUCTURE_REPAIR_MAX_CHARS", "9000"))
    missing_by_key: Dict[str, Dict[str, str]] = {}
    for item in (missing_structures or []):
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("missing_item") or "").strip()
            kind = str(item.get("type") or "").strip() or "structure"
            reason = str(item.get("reason") or "").strip()
        else:
            label = str(item).strip()
            kind = "structure"
            reason = ""
        if not label:
            continue
        missing_by_key[planner._normalize_for_match(label)] = {
            "label": label,
            "type": kind,
            "reason": reason,
        }

    if not missing_by_key:
        return []

    grouped: Dict[str, Dict[str, Any]] = {}
    required = list((shape_contract or {}).get("required_structures") or [])
    for item in required:
        label = str(item.get("label") or "").strip()
        requirement = str(item.get("requirement") or "required").strip().lower()
        if not label or requirement == "optional":
            continue
        key = planner._normalize_for_match(label)
        if key not in missing_by_key:
            continue
        entry = grouped.setdefault(
            label,
            {
                "required_labels": [],
                "missing_items": [],
            },
        )
        entry["required_labels"].append(label)
        entry["missing_items"].append(
            {
                "label": label,
                "type": str(item.get("type") or "structure"),
                "reason": str(missing_by_key[key].get("reason") or ""),
            }
        )

    jobs: List[Dict[str, Any]] = []
    for block_type, payload in grouped.items():
        snippet = planner._build_structure_repair_snippet(
            procedure_text=procedure_text,
            missing_structures=list(payload.get("missing_items") or []),
            analyses=analyses,
            segments_dir=segments_dir,
            rescue_segment_ids=rescue_segment_ids,
            max_chars=max_chars,
        )
        if not snippet.strip():
            continue
        jobs.append(
            {
                "target_block_type": block_type,
                "required_labels": [str(item).strip() for item in (payload.get("required_labels") or []) if str(item).strip()],
                "snippet": snippet,
            }
        )
    return jobs


def plan_text_covers_structure(
    plan_text: str,
    label: str,
    aliases: Optional[List[str]] = None,
) -> bool:
    candidates = [label] + [str(x) for x in (aliases or []) if str(x).strip()]
    for candidate in candidates:
        candidate_text = str(candidate or "").strip()
        if not candidate_text:
            continue
        candidate_lower = candidate_text.lower()
        if candidate_lower and candidate_lower in plan_text:
            return True
        meaningful_tokens = [
            tok
            for tok in re.findall(r"[a-z0-9]+", candidate_lower)
            if len(tok) >= 4 and tok not in {"section", "protocol", "procedure", "steps", "step"}
        ]
        if not meaningful_tokens:
            continue
        unique_tokens: List[str] = []
        seen_tokens: Set[str] = set()
        for tok in meaningful_tokens:
            if tok not in seen_tokens:
                seen_tokens.add(tok)
                unique_tokens.append(tok)
        matches = sum(1 for tok in unique_tokens if tok in plan_text)
        if len(unique_tokens) == 1:
            if len(unique_tokens[0]) >= 8 and matches >= 1:
                return True
            continue
        required_matches = 2 if len(unique_tokens) >= 2 else 1
        if matches >= required_matches:
            return True
    return False


def build_structure_repair_snippet(
    planner: PlanningHooks,
    procedure_text: str,
    missing_structures: List[Dict[str, str]],
    analyses: List[Dict[str, Any]],
    segments_dir: str,
    rescue_segment_ids: List[str],
    timing_anchors: Optional[List[Dict[str, Any]]] = None,
    max_chars: Optional[int] = None,
) -> str:
    max_chars = max_chars or int(os.getenv("PLANNER_STRUCTURE_REPAIR_MAX_CHARS", "9000"))
    sections = planner._extract_markdown_sections(procedure_text)
    normalized_missing: List[Dict[str, str]] = []
    for item in (missing_structures or []):
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("missing_item") or "").strip()
            kind = str(item.get("type") or "").strip()
            reason = str(item.get("reason") or "").strip()
        else:
            label = str(item).strip()
            kind = ""
            reason = ""
        if not label:
            continue
        if not kind:
            kind = "structure"
        normalized_missing.append({"label": label, "type": kind, "reason": reason})
    parts: List[str] = []
    seen_keys: Set[str] = set()
    total_chars = 0

    def add_part(label: str, snippet: str) -> None:
        nonlocal total_chars
        text = (snippet or "").strip()
        if not text:
            return
        key = planner._normalize_for_match(text[:240])
        if not key or key in seen_keys:
            return
        remaining = max_chars - total_chars
        if remaining <= 0:
            return
        if len(text) > remaining:
            text = text[:remaining].rstrip()
        block = f"### {label}\n\n{text}".strip()
        parts.append(block)
        seen_keys.add(key)
        total_chars += len(block) + 2

    missing_timepoint_labels = [
        planner._normalize_for_match(str(item.get("label") or ""))
        for item in normalized_missing
        if str(item.get("type") or "") == "timepoint"
    ]
    matching_anchor_ids = [
        str(anchor.get("anchor_id") or "")
        for anchor in (timing_anchors or [])
        if planner._normalize_for_match(str(anchor.get("label") or ""))
        in missing_timepoint_labels
    ]
    if matching_anchor_ids:
        snippets = planner._build_semantic_timepoint_snippets(
            procedure_text,
            timing_anchors or [],
            matching_anchor_ids,
            max_chars=max(1200, min(max_chars // 2, 3500)),
        )
        anchor_by_id = {
            str(anchor.get("anchor_id") or ""): anchor
            for anchor in (timing_anchors or [])
        }
        for anchor_id, snippet in snippets.items():
            add_part(
                str(anchor_by_id.get(anchor_id, {}).get("label") or anchor_id),
                snippet,
            )

    for item in normalized_missing:
        label = str(item.get("label") or "").strip()
        kind = str(item.get("type") or "").strip()
        low_label = label.lower()

        if kind == "setup":
            for heading, body in sections:
                low_heading = (heading or "").lower()
                if any(tok in low_heading for tok in ["preparation", "setup", "before you begin"]):
                    add_part(heading or label, body)
        elif kind in {"branch", "ordered_section", "final_cycle", "phase"}:
            tokens = [tok for tok in re.findall(r"[a-z0-9]+", low_label) if len(tok) >= 4]
            for heading, body in sections:
                hay = f"{heading}\n{body}".lower()
                if planner._plan_text_covers_structure(hay, label, tokens):
                    add_part(heading or label, body)
        elif kind == "loop":
            for heading, body in sections:
                hay = f"{heading}\n{body}".lower()
                if any(tok in hay for tok in ["repeat", "repetition", "cycle", "cyclic", "iteration"]):
                    add_part(heading or label, body)
        elif kind in {"structure", "early_stage", "tail_stage", "traceability", "order"}:
            tokens = [tok for tok in re.findall(r"[a-z0-9]+", low_label) if len(tok) >= 4]
            for heading, body in sections:
                hay = f"{heading}\n{body}".lower()
                if sum(tok in hay for tok in tokens[:8]) >= max(1, min(2, len(tokens[:8]))):
                    add_part(heading or label, body)

    if rescue_segment_ids and total_chars < max_chars:
        text_map = planner._read_segment_text_map(analyses, segments_dir)
        analysis_by_id = {
            str(entry.get("segment_id") or ""): entry
            for entry in (analyses or [])
            if isinstance(entry, dict) and entry.get("segment_id")
        }
        for seg_id in rescue_segment_ids:
            seg_text = (text_map.get(seg_id) or "").strip()
            if not seg_text:
                continue
            title = str((analysis_by_id.get(seg_id) or {}).get("title") or seg_id)
            add_part(title, seg_text)

    return "\n\n".join(parts).strip()


# Grounding corrections and continuity preservation.


def filter_plan_to_grounded_steps(
    planner: PlanningHooks,
    plan: ProtocolPlan,
    reference_text: str,
    supporting_view_texts: Optional[Dict[str, str]] = None,
) -> ProtocolPlan:
    grounded_blocks: List[OperationBlock] = []
    section_by_id = {s.section_id: s for s in (plan.sections or []) if getattr(s, "section_id", None)}
    grounded_sections: List[ProtocolSectionPlan] = []

    for block in (plan.blocks or []):
        kept_groups: List[TimepointGroup] = []
        kept_step_ids: List[str] = []
        for group in (block.timeline_nodes or []):
            kept_steps: List[ProtocolStepSkeleton] = []
            for step in (group.steps or []):
                evidence_text = step.source_sentence or step.source_text or ""
                if planner._text_supported_by_grounding_views(
                    evidence_text,
                    reference_text,
                    supporting_view_texts,
                ):
                    kept_steps.append(step)
                    if step.step_id:
                        kept_step_ids.append(step.step_id)
            if kept_steps:
                group.steps = kept_steps
                kept_groups.append(group)
        if kept_groups:
            block.timeline_nodes = kept_groups
            grounded_blocks.append(block)
            section = section_by_id.get(block.block_id)
            if section is None:
                section = ProtocolSectionPlan(
                    section_id=block.block_id,
                    name=block.block_type,
                    goal=f"Execute {block.block_type} steps",
                    step_ids=kept_step_ids,
                )
            else:
                section.step_ids = kept_step_ids
            grounded_sections.append(section)

    plan.blocks = grounded_blocks
    plan.sections = grounded_sections
    plan.bind_hierarchical_steps_as_canonical()
    return plan


def apply_grounding_filter_if_safe(
    planner: PlanningHooks,
    task_query: str,
    procedure_text: str,
    plan: ProtocolPlan,
    analyses: List[Dict[str, Any]],
    supporting_view_texts: Optional[Dict[str, str]] = None,
) -> Tuple[ProtocolPlan, Dict[str, Any]]:
    unsupported_before = planner._collect_unsupported_steps(
        plan,
        procedure_text,
        supporting_view_texts,
    )
    if not unsupported_before:
        return plan, {
            "applied": False,
            "reason": "no_unsupported_steps",
            "unsupported_before": [],
            "unsupported_after": [],
        }

    before_block_ids = {
        str(block.block_id or "") for block in (plan.blocks or [])
    }
    before_node_count = sum(
        len(block.timeline_nodes or []) for block in (plan.blocks or [])
    )
    candidate = copy.deepcopy(plan)
    candidate = planner._filter_plan_to_grounded_steps(
        candidate,
        procedure_text,
        supporting_view_texts,
    )
    if not candidate.step_count():
        return plan, {
            "applied": False,
            "reason": "filter_removed_all_steps",
            "unsupported_before": unsupported_before,
            "unsupported_after": [],
        }

    after_block_ids = {
        str(block.block_id or "") for block in (candidate.blocks or [])
    }
    after_node_count = sum(
        len(block.timeline_nodes or []) for block in (candidate.blocks or [])
    )
    unsupported_after = planner._collect_unsupported_steps(
        candidate,
        procedure_text,
        supporting_view_texts,
    )

    if (
        len(unsupported_after) < len(unsupported_before)
        and before_block_ids == after_block_ids
        and after_node_count == before_node_count
    ):
        return candidate, {
            "applied": True,
            "reason": "reduced_unsupported_without_hurting_structure",
            "unsupported_before": unsupported_before,
            "unsupported_after": unsupported_after,
        }

    return plan, {
        "applied": False,
        "reason": "would_not_improve_grounding_safely",
        "unsupported_before": unsupported_before,
        "unsupported_after": unsupported_after,
    }


async def apply_grounding_filter_with_continuity_guard(
    planner: PlanningHooks,
    task_query: str,
    procedure_text: str,
    plan: ProtocolPlan,
    analyses: List[Dict[str, Any]],
    semantic_operation_bundle: Optional[Dict[str, Any]] = None,
    supporting_view_texts: Optional[Dict[str, str]] = None,
) -> Tuple[ProtocolPlan, Dict[str, Any]]:
    """Rebind source-backed bridge operations, then reuse the existing filter."""
    unsupported_step_ids = planner._collect_unsupported_steps(
        plan,
        procedure_text,
        supporting_view_texts,
    )
    units = [
        item
        for item in ((semantic_operation_bundle or {}).get("units") or [])
        if isinstance(item, dict)
        and str(item.get("unit_id") or "").strip()
        and str(item.get("quote") or "").strip()
        and str(item.get("view_type") or "").strip() == "procedure"
    ]
    if not unsupported_step_ids or not units:
        return planner._apply_grounding_filter_if_safe(
            task_query=task_query,
            procedure_text=procedure_text,
            plan=plan,
            analyses=analyses,
            supporting_view_texts=supporting_view_texts,
        )

    unsupported_set = set(unsupported_step_ids)
    before_steps = plan.step_list()
    candidates: List[Dict[str, Any]] = []
    units_by_step_id: Dict[str, Dict[str, Any]] = {}
    for index, step in enumerate(before_steps):
        step_id = str(step.step_id or "")
        if step_id not in unsupported_set:
            continue
        source_position = getattr(step, "source_line_index", None)
        source_unit = next(
            (
                unit
                for unit in units
                if source_position is not None
                and int(unit.get("source_start") or 0) <= source_position
                < int(unit.get("source_end") or 0)
            ),
            None,
        )
        if source_unit is None:
            continue
        previous_step = before_steps[index - 1] if index > 0 else None
        next_step = before_steps[index + 1] if index + 1 < len(before_steps) else None
        units_by_step_id[step_id] = source_unit
        candidates.append(
            {
                "step_id": step_id,
                "phase": str(step.phase or ""),
                "title": str(step.title or ""),
                "action": str(step.action or ""),
                "source_sentence": str(step.source_sentence or step.source_text or ""),
                "exact_source_quote": str(source_unit.get("quote") or "")[:800],
                "previous_step": (
                    {
                        "step_id": str(previous_step.step_id or ""),
                        "title": str(previous_step.title or ""),
                        "action": str(previous_step.action or ""),
                    }
                    if previous_step is not None
                    else None
                ),
                "next_step": (
                    {
                        "step_id": str(next_step.step_id or ""),
                        "title": str(next_step.title or ""),
                        "action": str(next_step.action or ""),
                    }
                    if next_step is not None
                    else None
                ),
            }
        )
    if not candidates:
        return planner._apply_grounding_filter_if_safe(
            task_query=task_query,
            procedure_text=procedure_text,
            plan=plan,
            analyses=analyses,
            supporting_view_texts=supporting_view_texts,
        )

    schema = {
        "type": "object",
        "properties": {
            "restore_step_ids": {
                "type": "array",
                "items": {"type": "string"},
            },
            "reason": {"type": "string"},
        },
        "required": ["restore_step_ids", "reason"],
    }
    try:
        verdict = await planner._call_model(
            system_prompt=(
                "You audit experimental steps that a grounding filter is about to remove. "
                "Each candidate is already paired with one exact source quote."
            ),
            prompt=(
                f"Task query:\n{task_query}\n\n"
                "Candidates with exact source quotes and immediate neighbors (JSON):\n"
                f"{json.dumps(candidates, indent=2, ensure_ascii=False)}\n\n"
                "Instructions:\n"
                "1. Decide whether removing a step creates an operational dependency gap, such as acting on a pellet or supernatant without the source-defined operation that creates it.\n"
                "2. Preserve only a candidate that is necessary for continuity and whose exact source quote supports the same action.\n"
                "3. Do not restore background, explanatory, duplicate, readiness-only, or evaluation-only text.\n"
                "4. Use only exact step_id values from the supplied JSON.\n"
                "5. Do not invent an action, parameter, or source quote. This is one audit pass only.\n"
                "Return JSON only with every key in this shape:\n"
                '{"restore_step_ids": ["..."], "reason": "..."}\n'
            ),
            schema=schema,
            temperature=0.0,
        )
    except Exception as exc:
        filtered_plan, report = planner._apply_grounding_filter_if_safe(
            task_query=task_query,
            procedure_text=procedure_text,
            plan=plan,
            analyses=analyses,
            supporting_view_texts=supporting_view_texts,
        )
        report["continuity_error"] = str(exc)
        return filtered_plan, report

    valid_bindings: Dict[str, Dict[str, Any]] = {}
    requested_ids = {
        str(value).strip()
        for value in (
            verdict.get("restore_step_ids") if isinstance(verdict, dict) else []
        ) or []
        if str(value).strip()
    }
    for step_id in requested_ids:
        unit = units_by_step_id.get(step_id)
        if unit is None:
            continue
        quote = str(unit.get("quote") or "").strip()
        start = int(unit.get("source_start") or 0)
        end = int(unit.get("source_end") or 0)
        if not quote or start < 0 or end <= start:
            continue
        if procedure_text[start:end] != quote:
            continue
        valid_bindings[step_id] = unit

    rebound_plan = copy.deepcopy(plan)
    rebound_plan.bind_hierarchical_steps_as_canonical()
    for step in rebound_plan.iter_steps():
        unit = valid_bindings.get(str(step.step_id or ""))
        if unit is None:
            continue
        quote = str(unit.get("quote") or "").strip()
        step.source_sentence = quote
        step.source_text = quote
        step.source_line_index = int(unit.get("source_start") or 0)

    filtered_plan, report = planner._apply_grounding_filter_if_safe(
        task_query=task_query,
        procedure_text=procedure_text,
        plan=rebound_plan,
        analyses=analyses,
        supporting_view_texts=supporting_view_texts,
    )
    report = dict(report or {})
    report["continuity_checked"] = True
    report["restored_step_ids"] = list(valid_bindings)
    report["continuity_reason"] = str(
        (verdict.get("reason") if isinstance(verdict, dict) else "") or ""
    )
    report["unsupported_before"] = unsupported_step_ids
    if valid_bindings:
        report["applied"] = True
        report["reason"] = "source_rebound_before_grounding_filter"
    return filtered_plan, report


# Merge accepted repairs, restore source positions, and normalize.


def merge_structure_repairs(
    planner: PlanningHooks,
    base: ProtocolPlan,
    extra: ProtocolPlan,
) -> ProtocolPlan:
    if not (extra.blocks or []):
        return base

    used_block_ids = {str(b.block_id) for b in (base.blocks or []) if getattr(b, "block_id", None)}
    used_step_ids = {
        str(s.step_id) for s in base.iter_steps() if getattr(s, "step_id", None)
    }
    existing_signatures = {
        (
            planner._normalize_for_match(s.source_sentence or s.source_text or s.description or ""),
            planner._normalize_for_match((s.timing.timepoint_label if getattr(s, "timing", None) else "") or ""),
        )
        for s in base.iter_steps()
    }

    next_block_num = len(used_block_ids) + 1
    next_step_num = len(used_step_ids) + 1

    def alloc_block_id() -> str:
        nonlocal next_block_num
        while True:
            cand = f"B{next_block_num:02d}"
            next_block_num += 1
            if cand not in used_block_ids:
                used_block_ids.add(cand)
                return cand

    def alloc_step_id() -> str:
        nonlocal next_step_num
        while True:
            cand = f"S{next_step_num:02d}"
            next_step_num += 1
            if cand not in used_step_ids:
                used_step_ids.add(cand)
                return cand

    extra_sections_by_id = {
        s.section_id: s for s in (extra.sections or []) if getattr(s, "section_id", None)
    }

    for block in (extra.blocks or []):
        original_block_id = block.block_id or alloc_block_id()
        target_block: Optional[OperationBlock] = None
        for existing_block in (base.blocks or []):
            if planner._normalize_for_match(existing_block.block_type or "") == planner._normalize_for_match(block.block_type or ""):
                target_block = existing_block
                break

        if target_block is not None:
            new_block_id = target_block.block_id
        else:
            if original_block_id in used_block_ids:
                new_block_id = alloc_block_id()
            else:
                used_block_ids.add(original_block_id)
                new_block_id = original_block_id
            block.block_id = new_block_id

        kept_group_steps: List[ProtocolStepSkeleton] = []
        existing_group_by_label: Dict[str, TimepointGroup] = {}
        if target_block is not None:
            existing_group_by_label = {
                planner._normalize_for_match(g.timepoint_label or ""): g
                for g in (target_block.timeline_nodes or [])
                if getattr(g, "timepoint_label", None)
            }
        new_groups: List[TimepointGroup] = []
        for group in (block.timeline_nodes or []):
            unique_steps: List[ProtocolStepSkeleton] = []
            for step in (group.steps or []):
                signature = (
                    planner._normalize_for_match(step.source_sentence or step.source_text or step.description or ""),
                    planner._normalize_for_match((step.timing.timepoint_label if getattr(step, "timing", None) else "") or ""),
                )
                if signature in existing_signatures:
                    continue
                existing_signatures.add(signature)
                if not step.step_id or step.step_id in used_step_ids:
                    step.step_id = alloc_step_id()
                else:
                    used_step_ids.add(step.step_id)
                step.block_ref_id = new_block_id
                unique_steps.append(step)
                kept_group_steps.append(step)
            if not unique_steps:
                continue
            group.steps = unique_steps
            group_key = planner._normalize_for_match(group.timepoint_label or "")
            if target_block is not None and group_key in existing_group_by_label:
                existing_group_by_label[group_key].steps.extend(unique_steps)
            else:
                new_groups.append(group)

        if not kept_group_steps:
            continue

        if target_block is not None:
            target_block.timeline_nodes.extend(new_groups)
        else:
            block.timeline_nodes = new_groups
            if not block.timeline_nodes:
                continue
            base.blocks.append(block)
        section = extra_sections_by_id.get(original_block_id)
        if section is None:
            section = ProtocolSectionPlan(
                section_id=new_block_id,
                name=block.block_type,
                goal=f"Execute {block.block_type} steps",
                step_ids=[s.step_id for s in kept_group_steps if s.step_id],
            )
        else:
            section.section_id = new_block_id
            if target_block is None:
                section.step_ids = [s.step_id for s in kept_group_steps if s.step_id]
        if target_block is not None:
            existing_section = next((s for s in (base.sections or []) if s.section_id == new_block_id), None)
            if existing_section is not None:
                existing_section.step_ids = list(dict.fromkeys((existing_section.step_ids or []) + [s.step_id for s in kept_group_steps if s.step_id]))
            else:
                section.step_ids = [s.step_id for s in kept_group_steps if s.step_id]
                base.sections.append(section)
        else:
            base.sections.append(section)

    def block_min_line(block: OperationBlock) -> int:
        indices = [
            s.source_line_index
            for g in (block.timeline_nodes or [])
            for s in (g.steps or [])
            if getattr(s, "source_line_index", None) is not None
        ]
        return min(indices) if indices else 999999999

    base.blocks = sorted(base.blocks or [], key=lambda b: (block_min_line(b), planner._normalize_for_match(b.block_type or "")))
    section_by_id = {s.section_id: s for s in (base.sections or []) if getattr(s, "section_id", None)}
    ordered_sections: List[ProtocolSectionPlan] = []
    seen_section_ids: Set[str] = set()
    for block in (base.blocks or []):
        section = section_by_id.get(block.block_id)
        if section is None or section.section_id in seen_section_ids:
            continue
        ordered_sections.append(section)
        seen_section_ids.add(section.section_id)
    for section in (base.sections or []):
        if section.section_id not in seen_section_ids:
            ordered_sections.append(section)
            seen_section_ids.add(section.section_id)
    base.sections = ordered_sections
    base.bind_hierarchical_steps_as_canonical()
    return base


def merge_missing_timepoints(
    planner: PlanningHooks,
    base: ProtocolPlan,
    extra: ProtocolPlan,
) -> ProtocolPlan:
    return planner._merge_structure_repairs(base, extra)


def assign_source_line_indices(
    planner: PlanningHooks,
    plan: ProtocolPlan,
    reference_text: str,
) -> None:
    if not plan or not plan.step_count() or not reference_text:
        return

    ref_lines = reference_text.splitlines(keepends=True)
    norm_lines = [planner._normalize_for_match(line) for line in ref_lines]
    line_offsets: List[int] = []
    cursor = 0
    for line in ref_lines:
        line_offsets.append(cursor)
        cursor += len(line)

    def normalize_with_offsets(text: str) -> Tuple[str, List[int]]:
        normalized: List[str] = []
        offsets: List[int] = []
        pending_space = False
        for position, char in enumerate(text or ""):
            char = "-" if char == "–" else char.lower()
            if char.isalnum():
                if pending_space and normalized:
                    normalized.append(" ")
                    offsets.append(position)
                normalized.append(char)
                offsets.append(position)
                pending_space = False
            elif normalized:
                pending_space = True
        return "".join(normalized), offsets

    normalized_reference, normalized_offsets = normalize_with_offsets(reference_text)

    occurrence_cursors: Dict[str, int] = {}

    def locate(needle: str, start_at: int = 0) -> Optional[Tuple[int, int]]:
        if not needle:
            return None
        exact = planner._locate_semantic_quote(
            reference_text,
            needle,
            start_at=start_at,
        )
        if exact:
            return exact[0], exact[1]

        normalized_needle, _ = normalize_with_offsets(needle)
        if normalized_needle:
            probes = [normalized_needle]
            first_sentence = re.split(r"(?<=[.!?])\s+", needle.strip(), maxsplit=1)[0]
            normalized_first_sentence, _ = normalize_with_offsets(first_sentence)
            if len(normalized_first_sentence.split()) >= 4:
                probes.append(normalized_first_sentence)
            tokens = normalized_needle.split()
            if len(tokens) > 8:
                probes.append(" ".join(tokens[:8]))
            normalized_start = bisect_left(normalized_offsets, start_at)
            for normalized_probe in probes:
                normalized_position = normalized_reference.find(
                    normalized_probe,
                    normalized_start,
                )
                if normalized_position >= 0 and normalized_position < len(normalized_offsets):
                    normalized_end = min(
                        normalized_position + len(normalized_probe) - 1,
                        len(normalized_offsets) - 1,
                    )
                    return (
                        normalized_offsets[normalized_position],
                        normalized_offsets[normalized_end] + 1,
                    )

        norm = planner._normalize_for_match(needle)
        if not norm:
            return None
        probe = norm[:80]
        if probe:
            for i, nl in enumerate(norm_lines):
                if line_offsets[i] >= start_at and probe in nl:
                    return line_offsets[i], line_offsets[i] + len(ref_lines[i])
        tokens = [t for t in re.split(r"[^a-z0-9]+", norm) if t]
        probe2 = " ".join(tokens[:8]).strip()
        if probe2:
            for i, nl in enumerate(norm_lines):
                if line_offsets[i] >= start_at and probe2 in nl:
                    return line_offsets[i], line_offsets[i] + len(ref_lines[i])
        return None

    # Canonical iteration follows the planner-authored hierarchy and falls back
    # only for an in-memory legacy object that has not yet been promoted.
    step_objects = plan.step_list()

    seen_objects: Set[int] = set()
    for step in step_objects:
        object_id = id(step)
        if object_id in seen_objects:
            continue
        seen_objects.add(object_id)
        needle = step.source_sentence or step.source_text or step.description or ""
        occurrence_key = planner._normalize_for_match(needle)
        start_at = occurrence_cursors.get(occurrence_key, 0)
        located = locate(needle, start_at=start_at)
        if located is None and start_at:
            # One source sentence may legitimately ground multiple atomic steps.
            # Reuse its span only when no later occurrence exists.
            located = locate(needle, start_at=0)
        if located is None:
            step.source_line_index = None
            continue
        source_start, source_end = located
        step.source_line_index = source_start
        if source_start >= start_at:
            occurrence_cursors[occurrence_key] = source_end


def normalize_blocks_and_steps(
    planner: PlanningHooks,
    plan: ProtocolPlan,
    procedure_text: str,
) -> None:
    if not plan or not (plan.blocks or []):
        return

    used_step_ids = {
        s.step_id for s in plan.iter_steps() if getattr(s, "step_id", None)
    }

    def alloc_step_id(block_id: str) -> str:
        k = 1
        while True:
            cand = f"{block_id}-AUTO{k:03d}"
            k += 1
            if cand not in used_step_ids:
                used_step_ids.add(cand)
                return cand

    for block in plan.blocks:
        groups = list(block.timeline_nodes or [])
        merged: Dict[str, TimepointGroup] = {}
        order_keys: List[str] = []

        for g in groups:
            lbl = (g.timepoint_label or "").strip() or "General"
            norm_lbl = planner._normalize_for_match(lbl)
            if norm_lbl not in merged:
                merged[norm_lbl] = TimepointGroup(
                    timepoint_label=lbl,
                    raw_text_segment=(g.raw_text_segment or "").strip() or None,
                    source_segments=list(g.source_segments or []),
                    steps=list(g.steps or []),
                )
                order_keys.append(norm_lbl)
            else:
                tgt = merged[norm_lbl]
                tgt.source_segments = list(dict.fromkeys((tgt.source_segments or []) + (g.source_segments or [])))
                if g.raw_text_segment:
                    seg = g.raw_text_segment.strip()
                    if seg:
                        if tgt.raw_text_segment:
                            tgt.raw_text_segment = (tgt.raw_text_segment.rstrip() + "\n" + seg).strip()
                        else:
                            tgt.raw_text_segment = seg
                tgt.steps.extend(list(g.steps or []))

        normalized_groups: List[TimepointGroup] = []
        for key in order_keys:
            g = merged[key]
            g.steps = [s for s in (g.steps or []) if getattr(s, "description", None) or getattr(s, "source_sentence", None) or getattr(s, "source_text", None)]
            if not g.steps:
                need = (g.raw_text_segment or "").strip()
                if need:
                    step_id = alloc_step_id(block.block_id)
                    step_line_index = None
                    if procedure_text and g.timepoint_label:
                        norm_label = planner._normalize_for_match(g.timepoint_label)
                        for i, ln in enumerate(procedure_text.splitlines()):
                            if norm_label and norm_label in planner._normalize_for_match(ln):
                                step_line_index = i
                                break
                    new_step = ProtocolStepSkeleton(
                        step_id=step_id,
                        phase=block.block_type,
                        title=f"{g.timepoint_label} actions",
                        action="execute_timepoint",
                        description=need,
                        source_text=need,
                        source_sentence=need,
                        block_ref_id=block.block_id,
                        source_line_index=step_line_index,
                        candidate_sources=[plan.primary_anchor_id] if getattr(plan, "primary_anchor_id", None) else [],
                        status=GroundedParamStatus.NEEDS_FILLING,
                    )
                    g.steps.append(new_step)
            if g.steps or (g.raw_text_segment or "").strip():
                normalized_groups.append(g)

        block.timeline_nodes = normalized_groups
    plan.bind_hierarchical_steps_as_canonical()
    plan.normalize_existing_structure_order()


def collapse_plan_to_single_block(plan: ProtocolPlan, block_type: str) -> ProtocolPlan:
    if not (plan.blocks or []):
        return plan

    combined_groups: List[TimepointGroup] = []
    combined_steps: List[ProtocolStepSkeleton] = []
    combined_sources: List[str] = []
    block_text_parts: List[str] = []
    block_id = "B01"

    for block in (plan.blocks or []):
        if block.block_source_text:
            block_text_parts.append(str(block.block_source_text))
        for seg_id in (block.source_segments or []):
            if seg_id and seg_id not in combined_sources:
                combined_sources.append(seg_id)
        for group in (block.timeline_nodes or []):
            for step in (group.steps or []):
                step.phase = block_type
                step.block_ref_id = block_id
                combined_steps.append(step)
            combined_groups.append(group)

    if not combined_steps:
        return plan

    collapsed = OperationBlock(
        block_id=block_id,
        block_type=block_type,
        raw_text="\n\n".join(part for part in block_text_parts if part).strip(),
        source_segments=combined_sources,
        timeline_nodes=combined_groups,
    )
    plan.blocks = [collapsed]
    plan.sections = [
        ProtocolSectionPlan(
            section_id=block_id,
            name=block_type,
            goal=f"Execute {block_type} steps",
            step_ids=[str(step.step_id) for step in combined_steps if getattr(step, "step_id", None)],
        )
    ]
    plan.bind_hierarchical_steps_as_canonical()
    return plan
