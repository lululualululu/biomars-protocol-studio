"""Render grounded blocks, branches, stable step markers, and citations."""

from typing import Any, Callable, Dict, List, Optional, Tuple, Set
import re



def sanitize_heading_text(text: Optional[str]) -> str:
    if not text:
        return "General"
    s = str(text).strip()
    while s.startswith("#"):
        s = s[1:].lstrip()
    return s or "General"


def convert_citations_to_numeric(
    md: str,
    anchor_catalog: Dict[str, Dict[str, str]],
    allowed_evidence_ids: List[str],
) -> str:
    import re

    md = md or ""
    allowed_set = set(str(x) for x in (allowed_evidence_ids or []) if x)

    order: List[str] = []
    for m in re.finditer(r"\[evidence_id:\s*([^\]]+)\]", md):
        evid = m.group(1).strip()
        if allowed_set and evid not in allowed_set:
            continue
        if evid not in order:
            order.append(evid)

    if not order:
        return md

    id_to_num = {evid: i + 1 for i, evid in enumerate(order)}

    def repl(match: Any) -> str:
        evid = match.group(1).strip()
        if evid in id_to_num:
            return f"[{id_to_num[evid]}]"
        return ""

    md = re.sub(r"\[evidence_id:\s*([^\]]+)\]", repl, md)

    ref_lines: List[str] = []
    for evid in order:
        meta = (anchor_catalog or {}).get(evid) or {}
        title = (meta.get("title") or "").strip()
        url = (meta.get("url") or "").strip()
        if title and url:
            ref_lines.append(f"- [{id_to_num[evid]}] {title} — {url}")
        elif url:
            ref_lines.append(f"- [{id_to_num[evid]}] {url}")
        elif title:
            ref_lines.append(f"- [{id_to_num[evid]}] {title}")
        else:
            ref_lines.append(f"- [{id_to_num[evid]}] {evid}")

    references = "\n".join(ref_lines).strip()
    return md.rstrip() + f"\n\n## References\n\n{references}\n"


def render_global_body(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        if items:
            return "\n".join(f"- {item}" for item in items)
    if isinstance(value, dict):
        items = [
            f"- **{str(key).strip()}**: {str(item).strip()}"
            for key, item in value.items()
            if str(key).strip() and str(item).strip()
        ]
        if items:
            return "\n".join(items)
    return fallback


def assemble_protocol_markdown(
    goal: str,
    block_summaries: List[Dict[str, Any]],
    writer_payload: Dict[str, Any],
    evidence_scope_notes: List[str],
    allowed_evidence_ids: List[str],
    anchor_catalog: Dict[str, Dict[str, str]],
    render_trace: Optional[Dict[str, Any]] = None,
    *,
    convert_citations_to_numeric: Callable[..., Any],
    normalize_and_enforce_citations: Callable[..., Any],
    normalize_references_section: Callable[..., Any],
    render_block_markdown: Callable[..., Any],
    render_global_body: Callable[..., Any],
) -> str:
    """Assemble all protocol structure from the Plan, never from LLM Markdown."""
    step_text_by_id = writer_payload.get("step_text_by_id") or {}
    scope_suffix = ""
    if evidence_scope_notes:
        scope_suffix = "\n\nEvidence scope note: " + " ".join(evidence_scope_notes)

    sections = [
        f"## Protocol: {(goal or 'Generate Protocol').strip()}",
        "### Overview\n\n" + render_global_body(
            writer_payload.get("overview"),
            f"This protocol describes: {(goal or 'Generate Protocol').strip()}.",
        ) + scope_suffix,
        "### Materials/Reagents\n\n" + render_global_body(
            writer_payload.get("materials_reagents"),
            "Refer to the source-grounded procedure steps for required reagents.",
        ),
        "### Equipment\n\n" + render_global_body(
            writer_payload.get("equipment"),
            "Refer to the source-grounded procedure steps for required equipment.",
        ),
        "### Safety & Sterility\n\n" + render_global_body(
            writer_payload.get("safety_sterility"),
            "Follow the source-supported safety requirements and institutional procedures.",
        ),
    ]
    sections.extend(
        render_block_markdown(
            block,
            step_text_by_id=step_text_by_id,
            allowed_evidence_ids=set(allowed_evidence_ids),
            render_trace=render_trace,
        ).strip()
        for block in block_summaries
        if block
    )
    markdown = "\n\n".join(
        section.strip() for section in sections if section and section.strip()
    ) + "\n"
    markdown = normalize_and_enforce_citations(
        markdown,
        set(allowed_evidence_ids),
    )
    markdown = convert_citations_to_numeric(
        markdown,
        anchor_catalog,
        allowed_evidence_ids,
    )
    return normalize_references_section(markdown)


def normalize_references_section(md: str) -> str:
    import re

    md = md or ""
    # A bare Markdown marker is never a meaningful protocol heading.
    md = re.sub(r"(?m)^\s*#{1,6}\s*$\n?", "", md)
    md = re.sub(r"(?m)^\s*###\s+References\s*$", "## References", md)
    md = re.sub(r"(?m)^\s*####\s+References\s*$", "## References", md)
    return md


def normalize_heading_key(title: str) -> str:
    import re

    text = (title or "").strip().lower()
    text = re.sub(r"^\s*protocol:\s*", "protocol ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def ordered_unique(values: List[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


def render_block_markdown(
    block_summary: Dict[str, Any],
    step_text_by_id: Optional[Dict[str, str]] = None,
    allowed_evidence_ids: Optional[Set[str]] = None,
    render_trace: Optional[Dict[str, Any]] = None,
    *,
    select_faithful_step_text: Callable[..., Any],
) -> str:
    step_text_by_id = step_text_by_id or {}
    allowed_evidence_ids = allowed_evidence_ids or set()
    lines: List[str] = [f"## {block_summary.get('block_type')}", ""]
    timeline_nodes = list(block_summary.get("timeline_nodes") or [])
    for node in timeline_nodes:
        timepoint_label = (node.get("timepoint_label") or "General").strip()
        steps = list(node.get("steps") or [])
        if not steps:
            continue
        lines.append(f"### {timepoint_label}")
        lines.append("")
        contiguous_groups: List[Tuple[str, List[Dict[str, Any]]]] = []
        for step in steps:
            branch = (step.get("branch_id") or "").strip()
            key = branch or "__main__"
            if not contiguous_groups or contiguous_groups[-1][0] != key:
                contiguous_groups.append((key, []))
            contiguous_groups[-1][1].append(step)
        branch_seen = False
        for group_key, group_steps in contiguous_groups:
            if group_key != "__main__":
                lines.append(f"#### {group_key}")
                lines.append("")
                branch_seen = True
            elif branch_seen:
                lines.append("#### Shared continuation")
                lines.append("")
            operation_index = 0
            for step in group_steps:
                step_id = re.sub(
                    r"[^A-Za-z0-9_.:-]+",
                    "_",
                    str(step.get("step_id") or "").strip(),
                ).strip("_")
                requested_rewrite = step_text_by_id.get(step_id)
                desc = select_faithful_step_text(
                    step,
                    requested_rewrite,
                )
                if (
                    render_trace is not None
                    and (
                        not requested_rewrite
                        or desc != str(requested_rewrite).strip()
                    )
                ):
                    render_trace.setdefault("fallback_step_ids", []).append(step_id)
                if not desc:
                    desc = (step.get("action") or step.get("step_id") or "Perform the step").strip()
                extracted = step.get("planner_extracted_constraints") or {}
                extracted_items = (
                    extracted.get("extracted_list") or []
                    if isinstance(extracted, dict)
                    else []
                )
                missing_constraints = [
                    str(item).strip()
                    for item in extracted_items
                    if str(item).strip()
                    and str(item).strip().lower() not in desc.lower()
                ]
                if missing_constraints:
                    desc = (
                        desc.rstrip(" .")
                        + ". Source-defined parameters: "
                        + "; ".join(missing_constraints)
                        + "."
                    )
                evidence_ids = [
                    str(item).strip()
                    for item in (step.get("candidate_sources") or [])
                    if str(item).strip() in allowed_evidence_ids
                ]
                citation = (
                    f" [evidence_id: {evidence_ids[0]}]"
                    if evidence_ids
                    else ""
                )
                marker = (
                    f" <!-- protocol-step:{step_id} -->"
                    if step_id
                    else ""
                )
                role = str(step.get("execution_role") or "operation").strip().lower()
                if role == "operation_constraint":
                    lines.append(
                        f"> **Operational constraint:** {desc}{citation}{marker}"
                    )
                else:
                    operation_index += 1
                    lines.append(
                        f"{operation_index}. {desc}{citation}{marker}"
                    )
            lines.append("")
    return "\n".join(lines).strip() + "\n"


def normalize_and_enforce_citations(md: str, allowed_ids: Set[str]) -> str:
    """
        Step 1: Convert [sem_...] -> [evidence_id: sem_...]
        Step 2: Remove IDs not in whitelist.
        Step 3: Clean up residuals.
        """
    import re
        
    # Step 1: Normalize raw IDs if LLM forgot the prefix
    # Replace [sem_1234] with [evidence_id: sem_1234]
    # Regex looks for brackets containing something starting with sem_ or pmc_ or official_ or protio_
    # that doesn't already have "evidence_id:" prefix
        
    def normalize_repl(match):
        content = match.group(1)
        if content.startswith("evidence_id:"):
            return match.group(0) # Already correct
        return f"[evidence_id: {content}]"

    # Pattern matches [sem_...] or [pmc_...] etc.
    # Be careful not to match [1] style citations if we don't use them, but our IDs are typically alphanumeric with prefix
    md = re.sub(r'\[((?:sem|pmc|official|protio)_[a-zA-Z0-9_-]+)\]', normalize_repl, md)

    # Step 2: Enforce Whitelist
    # This regex matches [evidence_id: X]
    pattern = re.compile(r"\[evidence_id:\s*([^\]]+)\]")
        
    def enforce_repl(match: Any) -> str:
        evid = match.group(1).strip()
        if allowed_ids and evid in allowed_ids:
            return match.group(0) # Keep it
        return "" # Remove it

    md = pattern.sub(enforce_repl, md)
        
    # Step 3: Sanity clean any leftover raw [sem_...] that might have been missed or malformed
    # e.g. if normalize didn't catch it but it's still there
    # We generally want to remove any bracketed ID that isn't a valid citation now
    # But be careful of [1] or [Time] etc.
    # Let's just do a pass to remove any [sem_...] that remains
    md = re.sub(r'\[(?:sem|pmc|official|protio)_[^\]]+\]', '', md)

    return md
