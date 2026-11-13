"""Deterministic fidelity validation for rendered scientific protocols.

The validator compares a canonical :class:`ProtocolPlan` with Writer Markdown.
It intentionally has no model, prompt, retrieval, or agent dependency: semantic
construction belongs to Planning and controlled prose rewriting belongs to Writer.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .protocol_schemas import ProtocolPlan
from .protocol_text_fidelity import numeric_fact_tokens
from . import protocol_scope


class ProtocolIntegrityValidator:
    """Validate Plan-to-Markdown transmission with deterministic checks only."""

    _STEP_MARKER = re.compile(
        r"<!--\s*protocol-step:([A-Za-z0-9_.:-]+)\s*-->"
    )
    _NON_RENDERABLE_ROLES = set(protocol_scope.NON_RENDERABLE_ROLES)
    _WRITER_RETRYABLE_CODES = {
        "MISSING_PLAN_BLOCKS",
        "UNAUTHORIZED_TOP_LEVEL_SECTIONS",
        "MISSING_CANONICAL_STEP_MARKERS",
        "DUPLICATE_CANONICAL_STEP_MARKERS",
        "UNKNOWN_CANONICAL_STEP_MARKERS",
        "CANONICAL_STEP_ORDER_MISMATCH",
        "NUMERIC_FACT_DRIFT",
        "TIMELINE_NODE_COVERAGE_INCOMPLETE",
        "BRANCH_COVERAGE_INCOMPLETE",
        "MARKDOWN_FENCES",
        "RAW_CITATION_TAGS",
    }

    @staticmethod
    def _normalize_heading_key(title: str) -> str:
        text = str(title or "").strip().lower()
        text = re.sub(r"^\s*protocol:\s*", "protocol ", text)
        return re.sub(r"[^a-z0-9]+", " ", text).strip()

    @staticmethod
    def _plan_preserves_assessment_readouts(plan: ProtocolPlan) -> bool:
        return protocol_scope.plan_preserves_assessment_readouts(plan)

    def _renderable_plan_entries(
        self,
        plan: ProtocolPlan,
    ) -> List[Tuple[Any, Any, Any]]:
        """Return canonical ``(block, timeline node, step)`` Writer entries."""
        profile = plan.query_focus_profile or {}
        preserve_readouts = self._plan_preserves_assessment_readouts(plan)
        primary_block_keys = {
            self._normalize_heading_key(name)
            for name in (profile.get("primary_section_names") or [])
            if str(name).strip()
        }
        entries: List[Tuple[Any, Any, Any]] = []
        for block in plan.blocks or []:
            block_is_primary = (
                self._normalize_heading_key(block.block_type) in primary_block_keys
            )
            include_readouts = preserve_readouts or block_is_primary
            for node in block.timeline_nodes or []:
                for step in node.steps or []:
                    if not protocol_scope.step_is_renderable_for_query(
                        step, include_readouts, non_renderable_roles=self._NON_RENDERABLE_ROLES,
                    ):
                        continue
                    entries.append((block, node, step))
        return entries

    @staticmethod
    def _split_level2_sections(markdown: str) -> List[Tuple[str, str]]:
        sections: List[Tuple[str, List[str]]] = []
        current_title: Optional[str] = None
        current_lines: List[str] = []

        def flush() -> None:
            nonlocal current_title, current_lines
            if current_title is not None:
                sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = None
            current_lines = []

        for line in str(markdown or "").splitlines():
            match = re.match(r"^\s*##\s+(.+?)\s*$", line)
            if match:
                flush()
                current_title = match.group(1).strip()
            elif current_title is not None:
                current_lines.append(line)
        flush()
        return [(title, body) for title, body in sections if title]

    def _extract_references_section(self, markdown: str) -> str:
        for title, content in self._split_level2_sections(markdown):
            if title.lower().strip() in {"references", "reference", "refs"}:
                return content or ""
        return ""

    @staticmethod
    def _extract_numeric_reference_numbers(references_text: str) -> List[int]:
        return sorted(
            {
                int(match.group(1))
                for match in re.finditer(
                    r"^\s*[-*]?\s*\[(\d+)\]\s+",
                    references_text or "",
                    flags=re.MULTILINE,
                )
            }
        )

    def _count_citations(
        self,
        markdown: str,
        global_ref_nums: Optional[set] = None,
    ) -> int:
        text = str(markdown or "")
        count = len(re.findall(r"\[evidence_id:\s*[^\]]+\]", text))
        count += len(re.findall(r"\[cite\]", text, flags=re.IGNORECASE))
        ref_nums = set(global_ref_nums or set())
        if not ref_nums:
            ref_nums = set(
                self._extract_numeric_reference_numbers(
                    self._extract_references_section(text)
                )
            )
        for match in re.finditer(r"\[(\d+)\]", text):
            number = int(match.group(1))
            if not ref_nums or number in ref_nums:
                count += 1
        return count

    def _analyze_markdown(self, markdown: str) -> Dict[str, Any]:
        text = str(markdown or "")
        references = self._extract_references_section(text)
        reference_numbers = set(self._extract_numeric_reference_numbers(references))
        citation_occurrences = self._count_citations(text, reference_numbers)
        sections = self._split_level2_sections(text)
        sections_with_citations = sum(
            1
            for _, body in sections
            if self._count_citations(body, reference_numbers) > 0
        )
        section_coverage = (
            sections_with_citations / len(sections) if sections else 0.0
        )

        references_incomplete = True
        if references:
            lines = [line.strip() for line in references.splitlines() if line.strip()]
            reference_lines = [
                line
                for line in lines
                if re.search(r"^\s*[-*]?\s*\[\d+\]\s+", line)
                or "[evidence_id:" in line
            ]
            references_incomplete = not reference_lines or not any(
                re.search(r"(?:https?://|www\.)", line, flags=re.IGNORECASE)
                for line in reference_lines
            )

        return {
            "has_code_fences": "```" in text,
            "has_raw_citation_ids": bool(
                re.search(r"\[sem_[0-9a-f]{40}\]", text)
            ),
            "markdown_citation_occurrences": citation_occurrences,
            "section_citation_coverage_ratio": section_coverage,
            "references_look_incomplete": references_incomplete,
        }

    def _rendered_step_text(self, markdown: str) -> Dict[str, str]:
        text = str(markdown or "")
        markers = list(self._STEP_MARKER.finditer(text))
        headings = list(re.finditer(r"(?m)^#{2,4}\s+.+$", text))
        rendered: Dict[str, str] = {}
        previous_marker_end = 0
        for marker in markers:
            heading_end = max(
                (
                    heading.end()
                    for heading in headings
                    if heading.end() <= marker.start()
                ),
                default=0,
            )
            start = max(previous_marker_end, heading_end)
            rendered.setdefault(marker.group(1), text[start:marker.start()])
            previous_marker_end = marker.end()
        return rendered

    @staticmethod
    def _ordered_unique(values: Iterable[str]) -> List[str]:
        seen = set()
        ordered: List[str] = []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                ordered.append(value)
        return ordered

    def _evaluate_plan_markdown_fidelity(
        self,
        plan: ProtocolPlan,
        markdown: str,
    ) -> Dict[str, Any]:
        entries = self._renderable_plan_entries(plan)
        renderable_block_objects = {id(block) for block, _, _ in entries}
        expected_blocks = [
            block
            for block in (plan.blocks or [])
            if id(block) in renderable_block_objects
        ]
        dropped_block_keys = {
            self._normalize_heading_key(title)
            for title in (
                (plan.query_focus_profile or {}).get("drop_section_names") or []
            )
            if str(title).strip()
        }
        dropped_plan_blocks = [
            str(block.block_type or "").strip()
            for block in (plan.blocks or [])
            if self._normalize_heading_key(block.block_type) in dropped_block_keys
        ]

        sections = self._split_level2_sections(markdown)
        present_titles = [title for title, _ in sections]
        present_block_counter = Counter(
            self._normalize_heading_key(title)
            for title in present_titles
            if not title.lower().startswith("protocol:")
            and self._normalize_heading_key(title)
            not in {
                self._normalize_heading_key("References"),
                self._normalize_heading_key("Reference"),
                self._normalize_heading_key("Refs"),
            }
        )
        expected_titles = [str(block.block_type or "").strip() for block in expected_blocks]
        missing_blocks: List[str] = []
        remaining_present = present_block_counter.copy()
        for title in expected_titles:
            key = self._normalize_heading_key(title)
            if key and remaining_present.get(key, 0) > 0:
                remaining_present[key] -= 1
            elif title:
                missing_blocks.append(title)

        allowed_keys = {
            self._normalize_heading_key("References"),
            self._normalize_heading_key("Reference"),
            self._normalize_heading_key("Refs"),
            *(
                self._normalize_heading_key(title)
                for title in expected_titles
                if title
            ),
        }
        unauthorized_sections = [
            title
            for title in present_titles
            if not title.lower().startswith("protocol:")
            and self._normalize_heading_key(title) not in allowed_keys
        ]

        expected_marker_ids = [
            str(step.step_id or "").strip()
            for _, _, step in entries
            if str(step.step_id or "").strip()
        ]
        expected_counts = Counter(expected_marker_ids)
        duplicate_plan_step_ids = sorted(
            step_id for step_id, count in expected_counts.items() if count > 1
        )
        rendered_marker_ids = self._STEP_MARKER.findall(markdown or "")
        rendered_counts = Counter(rendered_marker_ids)
        expected_set = set(expected_marker_ids)
        rendered_set = set(rendered_marker_ids)
        missing_steps = [
            step_id for step_id in expected_marker_ids if step_id not in rendered_set
        ]
        duplicate_steps = sorted(
            step_id for step_id, count in rendered_counts.items() if count > 1
        )
        unknown_steps = sorted(rendered_set - expected_set)

        known_once_in_output = self._ordered_unique(
            step_id for step_id in rendered_marker_ids if step_id in expected_set
        )
        expected_present_order = [
            step_id for step_id in expected_marker_ids if step_id in rendered_set
        ]
        marker_order_matches = known_once_in_output == expected_present_order
        out_of_order_steps: List[str] = []
        if not marker_order_matches:
            for index in range(max(len(known_once_in_output), len(expected_present_order))):
                actual = (
                    known_once_in_output[index]
                    if index < len(known_once_in_output)
                    else ""
                )
                expected = (
                    expected_present_order[index]
                    if index < len(expected_present_order)
                    else ""
                )
                if actual != expected:
                    out_of_order_steps.extend([actual, expected])
            out_of_order_steps = self._ordered_unique(out_of_order_steps)

        rendered_step_text = self._rendered_step_text(markdown)
        numeric_step_count = 0
        numeric_exact_step_count = 0
        numeric_source_token_count = 0
        numeric_rendered_token_count = 0
        numeric_drift_steps: List[str] = []
        approved_normalization_step_ids: List[str] = []
        normalization_warning_step_ids: List[str] = []
        missing_source_spans: List[str] = []
        missing_source_indices: List[str] = []

        for _, _, step in entries:
            step_id = str(step.step_id or "").strip()
            source_text = str(step.source_sentence or step.source_text or "").strip()
            if not source_text:
                missing_source_spans.append(step_id)
            if step.source_line_index is None:
                missing_source_indices.append(step_id)

            audit = (step.meta or {}).get("writer_normalization_audit") or {}
            audit_applied = isinstance(audit, dict) and bool(audit.get("applied"))
            numeric_basis = (
                str(audit.get("candidate_text") or "")
                if audit_applied
                else source_text
            )
            source_numbers = Counter(numeric_fact_tokens(numeric_basis))
            rendered_numbers = Counter(
                numeric_fact_tokens(rendered_step_text.get(step_id, ""))
            )
            if source_numbers or rendered_numbers:
                numeric_step_count += 1
                numeric_source_token_count += sum(source_numbers.values())
                numeric_rendered_token_count += sum(rendered_numbers.values())
                if source_numbers == rendered_numbers:
                    numeric_exact_step_count += 1
                else:
                    numeric_drift_steps.append(step_id)

            if isinstance(audit, dict) and audit:
                target = (
                    approved_normalization_step_ids
                    if audit_applied
                    else normalization_warning_step_ids
                )
                target.append(step_id)

        timeline_groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
        branch_groups: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        for block, node, step in entries:
            step_id = str(step.step_id or "").strip()
            if not step_id:
                continue
            block_id = str(block.block_id or "").strip()
            block_type = str(block.block_type or "").strip()
            timepoint_label = str(node.timepoint_label or "").strip()
            timeline_key = (block_id or block_type, timepoint_label)
            timeline_group = timeline_groups.setdefault(
                timeline_key,
                {
                    "block_id": block_id,
                    "block_type": block_type,
                    "timepoint_label": timepoint_label,
                    "expected_step_ids": [],
                },
            )
            timeline_group["expected_step_ids"].append(step_id)
            branch_id = str(step.branch_id or "").strip()
            if branch_id:
                branch_group = branch_groups.setdefault(
                    (*timeline_key, branch_id),
                    {
                        "block_id": block_id,
                        "block_type": block_type,
                        "timepoint_label": timepoint_label,
                        "branch_id": branch_id,
                        "expected_step_ids": [],
                    },
                )
                branch_group["expected_step_ids"].append(step_id)

        def missing_groups(groups: Dict[Any, Dict[str, Any]]) -> List[Dict[str, Any]]:
            result: List[Dict[str, Any]] = []
            for group in groups.values():
                missing_ids = [
                    step_id
                    for step_id in group["expected_step_ids"]
                    if step_id not in rendered_set
                ]
                if missing_ids:
                    item = {
                        key: value
                        for key, value in group.items()
                        if key != "expected_step_ids"
                    }
                    item["missing_step_ids"] = missing_ids
                    result.append(item)
            return result

        missing_timeline_nodes = missing_groups(timeline_groups)
        missing_branches = missing_groups(branch_groups)
        marker_coverage_ratio = (
            len(expected_set & rendered_set) / len(expected_set)
            if expected_set
            else 0.0
        )
        numeric_fidelity_ratio = (
            numeric_exact_step_count / numeric_step_count
            if numeric_step_count
            else 1.0
        )
        source_span_coverage_ratio = (
            (len(expected_marker_ids) - len(missing_source_spans))
            / len(expected_marker_ids)
            if expected_marker_ids
            else 0.0
        )

        return {
            "expected_marker_ids": expected_marker_ids,
            "rendered_marker_ids": rendered_marker_ids,
            "duplicate_plan_step_ids": duplicate_plan_step_ids,
            "missing_marker_ids": missing_steps,
            "duplicate_marker_ids": duplicate_steps,
            "unknown_marker_ids": unknown_steps,
            "out_of_order_step_ids": out_of_order_steps,
            "marker_order_matches": marker_order_matches,
            "marker_coverage_ratio": marker_coverage_ratio,
            "canonical_marker_exact": bool(expected_marker_ids)
            and not duplicate_plan_step_ids
            and rendered_marker_ids == expected_marker_ids,
            "missing_blocks": missing_blocks,
            "unauthorized_sections": unauthorized_sections,
            "dropped_plan_blocks": dropped_plan_blocks,
            "missing_timeline_nodes": missing_timeline_nodes,
            "missing_branches": missing_branches,
            "timeline_node_count": len(timeline_groups),
            "timeline_node_coverage_ratio": (
                (len(timeline_groups) - len(missing_timeline_nodes))
                / len(timeline_groups)
                if timeline_groups
                else 1.0
            ),
            "branch_count": len(branch_groups),
            "branch_coverage_ratio": (
                (len(branch_groups) - len(missing_branches)) / len(branch_groups)
                if branch_groups
                else 1.0
            ),
            "numeric_step_count": numeric_step_count,
            "numeric_source_token_count": numeric_source_token_count,
            "numeric_rendered_token_count": numeric_rendered_token_count,
            "numeric_fidelity_ratio": numeric_fidelity_ratio,
            "numeric_drift_step_ids": self._ordered_unique(numeric_drift_steps),
            "approved_normalization_step_ids": self._ordered_unique(
                approved_normalization_step_ids
            ),
            "normalization_warning_step_ids": self._ordered_unique(
                normalization_warning_step_ids
            ),
            "missing_source_span_step_ids": self._ordered_unique(
                missing_source_spans
            ),
            "missing_source_index_step_ids": self._ordered_unique(
                missing_source_indices
            ),
            "source_span_coverage_ratio": source_span_coverage_ratio,
            "source_index_coverage_ratio": (
                (len(expected_marker_ids) - len(missing_source_indices))
                / len(expected_marker_ids)
                if expected_marker_ids
                else 0.0
            ),
        }

    @staticmethod
    def _coerce_plan(data: Any) -> ProtocolPlan:
        if isinstance(data, ProtocolPlan):
            return data
        if isinstance(data, dict):
            return ProtocolPlan(**data)
        raise TypeError(
            f"protocol_plan must be ProtocolPlan or dict, got {type(data).__name__}"
        )

    def validate(
        self,
        protocol_plan: Any,
        final_markdown: str,
        *,
        grounding_input_empty: bool = False,
    ) -> Dict[str, Any]:
        """Return a stable validation report without invoking external services."""
        plan = self._coerce_plan(protocol_plan)
        markdown = str(final_markdown or "")
        fidelity = self._evaluate_plan_markdown_fidelity(plan, markdown)
        markdown_metrics = self._analyze_markdown(markdown)

        issues: List[str] = []
        issue_codes: List[str] = []
        warnings: List[str] = []
        warning_codes: List[str] = []

        def fail(code: str, message: str) -> None:
            if code not in issue_codes:
                issue_codes.append(code)
            issues.append(message)

        def warn(code: str, message: str) -> None:
            if code not in warning_codes:
                warning_codes.append(code)
            warnings.append(message)

        if not fidelity["expected_marker_ids"]:
            fail("NO_RENDERABLE_STEPS", "ProtocolPlan contains no renderable steps.")
        if fidelity["duplicate_plan_step_ids"]:
            fail(
                "DUPLICATE_PLAN_STEP_IDS",
                "ProtocolPlan contains duplicate step IDs: "
                + ", ".join(fidelity["duplicate_plan_step_ids"][:12]),
            )
        if fidelity["dropped_plan_blocks"]:
            fail(
                "QUERY_FOCUS_DROP_CONFLICT",
                "ProtocolPlan still contains blocks marked for removal by its "
                "query-focus profile: "
                + ", ".join(fidelity["dropped_plan_blocks"][:8]),
            )
        if fidelity["missing_blocks"]:
            fail(
                "MISSING_PLAN_BLOCKS",
                "Markdown is missing Plan blocks: "
                + ", ".join(fidelity["missing_blocks"][:8]),
            )
        if fidelity["unauthorized_sections"]:
            fail(
                "UNAUTHORIZED_TOP_LEVEL_SECTIONS",
                "Markdown introduced H2 sections not licensed by the Plan: "
                + ", ".join(fidelity["unauthorized_sections"][:8]),
            )
        if fidelity["missing_marker_ids"]:
            fail(
                "MISSING_CANONICAL_STEP_MARKERS",
                "Markdown is missing canonical steps: "
                + ", ".join(fidelity["missing_marker_ids"][:12]),
            )
        if fidelity["duplicate_marker_ids"]:
            fail(
                "DUPLICATE_CANONICAL_STEP_MARKERS",
                "Markdown repeats canonical steps: "
                + ", ".join(fidelity["duplicate_marker_ids"][:12]),
            )
        if fidelity["unknown_marker_ids"]:
            fail(
                "UNKNOWN_CANONICAL_STEP_MARKERS",
                "Markdown contains steps absent from the Plan: "
                + ", ".join(fidelity["unknown_marker_ids"][:12]),
            )
        if not fidelity["marker_order_matches"]:
            fail(
                "CANONICAL_STEP_ORDER_MISMATCH",
                "Canonical steps do not follow Plan order: "
                + ", ".join(fidelity["out_of_order_step_ids"][:12]),
            )
        if fidelity["numeric_drift_step_ids"]:
            fail(
                "NUMERIC_FACT_DRIFT",
                "Rendered numeric facts differ from their approved source basis: "
                + ", ".join(fidelity["numeric_drift_step_ids"][:12]),
            )
        if fidelity["missing_source_span_step_ids"]:
            fail(
                "PLAN_SOURCE_SPAN_COVERAGE_INCOMPLETE",
                "Renderable Plan steps lack source text: "
                + ", ".join(fidelity["missing_source_span_step_ids"][:12]),
            )
        if fidelity["missing_timeline_nodes"]:
            fail(
                "TIMELINE_NODE_COVERAGE_INCOMPLETE",
                "One or more Plan timeline nodes lost canonical steps.",
            )
        if fidelity["missing_branches"]:
            fail(
                "BRANCH_COVERAGE_INCOMPLETE",
                "One or more Plan branches lost canonical steps.",
            )
        if markdown_metrics["has_code_fences"]:
            fail("MARKDOWN_FENCES", "Markdown contains code fences.")
        if markdown_metrics["has_raw_citation_ids"]:
            fail("RAW_CITATION_TAGS", "Markdown contains unformatted citation IDs.")

        if fidelity["missing_source_index_step_ids"]:
            warn(
                "SOURCE_INDEX_COVERAGE_INCOMPLETE",
                "Some source-grounded steps lack a source character offset; text spans remain available.",
            )
        if fidelity["normalization_warning_step_ids"]:
            warn(
                "WRITER_NORMALIZATION_NOT_APPLIED",
                "One or more Writer normalization proposals were rejected or left unresolved.",
            )
        if grounding_input_empty:
            warn(
                "GROUNDING_INPUT_EMPTY_REPORTED",
                "Writer reported unavailable external anchor files; validation used Plan-embedded source spans.",
            )
        if (
            markdown_metrics["references_look_incomplete"]
            and markdown_metrics["markdown_citation_occurrences"]
        ):
            warn(
                "REFERENCES_INCOMPLETE",
                "References do not appear to map every numeric citation to title/URL metadata.",
            )
        if (
            len(markdown) >= 2000
            and markdown_metrics["section_citation_coverage_ratio"] < 0.4
        ):
            warn(
                "SECTION_CITATION_COVERAGE_LOW",
                "Fewer than 40% of H2 sections contain a rendered citation.",
            )

        rewrite_requests: List[Dict[str, Any]] = []
        affected_step_ids = self._ordered_unique(
            fidelity["missing_marker_ids"]
            + fidelity["duplicate_marker_ids"]
            + fidelity["out_of_order_step_ids"]
            + fidelity["numeric_drift_step_ids"]
        )
        for step_id in affected_step_ids:
            rewrite_requests.append(
                {
                    "step_id": step_id,
                    "problem": "Deterministic Plan-to-Markdown fidelity failure.",
                    "suggested_fix": (
                        "Re-render this canonical Plan step without changing its "
                        "source-supported facts or order."
                    ),
                }
            )

        approved = not issue_codes
        retryable = bool(set(issue_codes) & self._WRITER_RETRYABLE_CODES)
        metrics = {
            "total_steps": len(fidelity["expected_marker_ids"]),
            "grounding_input_empty": bool(grounding_input_empty),
            **markdown_metrics,
            "plan_markdown_fidelity": fidelity,
            "marker_coverage_ratio": fidelity["marker_coverage_ratio"],
            "canonical_marker_exact": fidelity["canonical_marker_exact"],
            "source_span_coverage_ratio": fidelity["source_span_coverage_ratio"],
            "source_index_coverage_ratio": fidelity["source_index_coverage_ratio"],
            "timeline_node_coverage_ratio": fidelity[
                "timeline_node_coverage_ratio"
            ],
            "branch_coverage_ratio": fidelity["branch_coverage_ratio"],
            "numeric_fidelity_ratio": fidelity["numeric_fidelity_ratio"],
            "numeric_drift_step_ids": fidelity["numeric_drift_step_ids"],
        }
        return {
            "approved": approved,
            "retryable": retryable,
            "summary": (
                "Protocol passed deterministic integrity validation."
                if approved
                else "Protocol failed deterministic integrity validation."
            ),
            "issues": issues,
            "issue_codes": issue_codes,
            "warnings": warnings,
            "warning_codes": warning_codes,
            "missing_steps": fidelity["missing_marker_ids"],
            "duplicate_steps": fidelity["duplicate_marker_ids"],
            "unknown_steps": fidelity["unknown_marker_ids"],
            "out_of_order_steps": fidelity["out_of_order_step_ids"],
            "missing_blocks": fidelity["missing_blocks"],
            "dropped_plan_blocks": fidelity["dropped_plan_blocks"],
            "missing_timeline_nodes": fidelity["missing_timeline_nodes"],
            "missing_branches": fidelity["missing_branches"],
            "numeric_drift_steps": fidelity["numeric_drift_step_ids"],
            "missing_source_spans": fidelity["missing_source_span_step_ids"],
            "rewrite_requests": rewrite_requests,
            "metrics": metrics,
        }
