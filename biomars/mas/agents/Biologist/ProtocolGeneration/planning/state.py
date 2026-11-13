"""Explicit data passed between Planning workflow stages.

Containers are frozen; the existing Plan and trace dictionaries remain shared
and mutable. RepairResult carries the latest procedure text into scope checks.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..protocol_schemas import DomainPack, EvidenceBundle, ProtocolPlan


@dataclass(frozen=True)
class PlanningInputs:
    """Prepared views, evidence, source identity, parser template and run trace."""
    domain_pack: DomainPack
    evidence_bundle: EvidenceBundle
    primary_id: str
    planning_trace: Dict[str, Any]
    planning_trace_path: str
    view_procedure_text: str
    analyses: List[Dict[str, Any]]
    segments_dir: str
    segment_analysis_bundle: Optional[Dict[str, Any]]
    template: str
    segment_analysis_text: str
    cleaned_segment_index: str
    cleaned_procedure: str
    cleaned_materials: str
    cleaned_reagent_setup: str
    cleaned_timing: str
    cleaned_troubleshooting: str
    grounding_support_views: Dict[str, str]
    parse_max_procedure: int
    parse_max_materials: int
    parse_max_reagent_setup: int
    semantic_operation_bundle: Dict[str, Any]
    shape_contract: Dict[str, Any]
    semantic_operation_checklist: str
    semantic_timing_inventory: str


@dataclass(frozen=True)
class ParsedPlan:
    """Initial Plan and the exact prompt context reused by repair paths."""
    plan: ProtocolPlan
    parsing_prompt: str
    base_system: str
    expected_timeline_anchors: List[Dict[str, Any]]
    shape_contract_prompt: str


@dataclass(frozen=True)
class RepairResult:
    """Accepted Plan and the latest procedure text, including partial recovery."""
    plan: ProtocolPlan
    cleaned_procedure: str
