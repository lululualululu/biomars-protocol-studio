import json
import logging
import os
import re
import hashlib
import copy
from bisect import bisect_left
from typing import Dict, Any, List, Optional, Tuple, Set
from pathlib import Path

from .planning import (
    input_preparation,
    initial_plan,
    plan_parsing,  # Compatibility export for existing module-level callers.
    plan_repairs,
    plan_checks,
    query_scope,
)
from .planning import artifacts as planning_artifacts
from .planning import plan_parser as planning_parser
from .planning import text as planning_text
from .planning.plan_parser import json_repair

from ....models.base_model import BaseModel
from ...base_agent import BaseAgent
from .protocol_schemas import (
    ProtocolPlan,
    ProtocolStepSkeleton,
    StepTiming,
    DomainPack,
    EvidenceBundle,
    OperationBlock,
    TimepointGroup,
)

logger = logging.getLogger(__name__)

DEFAULT_VIEW_CHAR_BUDGETS = {
    "materials": 2500,
    "timing": 2500,
    "troubleshooting": 2500,
}

SEMANTIC_OPERATION_UNITS_VERSION = "1.4"

class ProtocolPlanningAgent(BaseAgent):
    """
    Agent responsible for Phase 3: Protocol Planning.
    It takes a DomainPack (context) and EvidenceBundle (anchors) and generates
    a high-level skeletal plan (ProtocolPlan) using an LLM.
    """

    agent_type = "protocol_planning"

    # Entry points and input/output boundaries.

    def __init__(self, model: BaseModel, config: Dict[str, Any]):
        super().__init__(model, config)
        self.prompts_dir = Path(__file__).resolve().parents[5] / "biomars" / "mas" / "assets" / "protocol_generation"

    async def execute(self, context: Dict[str, Any], tools: Dict[str, Any]) -> Dict[str, Any]:
        """
        Standard execute method for BaseAgent compatibility.

        Args:
            context: Must contain 'domain_pack' and optionally 'evidence_bundle'.
                     'session_id' is helpful for logging.
        """
        sid = context.get("session_id") or context.get("sid") or "unknown"
        logger.info(f"{self.name}: Starting Protocol Planning for session {sid}")

        try:
            domain_pack = self._coerce_pydantic(context.get("domain_pack"), DomainPack, "domain_pack")
        except TypeError as e:
            raise ValueError(f"Missing or invalid 'domain_pack' in context: {e}")

        ev_raw = context.get("evidence_bundle")
        if not ev_raw:
            logger.warning("Missing 'evidence_bundle' in context. Proceeding with empty evidence.")
            evidence_bundle = EvidenceBundle(items=[], anchor_ids=[])
        else:
            evidence_bundle = self._coerce_pydantic(ev_raw, EvidenceBundle, "evidence_bundle")

        plan = await self._generate_block_plan(context, domain_pack, evidence_bundle)
        return {"protocol_plan": self._dump_pydantic(plan)}

    async def run(self, session: Any, context: Dict[str, Any], **kwargs) -> ProtocolPlan:
        """
        Legacy run method wrapper around execute().
        Maintains backward compatibility for direct calls.
        """
        sid = getattr(session, "id", None) or getattr(session, "session_id", None) or "unknown"

        exec_ctx = {
            **(context or {}),
            "session_id": sid,
            "domain_pack": (context or {}).get("domain_pack"),
            "evidence_bundle": (context or {}).get("evidence_bundle")
        }

        result = await self.execute(exec_ctx, tools={})

        context["protocol_plan"] = result["protocol_plan"]

        return ProtocolPlan(**result["protocol_plan"])

    def _coerce_pydantic(self, obj, cls, name: str):
        """Helper to coerce dict to Pydantic model if needed."""
        if obj is None:
            return None
        if isinstance(obj, cls):
            return obj
        if isinstance(obj, dict):
            return cls(**obj)
        raise TypeError(f"{name} must be {cls.__name__} or dict, got {type(obj)}")

    def _dump_pydantic(self, obj):
        """Helper to dump Pydantic model to dict."""
        if hasattr(obj, "model_dump"):
            return obj.model_dump()
        if hasattr(obj, "dict"):
            return obj.dict()
        return obj

    # Structure-first workflow orchestration.

    async def _generate_block_plan(
        self,
        context: Dict[str, Any],
        domain_pack: DomainPack,
        evidence_bundle: EvidenceBundle
    ) -> ProtocolPlan:
        """Prepare inputs, parse a draft, repair it, then apply the requested scope."""
        inputs = await input_preparation.prepare_inputs(
            self, context, domain_pack, evidence_bundle, logger=logger,
        )
        draft = await initial_plan.parse_initial_plan(self, inputs)
        repaired = await plan_repairs.repair_plan(self, inputs, draft, logger=logger)
        plan = await query_scope.apply_query_scope(self, inputs, repaired, logger=logger)

        inputs.planning_trace["plan"]["final"] = self._planning_plan_trace(plan)
        self._persist_planning_trace(inputs.planning_trace_path, inputs.planning_trace)
        return plan

    # Source selection, segment analysis, and view construction.

    @classmethod
    def _select_segmentation_source(
        cls,
        methods_md_path: str,
        methods_md: str,
        raw_fulltext_md_path: Optional[str],
        raw_fulltext_md: str,
    ) -> Tuple[str, str]:
        methods_heading_count = cls._markdown_heading_count(methods_md)
        raw_heading_count = cls._markdown_heading_count(raw_fulltext_md)

        if raw_fulltext_md_path and os.path.exists(raw_fulltext_md_path):
            if methods_heading_count >= 2 and methods_heading_count > raw_heading_count:
                return methods_md_path, methods_md
            return raw_fulltext_md_path, raw_fulltext_md
        return methods_md_path, methods_md

    async def _analyze_segments(
        self,
        outline_path: str,
        evidence_id: str,
        source_file: str,
        segments_dir: str,
        task_query: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        Analyze each segment using LLM to generate content descriptions.

        Args:
            outline_path: Path to the __outline.json file
            evidence_id: Evidence/anchor ID
            source_file: Path to the original document
            segments_dir: Path to the segments directory

        Returns:
            Dict with segment analysis bundle data, or None if failed
        """
        from .sources.protocol_outline_segmenter import (
            prepare_segments_for_analysis,
            save_segment_analysis,
            parse_segment_analysis_response,
        )

        if not outline_path or not os.path.exists(outline_path):
            logger.warning(f"Cannot analyze segments: outline not found at {outline_path}")
            return None

        segments_data = prepare_segments_for_analysis(
            outline_path,
            task_query=task_query,
        )
        if not segments_data:
            logger.warning("No segments to analyze")
            return None

        logger.info(f"Analyzing {len(segments_data)} segments for {evidence_id}...")

        analyses = []
        system_prompt = "You are a scientific document analyzer. Output valid JSON only."

        for seg in segments_data:
            try:
                response = await self._call_model(
                    system_prompt=system_prompt,
                    prompt=seg["prompt"],
                )

                parsed = parse_segment_analysis_response(response)

                analysis = {
                    "segment_id": seg["segment_id"],
                    "segment_file": seg["segment_file"],
                    "title": seg["title"],
                    "level": seg["level"],
                    "order": seg["order"],
                    "char_count": seg["char_count"],
                    "content_summary": parsed.get("content_summary", ""),
                    "content_types": parsed.get("content_types", ["OTHER"]),
                    "primary_view": parsed.get("primary_view", ""),
                    "query_relevance": parsed.get("query_relevance", ""),
                    "suggested_uses": parsed.get("suggested_uses", []),
                    "key_entities": parsed.get("key_entities", []),
                    "has_tables": parsed.get("has_tables", False),
                    "has_lists": parsed.get("has_lists", False),
                    "has_parameters": parsed.get("has_parameters", False),
                }
                analyses.append(analysis)

                logger.debug(f"Analyzed segment {seg['segment_id']}: {seg['title'][:30]}...")

            except Exception as e:
                logger.warning(f"Failed to analyze segment {seg['segment_id']}: {e}")
                analyses.append({
                    "segment_id": seg["segment_id"],
                    "segment_file": seg["segment_file"],
                    "title": seg["title"],
                    "level": seg["level"],
                    "order": seg["order"],
                    "char_count": seg["char_count"],
                    "content_summary": f"Section: {seg['title']}",
                    "content_types": ["OTHER"],
                    "primary_view": "",
                    "query_relevance": "",
                    "suggested_uses": [],
                    "key_entities": [],
                    "has_tables": False,
                    "has_lists": False,
                    "has_parameters": False,
                })

        analysis_path = save_segment_analysis(
            evidence_id=evidence_id,
            source_file=source_file,
            segments_dir=segments_dir,
            analyses=analyses,
        )

        logger.info(f"Saved segment analysis to {analysis_path}")

        return {
            "evidence_id": evidence_id,
            "source_file": source_file,
            "segments_dir": segments_dir,
            "analyses": analyses,
            "total_segments": len(analyses),
            "analysis_path": analysis_path,
        }

    async def build_and_persist_views(
        self,
        analyses: List[Dict[str, Any]],
        anchors_dir: str,
        evidence_id: str,
        segments_dir: str,
        max_segments_per_view: int = 6,
        task_query: str = "",
    ) -> Dict[str, str]:
        os.makedirs(anchors_dir, exist_ok=True)

        def _safe_list(val: Any) -> List[str]:
            if isinstance(val, list):
                return [str(v) for v in val if v is not None]
            return []

        def _read_excerpt(path: str, limit: int) -> str:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    text = f.read()
                if len(text) > limit:
                    return text[:limit].rstrip()
                return text
            except Exception:
                return ""

        bucket_synonyms = {
            "procedure": {"procedure", "methods", "protocol_steps", "workflow", "steps"},
            "materials": {"materials", "reagents", "chemicals", "antibodies", "equipment", "supplies", "catalog"},
            "reagent_setup": {
                "reagent_setup",
                "reagent_preparation",
                "stock_solutions",
                "buffer_recipes",
                "media_preparation",
                "solution_preparation",
            },
            "timing": {"timing", "schedule", "duration", "timeline"},
            "troubleshooting": {"troubleshooting", "problem", "problems", "solutions", "tips", "common_problems"},
        }

        procedure_allow_types = {"procedure", "methods", "protocol_steps", "workflow", "steps"}

        def _has_reagent_setup_semantics(types_in: List[str]) -> bool:
            types_set = {str(item).lower() for item in (types_in or []) if item}
            return bool(types_set.intersection(bucket_synonyms["reagent_setup"]))

        def _is_reagent_setup_candidate(
            primary_view_in: str,
            query_relevance_in: str,
            types_in: List[str],
        ) -> bool:
            types_set = {str(item).lower() for item in (types_in or []) if item}
            primary_view = str(primary_view_in or "").strip().lower()
            query_relevance = str(query_relevance_in or "").strip().lower()
            if query_relevance == "off_query":
                return False
            return primary_view == "reagent_setup" or (
                _has_reagent_setup_semantics(types_in)
                and not types_set.intersection(procedure_allow_types)
            )

        def _is_procedure_candidate(
            primary_view_in: str,
            query_relevance_in: str,
            types_in: List[str],
        ) -> bool:
            types_set = set(t.lower() for t in (types_in or []) if t)
            primary_view = str(primary_view_in or "").strip().lower()
            query_relevance = str(query_relevance_in or "").strip().lower()
            if not primary_view or query_relevance == "off_query":
                return False
            if primary_view == "procedure":
                return True
            # Some source sections combine inventories/recipes with concrete
            # operations. This decision still relies only on LLM-produced roles.
            return (
                primary_view in {"materials", "reagent_setup"}
                and bool(types_set.intersection(procedure_allow_types))
            )

        regex_map = {
            "troubleshooting": re.compile(
                r"\b(problem|cause|solution|troubleshoot|if\s+.*then|if\s+.*,\s*use)\b",
                flags=re.IGNORECASE,
            ),
        }

        def _safe_truncate(text: str, limit: int, marker: str = " [TRUNCATED]") -> Tuple[str, bool, int]:
            if not text:
                return "", False, 0
            if len(text) <= limit:
                return text, False, len(text)
            cut = limit
            boundary = text.rfind("\n", 0, limit)
            if boundary < 0:
                boundary = text.rfind(" ", 0, limit)
            if boundary > 0:
                cut = boundary
            truncated = text[:cut].rstrip()
            suffix = marker or ""
            return truncated + suffix, True, cut

        def _find_anchor(text: str, patterns: List[re.Pattern]) -> Tuple[str, int]:
            for pat in patterns:
                m = pat.search(text)
                if m:
                    return pat.pattern, m.start()
            return "", -1

        def _document_order_key(item: Dict[str, Any]) -> Tuple[int, str]:
            order_val = item.get("order")
            if isinstance(order_val, (int, float)):
                return int(order_val), str(item.get("segment_id") or "")
            if isinstance(order_val, str) and order_val.strip().isdigit():
                return int(order_val.strip()), str(item.get("segment_id") or "")
            seg_id = str(item.get("segment_id") or "")
            m = re.search(r"(\d+)$", seg_id)
            if m:
                return int(m.group(1)), seg_id
            return 10**9, seg_id

        def _bucket_slice(text: str, bucket: str, max_chars: int) -> Tuple[str, str, int, bool, int]:
            patterns = []
            if bucket == "troubleshooting":
                patterns = [
                    re.compile(r"\bTROUBLESHOOTING\b", re.IGNORECASE),
                    re.compile(r"\bproblem\b", re.IGNORECASE),
                    re.compile(r"\bsolution\b", re.IGNORECASE),
                    re.compile(r"\bif\b.+\bthen\b", re.IGNORECASE),
                ]

            anchor_pat, anchor_pos = _find_anchor(text, patterns)
            start = 0
            if anchor_pos >= 0 and bucket != "procedure":
                start = max(0, anchor_pos - 200)
                start_boundary = text.rfind("\n", 0, start)
                if start_boundary > 0:
                    start = start_boundary + 1
            window = text[start:]
            marker = "" if bucket == "procedure" else " [TRUNCATED]"
            sliced, truncated, cut_pos = _safe_truncate(window, max_chars, marker=marker)
            return sliced, anchor_pat, anchor_pos, truncated, cut_pos

        procedure_total_budget = max(
            1,
            int(os.getenv("PLANNER_PARSE_MAX_CHARS_PROCEDURE", "50000")),
        )
        procedure_coverage_reserve = min(
            max(
                0,
                int(os.getenv("PLANNER_PROCEDURE_COVERAGE_TOPUP_CHARS", "12000")),
            ),
            max(0, procedure_total_budget - 1),
        )
        procedure_selection_budget = max(
            1,
            procedure_total_budget - procedure_coverage_reserve,
        )
        reagent_total_budget = max(
            1,
            int(os.getenv("PLANNER_PARSE_MAX_CHARS_REAGENT_SETUP", "20000")),
        )
        reagent_coverage_reserve = min(
            max(
                0,
                int(os.getenv("PLANNER_REAGENT_COVERAGE_TOPUP_CHARS", "6000")),
            ),
            max(0, reagent_total_budget - 1),
        )
        reagent_selection_budget = max(
            1,
            reagent_total_budget - reagent_coverage_reserve,
        )
        dynamic_selection_budgets = {
            "procedure": procedure_selection_budget,
            "reagent_setup": reagent_selection_budget,
        }
        coverage_reserves = {
            "procedure": procedure_coverage_reserve,
            "reagent_setup": reagent_coverage_reserve,
        }

        def _view_block_size(item: Dict[str, Any], bucket: str, budget: int) -> int:
            comment = (
                f"<!-- bucket:{bucket} | segment_id:{item.get('segment_id')} | "
                f"title:{item.get('title', '')} | path:{item.get('segment_path', '')} -->\n"
            )
            text_chars = len((item.get("segment_text") or "").strip())
            return min(budget, len(comment) + text_chars + 2)

        def _fit_dynamic_budget(
            items: List[Dict[str, Any]],
            bucket: str,
        ) -> List[Dict[str, Any]]:
            budget = dynamic_selection_budgets[bucket]
            ranked = sorted(
                items,
                key=lambda x: (-x["scores"].get(bucket, 0), _document_order_key(x)),
            )
            selected: List[Dict[str, Any]] = []
            used_chars = 0
            for item in ranked:
                item_chars = _view_block_size(item, bucket, budget)
                if selected and used_chars + item_chars > budget:
                    continue
                selected.append(item)
                used_chars += item_chars
                if used_chars >= budget:
                    break
            logger.info(
                "%s selection budget=%s reserve=%s candidates=%s selected=%s estimated_chars=%s",
                bucket,
                budget,
                coverage_reserves[bucket],
                len(items),
                len(selected),
                used_chars,
            )
            return sorted(selected, key=_document_order_key)

        scored: List[Dict[str, Any]] = []
        for idx, a in enumerate(analyses or []):
            seg_path = a.get("segment_file") or ""
            if seg_path and not os.path.isabs(seg_path):
                seg_path = os.path.join(segments_dir, seg_path)
            seg_id = str(a.get("segment_id") or f"seg_{idx+1:02d}")
            title = str(a.get("title") or "").strip()
            level = a.get("level")
            types = [t.lower() for t in _safe_list(a.get("content_types"))]
            primary_view = str(a.get("primary_view") or "").strip().lower()
            query_relevance = str(a.get("query_relevance") or "").strip().lower()
            uses = [u.lower() for u in _safe_list(a.get("suggested_uses"))]
            has_params = bool(a.get("has_parameters"))
            segment_text = _read_excerpt(seg_path, 2000000)
            excerpt = segment_text
            excerpt_lower = excerpt.lower()
            order_val = a.get("order")

            def _score(bucket: str) -> int:
                score = 0
                if query_relevance == "off_query":
                    return -100
                if bucket == "procedure":
                    if not _is_procedure_candidate(
                        primary_view,
                        query_relevance,
                        types,
                    ):
                        return -100
                if bucket == "reagent_setup" and not _is_reagent_setup_candidate(
                    primary_view,
                    query_relevance,
                    types,
                ):
                    return -100
                if title and bucket not in {"procedure", "materials", "timing"}:
                    title_lower = title.lower()
                    if any(k in title_lower for k in bucket_synonyms[bucket]):
                        score += 1
                if primary_view == bucket:
                    score += 6
                elif any(t in bucket_synonyms[bucket] for t in types):
                    score += 2
                if bucket != "procedure" and any(
                    k in u for u in uses for k in bucket_synonyms[bucket]
                ):
                    score += 2
                if bucket in regex_map and regex_map[bucket].search(excerpt_lower):
                    score += 3
                if bucket == "procedure":
                    if query_relevance == "core":
                        score += 3
                    elif query_relevance == "supporting":
                        score += 1
                if has_params and bucket in {"procedure", "reagent_setup", "timing"}:
                    score += 1
                return score

            scored.append(
                {
                    "segment_id": seg_id,
                    "title": title,
                    "level": level,
                    "segment_path": seg_path,
                    "primary_view": primary_view,
                    "query_relevance": query_relevance,
                    "types": types,
                    "uses": uses,
                    "has_parameters": has_params,
                    "order": order_val,
                    "excerpt": excerpt,
                    "excerpt_500": _safe_truncate(excerpt, 500)[0],
                    "segment_text": segment_text,
                    "scores": {b: _score(b) for b in bucket_synonyms.keys()},
                }
            )

        async def _pick(bucket: str) -> List[Dict[str, Any]]:
            candidates = [
                s for s in scored
                if s["scores"].get(bucket, 0) >= 4 and s.get("segment_path")
            ]
            if bucket == "procedure":
                candidates.sort(key=lambda x: (-x["scores"].get(bucket, 0), _document_order_key(x)))
            else:
                candidates.sort(key=lambda x: (x["scores"].get(bucket, 0), x.get("segment_id", "")), reverse=True)
            if not candidates:
                fallback = [
                    s
                    for s in scored
                    if s.get("segment_path")
                    and s.get("query_relevance") != "off_query"
                ]
                if bucket == "procedure":
                    fallback = [
                        s for s in fallback
                        if _is_procedure_candidate(
                            s.get("primary_view") or "",
                            s.get("query_relevance") or "",
                            s.get("types") or [],
                        )
                    ]
                elif bucket == "reagent_setup":
                    fallback = [
                        s for s in fallback
                        if _is_reagent_setup_candidate(
                            s.get("primary_view") or "",
                            s.get("query_relevance") or "",
                            s.get("types") or [],
                        )
                    ]
                if bucket == "procedure":
                    fallback.sort(key=lambda x: (-x["scores"].get(bucket, 0), _document_order_key(x)))
                    candidates = fallback
            selected = (
                _fit_dynamic_budget(candidates, bucket)
                if bucket in dynamic_selection_budgets
                else candidates[:max_segments_per_view]
            )
            top_score = max(
                (s["scores"].get(bucket, 0) for s in scored),
                default=0,
            )
            low_confidence = top_score < 4
            if low_confidence:
                logger.info(
                    f"Low-confidence {bucket} selection; invoking LLM judge."
                )
                judge_candidates = [
                    s for s in scored if s.get("segment_path")
                ]
                judge_pool = sorted(
                    judge_candidates,
                    key=lambda x: (
                        x["scores"].get(bucket, 0),
                        -_document_order_key(x)[0],
                    ),
                    reverse=True,
                )
                if bucket not in dynamic_selection_budgets:
                    judge_pool = judge_pool[:6]
                chosen_ids = await self.llm_judge_bucket(
                    judge_pool,
                    bucket,
                    task_query=task_query,
                )
                if chosen_ids:
                    judged_items = [
                        s
                        for s in judge_pool
                        if s.get("segment_id") in chosen_ids
                    ]
                    if bucket in dynamic_selection_budgets:
                        selected = _fit_dynamic_budget(judged_items, bucket)
                    else:
                        selected = judged_items
                else:
                    selected = []
            return selected if bucket in dynamic_selection_budgets else selected[:max_segments_per_view]

        procedure_items = await _pick("procedure")
        materials_items = await _pick("materials")
        reagent_setup_items = await _pick("reagent_setup")
        timing_items = await _pick("timing")
        troubleshooting_items = await _pick("troubleshooting")

        def _persist_view(bucket: str, items: List[Dict[str, Any]]) -> str:
            out_path = os.path.join(anchors_dir, f"{evidence_id}__view_{bucket}.md")
            parts: List[str] = []
            used_chars = 0
            for it in items:
                comment = (
                    f"<!-- bucket:{bucket} | segment_id:{it.get('segment_id')} | "
                    f"title:{it.get('title','')} | path:{it.get('segment_path','')} -->\n"
                )
                separator_chars = 2 if parts else 0
                if bucket in dynamic_selection_budgets:
                    remaining = dynamic_selection_budgets[bucket] - used_chars - separator_chars
                    text_budget = remaining - len(comment)
                    if text_budget <= 0:
                        break
                    bucket_budget = text_budget
                else:
                    bucket_budget = int(DEFAULT_VIEW_CHAR_BUDGETS.get(bucket, 2500))
                slice_text, anchor_pat, anchor_pos, truncated, cut_pos = _bucket_slice(
                    it.get("segment_text") or "",
                    bucket,
                    max_chars=bucket_budget,
                )
                logger.info(
                    f"View slice bucket={bucket} segment_id={it.get('segment_id')} "
                    f"anchor={anchor_pat or 'none'} anchor_pos={anchor_pos} "
                    f"truncated={truncated} cut_pos={cut_pos}"
                )
                block = comment + (slice_text or "").strip()
                if block.strip():
                    parts.append(block)
                    used_chars += separator_chars + len(block)
            view_text = "\n\n".join(parts).strip()
            with open(out_path, "w", encoding="utf-8") as f:
                if view_text:
                    f.write(view_text.rstrip() + "\n")
            return os.path.abspath(out_path)

        views = {
            "procedure": _persist_view("procedure", procedure_items),
            "materials": _persist_view("materials", materials_items),
            "reagent_setup": _persist_view("reagent_setup", reagent_setup_items),
            "timing": _persist_view("timing", timing_items),
            "troubleshooting": _persist_view("troubleshooting", troubleshooting_items),
        }

        index_path = os.path.join(anchors_dir, f"{evidence_id}__segment_index.md")
        with open(index_path, "w", encoding="utf-8") as f:
            for it in scored:
                f.write(f"- segment_id: {it.get('segment_id','')}\n")
                f.write(f"  title: {it.get('title','')}\n")
                f.write(f"  level: {it.get('level','')}\n")
                f.write(f"  file: {it.get('segment_path','')}\n")
                f.write(f"  primary_view: {it.get('primary_view','')}\n")
                f.write(f"  query_relevance: {it.get('query_relevance','')}\n")
                f.write(f"  types: {', '.join(it.get('types', []))}\n")
                f.write(f"  uses: {', '.join(it.get('uses', []))}\n")
                f.write(f"  has_parameters: {bool(it.get('has_parameters'))}\n")
                f.write("  excerpt: |\n")
                excerpt_lines = (it.get("excerpt_500") or "").splitlines()
                for line in excerpt_lines:
                    f.write(f"    {line}\n")
                f.write("\n")

        views["segment_index"] = os.path.abspath(index_path)
        return views

    async def llm_judge_bucket(
        self,
        candidates: List[Dict[str, Any]],
        target_bucket: str,
        task_query: str = "",
    ) -> List[str]:
        if not candidates:
            return []
        bucket_contracts = {
            "procedure": (
                "Select segments whose body contains executable operations that are "
                "core or immediate prerequisites for the task query. Exclude headings, "
                "narrative summaries, background, results, downstream applications, "
                "and optional characterization not requested by the query."
            ),
            "materials": (
                "Select segments whose body primarily provides an inventory of reagents, "
                "consumables, antibodies, equipment, or supplier/catalog information needed "
                "for the requested protocol. Exclude narrative mentions of individual items."
            ),
            "reagent_setup": (
                "Select only segments whose primary purpose is defining named recipes "
                "or preparing media, buffers, stocks, solutions, or coatings. Exclude "
                "experimental workflows that merely add or mention reagents."
            ),
            "timing": (
                "Select segments whose body primarily defines the protocol schedule, stage "
                "timing, timepoint summary, or duration overview. Exclude isolated durations "
                "that merely occur inside experimental instructions."
            ),
        }
        prompt_lines = [
            f"Target bucket: {target_bucket}",
            f"Task query: {task_query or 'Not provided'}",
            "Pick which candidates belong to the target bucket.",
            bucket_contracts.get(target_bucket, ""),
            "Return JSON only: {\"chosen\": [\"seg_04\", ...]}",
            "",
        ]
        for idx, c in enumerate(candidates, start=1):
            seg_id = str(c.get("segment_id") or "")
            title = str(c.get("title") or "")
            excerpt = str(c.get("excerpt_500") or "")
            prompt_lines.append(f"{idx}. segment_id: {seg_id}")
            prompt_lines.append(f"   title: {title}")
            prompt_lines.append(f"   excerpt: {excerpt}")
        prompt = "\n".join(prompt_lines)
        try:
            response = await self._call_model(
                system_prompt="You are a strict classifier. Output JSON only.",
                prompt=prompt,
            )
            if isinstance(response, dict):
                chosen = response.get("chosen", [])
            else:
                cleaned = self._strip_code_fences(str(response))
                data = json.loads(cleaned)
                chosen = data.get("chosen", [])
            if isinstance(chosen, list):
                return [str(x) for x in chosen if x]
            return []
        except Exception:
            return []

    async def _verify_and_repair_procedure_view(
        self,
        task_query: str,
        procedure_view_text: str,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        semantic_operation_bundle: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = await self._verify_and_repair_semantic_view(
            view_type="procedure",
            task_query=task_query,
            view_text=procedure_view_text,
            analyses=analyses,
            segments_dir=segments_dir,
            semantic_operation_bundle=semantic_operation_bundle,
        )
        return {
            **result,
            "procedure_view_text": str(result.get("view_text") or procedure_view_text),
        }

    async def _verify_and_repair_reagent_setup_view(
        self,
        task_query: str,
        reagent_setup_view_text: str,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        semantic_operation_bundle: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        result = await self._verify_and_repair_semantic_view(
            view_type="reagent_setup",
            task_query=task_query,
            view_text=reagent_setup_view_text,
            analyses=analyses,
            segments_dir=segments_dir,
            semantic_operation_bundle=semantic_operation_bundle,
        )
        return {
            **result,
            "reagent_setup_view_text": str(
                result.get("view_text") or reagent_setup_view_text
            ),
        }

    async def _verify_and_repair_semantic_view(
        self,
        view_type: str,
        task_query: str,
        view_text: str,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        semantic_operation_bundle: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if view_type not in {"procedure", "reagent_setup"}:
            raise ValueError(f"Unsupported semantic coverage view: {view_type}")
        if not analyses:
            return {"view_text": view_text}
        # Procedure has no safe backbone when its selected view is empty. Reagent
        # coverage is different: a recipe may live inside a mixed Procedure
        # segment, so the semantic catalog audit must still run from an empty
        # Reagent view and recover only exact source-verified recipe spans.
        if view_type == "procedure" and not view_text.strip():
            return {"view_text": view_text}

        selected_segment_ids = self._extract_selected_segment_ids_from_view(view_text)
        catalog = self._compact_segment_catalog(analyses)
        segment_texts = self._read_segment_text_map(analyses, segments_dir)
        partially_selected_ids = {
            segment_id
            for segment_id in selected_segment_ids
            if (segment_texts.get(segment_id) or "").strip()
            and (segment_texts.get(segment_id) or "").strip() not in view_text
        }
        selected_catalog = [
            item for item in catalog if item.get("segment_id") in selected_segment_ids
        ]
        repair_catalog = []
        for item in catalog:
            segment_id = str(item.get("segment_id") or "")
            if segment_id in selected_segment_ids and segment_id not in partially_selected_ids:
                continue
            candidate = dict(item)
            if segment_id in partially_selected_ids:
                candidate["selected_but_truncated"] = True
            repair_catalog.append(candidate)
        schema = {
            "type": "object",
            "properties": {
                "view_ok": {"type": "boolean"},
                "required_structures": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "type": {"type": "string"},
                            "covered_in_view": {"type": "boolean"},
                        },
                        "required": ["label", "type", "covered_in_view"],
                    },
                },
                "missing_in_view": {
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
                "rescue_segment_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["view_ok", "required_structures", "missing_in_view", "rescue_segment_ids"],
        }
        if view_type == "procedure":
            audit_role = "procedure-view workflow coverage"
            audit_task = "query-critical executable workflow structures"
            focus_instruction = (
                "Focus on setup/preparation, earliest timeline stages, explicit branches, "
                "and cycle/repeat logic."
            )
            exclusion_instruction = (
                "Do not rescue materials, reagent recipes, notes, validation, results, or "
                "discussion merely for completeness; those belong to separate views or "
                "supporting context unless the query explicitly makes them operational."
            )
        else:
            audit_role = "reagent-setup recipe coverage"
            audit_task = "named recipes and their executable preparation details"
            focus_instruction = (
                "Focus on named media, buffers, stocks, solutions, coatings, complete "
                "composition blocks, preparation steps, storage conditions, and working "
                "concentrations required by the selected workflow."
            )
            exclusion_instruction = (
                "Do not rescue plain materials inventories, supplier tables, experimental "
                "workflow steps that merely mention a reagent, results, discussion, or "
                "recipes unrelated to the requested workflow."
            )
        current_semantic_inventory = self._compact_semantic_inventory_for_view(
            semantic_operation_bundle,
            view_type,
        )
        workflow_semantic_inventory: Dict[str, Any] = {}
        if view_type == "reagent_setup":
            workflow_semantic_inventory = self._compact_semantic_inventory_for_view(
                semantic_operation_bundle,
                "procedure",
            )
        workflow_inventory_text = ""
        if workflow_semantic_inventory:
            workflow_inventory_text = (
                "Selected workflow full-view semantic inventory (context only; "
                "do not rescue workflow operations into the reagent view):\n"
                f"{json.dumps(workflow_semantic_inventory, indent=2, ensure_ascii=False)}\n\n"
            )
        result: Dict[str, Any] = {}
        try:
            result = await self._call_model(
                system_prompt=f"You audit {audit_role} for biological protocol planning.",
                prompt=(
                    f"Task: verify whether the selected {view_type} view preserves {audit_task} before parsing.\n\n"
                    f"Query:\n{task_query}\n\n"
                    f"Selected segment catalog (JSON):\n{json.dumps(selected_catalog, indent=2, ensure_ascii=False)}\n\n"
                    f"Current {view_type} full-view semantic inventory (JSON):\n"
                    f"{json.dumps(current_semantic_inventory, indent=2, ensure_ascii=False)}\n\n"
                    f"{workflow_inventory_text}"
                    f"Candidate repair catalog (unselected or truncated; JSON):\n{json.dumps(repair_catalog, indent=2, ensure_ascii=False)}\n\n"
                    "Instructions:\n"
                    f"1. Decide whether the selected {view_type} view already preserves {audit_task}.\n"
                    f"2. {focus_instruction}\n"
                    "3. If the current view is missing required content, list it under missing_in_view.\n"
                    "4. If rescue is needed, return only the necessary rescue_segment_ids from the catalog, ordered by importance. Character budget, not a fixed segment count, limits the repair.\n"
                    "5. Do not invent segment ids.\n"
                    f"6. {exclusion_instruction}\n"
                    "7. An empty reagent_setup view is not evidence that no recipes are required. "
                    "Use the workflow context and LLM-produced catalog metadata to identify source "
                    "segments that contain recipes consumed by the requested workflow.\n"
                ),
                schema=schema,
                temperature=0.0,
            )
        except Exception as e:
            logger.warning(
                "%s semantic verifier unavailable; keeping the existing view: %s",
                view_type,
                e,
            )
            return {"view_text": view_text, "verification_error": str(e)}

        missing = [item for item in (result.get("missing_in_view") or []) if isinstance(item, dict)] if isinstance(result, dict) else []
        rescue_segment_ids = [str(x) for x in (result.get("rescue_segment_ids") or []) if str(x).strip()] if isinstance(result, dict) else []
        analysis_by_id = {
            str(entry.get("segment_id") or ""): entry
            for entry in (analyses or [])
            if isinstance(entry, dict) and entry.get("segment_id")
        }
        primary_procedure_types = {"procedure", "protocol_steps"}
        secondary_procedure_types = {"methods", "workflow", "experimental_procedure"}
        reagent_types = {
            "reagent_setup",
            "reagent_preparation",
            "stock_solutions",
            "buffer_recipes",
            "media_preparation",
            "solution_preparation",
        }
        support_view_types = {
            "background",
            "rationale",
            "results",
            "discussion",
            "validation",
            "quality_control",
            "troubleshooting",
            "reagent_preparation",
            "buffer_recipes",
            "materials",
            "references",
        }
        empty_view_bootstrap = False
        if view_type == "reagent_setup" and not view_text.strip() and not rescue_segment_ids:
            # The verifier can conservatively return an empty decision when the
            # selected Reagent view itself is empty. Bootstrap only from semantic
            # labels already produced by the segment-analysis LLM; exact-span
            # extraction below still decides which recipe text is admissible.
            rescue_segment_ids = [
                str(item.get("segment_id") or "")
                for item in catalog
                if str(item.get("segment_id") or "").strip()
                and str(item.get("query_relevance") or "").strip().lower()
                != "off_query"
                and (
                    str(item.get("primary_view") or "").strip().lower()
                    == "reagent_setup"
                    or bool(
                        {
                            str(value).strip().lower()
                            for value in (item.get("content_types") or [])
                            if str(value).strip()
                        }
                        & reagent_types
                    )
                )
            ]
            empty_view_bootstrap = bool(rescue_segment_ids)
            if empty_view_bootstrap and not missing:
                missing = [
                    {
                        "label": "Source-defined named preparations required by the workflow",
                        "type": "recipe_coverage",
                        "reason": "The selected Reagent view is empty despite recipe-bearing query-relevant segments.",
                    }
                ]
        eligible_rescue_ids: List[str] = []
        for segment_id in rescue_segment_ids:
            entry = analysis_by_id.get(str(segment_id)) or {}
            query_relevance = str(entry.get("query_relevance") or "").strip().lower()
            primary_view = str(entry.get("primary_view") or "").strip().lower()
            content_types = {
                str(item).strip().lower()
                for item in (entry.get("content_types") or [])
                if str(item).strip()
            }
            if query_relevance == "off_query":
                continue
            procedure_eligible = primary_view == "procedure" or content_types & primary_procedure_types or (
                content_types & secondary_procedure_types and not content_types & support_view_types
            )
            reagent_eligible = primary_view == "reagent_setup" or bool(content_types & reagent_types)
            if (view_type == "procedure" and procedure_eligible) or (
                view_type == "reagent_setup" and reagent_eligible
            ):
                eligible_rescue_ids.append(str(segment_id))
        rescue_segment_ids = eligible_rescue_ids
        repaired = await self._augment_semantic_view_with_rescue_segments(
            view_type=view_type,
            view_text=view_text,
            analyses=analyses,
            segments_dir=segments_dir,
            rescue_segment_ids=rescue_segment_ids,
            task_query=task_query,
            missing_structures=missing,
        )
        return {
            "view_text": repaired,
            "required_structures": list(result.get("required_structures") or []) if isinstance(result, dict) else [],
            "missing_in_view": missing,
            "rescue_segment_ids": rescue_segment_ids,
            "empty_view_bootstrap": empty_view_bootstrap,
        }

    async def _augment_view_with_rescue_segments(
        self,
        procedure_view_text: str,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        rescue_segment_ids: List[str],
        task_query: str = "",
        missing_structures: Optional[List[Dict[str, str]]] = None,
        max_chars: Optional[int] = None,
    ) -> str:
        """Backward-compatible Procedure wrapper for structure-repair callers."""
        return await self._augment_semantic_view_with_rescue_segments(
            view_type="procedure",
            view_text=procedure_view_text,
            analyses=analyses,
            segments_dir=segments_dir,
            rescue_segment_ids=rescue_segment_ids,
            task_query=task_query,
            missing_structures=missing_structures,
            max_chars=max_chars,
        )

    async def _augment_semantic_view_with_rescue_segments(
        self,
        view_type: str,
        view_text: str,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        rescue_segment_ids: List[str],
        task_query: str = "",
        missing_structures: Optional[List[Dict[str, str]]] = None,
        max_chars: Optional[int] = None,
    ) -> str:
        if not rescue_segment_ids:
            return view_text
        budget_env = (
            "PLANNER_PARSE_MAX_CHARS_REAGENT_SETUP"
            if view_type == "reagent_setup"
            else "PLANNER_PARSE_MAX_CHARS_PROCEDURE"
        )
        budget_default = "20000" if view_type == "reagent_setup" else "50000"
        max_chars = max_chars or int(
            os.getenv(budget_env, budget_default)
        )
        segment_texts = self._read_segment_text_map(analyses, segments_dir)
        existing_ids = set(self._extract_selected_segment_ids_from_view(view_text))
        analysis_by_id = {
            str(entry.get("segment_id") or ""): entry
            for entry in (analyses or [])
            if isinstance(entry, dict) and entry.get("segment_id")
        }
        current_text = view_text.rstrip()
        for seg_id in rescue_segment_ids:
            if not seg_id:
                continue
            text = (segment_texts.get(seg_id) or "").strip()
            if not text:
                continue
            segment_analysis = analysis_by_id.get(seg_id) or {}
            title = str(segment_analysis.get("title") or "")
            primary_view = str(segment_analysis.get("primary_view") or "").strip().lower()
            content_types = {
                str(item).strip().lower()
                for item in (segment_analysis.get("content_types") or [])
                if str(item).strip()
            }
            contains_procedure_content = bool(
                content_types
                & {
                    "procedure",
                    "protocol_steps",
                    "methods",
                    "workflow",
                    "experimental_procedure",
                }
            )
            dedicated_reagent_segment = (
                view_type == "reagent_setup"
                and primary_view == "reagent_setup"
                and not contains_procedure_content
            )
            provenance = (
                f"<!-- bucket:{view_type} | segment_id:{seg_id} | "
                f"title:{title} | semantic_rescue:true -->\n"
            )
            full_addition = provenance + text
            separator = "\n\n" if current_text else ""

            # A semantically selected segment is safest when retained verbatim.
            # If it is already partially present or cannot fit, ask the existing
            # semantic extractor for exact, source-verifiable repair spans.
            if (
                seg_id not in existing_ids
                and len(current_text) + len(separator) + len(full_addition) <= max_chars
                and (view_type != "reagent_setup" or dedicated_reagent_segment)
            ):
                current_text += separator + full_addition
                existing_ids.add(seg_id)
                continue

            bundle = await self._extract_semantic_operation_units(
                task_query=task_query,
                view_texts={view_type: text},
                coverage_targets=missing_structures or [],
                reagent_recipe_only=view_type == "reagent_setup",
            )
            for unit in (bundle.get("units") or []):
                if not isinstance(unit, dict) or unit.get("view_type") != view_type:
                    continue
                quote = str(unit.get("quote") or "").strip()
                quote_key = self._normalize_for_match(quote)
                if not quote_key or quote_key in self._normalize_for_match(current_text):
                    continue
                source_start = int(unit.get("source_start") or 0)
                source_end = int(unit.get("source_end") or 0)
                kind = str(unit.get("kind") or "mandatory")
                span_comment = (
                    f"<!-- semantic_coverage_add | segment_id:{seg_id} | "
                    f"title:{title} | kind:{kind} | source_span:{source_start}:{source_end} -->\n"
                )
                addition = span_comment + quote
                separator = "\n\n" if current_text else ""
                if len(current_text) + len(separator) + len(addition) > max_chars:
                    logger.info(
                        "Semantic %s rescue stopped at char budget=%s segment_id=%s",
                        view_type,
                        max_chars,
                        seg_id,
                    )
                    return current_text.rstrip() + "\n"
                current_text += separator + addition
        return current_text.rstrip() + "\n"

    # Source operation evidence and timeline coverage.

    @staticmethod
    def _semantic_operation_chunks(text: str, max_chars: int = 6000) -> List[Tuple[int, str]]:
        """Split source text at paragraph/line boundaries without interpreting biology."""
        source = str(text or "")
        if not source:
            return []

        chunks: List[Tuple[int, str]] = []
        start = 0
        while start < len(source):
            hard_end = min(len(source), start + max_chars)
            end = hard_end
            if hard_end < len(source):
                search_floor = start + max_chars // 2
                paragraph_end = source.rfind("\n\n", search_floor, hard_end)
                line_end = source.rfind("\n", search_floor, hard_end)
                boundary = max(paragraph_end, line_end)
                if boundary > start:
                    end = boundary
            if end <= start:
                end = hard_end
            chunks.append((start, source[start:end]))
            start = end
        return chunks

    async def _extract_semantic_operation_units(
        self,
        task_query: str,
        view_texts: Dict[str, str],
        coverage_targets: Optional[List[Dict[str, str]]] = None,
        reagent_recipe_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Ask the LLM which verbatim source spans are executable operations or recipes.

        The model makes the semantic decision; code only verifies that every returned
        quote exists in the supplied source. A failed optional extraction leaves the
        original planner path available.
        """
        schema = {
            "type": "object",
            "properties": {
                "units": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "quote": {"type": "string"},
                            "recipe_name": {"type": "string"},
                            "kind": {
                                "type": "string",
                                "enum": ["mandatory", "optional", "warning"],
                            },
                        },
                        "required": (
                            ["quote", "recipe_name", "kind"]
                            if reagent_recipe_only
                            else ["quote", "kind"]
                        ),
                    },
                },
                "time_anchors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "quote": {"type": "string"},
                            "label": {"type": "string"},
                            "plan_role": {
                                "type": "string",
                                "enum": [
                                    "timeline_node",
                                    "step_timing",
                                    "repeat_schedule",
                                ],
                            },
                            "branch_context": {"type": "string"},
                        },
                        "required": [
                            "quote",
                            "label",
                            "plan_role",
                            "branch_context",
                        ],
                    },
                },
            },
            "required": ["units", "time_anchors"],
        }
        bundle: Dict[str, Any] = {
            "version": SEMANTIC_OPERATION_UNITS_VERSION,
            "model": str(getattr(self.model, "model_name", "") or type(self.model).__name__),
            "source_sha256": {
                view_type: hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
                for view_type, text in view_texts.items()
            },
            "units": [],
            "time_anchors": [],
            "errors": [],
        }
        max_chars = int(os.getenv("PLANNER_SEMANTIC_OPERATION_CHUNK_CHARS", "6000"))
        seen_spans: Set[Tuple[str, int, int]] = set()
        seen_time_anchor_spans: Set[Tuple[int, int, str]] = set()
        quote_search_cursors: Dict[Tuple[str, int, str], int] = {}
        time_quote_search_cursors: Dict[Tuple[int, str], int] = {}
        target_context = ""
        if coverage_targets:
            target_context = (
                "Coverage repair mode is active. Return only source operations needed "
                "to restore one of these missing workflow structures; exclude unrelated "
                "operations from the same source segment:\n"
                f"{json.dumps(coverage_targets, ensure_ascii=False)}\n\n"
            )

        async def extract_chunk(
            view_type: str,
            chunk_index: int,
            chunk_start: int,
            chunk: str,
            *,
            audit_missing: bool = False,
        ) -> None:
            target = (
                "executable laboratory actions and workflow transitions"
                if view_type == "procedure"
                else "named recipe definitions and preparation of media, buffers, stocks, solutions, or coatings"
            )
            semantic_boundary = ""
            if view_type == "reagent_setup":
                semantic_boundary = (
                    "Reagent-view boundary:\n"
                    "- Include text whose primary purpose is to formulate, reconstitute, "
                    "produce, filter, aliquot, or store a named reusable preparation.\n"
                    "- Production steps for a named conditioned medium belong here when "
                    "they are part of that preparation recipe.\n"
                    "- Exclude experimental workflow steps that merely consume an already "
                    "prepared medium/reagent. Exclude routine experimental workflow "
                    "operations whose purpose is to consume, rather than prepare, it.\n\n"
                    + (
                        "Strict rescue mode: every returned unit must identify the named "
                        "preparation in recipe_name. Return no unit when the quote only uses "
                        "a reagent and does not define how that named preparation is made.\n\n"
                        if reagent_recipe_only
                        else ""
                    )
                )
            audit_context = ""
            if audit_missing:
                already_extracted = [
                    item["quote"]
                    for item in bundle["units"]
                    if item["view_type"] == view_type
                    and int(item["source_start"]) >= chunk_start
                    and int(item["source_end"]) <= chunk_start + len(chunk)
                ]
                already_time_anchors = [
                    item["quote"]
                    for item in bundle["time_anchors"]
                    if int(item["source_start"]) >= chunk_start
                    and int(item["source_end"]) <= chunk_start + len(chunk)
                ]
                audit_context = (
                    "This is one bounded completeness audit of a previous extraction. "
                    f"Return only {target} that are present in "
                    "the source chunk but absent from ALREADY_EXTRACTED. Do not repeat an "
                    "already extracted entry. Return an empty units list when it is complete.\n\n"
                    f"ALREADY_EXTRACTED:\n{json.dumps(already_extracted, ensure_ascii=False)}\n\n"
                    f"ALREADY_EXTRACTED_TIME_ANCHORS:\n"
                    f"{json.dumps(already_time_anchors, ensure_ascii=False)}\n\n"
                )
            prompt = (
                "Identify all reusable protocol operations in the source chunk.\n\n"
                f"{audit_context}"
                f"Requested workflow:\n{task_query}\n\n"
                f"{target_context}"
                f"Target content:\n{target}\n\n"
                f"{semantic_boundary}"
                "Rules:\n"
                "1. Copy one complete source sentence, author-numbered step, or named recipe entry VERBATIM for each unit.\n"
                "2. Include concrete laboratory actions even when written in passive or descriptive prose. "
                "In reagent_setup chunks, a named ingredient/composition list is an executable recipe even when it has no verb; "
                "copy its recipe heading and complete ingredient block together.\n"
                "3. Keep a multi-action source sentence intact; do not invent partial quotations.\n"
                "4. Exclude background, rationale, results, discussion, citations, figure legends, and plain inventories.\n"
                "5. Mark genuinely conditional actions as optional and cautions as warning.\n"
                "6. Return an empty units list when this chunk contains no target operation.\n\n"
                "Time-anchor rules (PROCEDURE chunks only):\n"
                "1. Return every source-defined timing expression that controls execution, "
                "including absolute timepoints, relative timing, developmental stages, "
                "named rounds/cycles, and repeat intervals. Do not rely on a fixed Day/Week vocabulary.\n"
                "2. Copy label verbatim from the source and copy the complete containing heading, "
                "sentence, or numbered step in quote.\n"
                "3. Use plan_role=timeline_node only when the source expression defines a distinct "
                "workflow stage, checkpoint, or cycle node. Use step_timing for a duration or relative "
                "wait attached to one action, and repeat_schedule for an interval or repetition policy.\n"
                "4. A future cross-reference, result/characterization time, figure/table mention, "
                "or narrative overview is not a timeline_node unless that same quote contains an "
                "operation scheduled at that time.\n"
                "5. Copy an explicit branch or workflow name into branch_context; otherwise return an empty string.\n"
                "6. For REAGENT_SETUP chunks always return an empty time_anchors list.\n\n"
                f"JSON contract:\n{json.dumps(schema, ensure_ascii=False)}\n\n"
                f"Source chunk:\n{chunk}"
            )
            try:
                response = await self._call_model(
                    system_prompt=(
                        "You extract evidence-grounded scientific protocol operations. "
                        "Return strict JSON only."
                    ),
                    prompt=prompt,
                    schema=schema,
                    temperature=0.0,
                )
            except Exception as exc:
                bundle["errors"].append(
                    {
                        "view_type": view_type,
                        "chunk_index": chunk_index,
                        "pass": "completeness_audit" if audit_missing else "initial",
                        "type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
                logger.warning(
                    "Semantic operation extraction failed for %s chunk %s (%s): %s",
                    view_type,
                    chunk_index + 1,
                    "completeness audit" if audit_missing else "initial pass",
                    exc,
                )
                return

            for item in (response.get("units") if isinstance(response, dict) else []) or []:
                if not isinstance(item, dict):
                    continue
                if (
                    reagent_recipe_only
                    and view_type == "reagent_setup"
                    and not str(item.get("recipe_name") or "").strip()
                ):
                    continue
                quote = str(item.get("quote") or "")
                quote_key = self._normalize_for_match(quote)
                cursor_key = (view_type, chunk_index, quote_key)
                located = self._locate_semantic_quote(
                    chunk,
                    quote,
                    start_at=quote_search_cursors.get(cursor_key, 0),
                )
                if not located:
                    continue
                local_start, local_end, exact_quote = located
                quote_search_cursors[cursor_key] = local_end
                source_start = chunk_start + local_start
                source_end = chunk_start + local_end
                span_key = (view_type, source_start, source_end)
                if span_key in seen_spans:
                    continue
                seen_spans.add(span_key)
                kind = str(item.get("kind") or "mandatory").lower()
                if kind not in {"mandatory", "optional", "warning"}:
                    kind = "mandatory"
                bundle["units"].append(
                    {
                        "view_type": view_type,
                        "source_start": source_start,
                        "source_end": source_end,
                        "quote": exact_quote,
                        "kind": kind,
                    }
                )

            if view_type != "procedure":
                return
            for item in (response.get("time_anchors") if isinstance(response, dict) else []) or []:
                if not isinstance(item, dict):
                    continue
                quote = str(item.get("quote") or "")
                label = str(item.get("label") or "").strip()
                role = str(item.get("plan_role") or "").strip().lower()
                if not quote or not label or role not in {
                    "timeline_node",
                    "step_timing",
                    "repeat_schedule",
                }:
                    continue
                quote_key = self._normalize_for_match(quote)
                cursor_key = (chunk_index, quote_key)
                located_quote = self._locate_semantic_quote(
                    chunk,
                    quote,
                    start_at=time_quote_search_cursors.get(cursor_key, 0),
                )
                if not located_quote:
                    continue
                local_start, local_end, exact_quote = located_quote
                located_label = self._locate_semantic_quote(exact_quote, label)
                if not located_label:
                    continue
                label_start, label_end, exact_label = located_label
                time_quote_search_cursors[cursor_key] = local_end
                source_start = chunk_start + local_start
                source_end = chunk_start + local_end
                absolute_label_start = source_start + label_start
                absolute_label_end = source_start + label_end
                span_key = (absolute_label_start, absolute_label_end, role)
                if span_key in seen_time_anchor_spans:
                    continue
                seen_time_anchor_spans.add(span_key)
                branch_context = str(item.get("branch_context") or "").strip()
                if branch_context and not self._locate_semantic_quote(
                    chunk,
                    branch_context,
                ):
                    branch_context = ""
                bundle["time_anchors"].append(
                    {
                        "source_start": source_start,
                        "source_end": source_end,
                        "label_start": absolute_label_start,
                        "label_end": absolute_label_end,
                        "quote": exact_quote,
                        "label": exact_label,
                        "plan_role": role,
                        "branch_context": branch_context,
                    }
                )

        for view_type in ("procedure", "reagent_setup"):
            source = str(view_texts.get(view_type) or "")
            if not source.strip():
                continue
            chunks = self._semantic_operation_chunks(source, max_chars=max_chars)
            for chunk_index, (chunk_start, chunk) in enumerate(chunks):
                await extract_chunk(view_type, chunk_index, chunk_start, chunk)

            # One bounded audit asks only for omissions. Exact-span validation and
            # span de-duplication still govern acceptance; there is no retry loop.
            for chunk_index, (chunk_start, chunk) in enumerate(chunks):
                await extract_chunk(
                    view_type,
                    chunk_index,
                    chunk_start,
                    chunk,
                    audit_missing=True,
                )
            bundle[
                "procedure_audit_completed"
                if view_type == "procedure"
                else "reagent_audit_completed"
            ] = True

        bundle["units"].sort(
            key=lambda item: (
                0 if item["view_type"] == "procedure" else 1,
                int(item["source_start"]),
                int(item["source_end"]),
            )
        )
        bundle["time_anchors"].sort(
            key=lambda item: (
                int(item["source_start"]),
                int(item["source_end"]),
                str(item["plan_role"]),
            )
        )
        counters = {"procedure": 0, "reagent_setup": 0}
        for item in bundle["units"]:
            view_type = item["view_type"]
            counters[view_type] += 1
            prefix = "P" if view_type == "procedure" else "R"
            item["unit_id"] = f"{prefix}{counters[view_type]:03d}"
        for index, item in enumerate(bundle["time_anchors"], start=1):
            item["anchor_id"] = f"T{index:03d}"
        logger.info(
            "Semantic inventory extracted: procedure=%s reagent_setup=%s time_anchors=%s errors=%s",
            counters["procedure"],
            counters["reagent_setup"],
            len(bundle["time_anchors"]),
            len(bundle["errors"]),
        )
        return bundle

    async def _load_or_extract_semantic_operation_units(
        self,
        task_query: str,
        view_texts: Dict[str, str],
        cache_path: str,
    ) -> Dict[str, Any]:
        source_sha256 = {
            view_type: hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
            for view_type, text in view_texts.items()
        }
        model_name = str(getattr(self.model, "model_name", "") or type(self.model).__name__)
        cached = self._read_optional_json(cache_path)
        if (
            cached.get("version") == SEMANTIC_OPERATION_UNITS_VERSION
            and cached.get("source_sha256") == source_sha256
            and cached.get("model") == model_name
            and isinstance(cached.get("units"), list)
            and isinstance(cached.get("time_anchors"), list)
        ):
            logger.info("Reusing cached semantic operation inventory: %s", cache_path)
            return cached

        bundle = await self._extract_semantic_operation_units(task_query, view_texts)
        if cache_path:
            try:
                Path(cache_path).write_text(
                    json.dumps(bundle, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("Unable to persist semantic operation inventory: %s", exc)
        return bundle

    @staticmethod
    def _semantic_timeline_anchors(bundle: Dict[str, Any]) -> List[Dict[str, Any]]:
        return [
            item
            for item in (bundle.get("time_anchors") or [])
            if isinstance(item, dict)
            and str(item.get("anchor_id") or "").strip()
            and str(item.get("label") or "").strip()
            and str(item.get("plan_role") or "") == "timeline_node"
        ]

    @staticmethod
    def _format_required_timeline_anchors(
        anchors: List[Dict[str, Any]],
    ) -> str:
        """Format LLM instructions without leaking internal anchor identifiers."""
        lines: List[str] = []
        for item in anchors:
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            branch = str(item.get("branch_context") or "").strip()
            lines.append(f"- {label}" + (f" [branch: {branch}]" if branch else ""))
        return "\n".join(lines)

    @staticmethod
    def _format_semantic_timing_inventory(bundle: Dict[str, Any]) -> str:
        anchors = [
            item
            for item in (bundle.get("time_anchors") or [])
            if isinstance(item, dict)
            and str(item.get("anchor_id") or "").strip()
        ]
        if not anchors:
            return ""
        lines = [
            "---",
            "SEMANTIC TIMING INVENTORY",
            "Every entry is mapped to an exact PROCEDURE source span.",
            "Create a distinct timeline_node only for entries marked timeline_node.",
            "Attach step_timing and repeat_schedule entries to their source operation; "
            "do not invent Day/Week nodes for them.",
            "Keep identical labels separate when their branch_context or source operation differs.",
        ]
        for item in anchors:
            branch = str(item.get("branch_context") or "").strip()
            branch_text = f"; branch={branch}" if branch else ""
            lines.append(
                f"- [{item.get('plan_role')}{branch_text}] "
                f"label={json.dumps(str(item.get('label') or ''), ensure_ascii=False)}; "
                f"source={json.dumps(str(item.get('quote') or ''), ensure_ascii=False)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _compact_semantic_inventory_for_view(
        bundle: Optional[Dict[str, Any]],
        view_type: str,
    ) -> Dict[str, Any]:
        data = bundle or {}
        units = [
            {
                "unit_id": str(item.get("unit_id") or ""),
                "kind": str(item.get("kind") or ""),
                "quote": str(item.get("quote") or ""),
            }
            for item in (data.get("units") or [])
            if isinstance(item, dict) and str(item.get("view_type") or "") == view_type
        ]
        time_anchors = []
        if view_type == "procedure":
            time_anchors = [
                {
                    "anchor_id": str(item.get("anchor_id") or ""),
                    "label": str(item.get("label") or ""),
                    "plan_role": str(item.get("plan_role") or ""),
                    "branch_context": str(item.get("branch_context") or ""),
                    "quote": str(item.get("quote") or ""),
                }
                for item in (data.get("time_anchors") or [])
                if isinstance(item, dict)
            ]
        return {"units": units, "time_anchors": time_anchors}


    @staticmethod
    def _format_semantic_operation_checklist(bundle: Dict[str, Any]) -> str:
        units = [item for item in (bundle.get("units") or []) if isinstance(item, dict)]
        if not units:
            return ""
        lines = [
            "---",
            "SEMANTIC OPERATION COVERAGE CHECKLIST",
            "The following entries are verbatim spans already present in the supplied views.",
            "They are a coverage checklist, not additional evidence.",
            "Create one or more grounded steps for every mandatory operation.",
            "Retain an optional operation only when the requested workflow asks for it "
            "or it is needed to preserve an explicit operational branch.",
            "Do not merge distinct checklist entries merely to shorten the plan.",
            "Recipe entries must appear in a Reagent Preparation/Setup block.",
        ]
        for item in units:
            lines.append(
                f"- {item.get('unit_id')} [{item.get('view_type')}/{item.get('kind')}]: "
                f"{json.dumps(str(item.get('quote') or ''), ensure_ascii=False)}"
            )
        return "\n".join(lines)

    # Source protocol shape inference and formatting.

    async def _infer_protocol_shape_contract_semantic(
        self,
        task_query: str,
        procedure_text: str,
    ) -> Dict[str, Any]:
        """
        Infer nontrivial source structure without assay-name or benchmark rules.

        Branches, loops, phases, ordered sections, and final exceptions require
        LLM judgment. Every accepted label and alias must occur verbatim in the
        source.
        """
        neutral = self._infer_protocol_shape_contract(task_query, procedure_text)
        if self.model is None or not (procedure_text or "").strip():
            return neutral

        schema = {
            "type": "object",
            "properties": {
                "shape": {
                    "type": "string",
                    "enum": [
                        "generic",
                        "ordered_sections",
                        "branching",
                        "cyclic",
                        "branching_cyclic",
                        "staged",
                    ],
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                },
                "required_structures": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {"type": "string"},
                            "type": {
                                "type": "string",
                                "enum": [
                                    "ordered_section",
                                    "branch",
                                    "loop",
                                    "final_cycle",
                                    "setup",
                                    "phase",
                                ],
                            },
                            "aliases": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "requirement": {
                                "type": "string",
                                "enum": ["required", "optional"],
                            },
                        },
                        "required": ["label", "type", "aliases", "requirement"],
                    },
                },
                "order_constraints": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
            },
            "required": [
                "shape",
                "confidence",
                "required_structures",
                "order_constraints",
            ],
        }
        try:
            result = await self._call_model(
                system_prompt=(
                    "You identify evidence-supported control-flow structure in "
                    "scientific protocols. Return strict JSON only."
                ),
                prompt=(
                    "Infer only workflow structures that must remain distinct "
                    "when this source is converted into an executable plan.\n\n"
                    f"Requested workflow:\n{task_query}\n\n"
                    f"PROCEDURE SOURCE:\n{procedure_text[:30000]}\n\n"
                    "Rules:\n"
                    "1. Detect meaning, not assay names or a fixed vocabulary.\n"
                    "2. Preserve explicit alternatives, branches, repeated cycles, "
                    "final-cycle exceptions, and named phases.\n"
                    "3. Copy every label and alias verbatim from a source heading "
                    "or sentence. Do not create labels from the query.\n"
                    "4. Include an order constraint only when the source explicitly "
                    "establishes that order.\n"
                    "5. Mark paths introduced as optional, conditional, or if-needed "
                    "with requirement=optional.\n"
                    "6. Exclude material lists, recipe references, equipment, notes, "
                    "validation, results, and discussion unless the requested workflow "
                    "explicitly makes them part of the operational control flow.\n"
                    "7. If no nontrivial structure is explicit, return shape=generic "
                    "with empty arrays.\n"
                    "8. Use exactly this JSON contract:\n"
                    + json.dumps(schema, ensure_ascii=False)
                ),
                schema=schema,
                temperature=0.0,
            )
        except Exception as exc:
            logger.warning(
                "Semantic shape inference failed; leaving shape unconstrained: %s",
                exc,
            )
            return neutral

        if not isinstance(result, dict):
            return neutral
        source_lower = (procedure_text or "").lower()
        allowed_types = {
            "ordered_section",
            "branch",
            "loop",
            "final_cycle",
            "setup",
            "phase",
        }
        structures: List[Dict[str, Any]] = []
        seen_labels: Set[str] = set()
        for item in result.get("required_structures") or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label") or "").strip()
            kind = str(item.get("type") or "").strip().lower()
            if not label or kind not in allowed_types or label.lower() not in source_lower:
                continue
            label_key = self._normalize_for_match(label)
            if not label_key or label_key in seen_labels:
                continue
            seen_labels.add(label_key)
            aliases = []
            for alias in item.get("aliases") or []:
                alias_text = str(alias or "").strip()
                if (
                    alias_text
                    and alias_text.lower() in source_lower
                    and alias_text.lower() != label.lower()
                ):
                    aliases.append(alias_text)
            requirement = str(item.get("requirement") or "required").strip().lower()
            if requirement not in {"required", "optional"}:
                requirement = "required"
            structures.append(
                {
                    "label": label,
                    "type": kind,
                    "aliases": aliases[:6],
                    "requirement": requirement,
                }
            )

        valid_labels = {item["label"] for item in structures}
        constraints: List[List[str]] = []
        for pair in result.get("order_constraints") or []:
            if (
                isinstance(pair, (list, tuple))
                and len(pair) == 2
                and str(pair[0]) in valid_labels
                and str(pair[1]) in valid_labels
                and str(pair[0]) != str(pair[1])
            ):
                constraints.append([str(pair[0]), str(pair[1])])

        shape = str(result.get("shape") or "generic").strip().lower()
        confidence = str(result.get("confidence") or "low").strip().lower()
        if (
            confidence != "high"
            or not structures
            or shape not in {
                "ordered_sections",
                "branching",
                "cyclic",
                "branching_cyclic",
                "staged",
            }
        ):
            return neutral
        return {
            "shape": shape,
            "confidence": "high",
            "required_structures": structures,
            "order_constraints": constraints,
        }

    def _infer_protocol_shape_contract(
        self,
        task_query: str,
        procedure_text: str,
    ) -> Dict[str, Any]:
        # Neutral result used only when semantic inference is unavailable.  It
        # must not infer protocol structure from headings, keywords, or regexes.
        return {
            "shape": "generic",
            "confidence": "low",
            "required_structures": [],
            "order_constraints": [],
        }

    def _format_shape_contract_for_prompt(self, contract: Dict[str, Any]) -> str:
        if not isinstance(contract, dict) or not (contract.get("required_structures") or []):
            return ""
        lines = [
            "---",
            "SHAPE_CONTRACT (high-confidence protocol structure to preserve):",
            f"- shape: {contract.get('shape')}",
            f"- confidence: {contract.get('confidence')}",
            "- required distinct structures:",
        ]
        for item in contract.get("required_structures") or []:
            label = str(item.get("label") or "").strip()
            kind = str(item.get("type") or "").strip()
            requirement = str(item.get("requirement") or "required").strip().lower()
            aliases = [str(x).strip() for x in (item.get("aliases") or []) if str(x).strip()]
            if not label:
                continue
            alias_text = f" (aliases: {', '.join(aliases[:4])})" if aliases else ""
            lines.append(f"  - {label} [{kind}; {requirement}]{alias_text}")
        constraints = contract.get("order_constraints") or []
        if constraints:
            lines.append("- order constraints:")
            for pair in constraints:
                if isinstance(pair, (list, tuple)) and len(pair) == 2:
                    lines.append(f"  - {pair[0]} -> {pair[1]}")
        lines.extend(
            [
                "",
                "You MUST preserve these structures explicitly in the parsed plan when they are present in the procedure view.",
                "Do NOT merge numbered major sections into a generic preparation block.",
                "Do NOT collapse branch-specific preparation into a single generic branch.",
                "Do NOT fold final-cycle exceptions into a generic repeat/cycle block.",
            ]
        )
        return "\n".join(lines)

    # Alternate preparation and diagnostics (not called by execute).

    async def _prepare_structure_first_state(
        self,
        context: Dict[str, Any],
        domain_pack: DomainPack,
        evidence_bundle: EvidenceBundle,
    ) -> Dict[str, Any]:
        anchor_methods_paths = context.get("anchor_methods_paths", {}) or {}
        anchor_raw_paths = context.get("anchor_raw_paths", {}) or {}
        if not anchor_methods_paths:
            raise ValueError("No anchor methods paths available for structure-first planning.")

        primary_id = context.get("primary_anchor_id")
        if not primary_id and evidence_bundle.anchor_ids:
            primary_id = evidence_bundle.anchor_ids[0]
        if not primary_id or primary_id not in anchor_methods_paths:
            if anchor_methods_paths:
                primary_id = list(anchor_methods_paths.keys())[0]
            else:
                raise ValueError("No valid anchor ID found in paths.")

        methods_md_path = anchor_methods_paths[primary_id]
        if not os.path.exists(methods_md_path):
            raise FileNotFoundError(f"Anchor file not found: {methods_md_path}")
        with open(methods_md_path, "r", encoding="utf-8") as handle:
            methods_md = handle.read()

        sid = str(context.get("session_id") or context.get("sid") or "unknown")
        anchors_dir = self._resolve_structure_first_artifacts_dir(context, methods_md_path, sid)
        artifacts: Dict[str, Any] = {
            "anchors_dir": str(anchors_dir),
            "outline_path": str(anchors_dir / f"{primary_id}__outline.json"),
            "segments_dir": str(anchors_dir / f"{primary_id}__segments"),
            "view_procedure_path": str(anchors_dir / f"{primary_id}__view_procedure.md"),
            "view_materials_path": str(anchors_dir / f"{primary_id}__view_materials.md"),
            "view_reagent_setup_path": str(anchors_dir / f"{primary_id}__view_reagent_setup.md"),
            "view_timing_path": str(anchors_dir / f"{primary_id}__view_timing.md"),
            "view_troubleshooting_path": str(anchors_dir / f"{primary_id}__view_troubleshooting.md"),
            "segment_index_path": str(anchors_dir / f"{primary_id}__segment_index.md"),
            "segment_analysis_path": str(anchors_dir / f"{primary_id}__segment_analysis.json"),
            "semantic_operation_units_path": str(
                anchors_dir / f"{primary_id}__semantic_operation_units.json"
            ),
        }

        segment_analysis_bundle = self._read_optional_json(artifacts.get("segment_analysis_path"))
        if segment_analysis_bundle:
            segment_analysis_bundle.setdefault("evidence_id", primary_id)
            segment_analysis_bundle.setdefault("segments_dir", artifacts.get("segments_dir"))
            segment_analysis_bundle.setdefault("analysis_path", artifacts.get("segment_analysis_path"))

        raw_fulltext_md_path = anchor_raw_paths.get(primary_id) or ""
        raw_fulltext_md = methods_md
        if raw_fulltext_md_path and os.path.exists(raw_fulltext_md_path):
            raw_fulltext_md = self._read_optional_text(raw_fulltext_md_path) or methods_md
        segmentation_source, segmentation_text = self._select_segmentation_source(
            methods_md_path=methods_md_path,
            methods_md=methods_md,
            raw_fulltext_md_path=raw_fulltext_md_path,
            raw_fulltext_md=raw_fulltext_md,
        )

        need_outline = not os.path.exists(str(artifacts.get("outline_path") or ""))
        need_views = not all(
            os.path.exists(str(artifacts.get(key) or ""))
            for key in [
                "view_procedure_path",
                "view_materials_path",
                "view_reagent_setup_path",
                "view_timing_path",
                "view_troubleshooting_path",
                "segment_index_path",
            ]
        )
        need_analysis = not segment_analysis_bundle or not segment_analysis_bundle.get("analyses")

        if need_outline or need_analysis or need_views:
            from .sources.protocol_outline_segmenter import persist_outline_artifacts

            persisted = persist_outline_artifacts(
                session_id=sid,
                evidence_id=primary_id,
                anchor_file_path=segmentation_source,
                markdown_text=segmentation_text,
                output_dir=str(anchors_dir),
            )
            artifacts.update(persisted or {})

            if need_analysis:
                try:
                    segment_analysis_bundle = await self._analyze_segments(
                        outline_path=artifacts.get("outline_path"),
                        evidence_id=primary_id,
                        source_file=segmentation_source,
                        segments_dir=artifacts.get("segments_dir"),
                        task_query=domain_pack.task.query,
                    )
                except Exception as e:
                    logger.warning(f"Segment analysis rebuild failed for structure-first state: {e}")
                    segment_analysis_bundle = segment_analysis_bundle or {}

            if need_views and segment_analysis_bundle and segment_analysis_bundle.get("analyses"):
                try:
                    views = await self.build_and_persist_views(
                        analyses=segment_analysis_bundle.get("analyses", []) or [],
                        anchors_dir=str(Path(artifacts.get("anchors_dir") or anchors_dir)),
                        evidence_id=primary_id,
                        segments_dir=str(artifacts.get("segments_dir") or ""),
                        task_query=domain_pack.task.query,
                    )
                    artifacts["view_procedure_path"] = str(views.get("procedure") or artifacts.get("view_procedure_path") or "")
                    artifacts["view_materials_path"] = str(views.get("materials") or artifacts.get("view_materials_path") or "")
                    artifacts["view_reagent_setup_path"] = str(views.get("reagent_setup") or artifacts.get("view_reagent_setup_path") or "")
                    artifacts["view_timing_path"] = str(views.get("timing") or artifacts.get("view_timing_path") or "")
                    artifacts["view_troubleshooting_path"] = str(views.get("troubleshooting") or artifacts.get("view_troubleshooting_path") or "")
                    artifacts["segment_index_path"] = str(views.get("segment_index") or artifacts.get("segment_index_path") or "")
                except Exception as e:
                    logger.warning(f"View rebuild failed for structure-first state: {e}")

        view_materials_text = self._read_optional_text(artifacts.get("view_materials_path"))
        view_reagent_setup_text = self._read_optional_text(artifacts.get("view_reagent_setup_path"))
        view_procedure_text = self._read_optional_text(artifacts.get("view_procedure_path"))
        view_timing_text = self._read_optional_text(artifacts.get("view_timing_path"))
        view_troubleshooting_text = self._read_optional_text(artifacts.get("view_troubleshooting_path"))
        segment_index_text = self._read_optional_text(artifacts.get("segment_index_path"))

        if not view_procedure_text.strip():
            view_procedure_text = methods_md
        if not view_timing_text.strip():
            view_timing_text = ""
        if not view_troubleshooting_text.strip():
            view_troubleshooting_text = ""
        if not segment_index_text.strip():
            segment_index_text = ""

        analyses = (segment_analysis_bundle or {}).get("analyses", []) if segment_analysis_bundle else []
        segments_dir = str(artifacts.get("segments_dir") or "")

        parse_max_procedure = int(os.getenv("PLANNER_PARSE_MAX_CHARS_PROCEDURE", "50000"))
        parse_max_materials = int(os.getenv("PLANNER_PARSE_MAX_CHARS_MATERIALS", "20000"))
        parse_max_reagent_setup = int(os.getenv("PLANNER_PARSE_MAX_CHARS_REAGENT_SETUP", "20000"))

        prompt_path = str(self.prompts_dir / "planner_block_parsing_v1.md")
        with open(prompt_path, "r", encoding="utf-8") as handle:
            template = handle.read()

        segment_analysis_text = planning_text.format_segment_analysis_for_prompt(
            segment_analysis_bundle
        )

        cleaned_procedure = self._normalize_planner_input(view_procedure_text or "")
        cleaned_materials = self._normalize_planner_input(view_materials_text or "")
        cleaned_reagent_setup = self._normalize_planner_input(view_reagent_setup_text or "")
        cleaned_timing = self._normalize_planner_input(view_timing_text or "")
        cleaned_troubleshooting = self._normalize_planner_input(view_troubleshooting_text or "")
        cleaned_segment_index = (segment_index_text or "").strip()
        shape_contract = await self._infer_protocol_shape_contract_semantic(
            domain_pack.task.query,
            cleaned_procedure,
        )
        shape_contract_prompt = self._format_shape_contract_for_prompt(shape_contract)

        return {
            "primary_id": primary_id,
            "methods_md": methods_md,
            "artifacts": artifacts,
            "segment_analysis_bundle": segment_analysis_bundle,
            "analyses": analyses,
            "segments_dir": segments_dir,
            "view_materials_text": view_materials_text,
            "view_reagent_setup_text": view_reagent_setup_text,
            "view_procedure_text": view_procedure_text,
            "view_timing_text": view_timing_text,
            "view_troubleshooting_text": view_troubleshooting_text,
            "segment_index_text": segment_index_text,
            "segment_analysis_text": segment_analysis_text,
            "template": template,
            "parse_max_procedure": parse_max_procedure,
            "parse_max_materials": parse_max_materials,
            "parse_max_reagent_setup": parse_max_reagent_setup,
            "cleaned_procedure": cleaned_procedure,
            "cleaned_materials": cleaned_materials,
            "cleaned_reagent_setup": cleaned_reagent_setup,
            "cleaned_timing": cleaned_timing,
            "cleaned_troubleshooting": cleaned_troubleshooting,
            "cleaned_segment_index": cleaned_segment_index,
            "shape_contract": shape_contract,
            "shape_contract_prompt": shape_contract_prompt,
        }

    # Compatibility methods delegate to the adjacent planning package.

    def _read_optional_text(self, path: Optional[str]) -> str:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.read_optional_text(path)

    def _read_optional_json(self, path: Optional[str]) -> Dict[str, Any]:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.read_optional_json(path)

    def _planning_view_trace(self, text: str) -> Dict[str, Any]:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.planning_view_trace(
            text,
            extract_segment_ids=self._extract_selected_segment_ids_from_view,
        )

    @staticmethod
    def _planning_plan_trace(plan: ProtocolPlan) -> Dict[str, Any]:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.planning_plan_trace(plan)

    @staticmethod
    def _compact_view_verification_trace(report: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.compact_view_verification_trace(report)

    @staticmethod
    def _compact_structure_guard_trace(report: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.compact_structure_guard_trace(report)

    @staticmethod
    def _persist_planning_trace(path: str, trace: Dict[str, Any]) -> None:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.persist_planning_trace(path, trace)

    @staticmethod
    def _persist_effective_views(
        artifacts: Dict[str, Any],
        view_texts: Dict[str, str],
    ) -> None:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.persist_effective_views(artifacts, view_texts)

    @staticmethod
    def _resolve_structure_first_artifacts_dir(
        context: Dict[str, Any],
        methods_md_path: str,
        session_id: str,
    ) -> Path:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.resolve_structure_first_artifacts_dir(
            context,
            methods_md_path,
            session_id,
        )

    def _read_segment_text_map(self, analyses: List[Dict[str, Any]], segments_dir: str) -> Dict[str, str]:
        """Compatibility entry point; implementation lives in planning.artifacts."""
        return planning_artifacts.read_segment_text_map(analyses, segments_dir)

    @staticmethod
    def _locate_semantic_quote(
        source_chunk: str,
        quote: str,
        start_at: int = 0,
    ) -> Optional[Tuple[int, int, str]]:
        """Compatibility entry point; implementation lives in planning.text."""
        return planning_text.locate_semantic_quote(source_chunk, quote, start_at)

    @staticmethod
    def _markdown_heading_count(text: str) -> int:
        """Compatibility entry point; implementation lives in planning.text."""
        return planning_text.markdown_heading_count(text)

    def _extract_view_headings(self, text: str, limit: int = 12) -> List[str]:
        """Compatibility entry point; implementation lives in planning.text."""
        return planning_text.extract_view_headings(text, limit)

    def _extract_selected_segment_ids_from_view(self, text: str) -> List[str]:
        """Compatibility entry point; implementation lives in planning.text."""
        return planning_text.extract_selected_segment_ids_from_view(text)

    def _compact_segment_catalog(self, analyses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Compatibility entry point; implementation lives in planning.text."""
        return planning_text.compact_segment_catalog(analyses)

    def _extract_markdown_sections(self, text: str) -> List[Tuple[str, str]]:
        """Compatibility entry point; implementation lives in planning.text."""
        return planning_text.extract_markdown_sections(text)

    def _normalize_planner_input(self, text: str) -> str:
        """Compatibility entry point; implementation lives in planning.text."""
        return planning_text.normalize_planner_input(text)

    def _normalize_for_match(self, text: str) -> str:
        """Compatibility entry point; implementation lives in planning.text."""
        return planning_text.normalize_for_match(text)

    def _parse_block_plan_from_llm(
        self,
        text: str,
        domain_pack: DomainPack,
        evidence_bundle: EvidenceBundle,
        primary_anchor_id: Optional[str] = None,
        segment_analysis_bundle: Optional[Dict[str, Any]] = None,
    ) -> ProtocolPlan:
        """Compatibility entry point; implementation lives in planning.plan_parser."""
        return planning_parser.parse_block_plan_from_llm(
            text,
            domain_pack,
            evidence_bundle,
            primary_anchor_id,
            segment_analysis_bundle,
            strip_fences=self._strip_code_fences,
            normalize_payload=self._normalize_block_plan_payload,
            json_repair_module=json_repair,
        )

    def _normalize_block_plan_payload(self, data: Any) -> Dict[str, Any]:
        """Compatibility entry point; implementation lives in planning.plan_parser."""
        return planning_parser.normalize_block_plan_payload(
            data,
            group_flat_steps=self._blocks_from_flat_plan_payload,
        )

    def _blocks_from_flat_plan_payload(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Compatibility entry point; implementation lives in planning.plan_parser."""
        return planning_parser.blocks_from_flat_plan_payload(data)

    def _strip_code_fences(self, text: str) -> str:
        """Compatibility entry point; implementation lives in planning.text."""
        return planning_text.strip_code_fences(text)

    # Compatibility delegates to planning.query_scope.

    async def _extract_query_focus_profile(
        self,
        plan: ProtocolPlan,
        task_query: str,
    ) -> Dict[str, Any]:
        """Compatibility entry point for ``query_scope.extract_query_focus_profile``."""
        return await query_scope.extract_query_focus_profile(self, plan, task_query)

    def _build_query_focus_context(self, plan: ProtocolPlan) -> Dict[str, Any]:
        """Compatibility entry point for ``query_scope.build_query_focus_context``."""
        return query_scope.build_query_focus_context(plan)

    def _normalize_query_focus_profile(self, data: Any) -> Dict[str, Any]:
        """Compatibility entry point for ``query_scope.normalize_query_focus_profile``."""
        return query_scope.normalize_query_focus_profile(self, data)

    def _normalize_section_label(self, text: str) -> str:
        """Compatibility entry point for ``query_scope.normalize_section_label``."""
        return query_scope.normalize_section_label(text)

    def _apply_query_focus_profile(
        self,
        plan: ProtocolPlan,
        profile: Dict[str, Any],
    ) -> None:
        """Compatibility entry point for ``query_scope.apply_query_focus_profile``."""
        return query_scope.apply_query_focus_profile(self, plan, profile)

    async def _verify_query_focus_preservation(
        self,
        task_query: str,
        pre_focus_plan: ProtocolPlan,
        post_focus_plan: ProtocolPlan,
        query_focus_profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Compatibility entry point for ``query_scope.verify_query_focus_preservation``."""
        return await query_scope.verify_query_focus_preservation(
            self,
            task_query,
            pre_focus_plan,
            post_focus_plan,
            query_focus_profile,
        )

    def _restore_query_focus_sections(
        self,
        plan: ProtocolPlan,
        pre_focus_plan: ProtocolPlan,
        section_names: List[str],
    ) -> List[str]:
        """Compatibility entry point for ``query_scope.restore_query_focus_sections``."""
        return query_scope.restore_query_focus_sections(
            self,
            plan,
            pre_focus_plan,
            section_names,
        )

    def _reconcile_query_focus_profile_with_plan(
        self,
        plan: ProtocolPlan,
        profile: Dict[str, Any],
        *,
        restored_section_names: Optional[List[str]] = None,
        full_revert: bool = False,
    ) -> List[str]:
        """Compatibility entry point for ``query_scope.reconcile_query_focus_profile_with_plan``."""
        return query_scope.reconcile_query_focus_profile_with_plan(
            self,
            plan,
            profile,
            restored_section_names=restored_section_names,
            full_revert=full_revert,
        )

    # Compatibility delegates to planning.plan_checks.

    def _audit_semantic_timeline_coverage(
        self,
        anchors: List[Dict[str, Any]],
        plan: ProtocolPlan,
    ) -> Dict[str, Any]:
        """Compatibility entry point for ``plan_checks.audit_semantic_timeline_coverage``."""
        return plan_checks.audit_semantic_timeline_coverage(self, anchors, plan)

    def _summarize_plan_outline(self, plan: ProtocolPlan) -> Dict[str, Any]:
        """Compatibility entry point for ``plan_checks.summarize_plan_outline``."""
        return plan_checks.summarize_plan_outline(plan)

    async def _verify_parsed_plan_structure(
        self,
        task_query: str,
        procedure_text: str,
        plan: ProtocolPlan,
        analyses: List[Dict[str, Any]],
        segments_dir: str,
        shape_contract: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Compatibility entry point for ``plan_checks.verify_parsed_plan_structure``."""
        return await plan_checks.verify_parsed_plan_structure(
            self,
            task_query,
            procedure_text,
            plan,
            analyses,
            segments_dir,
            shape_contract,
            logger=logger,
        )

    def _structure_issue_score(self, guard: Dict[str, Any]) -> int:
        """Compatibility entry point for ``plan_checks.structure_issue_score``."""
        return plan_checks.structure_issue_score(guard)

    def _text_supports_span(self, reference_text: str, candidate_text: str) -> bool:
        # Markdown emphasis is presentation-only evidence noise (for example
        # ``*g*`` versus ``g``); ignore it only for source-span grounding.
        """Compatibility entry point for ``plan_checks.text_supports_span``."""
        return plan_checks.text_supports_span(self, reference_text, candidate_text)

    def _text_supported_by_grounding_views(
        self,
        candidate_text: str,
        procedure_text: str,
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Compatibility entry point for ``plan_checks.text_supported_by_grounding_views``."""
        return plan_checks.text_supported_by_grounding_views(
            self,
            candidate_text,
            procedure_text,
            supporting_view_texts,
        )

    def _collect_unsupported_steps(
        self,
        plan: ProtocolPlan,
        reference_text: str,
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> List[str]:
        """Compatibility entry point for ``plan_checks.collect_unsupported_steps``."""
        return plan_checks.collect_unsupported_steps(
            self,
            plan,
            reference_text,
            supporting_view_texts,
        )

    def _block_has_traceable_evidence(self, block: OperationBlock, procedure_text: str) -> bool:
        """Compatibility entry point for ``plan_checks.block_has_traceable_evidence``."""
        return plan_checks.block_has_traceable_evidence(self, block, procedure_text)

    def _collect_untraceable_block_labels(
        self,
        plan: ProtocolPlan,
        procedure_text: str,
    ) -> List[Dict[str, str]]:
        """Compatibility entry point for ``plan_checks.collect_untraceable_block_labels``."""
        return plan_checks.collect_untraceable_block_labels(self, plan, procedure_text)

    def _summarize_guard_categories(self, guard: Dict[str, Any]) -> Dict[str, List[str]]:
        """Compatibility entry point for ``plan_checks.summarize_guard_categories``."""
        return plan_checks.summarize_guard_categories(guard)

    # Compatibility delegates to planning.plan_repairs.

    @staticmethod
    def _should_attempt_structure_repair(
        plan_guard: Dict[str, Any],
        semantic_operation_bundle: Optional[Dict[str, Any]] = None,
    ) -> bool:
        # Semantic units guide parsing and grounding; their mere existence does
        # not resolve a structure that the post-parse guard found missing.
        """Compatibility entry point for ``plan_repairs.should_attempt_structure_repair``."""
        return plan_repairs.should_attempt_structure_repair(
            plan_guard,
            semantic_operation_bundle,
        )

    @staticmethod
    def _build_semantic_timepoint_snippets(
        text: str,
        anchors: List[Dict[str, Any]],
        target_anchor_ids: List[str],
        max_chars: int,
    ) -> Dict[str, str]:
        """Compatibility entry point for ``plan_repairs.build_semantic_timepoint_snippets``."""
        return plan_repairs.build_semantic_timepoint_snippets(
            text,
            anchors,
            target_anchor_ids,
            max_chars,
        )

    def _uses_shape_block_level_repair(self, contract: Optional[Dict[str, Any]]) -> bool:
        """Compatibility entry point for ``plan_repairs.uses_shape_block_level_repair``."""
        return plan_repairs.uses_shape_block_level_repair(contract)

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
        """Compatibility entry point for ``plan_repairs.build_shape_block_repair_jobs``."""
        return plan_repairs.build_shape_block_repair_jobs(
            self,
            procedure_text,
            missing_structures,
            shape_contract,
            analyses,
            segments_dir,
            rescue_segment_ids,
            max_chars,
        )

    def _plan_text_covers_structure(
        self,
        plan_text: str,
        label: str,
        aliases: Optional[List[str]] = None,
    ) -> bool:
        """Compatibility entry point for ``plan_repairs.plan_text_covers_structure``."""
        return plan_repairs.plan_text_covers_structure(plan_text, label, aliases)

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
        """Compatibility entry point for ``plan_repairs.build_structure_repair_snippet``."""
        return plan_repairs.build_structure_repair_snippet(
            self,
            procedure_text,
            missing_structures,
            analyses,
            segments_dir,
            rescue_segment_ids,
            timing_anchors,
            max_chars,
        )

    def _filter_plan_to_grounded_steps(
        self,
        plan: ProtocolPlan,
        reference_text: str,
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> ProtocolPlan:
        """Compatibility entry point for ``plan_repairs.filter_plan_to_grounded_steps``."""
        return plan_repairs.filter_plan_to_grounded_steps(
            self,
            plan,
            reference_text,
            supporting_view_texts,
        )

    def _apply_grounding_filter_if_safe(
        self,
        task_query: str,
        procedure_text: str,
        plan: ProtocolPlan,
        analyses: List[Dict[str, Any]],
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> Tuple[ProtocolPlan, Dict[str, Any]]:
        """Compatibility entry point for ``plan_repairs.apply_grounding_filter_if_safe``."""
        return plan_repairs.apply_grounding_filter_if_safe(
            self,
            task_query,
            procedure_text,
            plan,
            analyses,
            supporting_view_texts,
        )

    async def _apply_grounding_filter_with_continuity_guard(
        self,
        task_query: str,
        procedure_text: str,
        plan: ProtocolPlan,
        analyses: List[Dict[str, Any]],
        semantic_operation_bundle: Optional[Dict[str, Any]] = None,
        supporting_view_texts: Optional[Dict[str, str]] = None,
    ) -> Tuple[ProtocolPlan, Dict[str, Any]]:
        """Compatibility entry point for ``plan_repairs.apply_grounding_filter_with_continuity_guard``."""
        return await plan_repairs.apply_grounding_filter_with_continuity_guard(
            self,
            task_query,
            procedure_text,
            plan,
            analyses,
            semantic_operation_bundle,
            supporting_view_texts,
        )

    def _merge_structure_repairs(self, base: ProtocolPlan, extra: ProtocolPlan) -> ProtocolPlan:
        """Compatibility entry point for ``plan_repairs.merge_structure_repairs``."""
        return plan_repairs.merge_structure_repairs(self, base, extra)

    def _merge_missing_timepoints(self, base: ProtocolPlan, extra: ProtocolPlan) -> ProtocolPlan:
        # Missing-timepoint repair is a specialized structure repair. Reuse
        # the block-aware merger so identical labels in independent blocks are
        # preserved and repaired nodes follow their own block context.
        """Compatibility entry point for ``plan_repairs.merge_missing_timepoints``."""
        return plan_repairs.merge_missing_timepoints(self, base, extra)

    def _assign_source_line_indices(self, plan: ProtocolPlan, reference_text: str) -> None:
        """Compatibility entry point for ``plan_repairs.assign_source_line_indices``."""
        return plan_repairs.assign_source_line_indices(self, plan, reference_text)

    def _normalize_blocks_and_steps(self, plan: ProtocolPlan, procedure_text: str) -> None:
        """Compatibility entry point for ``plan_repairs.normalize_blocks_and_steps``."""
        return plan_repairs.normalize_blocks_and_steps(self, plan, procedure_text)

    def _collapse_plan_to_single_block(self, plan: ProtocolPlan, block_type: str) -> ProtocolPlan:
        """Compatibility entry point for ``plan_repairs.collapse_plan_to_single_block``."""
        return plan_repairs.collapse_plan_to_single_block(plan, block_type)
