import os
import json
import logging
import re
from collections import Counter
from typing import Dict, Any, List, Optional, Tuple, Set

from ....models.base_model import BaseModel
from .writer import inputs as writer_inputs
from .writer import response as writer_response
from .writer import rendering as writer_rendering
from ...base_agent import BaseAgent
from .protocol_schemas import (
    ProtocolPlan,
    ProtocolStepSkeleton,
)
from .protocol_text_fidelity import numeric_fact_tokens, strip_leading_source_ordinal

logger = logging.getLogger(__name__)

_extract_balanced_json_substring = writer_response._extract_balanced_json_substring

class ProtocolGroundedWriterAgent(BaseAgent):
    """
    Agent responsible for Phase 4: Protocol Grounded Writing.
    It renders the source-grounded hierarchical ProtocolPlan as Markdown while
    preserving block, timeline, branch, step-marker, and citation contracts.
    """
    
    agent_type = "protocol_grounded_writer"

    def __init__(self, model: BaseModel, config: Dict[str, Any]):
        super().__init__(model, config)
        self.writing_prompt_template = (
            "You are an expert protocol writer.\n"
            "Task: Rewrite grounded protocol content without changing its structure or facts.\n\n"
            "Title: {title}\n"
            "Goal: {goal}\n\n"
            "Grounded Blocks Structure (JSON):\n"
            "{steps_json}\n\n"
            "Evidence Scope Notes (JSON):\n"
            "{evidence_scope_notes_json}\n\n"
            "Deterministic Validation Feedback (JSON):\n"
            "{validation_feedback_json}\n\n"
            "Allowed evidence_ids (the renderer adds citations; do not write citations): {allowed_evidence_ids}\n\n"
            "Instructions:\n"
            "1. Return one JSON object only; do not return Markdown headings, numbering, citations, or code fences.\n"
            "2. Use exactly this schema:\n"
            "   {{\"overview\": \"...\", \"materials_reagents\": \"...\", "
            "\"equipment\": \"...\", \"safety_sterility\": \"...\", "
            "\"step_text_by_id\": {{\"<step_id>\": \"...\"}}, "
            "\"normalization_candidates\": ["
            "{{\"step_id\": \"...\", \"source_fragment\": \"...\", "
            "\"normalized_fragment\": \"...\", \"category\": \"...\", "
            "\"reason\": \"...\"}}]}}\n"
            "3. `step_text_by_id` may contain only step IDs present in the grounded Plan.\n"
            "4. Rewrite each step as one clear, executable instruction. Preserve its meaning, "
            "branch context, every concrete value, unit, condition, and source-supported constraint.\n"
            "5. Do not combine, split, omit, reorder, or invent steps. Do not add facts from general knowledge.\n"
            "6. For `execution_role=operation_constraint`, rewrite the constraint faithfully; "
            "the renderer will format it as a note.\n"
            "7. The four global fields are Markdown bodies only. Treat Materials/Reagents as a concise "
            "inventory; those facts belong only in the corresponding grounded procedure step.\n"
            "8. Use Evidence Scope Notes in the overview when applicable, without broadening their scope.\n"
            "9. Validation feedback identifies deterministic fidelity problems only. Correct the relevant Plan node; "
            "do not create new sections or actions.\n"
            "10. You may normalize likely PDF/OCR or typography damage, but never do so silently. "
            "Add a `normalization_candidates` record only when the proposal interprets or changes a "
            "numeric value, unit, scientific notation, entity, source meaning, or source-internal "
            "contradiction. Ordinary grammar, spacing, punctuation, and Unicode styling that leave all "
            "facts unchanged need no record. Do not use external knowledge to fill missing facts.\n"
            "11. A normalization candidate is only a proposal. An independent evidence audit decides "
            "whether the candidate may be rendered. Report source contradictions even when you preserve "
            "the original text. Do not expose internal Plan step IDs in reader-facing prose; express "
            "cross-step references in plain language while preserving their scope.\n"
        )

    async def execute(
        self,
        context: Dict[str, Any],
        tools: Dict[str, Any],  # noqa: ARG002 - Required by the BaseAgent contract.
    ) -> Dict[str, Any]:
        sid = context.get("session_id") or "unknown"
        logger.info(f"{self.name}: Starting Grounded Writing for session {sid}")

        # 1. Retrieve Context
        try:
            plan_raw = context.get("protocol_plan")
            protocol_plan = self._coerce_pydantic(plan_raw, ProtocolPlan, "protocol_plan")
            protocol_plan.normalize_existing_structure_order()
            anchor_paths_input = context.get("anchor_methods_paths") or {}
        except Exception as e:
            logger.error(f"Context validation failed: {e}")
            raise ValueError(f"Context validation failed: {e}")

        manifest_path = context.get("anchors_manifest_path")
        validation_feedback = context.get("validation_feedback")
        if validation_feedback is None:
            # Persisted pre-validator sessions may still carry this context key.
            validation_feedback = context.get("critic_feedback")

        anchor_paths, valid_count, total_count = self._resolve_anchor_paths(
            sid,
            anchor_paths_input,
            manifest_path,
        )

        # [NEW] Single Source Restriction (Structure-First Consistency)
        # If 'primary_anchor_id' is present (passed from Orchestrator/Planning), enforce it strictly.
        primary_anchor_id = context.get("primary_anchor_id")

        if primary_anchor_id and primary_anchor_id in anchor_paths:
            logger.info(f"Enforcing Single Source Mode using primary anchor: {primary_anchor_id}")
            # Restrict paths and allowed IDs to ONLY the primary anchor
            anchor_paths = {primary_anchor_id: anchor_paths[primary_anchor_id]}
            
        if total_count and not valid_count:
            logger.warning(
                f"{self.name}: All {total_count} anchor paths from context were invalid for session {sid}."
            )

        grounding_input_empty = False
        if not anchor_paths:
            grounding_input_empty = True
            logger.warning(
                f"{self.name}: No valid anchor fulltexts available for session {sid}; "
                "rendering the existing source-grounded Plan without anchor metadata."
            )

        allowed_evidence_ids: List[str] = []
        if anchor_paths:
            allowed_evidence_ids = list(anchor_paths.keys())
        else:
            anchor_ids = context.get("anchor_ids") or []
            allowed_evidence_ids = list(anchor_ids)

        anchor_catalog = self._load_anchor_catalog(manifest_path)
        if allowed_evidence_ids:
            allowed_set = set(str(x) for x in allowed_evidence_ids if x)
            anchor_catalog = {k: v for k, v in anchor_catalog.items() if k in allowed_set}

        # 2. Render the source-grounded Plan. Writer does not perform a second
        # parameter extraction or hidden retrieval pass.
        logger.info("Starting Writing Sub-routine...")

        render_trace: Dict[str, Any] = {}
        final_markdown = await self._run_writing(
            protocol_plan,
            allowed_evidence_ids,
            anchor_catalog=anchor_catalog,
            validation_feedback=validation_feedback,
            render_trace=render_trace,
        )

        return {
            "protocol_plan": self._dump_pydantic(protocol_plan),
            "final_markdown": final_markdown,
            "grounding_input_empty": grounding_input_empty,
            "writer_render_trace": render_trace,
        }

    async def _run_writing(
        self,
        plan: ProtocolPlan,
        allowed_evidence_ids: List[str],
        anchor_catalog: Optional[Dict[str, Dict[str, str]]] = None,
        validation_feedback: Optional[Dict[str, Any]] = None,
        render_trace: Optional[Dict[str, Any]] = None,
    ) -> str:
        task_query = plan.task.query if plan.task else "Generate Protocol"
        for step in plan.iter_steps():
            if "writer_normalization_audit" in (step.meta or {}):
                step.meta = dict(step.meta or {})
                step.meta.pop("writer_normalization_audit", None)
        query_prefers_analysis = self._plan_preserves_assessment_readouts(plan)
        primary_block_keys = {
            self._normalize_heading_key(str(name))
            for name in (
                (plan.query_focus_profile or {}).get("primary_section_names")
                or []
            )
            if str(name).strip()
        }

        # [Refactored] Construct hierarchical summary for writer prompt
        hierarchical_summary = []
        evidence_scope_notes = self._derive_evidence_scope_notes(
            allowed_evidence_ids,
            anchor_catalog,
        )

        # Helper to format a single step
        def format_step_for_prompt(step: ProtocolStepSkeleton) -> Dict[str, Any]:
            return {
                "step_id": step.step_id,
                "action": step.action,
                "description": strip_leading_source_ordinal(step.description),
                "source_sentence": strip_leading_source_ordinal(
                    step.source_sentence or step.source_text or ""
                ),
                "branch_id": getattr(step, "branch_id", None), # [NEW] Pass branch info
                "execution_role": (step.meta or {}).get("execution_role", "operation"),
                "candidate_sources": list(step.candidate_sources or []),
                "planner_extracted_constraints": step.parameters_extracted or {},
            }

        # ProtocolPlan promotes historical flat-only input at its loading boundary.
        # Writer therefore consumes only the canonical block -> timeline -> step form.
        for block in plan.blocks or []:
            block_is_primary = (
                self._normalize_heading_key(str(block.block_type or ""))
                in primary_block_keys
            )
            include_block_readouts = query_prefers_analysis or block_is_primary
            block_summary = {
                "block_id": block.block_id,
                "block_type": self._sanitize_heading_text(block.block_type),
                "inputs": block.inputs or [],
                "raw_text": block.block_source_text or "",
                "timeline_nodes": [],
            }
            for timepoint in block.timeline_nodes or []:
                visible_steps = [
                    step
                    for step in timepoint.steps
                    if self._step_is_renderable_for_query(
                        step,
                        include_block_readouts,
                    )
                ]
                if not visible_steps:
                    continue
                block_summary["timeline_nodes"].append(
                    {
                        "timepoint_label": self._sanitize_heading_text(
                            timepoint.timepoint_label
                        ),
                        "steps": [
                            format_step_for_prompt(step) for step in visible_steps
                        ],
                    }
                )
            if block_summary["timeline_nodes"]:
                hierarchical_summary.append(block_summary)
            else:
                logger.info(
                    "Skipping block %s because it has no renderable canonical steps.",
                    block.block_id,
                )

        steps_json = json.dumps(hierarchical_summary, indent=2, ensure_ascii=False)
        validation_prompt_payload: Dict[str, Any] = {}
        if isinstance(validation_feedback, dict) and validation_feedback:
            validation_prompt_payload = {
                "summary": validation_feedback.get("summary"),
                "approved": validation_feedback.get("approved"),
                "issue_codes": validation_feedback.get("issue_codes") or [],
                "issues": (validation_feedback.get("issues") or [])[:8],
                "rewrite_requests": (
                    validation_feedback.get("rewrite_requests") or []
                )[:8],
            }
        prompt = self.writing_prompt_template.format(
            title=plan.title,
            goal=task_query,
            steps_json=steps_json,
            evidence_scope_notes_json=json.dumps(evidence_scope_notes, indent=2, ensure_ascii=False),
            validation_feedback_json=json.dumps(
                validation_prompt_payload,
                indent=2,
                ensure_ascii=False,
            ),
            allowed_evidence_ids=", ".join(sorted(set(allowed_evidence_ids))) or "none",
        )
        
        response = await self._call_model(system_prompt="You are a protocol writer.", prompt=prompt)
        expected_step_id_order = [
            str(step.get("step_id") or "").strip()
            for block in hierarchical_summary
            for node in (block.get("timeline_nodes") or [])
            for step in (node.get("steps") or [])
            if str(step.get("step_id") or "").strip()
        ]
        expected_step_ids = set(expected_step_id_order)
        writer_payload = self._parse_writer_payload(
            response,
            expected_step_ids,
            render_trace=render_trace,
        )
        normalization_candidates = self._collect_normalization_candidates(
            hierarchical_summary,
            writer_payload,
        )
        normalization_decisions = await self._audit_normalization_candidates(
            normalization_candidates
        )
        self._apply_normalization_audit(
            plan,
            hierarchical_summary,
            writer_payload,
            normalization_candidates,
            normalization_decisions,
        )
        rendered_step_count = len(writer_payload.get("step_text_by_id") or {})
        missing_rewrite_step_ids = [
            step_id
            for step_id in expected_step_id_order
            if step_id not in (writer_payload.get("step_text_by_id") or {})
        ]
        if render_trace is not None:
            render_trace.update(
                {
                    "expected_step_count": len(expected_step_id_order),
                    "expected_step_ids": expected_step_id_order,
                    "llm_returned_step_count": rendered_step_count,
                    "missing_rewrite_step_ids": missing_rewrite_step_ids,
                    "normalization_candidate_count": len(normalization_candidates),
                    "normalization_decision_count": len(normalization_decisions),
                    "normalization_applied_count": sum(
                        1
                        for step in plan.iter_steps()
                        if bool(
                            ((step.meta or {}).get("writer_normalization_audit") or {}).get(
                                "applied"
                            )
                        )
                    ),
                }
            )
        if rendered_step_count != len(expected_step_ids):
            logger.warning(
                "Writer returned controlled text for %d/%d steps; "
                "missing steps will use their canonical Plan descriptions.",
                rendered_step_count,
                len(expected_step_ids),
            )
        markdown = self._assemble_protocol_markdown(
            goal=task_query,
            block_summaries=hierarchical_summary,
            writer_payload=writer_payload,
            evidence_scope_notes=evidence_scope_notes,
            allowed_evidence_ids=allowed_evidence_ids,
            anchor_catalog=anchor_catalog or {},
            render_trace=render_trace,
        )
        if render_trace is not None:
            render_trace["fallback_step_ids"] = self._ordered_unique(
                render_trace.get("fallback_step_ids") or []
            )
            logger.info(
                "Writer render trace: expected=%d returned=%d fallback=%d unexpected=%d",
                render_trace.get("expected_step_count", 0),
                render_trace.get("llm_returned_step_count", 0),
                len(render_trace.get("fallback_step_ids") or []),
                len(render_trace.get("unexpected_step_ids") or []),
            )
        return markdown

    def _coerce_pydantic(self, obj, cls, name: str):
        if obj is None:
            return None
        if isinstance(obj, cls):
            return obj
        if isinstance(obj, dict):
            return cls(**obj)
        raise TypeError(f"{name} must be {cls.__name__} or dict, got {type(obj)}")

    def _dump_pydantic(self, obj):
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "dict"):
            return obj.dict()
        return obj

    # Evidence inputs and scope: compatibility methods keep existing overrides working.

    def _load_anchor_catalog(self, manifest_path: Optional[str]) -> Dict[str, Dict[str, str]]:
        return writer_inputs.load_anchor_catalog(manifest_path)

    def _resolve_anchor_paths(
        self,
        session_id: str,
        anchor_paths: Dict[str, str],
        manifest_path: Optional[str],
    ) -> Tuple[Dict[str, str], int, int]:
        return writer_inputs.resolve_anchor_paths(
            session_id,
            anchor_paths,
            manifest_path,
            agent_name=self.name,
            logger=logger,
        )

    def _derive_evidence_scope_notes(
        self,
        allowed_evidence_ids: List[str],
        anchor_catalog: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> List[str]:
        return writer_inputs.derive_evidence_scope_notes(allowed_evidence_ids, anchor_catalog)

    def _plan_preserves_assessment_readouts(self, plan: ProtocolPlan) -> bool:
        return writer_inputs.plan_preserves_assessment_readouts(plan)

    @staticmethod
    def _step_is_renderable_for_query(
        step: ProtocolStepSkeleton,
        include_readouts: bool,
    ) -> bool:
        return writer_inputs.step_is_renderable_for_query(step, include_readouts)

    # Response parsing and source-fact audits: compatibility methods keep existing overrides working.

    def _safe_parse_json(self, text: str) -> Any:
        return writer_response.safe_parse_json(
            text,
            _extract_balanced_json_substring=_extract_balanced_json_substring,
            logger=logger,
        )

    def _parse_writer_payload(
        self,
        response: str,
        expected_step_ids: Set[str],
        render_trace: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return writer_response.parse_writer_payload(
            response,
            expected_step_ids,
            render_trace,
            safe_parse_json=self._safe_parse_json,
            sanitize_step_rewrite=self._sanitize_step_rewrite,
            logger=logger,
        )

    def _sanitize_step_rewrite(self, text: str) -> str:
        return writer_response.sanitize_step_rewrite(
            text,
            strip_markdown_fences=self._strip_markdown_fences,
        )

    @staticmethod
    def _numeric_tokens(text: str) -> List[str]:
        return writer_response.numeric_tokens(text, numeric_fact_tokens=numeric_fact_tokens)

    def _select_faithful_step_text(
        self,
        step: Dict[str, Any],
        rewrite: Optional[str],
    ) -> str:
        return writer_response.select_faithful_step_text(
            step,
            rewrite,
            numeric_tokens=self._numeric_tokens,
            strip_leading_source_ordinal=strip_leading_source_ordinal,
        )

    def _collect_normalization_candidates(
        self,
        block_summaries: List[Dict[str, Any]],
        writer_payload: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        return writer_response.collect_normalization_candidates(
            block_summaries,
            writer_payload,
            numeric_tokens=self._numeric_tokens,
            strip_leading_source_ordinal=strip_leading_source_ordinal,
        )

    async def _audit_normalization_candidates(
        self,
        candidates: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        return await writer_response.audit_normalization_candidates(
            candidates,
            call_model=self._call_model,
            safe_parse_json=self._safe_parse_json,
            logger=logger,
        )

    def _apply_normalization_audit(
        self,
        plan: ProtocolPlan,
        block_summaries: List[Dict[str, Any]],
        writer_payload: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        decisions: List[Dict[str, Any]],
    ) -> None:
        return writer_response.apply_normalization_audit(
            plan,
            block_summaries,
            writer_payload,
            candidates,
            decisions,
        )

    def _strip_markdown_fences(self, md: str) -> str:
        return writer_response.strip_markdown_fences(md)

    # Markdown rendering: compatibility methods keep existing overrides working.

    def _sanitize_heading_text(self, text: Optional[str]) -> str:
        return writer_rendering.sanitize_heading_text(text)

    def _convert_citations_to_numeric(
        self,
        md: str,
        anchor_catalog: Dict[str, Dict[str, str]],
        allowed_evidence_ids: List[str],
    ) -> str:
        return writer_rendering.convert_citations_to_numeric(
            md,
            anchor_catalog,
            allowed_evidence_ids,
        )

    def _render_global_body(self, value: Any, fallback: str) -> str:
        return writer_rendering.render_global_body(value, fallback)

    def _assemble_protocol_markdown(
        self,
        goal: str,
        block_summaries: List[Dict[str, Any]],
        writer_payload: Dict[str, Any],
        evidence_scope_notes: List[str],
        allowed_evidence_ids: List[str],
        anchor_catalog: Dict[str, Dict[str, str]],
        render_trace: Optional[Dict[str, Any]] = None,
    ) -> str:
        return writer_rendering.assemble_protocol_markdown(
            goal,
            block_summaries,
            writer_payload,
            evidence_scope_notes,
            allowed_evidence_ids,
            anchor_catalog,
            render_trace,
            convert_citations_to_numeric=self._convert_citations_to_numeric,
            normalize_and_enforce_citations=self._normalize_and_enforce_citations,
            normalize_references_section=self._normalize_references_section,
            render_block_markdown=self._render_block_markdown,
            render_global_body=self._render_global_body,
        )

    def _normalize_references_section(self, md: str) -> str:
        return writer_rendering.normalize_references_section(md)

    def _normalize_heading_key(self, title: str) -> str:
        return writer_rendering.normalize_heading_key(title)

    @staticmethod
    def _ordered_unique(values: List[str]) -> List[str]:
        return writer_rendering.ordered_unique(values)

    def _render_block_markdown(
        self,
        block_summary: Dict[str, Any],
        step_text_by_id: Optional[Dict[str, str]] = None,
        allowed_evidence_ids: Optional[Set[str]] = None,
        render_trace: Optional[Dict[str, Any]] = None,
    ) -> str:
        return writer_rendering.render_block_markdown(
            block_summary,
            step_text_by_id,
            allowed_evidence_ids,
            render_trace,
            select_faithful_step_text=self._select_faithful_step_text,
        )

    def _normalize_and_enforce_citations(self, md: str, allowed_ids: Set[str]) -> str:
        return writer_rendering.normalize_and_enforce_citations(md, allowed_ids)
