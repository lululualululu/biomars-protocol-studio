"""
Protocol Schemas for Bio-MARS / InternAgent

This module defines Pydantic models for the Protocol Generation Task ("AutoProtocol").
It includes data structures for protocol requests, loaded documents, LLM-synthesized
domain content, and the comprehensive DomainPack.
"""

import re
from typing import Any, Dict, Iterator, List, Optional, Tuple
from enum import Enum
from pydantic import BaseModel, Field, ConfigDict

class ProtocolRequest(BaseModel):
    """User-facing request for a protocol generation run."""
    query: str
    title: Optional[str] = None
    cell_line: Optional[str] = None
    species: Optional[str] = None
    scope: str
    user_domain_docs: List[str] = Field(
        default_factory=list,
        description="Paths to user-provided domain docs (lab profile, old SOPs, etc.).",
    )


class LoadedDoc(BaseModel):
    """A domain document loaded from disk or other sources."""
    doc_id: str
    path: str
    title: Optional[str] = None
    source: str = "user"
    content: str
    summary: Optional[str] = None
    meta: Dict[str, Any] = Field(default_factory=dict)


class DomainPackLLMContent(BaseModel):
    """Portion of DomainPack that we expect the LLM to synthesize."""
    lab_profile: str
    constraints: Dict[str, Any]
    safety_rules: Dict[str, Any]
    env_capabilities: Dict[str, Any]


class DomainPack(BaseModel):
    """Normalized context for all downstream protocol agents."""
    task: ProtocolRequest
    lab_profile: str
    constraints: Dict[str, Any]
    safety_rules: Dict[str, Any]
    env_capabilities: Dict[str, Any]
    domain_docs: List[LoadedDoc] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    """
    A single piece of research evidence.
    Could be a paper, a protocol from protocols.io, or a datasheet.
    """
    id: str = Field(..., description="Unique identifier for this evidence item")
    source: str = Field(..., description="Source type: 'official', 'pmc', 'protocols_io', 'manual', etc.")
    title: Optional[str] = Field(None, description="Title of the paper or protocol")
    year: Optional[int] = Field(None, description="Publication year if available")
    citation_count: Optional[int] = Field(None, description="Number of citations if available")
    authority_score: float = Field(0.0, description="Computed score of relevance/authority (0-1)")
    url: Optional[str] = Field(None, description="URL to the source")
    markdown: str = Field(..., description="Cleaned content/text relevant to protocol generation")
    tags: List[str] = Field(default_factory=list, description="Tags like 'method', 'reagent', 'review'")
    meta: Dict[str, Any] = Field(default_factory=dict, description="Additional metadata (authors, journal, etc.)")
    extracted_params: Dict[str, Any] = Field(default_factory=dict, description="Parameters extracted from this evidence for grounding")


class EvidenceBundle(BaseModel):
    """
    Collection of gathered evidence for a protocol task.
    Produced by ProtocolResearchAgent.
    """
    items: List[EvidenceItem] = Field(default_factory=list, description="List of collected evidence items")
    anchor_ids: List[str] = Field(default_factory=list, description="IDs of 'anchor' protocols (primary references)")
    context_updates: Dict[str, Any] = Field(default_factory=dict, description="Dynamic context updates to pass back to session")


class GroundedParamStatus(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    NEEDS_FILLING = "NEEDS_FILLING"
    HEURISTIC = "HEURISTIC"
    ESTIMATED = "ESTIMATED"
    UNRESOLVED = "UNRESOLVED"


class ProtocolParameter(BaseModel):
    name: str = Field(..., description="Parameter name")
    value: Optional[str] = Field(None, description="Filled value or None")
    unit: Optional[str] = Field(None, description="Unit string, e.g., °C, min, µL")
    status: GroundedParamStatus = Field(default=GroundedParamStatus.NEEDS_FILLING, description="Filling status for this parameter")
    evidence_ids: List[str] = Field(default_factory=list, description="Evidence IDs supporting this parameter")
    citation: Optional[str] = Field(None, description="Text citation or quote from source")
    notes: Optional[str] = Field(None, description="Optional notes or rationale")


class CandidateSource(BaseModel):
    evidence_id: str = Field(..., description="Evidence ID for candidate source")
    reason: Optional[str] = Field(None, description="Short reason why this evidence is relevant")


class StepTiming(BaseModel):
    timepoint_label: str = Field(..., description='Label such as "Day 0", "Day 7", "Week 4", or "Hour 2"')
    offset_hours: Optional[float] = Field(
        None,
        description="Approximate offset from experiment start in hours",
    )
    duration_minutes: Optional[float] = Field(
        None,
        description="Duration of this step or incubation in minutes",
    )
    incubation: Optional[str] = Field(
        None,
        description='Incubation description, e.g., "overnight at 37°C, 5% CO₂"',
    )
    trigger: Optional[str] = Field(
        None,
        description='Trigger condition, e.g., "70–80% confluency"',
    )


class TimelineMilestone(BaseModel):
    label: Optional[str] = Field(None, description="Short description of the milestone")
    expected_timepoint: Optional[str] = Field(
        None,
        description='Expected timepoint label, e.g., "Day 7–10"',
    )
    qc: List[str] = Field(
        default_factory=list,
        description="QC checks or readouts associated with this milestone",
    )


class ProtocolTimeline(BaseModel):
    time_unit: str = Field(..., description='Dominant unit for the schedule, e.g., "day" or "hour"')
    start: str = Field(..., description='Label for the start timepoint, usually "Day 0"')
    milestones: List[TimelineMilestone] = Field(
        default_factory=list,
        description="Key biological or procedural milestones along the timeline",
    )


class ProtocolStepSkeleton(BaseModel):
    step_id: str = Field(..., description="Unique step identifier, e.g., S01")
    phase: str = Field(..., description="Name of the phase/section this step belongs to")
    title: str = Field(..., description="Short human-readable step name")
    action: str = Field(..., description="Machine-friendly verb in snake_case, e.g., seed_cells")
    description: Optional[str] = Field(None, description="Brief summary of the action")
    
    parameters_needed: List[str] = Field(default_factory=list, description="List of parameter names that need to be extracted")
    parameters: Dict[str, ProtocolParameter] = Field(default_factory=dict, description="Parameter map (filled during grounding)")
    
    # [NEW] The exact text chunk from the source method that this step is based on
    source_text: Optional[str] = Field(None, description="Exact source text chunk for this step")
    
    # [NEW] Parameters already extracted during the parsing phase (optional pre-grounding)
    parameters_extracted: Dict[str, Any] = Field(default_factory=dict, description="Parameters extracted during parsing phase")

    candidate_sources: List[str] = Field(default_factory=list, description="List of evidence IDs (anchors) relevant to this step")
    
    # [NEW] Original sentence from the method text, if available (more granular than source_text)
    source_sentence: Optional[str] = Field(None, description="Exact sentence(s) from source text describing this step")
    
    # [NEW] Reference ID to the parent OperationBlock
    block_ref_id: Optional[str] = Field(None, description="ID of the parent OperationBlock")

    # Retain the field name for serialized-plan compatibility; the value is a
    # source character offset so operations on the same Markdown line remain ordered.
    source_line_index: Optional[int] = Field(None, description="Character offset in the source text for ordering and traceability")

    # Branching logic for parallel workflows (for example, "Branch A" and "Branch B")
    branch_id: Optional[str] = Field(None, description="Identifier for the parallel branch this step belongs to")

    timing: Optional[StepTiming] = Field(
        None,
        description="Timing information for this step (timepoint, duration, trigger)",
    )
    
    inputs: List[str] = Field(default_factory=list, description="Logical inputs for this step")
    outputs: List[str] = Field(default_factory=list, description="Logical outputs for this step")
    
    status: GroundedParamStatus = Field(default=GroundedParamStatus.NEEDS_FILLING, description="Overall status for this step")
    notes: Optional[str] = Field(None, description="Optional notes or TODOs")
    meta: Dict[str, Any] = Field(default_factory=dict, description="Metadata for processing state, e.g., retrieval mode, unresolved counts.")


class TimepointGroup(BaseModel):
    """
    Groups steps by a specific timepoint within an OperationBlock.
    """
    timepoint_label: str = Field(..., description='Label such as "Day 0", "General", or "Week 1"')
    raw_text_segment: Optional[str] = Field(None, description="Optional raw text segment corresponding to this timepoint")
    source_segments: List[str] = Field(
        default_factory=list,
        description="Segment IDs supporting this timepoint group",
    )
    steps: List[ProtocolStepSkeleton] = Field(default_factory=list, description="List of steps for this timepoint")


class ProtocolSectionPlan(BaseModel):
    section_id: str = Field(..., description="Unique section identifier")
    name: str = Field(..., description="Human-readable name for the section")
    goal: Optional[str] = Field(None, description="Brief description of the section's purpose")
    step_ids: List[str] = Field(default_factory=list, description="List of step IDs in this section")


class OperationBlock(BaseModel):
    """
    Represents a high-level operational unit (e.g., 'Cell Culture', 'Cell Isolation').
    Maps to a contiguous chunk of the original method text.
    Now supports hierarchical timepoint grouping.
    """
    model_config = ConfigDict(populate_by_name=True)
    block_id: str = Field(..., description="Unique block identifier")
    block_type: str = Field(..., description="Type of operation, e.g., 'Culture', 'Isolation', 'Analysis'")
    block_source_text: str = Field(
        ...,
        alias="raw_text",
        description="The complete source text paragraph(s) for this block",
    )
    source_segments: List[str] = Field(
        default_factory=list,
        description="Segment IDs supporting this block",
    )
    
    # [Refactored] Use hierarchical timepoint groups instead of flat steps
    timeline_nodes: List[TimepointGroup] = Field(default_factory=list, description="List of timepoint groups within this block")
    
    inputs: List[str] = Field(default_factory=list, description="Input materials/samples for this block")
    outputs: List[str] = Field(default_factory=list, description="Output products of this block")


# ===========================
# Segment Analysis (Pre-Planning)
# ===========================

# Note: content_types is a flexible List[str] - LLM dynamically identifies
# what types of content each segment provides (e.g., "background", "reagents",
# "procedure", "timing", "troubleshooting", "results", "figures", etc.)
# This allows adaptation to any biological protocol structure.


class SegmentAnalysis(BaseModel):
    """
    LLM-analyzed content description for a single segment.
    Used to guide Plan and Writer to the correct segment for specific content types.
    """
    segment_id: str = Field(..., description="Unique segment identifier")
    segment_file: str = Field(..., description="Path to the segment markdown file")
    title: str = Field(..., description="Section title from the heading")
    level: int = Field(..., description="Heading level (1 for #, 2 for ##)")
    order: int = Field(..., description="Order index in the document (1-based)")
    
    # LLM-generated content analysis
    content_summary: str = Field(..., description="Brief description of what this section contains")
    content_types: List[str] = Field(
        default_factory=list, 
        description="LLM-identified content types (flexible, e.g., 'reagents', 'procedure', 'timing', 'troubleshooting')"
    )
    suggested_uses: List[str] = Field(
        default_factory=list, 
        description="Suggested uses for protocol generation (e.g., 'extract reagent concentrations', 'get incubation times')"
    )
    key_entities: List[str] = Field(
        default_factory=list,
        description="Key entities mentioned (chemicals, genes, equipment, cell lines, etc.)"
    )
    
    # Metadata
    char_count: int = Field(0, description="Character count of the segment")
    has_tables: bool = Field(False, description="Whether the segment contains tables")
    has_lists: bool = Field(False, description="Whether the segment contains lists")
    has_parameters: bool = Field(False, description="Whether the segment contains quantitative parameters")


class SegmentAnalysisBundle(BaseModel):
    """
    Collection of segment analyses for a document.
    Produced by segment analysis step before planning.
    """
    evidence_id: str = Field(..., description="Evidence/anchor ID this analysis belongs to")
    source_file: str = Field(..., description="Path to the original document")
    segments_dir: str = Field(..., description="Path to the segments directory")
    analyses: List[SegmentAnalysis] = Field(default_factory=list, description="List of segment analyses")
    total_segments: int = Field(0, description="Total number of segments")
    analyzed_at: str = Field("", description="ISO timestamp of when analysis was performed")
    
    def get_segments_by_type(self, content_type: str) -> List[SegmentAnalysis]:
        """Get all segments that provide a specific content type (case-insensitive match)."""
        content_type_lower = content_type.lower()
        return [s for s in self.analyses 
                if any(content_type_lower in ct.lower() for ct in s.content_types)]
    
    def get_segment_for_use(self, use_keyword: str) -> List[SegmentAnalysis]:
        """Get segments whose suggested_uses contain the keyword."""
        keyword_lower = use_keyword.lower()
        return [s for s in self.analyses 
                if any(keyword_lower in u.lower() for u in s.suggested_uses)]


class ProtocolPlan(BaseModel):
    plan_id: str = Field(..., description="Unique plan identifier")
    title: str = Field(..., description="Human-readable plan title")
    description: Optional[str] = Field(None, description="Optional description of the plan")
    task: ProtocolRequest = Field(..., description="Original protocol request")
    domain_pack: DomainPack = Field(..., description="Domain context for planning")
    sections: List[ProtocolSectionPlan] = Field(default_factory=list, description="List of planned sections")
    
    # [NEW] List of high-level operation blocks
    blocks: List[OperationBlock] = Field(default_factory=list, description="List of high-level operation blocks")
    
    steps: List[ProtocolStepSkeleton] = Field(
        default_factory=list,
        exclude=True,
        description=(
            "Legacy in-memory compatibility view. New serialized plans store steps "
            "only under blocks[*].timeline_nodes[*].steps."
        ),
    )
    timeline: Optional[ProtocolTimeline] = Field(
        None,
        description="High-level experiment schedule and milestones",
    )
    anchor_ids: List[str] = Field(default_factory=list, description="Anchor evidence IDs used for planning")
    primary_anchor_id: Optional[str] = Field(None, description="The ID of the primary anchor protocol used for methods-first planning")
    
    # [NEW] Segment analysis for content routing
    segment_analysis_bundle: Optional[SegmentAnalysisBundle] = Field(
        None, 
        description="LLM-analyzed segment descriptions for routing content to correct sources"
    )
    recommendation_guide: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Structured evidence-backed operational guidance extracted from results, discussion, "
            "and troubleshooting sections. Used to distinguish default SOP choices, discouraged "
            "comparators, conditional branches, applicability boundaries, evaluation-only methods, "
            "named preparation definitions, negative-result boundaries, comparison-arm boundaries, "
            "and assay/readout layouts when recipes or execution limits are explicitly supported by the source."
        ),
    )
    query_focus_profile: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Prompt-derived profile describing the query's true operational focus, including "
            "primary sections, minimal supporting sections, de-emphasized sections, and scope notes. "
            "Used to keep the final SOP aligned to the user's requested task rather than the full paper."
        ),
    )

    def model_post_init(self, _context: Any) -> None:
        if not self.bind_hierarchical_steps_as_canonical() and self.steps:
            self._promote_legacy_flat_steps()
            self.bind_hierarchical_steps_as_canonical()

    def iter_steps(self) -> Iterator[ProtocolStepSkeleton]:
        """Iterate over canonical steps in block/node/source order."""
        hierarchical_steps = [
            step
            for block in (self.blocks or [])
            for timepoint in (block.timeline_nodes or [])
            for step in (timepoint.steps or [])
        ]
        if hierarchical_steps:
            yield from hierarchical_steps
            return
        # Defensive compatibility for an object mutated after validation. Normal
        # deserialization promotes a legacy flat plan into blocks in model_post_init.
        yield from (self.steps or [])

    def step_list(self) -> List[ProtocolStepSkeleton]:
        """Return a materialized canonical step view for callers that need a list."""
        return list(self.iter_steps())

    def step_index(self) -> Dict[str, ProtocolStepSkeleton]:
        """Index canonical steps by id without creating a second stored step list."""
        index: Dict[str, ProtocolStepSkeleton] = {}
        for step in self.iter_steps():
            step_id = str(step.step_id or "").strip()
            if step_id and step_id not in index:
                index[step_id] = step
        return index

    def step_count(self) -> int:
        """Return the number of canonical hierarchical steps."""
        return sum(1 for _ in self.iter_steps())

    def _promote_legacy_flat_steps(self) -> int:
        """Convert a historical flat-only plan into the canonical hierarchy.

        Existing block metadata is reused when present. Steps are grouped by their
        block reference/phase and then by explicit timing label; untimed legacy
        steps remain under ``General`` rather than receiving an invented day.
        """
        legacy_steps = list(self.steps or [])
        if not legacy_steps:
            return 0

        blocks = list(self.blocks or [])
        block_by_id = {
            str(block.block_id or ""): block
            for block in blocks
            if str(block.block_id or "")
        }
        block_by_type = {
            str(block.block_type or ""): block
            for block in blocks
            if str(block.block_type or "")
        }
        section_by_name = {
            str(section.name or ""): section
            for section in (self.sections or [])
            if str(section.name or "")
        }
        section_by_id = {
            str(section.section_id or ""): section
            for section in (self.sections or [])
            if str(section.section_id or "")
        }
        node_maps: Dict[str, Dict[str, TimepointGroup]] = {}
        node_orders: Dict[str, List[str]] = {}

        for step in legacy_steps:
            phase = str(step.phase or "Procedure").strip() or "Procedure"
            requested_id = str(step.block_ref_id or "").strip()
            block = block_by_id.get(requested_id) if requested_id else None
            if block is None:
                block = block_by_type.get(phase)
            if block is None:
                section = section_by_name.get(phase)
                block_id = (
                    str(section.section_id or "").strip()
                    if section is not None
                    else f"legacy-B{len(blocks) + 1:02d}"
                )
                block = OperationBlock(
                    block_id=block_id,
                    block_type=phase,
                    block_source_text="",
                    timeline_nodes=[],
                )
                blocks.append(block)
                block_by_id[block_id] = block
                block_by_type[phase] = block

            step.block_ref_id = block.block_id
            timepoint_label = (
                str(step.timing.timepoint_label or "").strip()
                if step.timing is not None
                else ""
            ) or "General"
            block_key = str(block.block_id or "")
            node_map = node_maps.setdefault(block_key, {})
            node_order = node_orders.setdefault(block_key, [])
            if timepoint_label not in node_map:
                node_map[timepoint_label] = TimepointGroup(
                    timepoint_label=timepoint_label,
                    steps=[],
                )
                node_order.append(timepoint_label)
            node_map[timepoint_label].steps.append(step)

        for block in blocks:
            block_key = str(block.block_id or "")
            if block_key in node_maps:
                block.timeline_nodes = [
                    node_maps[block_key][label]
                    for label in node_orders[block_key]
                ]
                if not block.block_source_text:
                    block.block_source_text = "\n".join(
                        str(step.source_sentence or step.source_text or step.description or "").strip()
                        for node in block.timeline_nodes
                        for step in (node.steps or [])
                        if str(step.source_sentence or step.source_text or step.description or "").strip()
                    )

            step_ids = [
                str(step.step_id or "")
                for node in (block.timeline_nodes or [])
                for step in (node.steps or [])
                if str(step.step_id or "")
            ]
            section = section_by_id.get(block_key)
            if section is None:
                section = ProtocolSectionPlan(
                    section_id=block_key,
                    name=block.block_type,
                    goal=f"Execute {block.block_type} steps",
                    step_ids=step_ids,
                )
                self.sections.append(section)
                section_by_id[block_key] = section
            else:
                section.step_ids = step_ids

        self.blocks = blocks
        return len(legacy_steps)

    def bind_hierarchical_steps_as_canonical(self) -> int:
        """
        Rebind the in-memory compatibility view to the hierarchical Step objects.

        Structure-first Writer and rendering consume block/timeline Steps. Historical
        payloads may contain both representations. The hierarchy is always
        authoritative; new serialization excludes the top-level compatibility field.
        """
        hierarchical_steps = [
            step
            for block in (self.blocks or [])
            for timepoint in (block.timeline_nodes or [])
            for step in (timepoint.steps or [])
        ]
        if hierarchical_steps:
            self.steps = hierarchical_steps
        return len(hierarchical_steps)

    def normalize_existing_structure_order(self) -> None:
        """
        Preserve planner-authored block and step order while synchronizing views.

        This is deliberately a structure-only operation: it does not add, remove,
        relabel, or rewrite protocol content. Explicit, non-overlapping numeric
        timepoints remain safe to sort chronologically. When timepoints overlap or
        share a start value, their planner-authored order is authoritative because
        source offsets often reflect paper layout rather than execution order.
        """
        if not self.blocks:
            return

        def numeric_key(label: str) -> Tuple[int, int]:
            values = [
                int(value)
                for value in re.findall(r"(?<!\d)[+-]?\d+", label or "")
            ]
            return (min(values), max(values)) if values else (999999, 999999)

        for block in self.blocks:
            nodes = list(block.timeline_nodes or [])
            numeric_keys = [numeric_key(node.timepoint_label or "") for node in nodes]
            ordered_numeric_keys = sorted(numeric_keys)
            use_chronological_order = bool(nodes) and all(
                key[0] != 999999 for key in numeric_keys
            ) and all(
                previous[1] < current[0]
                for previous, current in zip(
                    ordered_numeric_keys,
                    ordered_numeric_keys[1:],
                )
            )
            if use_chronological_order:
                block.timeline_nodes = [
                    node
                    for _, node in sorted(
                        enumerate(nodes),
                        key=lambda item: (
                            numeric_key(item[1].timepoint_label or "")[0],
                            numeric_key(item[1].timepoint_label or "")[1],
                            item[0],
                        ),
                    )
                ]
            else:
                block.timeline_nodes = nodes

        # Reagent preparation is an explicit prerequisite block in the
        # structure-first contract. A local planning repair may append it after the
        # procedure, so move only these standardized blocks to the front while
        # preserving relative order everywhere else.
        def is_reagent_preparation_block(block: OperationBlock) -> bool:
            label = re.sub(
                r"\s+",
                " ",
                str(block.block_type or "").replace("–", "-").strip().lower(),
            )
            return bool(re.search(r"\breagent\s+(?:preparation|setup)\b", label))

        preparation_blocks = [
            block for block in self.blocks if is_reagent_preparation_block(block)
        ]
        workflow_blocks = [
            block for block in self.blocks if not is_reagent_preparation_block(block)
        ]
        self.blocks = preparation_blocks + workflow_blocks

        section_by_id = {
            str(section.section_id or ""): section
            for section in (self.sections or [])
        }
        for block in self.blocks:
            section = section_by_id.get(str(block.block_id or ""))
            if section is not None:
                section.step_ids = [
                    step.step_id
                    for node in (block.timeline_nodes or [])
                    for step in (node.steps or [])
                    if step.step_id
                ]
        section_rank = {
            str(block.block_id or ""): index
            for index, block in enumerate(self.blocks)
        }
        self.sections = sorted(
            list(self.sections or []),
            key=lambda section: (
                section_rank.get(
                    str(section.section_id or ""),
                    len(section_rank),
                ),
                str(section.section_id or ""),
            ),
        )
        self.bind_hierarchical_steps_as_canonical()


# Public model names retained from the original Agent import path.
__all__ = [
    "ProtocolRequest",
    "LoadedDoc",
    "DomainPackLLMContent",
    "DomainPack",
    "EvidenceItem",
    "EvidenceBundle",
    "GroundedParamStatus",
    "ProtocolParameter",
    "CandidateSource",
    "StepTiming",
    "TimelineMilestone",
    "ProtocolTimeline",
    "ProtocolStepSkeleton",
    "TimepointGroup",
    "ProtocolSectionPlan",
    "OperationBlock",
    "SegmentAnalysis",
    "SegmentAnalysisBundle",
    "ProtocolPlan",
]
