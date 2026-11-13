"""Structural interface for existing Planning hooks; no Agent imports."""

from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union

from ..protocol_schemas import DomainPack, EvidenceBundle, OperationBlock, ProtocolPlan


class PlanningHooks(Protocol):
    """Stages call the original hooks so subclasses keep their existing behavior."""

    prompts_dir: Path

    async def _analyze_segments(
        self,
        outline_path: str,
        evidence_id: str,
        source_file: str,
        segments_dir: str,
        task_query: str = '',
    ) -> Optional[Dict[str, Any]]:
        ...

    async def _apply_grounding_filter_with_continuity_guard(
        self,
        task_query: str,
        procedure_text: str,
        plan: ProtocolPlan,
        analyses: List[Dict[str, Any]],
        semantic_operation_bundle: Optional[Dict[str, Any]] = None,
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> Tuple[ProtocolPlan, Dict[str, Any]]:
        ...

    def _apply_query_focus_profile(self, plan: ProtocolPlan, profile: Dict[str, Any]) -> None: ...

    def _assign_source_line_indices(self, plan: ProtocolPlan, reference_text: str) -> None: ...

    def _audit_semantic_timeline_coverage(
        self,
        anchors: List[Dict[str, Any]],
        plan: ProtocolPlan,
    ) -> Dict[str, Any]:
        ...

    async def _augment_view_with_rescue_segments(
        self,
        procedure_view_text: str,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        rescue_segment_ids: List[str],
        task_query: str = '',
        missing_structures: Optional[List[Dict[str, str]]] = None,
        max_chars: Optional[int] = None,
    ) -> str:
        ...

    @staticmethod
    def _build_semantic_timepoint_snippets(
        text: str,
        anchors: List[Dict[str, Any]],
        target_anchor_ids: List[str],
        max_chars: int,
    ) -> Dict[str, str]:
        ...

    def _build_shape_block_repair_jobs(
        self,
        procedure_text: str,
        missing_structures: List[Dict[str, str]],
        shape_contract: Optional[Dict[str, Any]],
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        rescue_segment_ids: List[str],
        max_chars: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        ...

    def _build_structure_repair_snippet(
        self,
        procedure_text: str,
        missing_structures: List[Dict[str, str]],
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        rescue_segment_ids: List[str],
        timing_anchors: Optional[List[Dict[str, Any]]] = None,
        max_chars: Optional[int] = None,
    ) -> str:
        ...

    async def _call_model(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        schema: Optional[Dict[str, Any]] = None,
        temperature: Optional[float] = None,
        structured_output: bool = False,
    ) -> Union[str, Dict[str, Any]]:
        ...

    @staticmethod
    def _compact_structure_guard_trace(report: Dict[str, Any]) -> Dict[str, Any]: ...

    @staticmethod
    def _compact_view_verification_trace(report: Dict[str, Any]) -> Dict[str, Any]: ...

    async def _extract_query_focus_profile(
        self,
        plan: ProtocolPlan,
        task_query: str,
    ) -> Dict[str, Any]:
        ...

    def _filter_plan_to_grounded_steps(
        self,
        plan: ProtocolPlan,
        reference_text: str,
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> ProtocolPlan:
        ...

    @staticmethod
    def _format_required_timeline_anchors(anchors: List[Dict[str, Any]]) -> str: ...

    @staticmethod
    def _format_semantic_operation_checklist(bundle: Dict[str, Any]) -> str: ...

    @staticmethod
    def _format_semantic_timing_inventory(bundle: Dict[str, Any]) -> str: ...

    def _format_shape_contract_for_prompt(self, contract: Dict[str, Any]) -> str: ...

    async def _infer_protocol_shape_contract_semantic(
        self,
        task_query: str,
        procedure_text: str,
    ) -> Dict[str, Any]:
        ...

    async def _load_or_extract_semantic_operation_units(
        self,
        task_query: str,
        view_texts: Dict[str, str],
        cache_path: str,
    ) -> Dict[str, Any]:
        ...

    def _merge_missing_timepoints(
        self,
        base: ProtocolPlan,
        extra: ProtocolPlan,
    ) -> ProtocolPlan:
        ...

    def _merge_structure_repairs(self, base: ProtocolPlan, extra: ProtocolPlan) -> ProtocolPlan: ...

    def _normalize_blocks_and_steps(self, plan: ProtocolPlan, procedure_text: str) -> None: ...

    def _normalize_for_match(self, text: str) -> str: ...

    def _normalize_planner_input(self, text: str) -> str: ...

    def _parse_block_plan_from_llm(
        self,
        text: str,
        domain_pack: DomainPack,
        evidence_bundle: EvidenceBundle,
        primary_anchor_id: Optional[str] = None,
        segment_analysis_bundle: Optional[Dict[str, Any]] = None,
    ) -> ProtocolPlan:
        ...

    @staticmethod
    def _persist_effective_views(artifacts: Dict[str, Any], view_texts: Dict[str, str]) -> None: ...

    @staticmethod
    def _persist_planning_trace(path: str, trace: Dict[str, Any]) -> None: ...

    @staticmethod
    def _planning_plan_trace(plan: ProtocolPlan) -> Dict[str, Any]: ...

    def _planning_view_trace(self, text: str) -> Dict[str, Any]: ...

    def _reconcile_query_focus_profile_with_plan(
        self,
        plan: ProtocolPlan,
        profile: Dict[str, Any],
        *,
        restored_section_names: Optional[List[str]] = None,
        full_revert: bool = False,
    ) -> List[str]:
        ...

    @staticmethod
    def _resolve_structure_first_artifacts_dir(
        context: Dict[str, Any],
        methods_md_path: str,
        session_id: str,
    ) -> Path:
        ...

    def _restore_query_focus_sections(
        self,
        plan: ProtocolPlan,
        pre_focus_plan: ProtocolPlan,
        section_names: List[str],
    ) -> List[str]:
        ...

    @classmethod
    def _select_segmentation_source(
        cls,
        methods_md_path: str,
        methods_md: str,
        raw_fulltext_md_path: Optional[str],
        raw_fulltext_md: str,
    ) -> Tuple[str, str]:
        ...

    @staticmethod
    def _semantic_timeline_anchors(bundle: Dict[str, Any]) -> List[Dict[str, Any]]: ...

    @staticmethod
    def _should_attempt_structure_repair(
        plan_guard: Dict[str, Any],
        semantic_operation_bundle: Optional[Dict[str, Any]] = None,
    ) -> bool:
        ...

    def _structure_issue_score(self, guard: Dict[str, Any]) -> int: ...

    def _summarize_plan_outline(self, plan: ProtocolPlan) -> Dict[str, Any]: ...

    async def _verify_and_repair_procedure_view(
        self,
        task_query: str,
        procedure_view_text: str,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        semantic_operation_bundle: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ...

    async def _verify_and_repair_reagent_setup_view(
        self,
        task_query: str,
        reagent_setup_view_text: str,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        semantic_operation_bundle: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ...

    async def _verify_parsed_plan_structure(
        self,
        task_query: str,
        procedure_text: str,
        plan: ProtocolPlan,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        shape_contract: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ...

    async def _verify_query_focus_preservation(
        self,
        task_query: str,
        pre_focus_plan: ProtocolPlan,
        post_focus_plan: ProtocolPlan,
        query_focus_profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        ...

    async def build_and_persist_views(
        self,
        analyses: List[Dict[str, Any]],
        anchors_dir: str,
        evidence_id: str,
        segments_dir: str,
        max_segments_per_view: int = 6,
        task_query: str = '',
    ) -> Dict[str, str]:
        ...

    def _build_query_focus_context(self, plan: ProtocolPlan) -> Dict[str, Any]:
        ...

    def _normalize_query_focus_profile(self, data: Any) -> Dict[str, Any]:
        ...

    def _normalize_section_label(self, text: str) -> str:
        ...

    def _block_has_traceable_evidence(
        self,
        block: OperationBlock,
        procedure_text: str,
    ) -> bool:
        ...

    def _compact_segment_catalog(self, analyses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        ...

    def _extract_view_headings(self, text: str, limit: int = 12) -> List[str]:
        ...

    def _infer_protocol_shape_contract(
        self,
        task_query: str,
        procedure_text: str,
    ) -> Dict[str, Any]:
        ...

    def _text_supported_by_grounding_views(
        self,
        candidate_text: str,
        procedure_text: str,
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> bool:
        ...

    def _text_supports_span(self, reference_text: str, candidate_text: str) -> bool:
        ...

    def _apply_grounding_filter_if_safe(
        self,
        task_query: str,
        procedure_text: str,
        plan: ProtocolPlan,
        analyses: List[Dict[str, Any]],
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> Tuple[ProtocolPlan, Dict[str, Any]]:
        ...

    def _collect_unsupported_steps(
        self,
        plan: ProtocolPlan,
        reference_text: str,
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        ...

    def _extract_markdown_sections(self, text: str) -> List[Tuple[str, str]]:
        ...

    @staticmethod
    def _locate_semantic_quote(
        source_chunk: str,
        quote: str,
        start_at: int = 0,
    ) -> Optional[Tuple[int, int, str]]:
        ...

    def _plan_text_covers_structure(
        self,
        plan_text: str,
        label: str,
        aliases: Optional[List[str]] = None,
    ) -> bool:
        ...

    def _read_segment_text_map(
        self,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
    ) -> Dict[str, str]:
        ...

    def _uses_shape_block_level_repair(self, contract: Optional[Dict[str, Any]]) -> bool:
        ...
