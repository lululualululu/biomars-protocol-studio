"""Adapt model JSON to the protocol hierarchy; no model calls or planning policy.

Optional callbacks preserve the Agent's existing subclass override points.
Normal domain callers can use these functions without an Agent.
"""

import json
import logging
import uuid
from typing import Any, Callable, Dict, List, Optional

try:
    import json_repair
except ImportError:
    json_repair = None

from ..protocol_schemas import (
    DomainPack,
    EvidenceBundle,
    GroundedParamStatus,
    OperationBlock,
    ProtocolPlan,
    ProtocolSectionPlan,
    ProtocolStepSkeleton,
    StepTiming,
    TimepointGroup,
)
from .text import strip_code_fences

logger = logging.getLogger(__name__)


def blocks_from_flat_plan_payload(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    steps_data = [s for s in (data.get("steps") or []) if isinstance(s, dict)]
    if not steps_data:
        return []

    section_order: List[str] = []
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        name = str(sec.get("name") or sec.get("section_id") or "").strip()
        if name and name not in section_order:
            section_order.append(name)

    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for step in steps_data:
        phase = str(step.get("phase") or step.get("section") or "Procedure").strip() or "Procedure"
        grouped.setdefault(phase, []).append(step)
        if phase not in section_order:
            section_order.append(phase)

    blocks: List[Dict[str, Any]] = []
    for idx, phase in enumerate(section_order, start=1):
        phase_steps = grouped.get(phase) or []
        if not phase_steps:
            continue
        source_lines = [
            str(
                step.get("source_sentence")
                or step.get("source_text")
                or step.get("description")
                or step.get("title")
                or ""
            ).strip()
            for step in phase_steps
        ]
        source_lines = [line for line in source_lines if line]
        blocks.append(
            {
                "block_id": f"B{idx:02d}",
                "block_type": phase,
                "raw_text": "\n".join(source_lines),
                "timeline_nodes": [
                    {
                        "timepoint_label": "General",
                        "steps": phase_steps,
                    }
                ],
            }
        )
    return blocks


def normalize_block_plan_payload(
    data: Any,
    *,
    group_flat_steps: Callable[[Dict[str, Any]], List[Dict[str, Any]]] = blocks_from_flat_plan_payload,
) -> Dict[str, Any]:
    if isinstance(data, list):
        logger.warning("Block plan payload is a top-level list; treating it as 'blocks'.")
        return {"blocks": data}
    if not isinstance(data, dict):
        raise ValueError(f"Block plan JSON must be an object, got {type(data)}")

    for wrapper_key in ("protocol_plan", "plan", "block_plan", "parsed_plan", "result"):
        wrapped = data.get(wrapper_key)
        if isinstance(wrapped, dict):
            wrapped_has_plan = any(
                key in wrapped
                for key in ("blocks", "operation_blocks", "operationBlocks", "sections", "steps")
            )
            if wrapped_has_plan:
                logger.warning("Unwrapped block plan payload from '%s'.", wrapper_key)
                data = wrapped
                break

    if "blocks" not in data:
        for alias in ("operation_blocks", "operationBlocks", "procedure_blocks", "protocol_blocks"):
            alias_value = data.get(alias)
            if isinstance(alias_value, list):
                logger.warning("Using '%s' as block plan 'blocks'.", alias)
                data["blocks"] = alias_value
                break

    if "blocks" not in data and isinstance(data.get("steps"), list):
        logger.warning("Block plan payload is flat; grouping top-level steps into blocks by phase.")
        data["blocks"] = group_flat_steps(data)

    blocks = data.get("blocks")
    if isinstance(blocks, dict):
        for key in ("items", "blocks", "operation_blocks", "procedure_blocks"):
            if isinstance(blocks.get(key), list):
                data["blocks"] = blocks[key]
                break

    return data


def parse_block_plan_from_llm(
    text: str,
    domain_pack: DomainPack,
    evidence_bundle: EvidenceBundle,
    primary_anchor_id: Optional[str] = None,
    segment_analysis_bundle: Optional[Dict[str, Any]] = None,
    *,
    strip_fences: Callable[[str], str] = strip_code_fences,
    normalize_payload: Callable[[Any], Dict[str, Any]] = normalize_block_plan_payload,
    json_repair_module: Any = json_repair,
) -> ProtocolPlan:
    """
    Parse the block-based JSON output from the LLM into a ProtocolPlan.
    Populates 'blocks', 'sections', and flattened 'steps'.
    Handles hierarchical structure: OperationBlock -> TimepointGroup -> ProtocolStepSkeleton
    """
    cleaned_text = strip_fences(text)
    data = {}
    try:
        data = json.loads(cleaned_text)
    except json.JSONDecodeError as e:
        logger.warning(f"Standard JSON parse failed: {e}. Attempting Repair...")

        if json_repair_module:
            try:
                data = json_repair_module.loads(cleaned_text)
                logger.info("JSON successfully repaired by json_repair.")
            except Exception as repair_e:
                logger.error(f"Critical: Even json_repair failed: {repair_e}")
                raise ValueError(f"LLM output is unfixable JSON. Raw: {cleaned_text[:200]}...")
        else:
            try:
                fixed_str = cleaned_text.replace('\n', '\\n')
                data = json.loads(fixed_str)
            except:
                logger.error("JSON repair failed (json_repair not installed).")
                raise e

    data = normalize_payload(data)
    if "blocks" not in data:
        raise ValueError("JSON missing 'blocks' key")

    blocks_data = data["blocks"]
    blocks: List[OperationBlock] = []
    sections: List[ProtocolSectionPlan] = []

    def _coerce_str_list(val: Any) -> List[str]:
        if isinstance(val, list):
            return [str(v) for v in val if v is not None]
        if isinstance(val, str) and val.strip():
            return [val.strip()]
        return []

    for b_idx, b_data in enumerate(blocks_data):
        block_id = b_data.get("block_id") or f"B{b_idx+1:02d}"
        block_type = b_data.get("block_type", "Unknown")
        block_source_segments = _coerce_str_list(b_data.get("source_segments"))

        timeline_nodes_data = b_data.get("timeline_nodes", [])

        if not timeline_nodes_data and "steps" in b_data:
            timeline_nodes_data = [{
                "timepoint_label": "General",
                "steps": b_data["steps"]
            }]

        parsed_timeline_groups: List[TimepointGroup] = []
        block_step_ids: List[str] = []

        for t_idx, t_node in enumerate(timeline_nodes_data):
            timepoint_label = t_node.get("timepoint_label", "General")
            timepoint_source_segments = _coerce_str_list(t_node.get("source_segments"))
            steps_data = t_node.get("steps", [])

            group_steps: List[ProtocolStepSkeleton] = []

            for s_idx, s_data in enumerate(steps_data):
                step_id = s_data.get("step_id") or f"{block_id}-T{t_idx}-S{s_idx+1:02d}"

                title = s_data.get("step_name") or s_data.get("title")
                action = s_data.get("action") or "generic_action"
                source_sentence = s_data.get("source_sentence") or s_data.get("source_text") or s_data.get("description")

                if not title or title.lower() in ["untitled step", "step", "untitled"]:
                    if action and action != "generic_action":
                        title = action.replace("_", " ").title()
                    elif source_sentence:
                        title = (source_sentence[:30] + "...") if len(source_sentence) > 30 else source_sentence
                    else:
                        title = f"Step {step_id}"

                if not source_sentence:
                     logger.warning(f"Step {step_id} missing 'source_sentence'. Available keys: {list(s_data.keys())}")

                params_extracted_raw = (
                    s_data.get("parameters_extracted") or 
                    s_data.get("extracted_parameters") or 
                    s_data.get("params") or 
                    []
                )
                params_extracted_dict = {}
                if isinstance(params_extracted_raw, list) and params_extracted_raw:
                    params_extracted_dict = {"extracted_list": params_extracted_raw}
                elif isinstance(params_extracted_raw, dict):
                    params_extracted_dict = params_extracted_raw

                planner_execution_role = str(
                    s_data.get("execution_role")
                    or s_data.get("semantic_role")
                    or ""
                ).strip().lower()
                if planner_execution_role not in {
                    "operation",
                    "operation_constraint",
                    "evaluation_only",
                    "context_only",
                }:
                    planner_execution_role = ""
                step_meta = {}
                if planner_execution_role:
                    step_meta = {
                        "planner_execution_role": planner_execution_role,
                        "execution_role": planner_execution_role,
                    }

                step_obj = ProtocolStepSkeleton(
                    step_id=step_id,
                    phase=block_type,
                    title=title,
                    action=action,
                    description=source_sentence, # Use source sentence as description
                    source_text=source_sentence, # CRITICAL: Map to source_text for Writer
                    source_sentence=source_sentence,
                    block_ref_id=block_id,
                    parameters={},
                    parameters_extracted=params_extracted_dict,
                    parameters_needed=s_data.get("parameters_needed", []),
                    inputs=s_data.get("inputs", []),
                    outputs=s_data.get("outputs", []),
                    candidate_sources=[primary_anchor_id] if primary_anchor_id else [],
                    status=GroundedParamStatus.NEEDS_FILLING,
                    branch_id=s_data.get("branch_id"), # [NEW] Populate branch_id
                    meta=step_meta,
                )

                timing_data = s_data.get("timing", {})
                if not isinstance(timing_data, dict): 
                    timing_data = {}

                final_timepoint = timing_data.get("timepoint_label") or timepoint_label

                # Use the same timing schema as the legacy Planning path.
                # Do not silently discard model-supplied offsets or conditions.
                step_obj.timing = StepTiming(
                    **dict(timing_data, timepoint_label=final_timepoint)
                )

                group_steps.append(step_obj)
                block_step_ids.append(step_id)

            parsed_group = TimepointGroup(
                timepoint_label=timepoint_label,
                raw_text_segment=t_node.get("raw_text_segment"),
                source_segments=timepoint_source_segments,
                steps=group_steps
            )
            parsed_timeline_groups.append(parsed_group)

        op_block = OperationBlock(
            block_id=block_id,
            block_type=block_type,
            block_source_text=b_data.get("block_source_text") or b_data.get("raw_text", ""),
            source_segments=block_source_segments,
            inputs=b_data.get("inputs", []),
            outputs=b_data.get("outputs", []),
            timeline_nodes=parsed_timeline_groups # Hierarchical field
        )
        blocks.append(op_block)

        section_obj = ProtocolSectionPlan(
            section_id=block_id, 
            name=block_type,
            goal=f"Execute {block_type} steps",
            step_ids=block_step_ids
        )
        sections.append(section_obj)

    plan = ProtocolPlan(
        plan_id=str(uuid.uuid4()),
        title=f"Protocol Plan: {domain_pack.task.query}",
        description="Generated by Structure-First Planner (Block-Based Hierarchy)",
        task=domain_pack.task,
        domain_pack=domain_pack,
        sections=sections,
        blocks=blocks,
        anchor_ids=evidence_bundle.anchor_ids,
        primary_anchor_id=primary_anchor_id,
        segment_analysis_bundle=segment_analysis_bundle,  # [NEW] Include segment analysis
    )

    return plan
