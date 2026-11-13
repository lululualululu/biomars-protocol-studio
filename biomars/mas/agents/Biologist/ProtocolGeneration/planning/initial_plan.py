"""Render the unchanged parser prompt and create the initial block plan."""

from .interfaces import PlanningHooks
from .state import PlanningInputs, ParsedPlan


async def parse_initial_plan(
    planner: PlanningHooks,
    inputs: PlanningInputs,
) -> ParsedPlan:
    """Render the unchanged parser prompt and create the initial block plan."""
    domain_pack = inputs.domain_pack
    evidence_bundle = inputs.evidence_bundle
    primary_id = inputs.primary_id
    planning_trace = inputs.planning_trace
    segment_analysis_bundle = inputs.segment_analysis_bundle
    template = inputs.template
    segment_analysis_text = inputs.segment_analysis_text
    cleaned_segment_index = inputs.cleaned_segment_index
    cleaned_procedure = inputs.cleaned_procedure
    cleaned_materials = inputs.cleaned_materials
    cleaned_reagent_setup = inputs.cleaned_reagent_setup
    cleaned_timing = inputs.cleaned_timing
    cleaned_troubleshooting = inputs.cleaned_troubleshooting
    parse_max_procedure = inputs.parse_max_procedure
    parse_max_materials = inputs.parse_max_materials
    parse_max_reagent_setup = inputs.parse_max_reagent_setup
    semantic_operation_bundle = inputs.semantic_operation_bundle
    shape_contract = inputs.shape_contract
    semantic_operation_checklist = inputs.semantic_operation_checklist
    semantic_timing_inventory = inputs.semantic_timing_inventory

    parsing_prompt = (
        template.replace("{method_text}", cleaned_procedure[:parse_max_procedure])
        .replace("{materials_text}", cleaned_materials[:parse_max_materials])
        .replace("{reagent_setup_text}", cleaned_reagent_setup[:parse_max_reagent_setup])
        .replace("{timing_text}", cleaned_timing[:parse_max_procedure])
        .replace("{troubleshooting_text}", cleaned_troubleshooting[:parse_max_procedure])
        .replace("{segment_index}", cleaned_segment_index)
        .replace("{segment_analysis}", segment_analysis_text or "(No segment analysis available)")
    )
    if semantic_operation_checklist:
        parsing_prompt += "\n\n" + semantic_operation_checklist + "\n"
    if semantic_timing_inventory:
        parsing_prompt += "\n\n" + semantic_timing_inventory + "\n"

    expected_timeline_anchors = planner._semantic_timeline_anchors(
        semantic_operation_bundle
    )
    if expected_timeline_anchors:
        parsing_prompt += (
            "\n\n---\n"
            "REQUIRED_SEMANTIC_TIMELINE_ANCHORS (must be represented as timeline_nodes):\n"
            + planner._format_required_timeline_anchors(expected_timeline_anchors)
            + "\n\n"
            "Preserve each source-defined node in its own workflow/branch context. "
            "Do not turn step_timing or repeat_schedule entries into artificial timeline nodes.\n"
        )
    shape_contract_prompt = planner._format_shape_contract_for_prompt(shape_contract)
    if shape_contract_prompt:
        parsing_prompt += "\n\n" + shape_contract_prompt + "\n"

    base_system = (
        "You are a precise scientific protocol parser. Return complete compact JSON "
        "without markdown, indentation, or explanatory text."
    )
    response = await planner._call_model(
        system_prompt=base_system,
        prompt=parsing_prompt,
        temperature=0.0,
    )

    plan = planner._parse_block_plan_from_llm(
        response, domain_pack, evidence_bundle,
        primary_anchor_id=primary_id,
        segment_analysis_bundle=segment_analysis_bundle,
    )
    planning_trace["plan"]["initial_parse"] = planner._planning_plan_trace(plan)

    return ParsedPlan(
        plan=plan,
        parsing_prompt=parsing_prompt,
        base_system=base_system,
        expected_timeline_anchors=expected_timeline_anchors,
        shape_contract_prompt=shape_contract_prompt,
    )
